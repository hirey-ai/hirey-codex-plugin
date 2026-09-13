#!/usr/bin/env python3
"""Local, secret-free planning and multipart upload helper for Agentic Media.

The MCP control plane remains outside this module.  A caller obtains a fresh
upload description from the live ``workspace_workflows`` operation and passes
it to ``upload`` over stdin.  URLs and headers are used in memory only; the
durable state writer accepts a deliberately small allowlist.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import time
from typing import Any, Callable, Iterable, Mapping
import urllib.error
import urllib.request


INTENTS = frozenset({"raw_source", "final_master", "evidence"})
EVIDENCE_IMAGE_MIME_BY_SUFFIX = {
    ".avif": "image/avif", ".gif": "image/gif", ".heic": "image/heic",
    ".heif": "image/heif", ".jpeg": "image/jpeg", ".jpg": "image/jpeg",
    ".png": "image/png", ".webp": "image/webp",
}
SOURCE_KINDS = frozenset({"local_file", "hirey_canonical_media"})
SESSION_STATUSES = frozenset({
    "ready", "uploading", "paused", "cancel_requested",
    "cancelled", "completed", "failed",
})
STATE_VERSION = 1

_STATE_KEYS = frozenset({
    "version", "source", "intent", "person_id", "session_binding_ref",
    "workspace_ref", "profile_ref", "work_ref", "upload_ref",
    "part_size", "total_size", "completed_parts", "status", "last_event_seq",
})
_SOURCE_KEYS = frozenset({
    "kind", "path", "media_ref", "name", "size", "mtime_ns",
    "head_sha256",
})
_COMPLETED_KEYS = frozenset({"part_number", "etag"})
_FORBIDDEN_KEY_FRAGMENTS = (
    "token", "secret", "password", "authorization", "cookie", "cipher",
    "presign", "signed_url", "upload_url", "download_url", "access_key",
    "session_id", "user_id", "content", "bytes",
    "api_key", "private_key", "credential", "key_id", "key_version",
    "encryption_version", "keyring",
)
class UploadError(RuntimeError):
    """A stable local validation or upload failure."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail or code


def _positive(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise UploadError("invalid_%s" % field, "%s must be a positive integer" % field) from exc
    if result <= 0:
        raise UploadError("invalid_%s" % field, "%s must be a positive integer" % field)
    return result


def _nonnegative(value: Any, field: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise UploadError("invalid_%s" % field, "%s must be a non-negative integer" % field) from exc
    if result < 0:
        raise UploadError("invalid_%s" % field, "%s must be a non-negative integer" % field)
    return result


def validate_intent(intent: str) -> str:
    value = str(intent or "").strip()
    if value not in INTENTS:
        raise UploadError("invalid_intent", "intent must be raw_source, final_master, or evidence")
    return value


def _video_signature(path: Path) -> str:
    with path.open("rb") as source:
        head = source.read(64)
    if len(head) >= 12 and head[4:8] == b"ftyp":
        return "iso-bmff"
    if head.startswith(b"\x1aE\xdf\xa3"):
        return "webm"
    raise UploadError("unsupported_video_format", "file header is not a supported video container")


def _image_signature(path: Path, mime_type: str) -> str:
    with path.open("rb") as source:
        head = source.read(64)
    brand = head[8:12] if len(head) >= 12 and head[4:8] == b"ftyp" else b""
    iso_image_brands = {
        "image/avif": {b"avif", b"avis"},
        "image/heic": {b"heic", b"heix", b"hevc", b"hevx"},
        "image/heif": {b"mif1", b"msf1", b"heif"},
    }
    matched = (
        (mime_type == "image/jpeg" and head.startswith(b"\xff\xd8\xff"))
        or (mime_type == "image/png" and head.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime_type == "image/gif" and head.startswith((b"GIF87a", b"GIF89a")))
        or (mime_type == "image/webp" and len(head) >= 12
            and head[:4] == b"RIFF" and head[8:12] == b"WEBP")
        or (mime_type in iso_image_brands and brand in iso_image_brands[mime_type])
    )
    if not matched:
        raise UploadError("unsupported_image_format", "file header does not match the image type")
    return mime_type.split("/", 1)[1]


def describe_local_source(path: str | os.PathLike[str]) -> dict[str, Any]:
    source = Path(path).expanduser().resolve(strict=True)
    info = source.stat()
    if not stat.S_ISREG(info.st_mode):
        raise UploadError("source_not_regular", "source must be a regular local file")
    if not os.access(source, os.R_OK):
        raise UploadError("source_not_readable", "source is not readable")
    if info.st_size <= 0:
        raise UploadError("source_empty", "source is empty")
    with source.open("rb") as handle:
        head_digest = hashlib.sha256(handle.read(1024 * 1024)).hexdigest()
    return {
        "kind": "local_file", "path": str(source), "name": source.name,
        "size": info.st_size, "mtime_ns": info.st_mtime_ns,
        "head_sha256": head_digest,
    }


def sha256_local_file(path: str | os.PathLike[str]) -> str:
    """Stream the entire file so work creation can bind the exact source bytes."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def describe_canonical_source(media_ref: str) -> dict[str, Any]:
    value = str(media_ref or "").strip()
    if not value or "://" in value or not value.startswith(("media_", "media:", "mas_", "mpb_", "pmr_")):
        raise UploadError(
            "invalid_canonical_media_ref",
            "canonical media must be a stable HiRey reference, never a remote video URL",
        )
    return {"kind": "hirey_canonical_media", "media_ref": value}


def preflight_local_file(
    path: str | os.PathLike[str], *, intent: str, allowed_suffixes: Iterable[str],
    max_bytes: int, quota_remaining_bytes: int | None, part_size: int, max_parts: int,
) -> dict[str, Any]:
    """Validate only rules explicitly supplied by the live control contract."""
    checked_intent = validate_intent(intent)
    maximum = _positive(max_bytes, "max_bytes")
    quota = (_nonnegative(quota_remaining_bytes, "quota_remaining_bytes")
             if quota_remaining_bytes is not None else None)
    chunk = _positive(part_size, "part_size")
    part_limit = _positive(max_parts, "max_parts")
    source = describe_local_source(path)
    allowed = {str(item).lower() if str(item).startswith(".") else "." + str(item).lower()
               for item in allowed_suffixes if str(item).strip()}
    if not allowed:
        raise UploadError("contract_missing_formats", "live contract did not describe allowed formats")
    suffix = Path(source["name"]).suffix.lower()
    if suffix not in allowed:
        raise UploadError("unsupported_video_format", "file extension is not allowed by the live contract")
    mime_type = mimetypes.guess_type(source["name"])[0]
    if checked_intent == "evidence" and suffix in EVIDENCE_IMAGE_MIME_BY_SUFFIX:
        mime_type = EVIDENCE_IMAGE_MIME_BY_SUFFIX[suffix]
        signature = _image_signature(Path(source["path"]), mime_type)
    else:
        mime_type = mime_type or "application/octet-stream"
        if not mime_type.startswith("video/"):
            raise UploadError(
                "unsupported_media_format",
                "raw_source/final_master require video; evidence accepts supported images or video",
            )
        signature = _video_signature(Path(source["path"]))
    if source["size"] > maximum:
        raise UploadError("upload_too_large", "file exceeds the live contract maximum")
    if quota is not None and source["size"] > quota:
        raise UploadError("quota_exceeded", "file exceeds the currently reported quota")
    count = (source["size"] + chunk - 1) // chunk
    if count > part_limit:
        raise UploadError("too_many_parts", "multipart plan exceeds the live contract part limit")
    return {
        "ok": True, "intent": checked_intent, "source": source,
        "container": signature, "mime_type": mime_type,
        "content_sha256": sha256_local_file(source["path"]),
        "part_size": chunk, "part_count": count, "total_size": source["size"],
    }


def _operation_entries(catalog: Any) -> list[Mapping[str, Any]]:
    if isinstance(catalog, list):
        entries = catalog
    elif isinstance(catalog, Mapping):
        entries = next(
            (catalog[key] for key in ("operations", "items", "actions")
             if isinstance(catalog.get(key), list)),
            None,
        )
    else:
        entries = None
    if entries is None or any(not isinstance(item, Mapping) for item in entries):
        raise UploadError("contract_not_describable", "live catalog has no operation list")
    return list(entries)


def inspect_live_contract(
    catalog: Any, role_operations: Mapping[str, str], *,
    required_roles: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Check an explicit semantic-role mapping against a fresh live catalog.

    Operation names are deliberately not embedded here.  The caller must map roles
    after inspecting ``workspace_workflows(action: \"catalog\")``; this prevents a
    local helper from becoming a second, stale control-plane contract.
    """
    required = {str(role).strip() for role in (
        required_roles if required_roles is not None else role_operations
    ) if str(role).strip()}
    if not required or set(role_operations) != required:
        raise UploadError("contract_roles_incomplete")
    entries = _operation_entries(catalog)
    by_name: dict[str, Mapping[str, Any]] = {}
    for item in entries:
        name = str(item.get("name") or item.get("operation") or item.get("action") or "").strip()
        if name:
            by_name[name] = item
    checked: dict[str, dict[str, Any]] = {}
    for role in sorted(required):
        operation = str(role_operations.get(role) or "").strip()
        item = by_name.get(operation)
        if item is None:
            raise UploadError("contract_operation_missing", "%s is absent from the live catalog" % operation)
        description = str(item.get("description") or item.get("purpose") or "").strip()
        input_schema = next(
            (item[key] for key in ("input_schema", "inputSchema", "request_schema", "inputs")
             if isinstance(item.get(key), Mapping)),
            None,
        )
        result_schema = next(
            (item[key] for key in ("result_schema", "output_schema", "outputSchema", "returns")
             if isinstance(item.get(key), Mapping)),
            None,
        )
        if not description or input_schema is None or result_schema is None:
            raise UploadError(
                "contract_not_describable",
                "%s must describe purpose, input schema, and result schema" % operation,
            )
        checked[role] = {
            "operation": operation,
            "description": description,
            "write": bool(item.get("write") or item.get("external_effect")),
            "confirmation_required": bool(item.get("confirmation_required")),
            "idempotency_required": bool(item.get("idempotency_required")),
        }
    return {"ok": True, "roles": checked}


def multipart_plan(total_size: int, part_size: int, max_parts: int = 10000) -> list[dict[str, int]]:
    total = _positive(total_size, "total_size")
    chunk = _positive(part_size, "part_size")
    limit = _positive(max_parts, "max_parts")
    count = (total + chunk - 1) // chunk
    if count > limit:
        raise UploadError("too_many_parts", "multipart plan exceeds the part limit")
    return [
        {
            "part_number": number,
            "offset": (number - 1) * chunk,
            "size": min(chunk, total - ((number - 1) * chunk)),
        }
        for number in range(1, count + 1)
    ]


def normalize_completed(parts: Iterable[Mapping[str, Any]], total_parts: int) -> list[dict[str, Any]]:
    seen: dict[int, str] = {}
    for item in parts:
        number = _positive(item.get("part_number"), "part_number")
        if number > total_parts:
            raise UploadError("invalid_completed_part", "completed part is outside the upload plan")
        etag = str(item.get("etag") or "").strip()
        if not etag:
            raise UploadError("invalid_completed_part", "completed part omitted etag")
        if number in seen and seen[number] != etag:
            raise UploadError("completed_part_conflict", "completed part has conflicting etags")
        seen[number] = etag
    return [{"part_number": number, "etag": seen[number]} for number in sorted(seen)]


def missing_parts(plan: Iterable[Mapping[str, int]], completed: Iterable[Mapping[str, Any]]) -> list[dict[str, int]]:
    full = [dict(item) for item in plan]
    done = {int(item["part_number"]) for item in completed}
    return [item for item in full if int(item["part_number"]) not in done]


def _reject_secrets(value: Any, path: str = "state") -> None:
    if isinstance(value, Mapping):
        for key, child in value.items():
            lowered = str(key).lower().replace("-", "_")
            if any(fragment in lowered for fragment in _FORBIDDEN_KEY_FRAGMENTS):
                raise UploadError("unsafe_session_state", "%s.%s is forbidden" % (path, key))
            _reject_secrets(child, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secrets(child, "%s[%d]" % (path, index))
    elif isinstance(value, str):
        lowered = value.lower()
        if ("x-amz-signature=" in lowered or lowered.startswith("bearer ")
                or "access_token=" in lowered or "refresh_token=" in lowered):
            raise UploadError("unsafe_session_state", "%s contains credential material" % path)


def validate_session_state(state: Mapping[str, Any]) -> dict[str, Any]:
    extra = set(state) - _STATE_KEYS
    if extra:
        raise UploadError("unsafe_session_state", "unknown state fields: %s" % ",".join(sorted(extra)))
    if int(state.get("version") or 0) != STATE_VERSION:
        raise UploadError("unsupported_state_version")
    validate_intent(str(state.get("intent") or ""))
    status_value = str(state.get("status") or "")
    if status_value not in SESSION_STATUSES:
        raise UploadError("invalid_session_status")
    source = state.get("source")
    if not isinstance(source, Mapping) or set(source) - _SOURCE_KEYS:
        raise UploadError("unsafe_session_state", "source state has unknown fields")
    if source.get("kind") not in SOURCE_KINDS:
        raise UploadError("invalid_source_kind")
    if source.get("kind") == "local_file" and not source.get("path"):
        raise UploadError("invalid_local_source")
    if source.get("kind") == "hirey_canonical_media" and not source.get("media_ref"):
        raise UploadError("invalid_canonical_media_ref")
    person_id = str(state.get("person_id") or "").strip()
    if not person_id:
        raise UploadError("person_binding_required")
    completed = state.get("completed_parts") or []
    if not isinstance(completed, list) or any(
        not isinstance(item, Mapping) or set(item) - _COMPLETED_KEYS for item in completed
    ):
        raise UploadError("unsafe_session_state", "completed_parts has unknown fields")
    _reject_secrets(state)
    return json.loads(json.dumps(state, sort_keys=True))


def new_session_state(
    *, source: Mapping[str, Any], intent: str, person_id: str,
    session_binding_ref: str = "", workspace_ref: str = "", profile_ref: str = "",
    work_ref: str = "", upload_ref: str = "", part_size: int = 0,
    total_size: int = 0, status: str = "ready",
) -> dict[str, Any]:
    state = {
        "version": STATE_VERSION, "source": dict(source), "intent": validate_intent(intent),
        "person_id": str(person_id), "session_binding_ref": str(session_binding_ref),
        "workspace_ref": str(workspace_ref), "profile_ref": str(profile_ref),
        "work_ref": str(work_ref), "upload_ref": str(upload_ref),
        "part_size": int(part_size or 0), "total_size": int(total_size or source.get("size") or 0),
        "completed_parts": [], "status": str(status), "last_event_seq": 0,
    }
    return validate_session_state(state)


def _protect_state_file(fd: int) -> None:
    """Restrict the open file before writing any private session metadata."""
    if os.name != "nt":
        os.fchmod(fd, 0o600)
        return
    import ctypes
    from ctypes import wintypes
    import msvcrt

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    secure = advapi.SetKernelObjectSecurity
    secure.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.c_void_p]
    secure.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    kernel.ReOpenFile.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
    kernel.ReOpenFile.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    descriptor = ctypes.c_void_p()
    # Protected DACL: only the file owner has access, without inherited grants.
    if not convert("D:P(A;;FA;;;OW)", 1, ctypes.byref(descriptor), None):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        # CRT descriptors do not request WRITE_DAC. Reopen the same object with
        # that right rather than resolving its filename a second time.
        security_handle = kernel.ReOpenFile(msvcrt.get_osfhandle(fd), 0x40000, 7, 0)
        if security_handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            if not secure(security_handle, 0x80000004, descriptor):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            kernel.CloseHandle(security_handle)
    finally:
        kernel.LocalFree(descriptor)


class SessionStore:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        return validate_session_state(json.loads(self.path.read_text(encoding="utf-8")))

    def save(self, state: Mapping[str, Any]) -> dict[str, Any]:
        clean = validate_session_state(state)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=self.path.name + ".", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                _protect_state_file(handle.fileno())
                json.dump(clean, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
        return clean

    def transition(self, status_value: str) -> dict[str, Any]:
        state = self.load()
        current = state["status"]
        allowed = {
            "ready": {"uploading", "paused", "cancel_requested"},
            "uploading": {"paused", "cancel_requested", "completed", "failed"},
            "paused": {"ready", "cancel_requested"},
            "cancel_requested": {"cancelled", "failed"},
            "failed": {"ready", "cancel_requested"},
        }
        if status_value not in allowed.get(current, set()):
            raise UploadError("invalid_state_transition", "%s cannot transition to %s" % (current, status_value))
        state["status"] = status_value
        return self.save(state)


def _part_size(plan: list[Mapping[str, int]], number: int) -> int:
    return int(plan[number - 1]["size"])


def progress_event(state: Mapping[str, Any], plan: list[Mapping[str, int]], *,
                   started_at: float, now: float | None = None, event: str = "progress") -> dict[str, Any]:
    current = time.monotonic() if now is None else now
    completed = normalize_completed(state.get("completed_parts") or [], len(plan))
    transferred = sum(_part_size(plan, int(item["part_number"])) for item in completed)
    total = int(state.get("total_size") or sum(int(item["size"]) for item in plan))
    elapsed = max(0.0, current - started_at)
    rate = transferred / elapsed if elapsed > 0 else 0.0
    remaining = max(0, total - transferred)
    return {
        "event": event, "seq": int(state.get("last_event_seq") or 0),
        "work_ref": str(state.get("work_ref") or ""),
        "upload_ref": str(state.get("upload_ref") or ""),
        "status": str(state.get("status") or ""),
        "parts_completed": len(completed), "parts_total": len(plan),
        "bytes_completed": transferred, "bytes_total": total,
        "percent": round((transferred * 100.0 / total), 2) if total else 0.0,
        "bytes_per_second": round(rate, 2),
        "eta_seconds": round(remaining / rate, 2) if rate > 0 else None,
    }


def _default_put(url: str, headers: Mapping[str, str], body: bytes) -> Mapping[str, Any]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return {"status": response.status, "etag": response.headers.get("etag") or ""}
    except urllib.error.HTTPError as exc:
        raise UploadError("part_http_%d" % exc.code, "part upload failed") from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise UploadError("part_transport_failed", "part upload outcome is unknown") from exc


def upload_from_description(
    store: SessionStore, description: Mapping[str, Any], *,
    put: Callable[[str, Mapping[str, str], bytes], Mapping[str, Any]] = _default_put,
    emit: Callable[[Mapping[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> dict[str, Any]:
    """Upload fresh missing-part grants without persisting any grant material."""
    state = store.load()
    if state["source"]["kind"] != "local_file":
        raise UploadError("canonical_media_is_control_plane_copy", "canonical media must use a live HiRey copy operation")
    source = describe_local_source(state["source"]["path"])
    for field in ("size", "mtime_ns", "head_sha256"):
        if source[field] != state["source"].get(field):
            raise UploadError("source_changed", "local source changed after preflight")
    total = _positive(description.get("total_size") or description.get("expected_size_bytes"), "total_size")
    chunk = _positive(description.get("part_size") or description.get("part_size_bytes"), "part_size")
    if total != source["size"]:
        raise UploadError("upload_size_mismatch")
    described_upload_ref = str(description.get("upload_ref") or description.get("upload_session_id") or "")
    if state.get("upload_ref") and described_upload_ref != state["upload_ref"]:
        raise UploadError("upload_ref_mismatch")
    plan = multipart_plan(total, chunk, _positive(description.get("max_parts") or 10000, "max_parts"))
    server_completed = normalize_completed(description.get("completed_parts") or [], len(plan))
    required = {item["part_number"]: item for item in missing_parts(plan, server_completed)}
    grants: dict[int, Mapping[str, Any]] = {}
    for grant in description.get("parts") or description.get("missing_parts") or []:
        if not isinstance(grant, Mapping):
            raise UploadError("invalid_part_grant")
        number = _positive(grant.get("part_number"), "part_number")
        if number not in required or number in grants:
            raise UploadError("unexpected_part_grant")
        url = str(grant.get("url") or "")
        local_http = re.match(r"^http://(?:127\.0\.0\.1|localhost)(?::[0-9]+)?/", url)
        if not url.startswith("https://") and local_http is None:
            raise UploadError("invalid_part_grant", "part grant must use HTTPS except on localhost")
        if str(grant.get("method") or "PUT").upper() != "PUT":
            raise UploadError("invalid_part_grant", "part grant method must be PUT")
        headers = grant.get("required_headers") or {}
        if not isinstance(headers, Mapping):
            raise UploadError("invalid_part_grant")
        grants[number] = grant
    if set(grants) != set(required):
        raise UploadError("missing_part_grant", "fresh grants must cover exactly the server-missing parts")

    state["part_size"] = chunk
    state["total_size"] = total
    state["completed_parts"] = server_completed
    if state["status"] in {"ready", "failed"}:
        state["status"] = "uploading"
    elif state["status"] != "uploading":
        raise UploadError("upload_not_active", "resume or activate the session before uploading")
    state = store.save(state)
    started = clock()
    event_sink = emit or (lambda value: print(json.dumps(value, sort_keys=True), flush=True))

    def record_event(event: str) -> dict[str, Any]:
        event_state = store.load()
        event_state["last_event_seq"] = int(event_state.get("last_event_seq") or 0) + 1
        event_state = store.save(event_state)
        event_sink(progress_event(event_state, plan, started_at=started, now=clock(), event=event))
        return event_state

    record_event("upload_started")

    for number in sorted(required):
        live = store.load()
        if live["status"] == "paused":
            return record_event("upload_paused")
        if live["status"] == "cancel_requested":
            return record_event("cancel_requested")
        part = required[number]
        with Path(source["path"]).open("rb") as handle:
            handle.seek(int(part["offset"]))
            body = handle.read(int(part["size"]))
        if len(body) != int(part["size"]):
            raise UploadError("source_short_read")
        grant = grants[number]
        result = put(str(grant["url"]), {str(k): str(v) for k, v in
                     (grant.get("required_headers") or {}).items()}, body)
        if int(result.get("status") or 0) < 200 or int(result.get("status") or 0) >= 300:
            raise UploadError("part_upload_failed")
        etag = str(result.get("etag") or "").strip()
        if not etag:
            raise UploadError("part_receipt_missing", "upload response omitted etag")
        latest = store.load()
        latest["completed_parts"] = normalize_completed(
            [*(latest.get("completed_parts") or []), {"part_number": number, "etag": etag}],
            len(plan),
        )
        state = store.save(latest)
        state = record_event("progress")
    return store.load()


def _json_out(value: Any) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    preflight = sub.add_parser("preflight")
    preflight.add_argument("--path", required=True)
    preflight.add_argument("--intent", required=True, choices=sorted(INTENTS))
    preflight.add_argument("--allowed-suffix", action="append", required=True)
    preflight.add_argument("--max-bytes", required=True, type=int)
    quota = preflight.add_mutually_exclusive_group(required=True)
    quota.add_argument("--quota-remaining-bytes", type=int)
    quota.add_argument("--quota-unlimited", action="store_true")
    preflight.add_argument("--part-size", required=True, type=int)
    preflight.add_argument("--max-parts", required=True, type=int)

    plan = sub.add_parser("plan")
    plan.add_argument("--total-size", required=True, type=int)
    plan.add_argument("--part-size", required=True, type=int)
    plan.add_argument("--max-parts", type=int, default=10000)

    transition = sub.add_parser("transition")
    transition.add_argument("--state", required=True)
    transition.add_argument("--status", required=True, choices=sorted(SESSION_STATUSES))

    initialize = sub.add_parser("init")
    initialize.add_argument("--state", required=True)
    initialize.add_argument("--intent", required=True, choices=sorted(INTENTS))
    initialize.add_argument("--person-id", required=True)
    initialize.add_argument("--session-binding-ref", default="")
    initialize.add_argument("--workspace-ref", default="")
    initialize.add_argument("--profile-ref", default="")
    initialize.add_argument("--work-ref", default="")
    initialize.add_argument("--upload-ref", default="")
    initialize.add_argument("--status", default="ready", choices=sorted(SESSION_STATUSES))

    upload = sub.add_parser("upload")
    upload.add_argument("--state", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "preflight":
            _json_out(preflight_local_file(
                args.path, intent=args.intent, allowed_suffixes=args.allowed_suffix,
                max_bytes=args.max_bytes,
                quota_remaining_bytes=None if args.quota_unlimited else args.quota_remaining_bytes,
                part_size=args.part_size, max_parts=args.max_parts,
            ))
        elif args.command == "plan":
            _json_out(multipart_plan(args.total_size, args.part_size, args.max_parts))
        elif args.command == "transition":
            _json_out(SessionStore(args.state).transition(args.status))
        elif args.command == "init":
            preflight = json.load(sys.stdin)
            if preflight.get("ok") is not True or not isinstance(preflight.get("source"), Mapping):
                raise UploadError("invalid_preflight_result")
            _json_out(SessionStore(args.state).save(new_session_state(
                source=preflight["source"], intent=args.intent, person_id=args.person_id,
                session_binding_ref=args.session_binding_ref, workspace_ref=args.workspace_ref,
                profile_ref=args.profile_ref, work_ref=args.work_ref,
                upload_ref=args.upload_ref,
                part_size=int(preflight.get("part_size") or 0),
                total_size=int(preflight.get("total_size") or 0), status=args.status,
            )))
        elif args.command == "upload":
            description = json.load(sys.stdin)
            upload_from_description(SessionStore(args.state), description)
        return 0
    except (UploadError, OSError, ValueError, json.JSONDecodeError) as exc:
        code = exc.code if isinstance(exc, UploadError) else "local_upload_failed"
        _json_out({"ok": False, "error_code": code})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
