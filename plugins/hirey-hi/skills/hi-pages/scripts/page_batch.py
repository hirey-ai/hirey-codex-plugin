#!/usr/bin/env python3
"""Local Page request/retry index. Never executes requests or grants authority.

Only caller-supplied Core receipts/readbacks are inspected; their JSON shape is
validated, not authenticated. Keep this state outside shared repositories: it
contains the complete private payloads needed to replay an interrupted request.
"""

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import urlencode
import uuid


CONTRACT = "hirey.local.page-batch.v1"
RECEIPT = "hirey.core.workspace.receipt.v1"
ALLOWED = {"page.create", "page.draft", "enrichment.request"}
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
# Exact, typed pre-execution refusals. Unknown failures can hide an applied
# effect and therefore keep the original request/key unresolved.
DEFINITE_REFUSALS = {"invalid_operation_payload", "invalid_params", "invalid_content",
                     "forbidden", "permission_denied", "explicit_user_confirmation_required",
                     "workspace_access_denied", "page_not_found", "page_revision_conflict",
                     "revision_conflict"}


class BatchError(ValueError):
    pass


def require(condition, code):
    if not condition:
        raise BatchError(code)


def text(value, code, maximum=200):
    require(isinstance(value, str) and 0 < len(value) <= maximum
            and value == value.strip() and not any(ord(c) < 32 for c in value), code)
    return value


def ref(value, prefix):
    value = text(value, "invalid_" + prefix + "_ref", 100)
    require(re.fullmatch(prefix + r"_[A-Za-z0-9_-]+", value), "invalid_" + prefix + "_ref")
    return value


def revision(value):
    require(type(value) is int and value >= 1, "invalid_revision")
    return value


def opaque_ref(value):
    value = text(value, "invalid_object_ref", 100)
    require(re.fullmatch(r"[a-z]{2,12}_[A-Za-z0-9_-]+", value), "invalid_object_ref")
    return value


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False, allow_nan=False).encode("utf-8")).hexdigest()


def content_digest(content):
    """Core pages_claims uses sorted compact JSON with ensure_ascii=True."""
    encoded = json.dumps(content, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    def unique(pairs):
        obj = {}
        for key, value in pairs:
            require(key not in obj, "duplicate_json_key")
            obj[key] = value
        return obj
    with Path(path).open(encoding="utf-8-sig") as stream:
        return json.load(stream, object_pairs_hook=unique,
                         parse_constant=lambda _: (_ for _ in ()).throw(BatchError("invalid_json_number")))


def check_scope(scope):
    require(isinstance(scope, dict) and set(scope) == {"person_id", "workspace_id"}, "invalid_scope")
    ref(scope["person_id"], "per")
    ref(scope["workspace_id"], "wsp")


@contextmanager
def writer_lock(path):
    """OS lock released on crash; never unlink the lock inode while in use."""
    lock_path = path.with_name(path.name + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt
            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            try:
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as error:
                raise BatchError("batch_in_use") from error
        else:
            import fcntl
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                raise BatchError("batch_in_use") from error
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def save(path, state):
    """Persist a complete replacement before exposing any outbound request."""
    state["updated_at"] = timestamp()
    fd, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(state, stream, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        if os.name != "nt":
            directory = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def initialize(manifest):
    require(isinstance(manifest, dict) and set(manifest) == {"scope", "targets"}, "invalid_manifest")
    check_scope(manifest["scope"])
    targets = manifest["targets"]
    require(isinstance(targets, list) and 0 < len(targets) <= 1000, "invalid_targets")
    indexed = {}
    for target in targets:
        require(isinstance(target, dict) and set(target) == {"subject_person_id", "source_refs"},
                "invalid_target")
        subject = ref(target["subject_person_id"], "per")
        require(subject not in indexed, "duplicate_subject")
        sources = target["source_refs"]
        require(isinstance(sources, list) and 0 < len(sources) <= 100, "invalid_source_refs")
        for source in sources:
            text(source, "invalid_source_ref", 1000)
        require(len(set(sources)) == len(sources), "duplicate_source_ref")
        indexed[subject] = {"source_refs": sources, "page_draft_id": None,
                            "observation": None, "verification": None}
    return {"contract": CONTRACT, "batch_id": uuid.uuid4().hex, "scope": manifest["scope"],
            "targets": indexed, "actions": [], "created_at": timestamp()}


def target_of(state, subject):
    require(subject in state["targets"], "subject_not_in_batch")
    return state["targets"][subject]


def validate_payload(state, subject, operation, payload):
    require(operation in ALLOWED, "operation_not_allowed")
    require(isinstance(payload, dict), "invalid_payload")
    target = target_of(state, subject)
    fields = {"page.create": {"subject_person_id", "content"},
              "page.draft": {"page_draft_id", "if_revision", "content"},
              "enrichment.request": {"subject_person_id", "linkedin_url", "query"}}[operation]
    require(set(payload) <= fields, "unexpected_payload_field")
    if operation != "page.draft":
        require(payload.get("subject_person_id") == subject, "subject_mismatch")
    if operation.startswith("page."):
        require(isinstance(payload.get("content"), dict), "invalid_content")
    if operation == "page.draft":
        require(target["page_draft_id"] and payload.get("page_draft_id") == target["page_draft_id"],
                "page_not_acquired")
        observation = target["observation"]
        require(observation and not observation.get("refresh_required")
                and revision(payload.get("if_revision")) == observation["revision"],
                "readback_revision_required")
    if operation == "enrichment.request":
        require("linkedin_url" in payload or "query" in payload, "enrichment_key_required")
        if "linkedin_url" in payload:
            text(payload["linkedin_url"], "invalid_linkedin_url", 2000)
        if "query" in payload:
            query = payload["query"]
            require(isinstance(query, dict) and set(query) == {"name", "anchors"}, "invalid_query")
            text(query["name"], "invalid_query_name", 200)
            require(isinstance(query["anchors"], list) and 0 < len(query["anchors"]) <= 10,
                    "invalid_query_anchors")
            for anchor in query["anchors"]:
                text(anchor, "invalid_query_anchor", 300)


def prepare(state, subject, operation, payload):
    # Replay is checked before revision validation: an interrupted request keeps
    # its exact payload/key even after a later read exposes a different revision.
    fingerprint = digest({"batch_id": state["batch_id"], "scope": state["scope"],
                          "subject_person_id": subject, "operation": operation, "payload": payload})
    action_id = "act_" + fingerprint
    existing = next((item for item in state["actions"] if item["action_id"] == action_id), None)
    if existing:
        if not any(item["ok"] for item in existing["receipts"]) and has_uncertainty(state, existing):
            existing["status"] = "unknown"
        elif existing["status"] == "failed":
            existing["status"] = "prepared"
        return {"action_id": action_id, "request": existing["request"], "replay": True}
    validate_payload(state, subject, operation, payload)
    target = target_of(state, subject)
    if operation.startswith("page."):
        require(not any(item["subject_person_id"] == subject and item["operation"].startswith("page.")
                        and item["status"] in {"prepared", "received", "unknown"} for item in state["actions"]),
                "page_request_unresolved")
        require(operation != "page.create" or not target["page_draft_id"], "page_already_acquired")
    else:
        require(not any(item["subject_person_id"] == subject and item["operation"] == operation
                        and item["status"] in {"prepared", "unknown"} for item in state["actions"]),
                "enrichment_request_unresolved")
    request = {"action": operation, "payload": {**payload,
               "idempotency_key": "page-batch:" + state["batch_id"] + ":" + fingerprint}}
    state["actions"].append({"action_id": action_id, "subject_person_id": subject,
                             "operation": operation, "request": request, "status": "prepared",
                             "receipts": [], "created_at": timestamp()})
    return {"action_id": action_id, "request": request, "replay": False}


def unwrap(value, operation, scope):
    require(isinstance(value, dict) and value.get("contract") == RECEIPT
            and value.get("operation") == operation and type(value.get("ok")) is bool,
            "invalid_receipt_envelope")
    # Scope fields are not present on all Core results. If provided, they must
    # agree; absence is never used to infer authority or authentication.
    for container in (value, value.get("result")):
        if isinstance(container, dict):
            for field in ("workspace_id",):
                require(field not in container or container[field] == scope[field], "scope_mismatch")
    require("person_id" not in value or value["person_id"] == scope["person_id"], "scope_mismatch")
    return value.get("result")


def failed_receipt(value, operation, scope):
    """Accept the structured Hi error envelope without inventing missing fields."""
    require(isinstance(value, dict) and value.get("ok") is False, "invalid_failure_envelope")
    require("operation" not in value or value["operation"] == operation, "invalid_receipt_envelope")
    require("contract" not in value or value["contract"] == RECEIPT, "invalid_receipt_envelope")
    for field in ("workspace_id", "person_id"):
        require(field not in value or value[field] == scope[field], "scope_mismatch")
    nested = value.get("error")
    code = value.get("error_code") or (nested.get("code") if isinstance(nested, dict) else None)
    require(isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", code)
            and "error" in value, "invalid_failure_envelope")
    if "code" in value:
        require(type(value["code"]) is int and 400 <= value["code"] <= 599, "invalid_failure_envelope")
    # Core server maps domain validation to HTTP 400 invalid_request. Its Page
    # CAS raises 409 conflict inside the transaction; idempotency collisions
    # have separate idempotency_key_* codes. Platform does not retain the prose
    # detail, so match the operation and machine code, never error wording.
    if value.get("code") == 400 and code == "invalid_request":
        return "failed"
    if operation == "page.draft" and value.get("code") == 409 and code == "conflict":
        return "failed"
    return "failed" if code in DEFINITE_REFUSALS and value.get("code", 400) < 500 else "unknown"


def has_uncertainty(state, action):
    # Read earlier receipts too, so an older saved index cannot lose uncertainty
    # merely because a previous retry changed its displayed status.
    return bool(action.get("uncertain_since") or action["status"] == "unknown"
                or any(not item["ok"] and failed_receipt(item, action["operation"], state["scope"]) == "unknown"
                       for item in action["receipts"]))


def uncertain(state, action_id):
    """Record a local missing-response fact, never a synthetic Core receipt."""
    action = next((item for item in state["actions"] if item["action_id"] == action_id), None)
    require(action is not None, "unknown_action")
    if any(item["ok"] for item in action["receipts"]):
        return {"action_id": action_id, "status": action["status"], "already_resolved": True}
    action.setdefault("uncertain_since", timestamp())
    action["status"] = "unknown"
    return {"action_id": action_id, "status": "unknown", "saved": False}


def record(state, action_id, receipt):
    action = next((item for item in state["actions"] if item["action_id"] == action_id), None)
    require(action is not None, "unknown_action")
    operation = action["operation"]
    previous = next((item for item in action["receipts"] if item["ok"]), None)
    if previous:
        require(receipt == previous, "receipt_conflict")
        return {"action_id": action_id, "status": action["status"], "replay": True}
    if isinstance(receipt, dict) and receipt.get("ok") is False:
        outcome = failed_receipt(receipt, operation, state["scope"])
        # A refusal before idempotency lookup says nothing about an earlier
        # attempt whose response was lost. Only its successful receipt resolves
        # that attempt; later 400/403/409 responses must not unlock a new key.
        if outcome == "unknown" or has_uncertainty(state, action):
            action.setdefault("uncertain_since", timestamp())
            outcome = "unknown"
        action["receipts"].append(receipt)
        action["status"] = outcome
        if operation == "page.draft" and outcome == "failed" and receipt.get("code") == 409:
            target = target_of(state, action["subject_person_id"])
            if target["observation"]:
                target["observation"]["refresh_required"] = True
        return {"action_id": action_id, "status": outcome, "saved": False}
    result = unwrap(receipt, operation, state["scope"])
    require(isinstance(result, dict), "invalid_receipt_result")
    target = target_of(state, action["subject_person_id"])
    subject = action["subject_person_id"]
    payload = action["request"]["payload"]
    if operation == "enrichment.request":
        opaque_ref(result.get("enrichment_request_id"))
        require(result.get("subject_person_id") == subject, "subject_mismatch")
        require(result.get("status") == "queued" and result.get("revision") == 1
                and type(result.get("revision")) is int
                and result.get("privacy") == "requester_private_until_explicit_page_publish"
                and isinstance(result.get("providers"), list)
                and sorted(result["providers"]) == ["exa", "monid"], "invalid_enrichment_receipt")
    else:
        page_id = opaque_ref(result.get("page_draft_id"))
        require(result.get("workspace_id") == state["scope"]["workspace_id"], "scope_mismatch")
        revision(result.get("revision"))
        require(isinstance(result.get("content_sha256"), str)
                and SHA256.fullmatch(result["content_sha256"]), "invalid_content_digest")
        if operation == "page.create":
            require(result.get("subject_person_id") == subject, "subject_mismatch")
            require(target["page_draft_id"] in (None, page_id), "page_mismatch")
            require(not any(other != subject and value["page_draft_id"] == page_id
                            for other, value in state["targets"].items()), "page_bound_to_other_subject")
            target["page_draft_id"] = page_id
        else:
            require(page_id == target["page_draft_id"] == payload["page_draft_id"], "page_mismatch")
            # Core allocates beyond every historical revision, which can leave
            # gaps above the current draft after an import or earlier history.
            require(result["revision"] > payload["if_revision"], "receipt_revision_mismatch")
            require(result["content_sha256"] == content_digest(payload["content"]),
                    "receipt_content_mismatch")
            if "subject_person_id" in result:
                require(result["subject_person_id"] == subject, "subject_mismatch")
    action["receipts"].append(receipt)
    action["status"] = "received"
    return {"action_id": action_id, "status": "received", "saved": False}


def verify(state, subject, readback):
    target = target_of(state, subject)
    require(target["page_draft_id"], "page_not_acquired")
    pages = unwrap(readback, "page.mine", state["scope"])
    require(readback["ok"] and isinstance(pages, list), "page_readback_failed")
    matching = [page for page in pages if isinstance(page, dict)
                and page.get("page_draft_id") == target["page_draft_id"]]
    require(len(matching) == 1, "page_readback_missing_or_ambiguous")
    page = matching[0]
    require(page.get("workspace_id") == state["scope"]["workspace_id"], "scope_mismatch")
    require(page.get("subject_person_id") == subject, "subject_mismatch")
    current_revision = revision(page.get("current_revision"))
    require(isinstance(page.get("content_json"), dict)
            and isinstance(page.get("content_sha256"), str)
            and SHA256.fullmatch(page["content_sha256"]), "invalid_page_readback")
    require(page["content_sha256"] == content_digest(page["content_json"]),
            "readback_content_digest_mismatch")
    successful = [action for action in state["actions"] if action["subject_person_id"] == subject
                  and action["operation"].startswith("page.")
                  and any(receipt["ok"] for receipt in action["receipts"])]
    require(successful, "page_receipt_required")
    action = successful[-1]
    receipt = next(item for item in action["receipts"] if item["ok"])["result"]
    expected_revision = receipt["revision"]
    outcome = "stale" if current_revision < expected_revision else (
        "changed" if current_revision > expected_revision else (
            "mismatch" if page["content_sha256"] != receipt["content_sha256"] else (
                "saved" if action["operation"] == "page.draft" else "observed")))
    latest_page = next(item for item in reversed(state["actions"])
                       if item["subject_person_id"] == subject and item["operation"].startswith("page."))
    if outcome == "saved" and latest_page is not action:
        outcome = "observed"
    # Preserve evidence of a newer observation: an older snapshot cannot make a
    # Page that changed since our save appear saved at the old revision again.
    observed = target["observation"]
    if observed and current_revision < observed["revision"]:
        outcome = "stale"
    if outcome != "stale":
        target["observation"] = {"revision": current_revision, "content_sha256": page["content_sha256"],
                                  "readback": page, "recorded_at": timestamp()}
    target["verification"] = {"status": outcome, "action_id": action["action_id"],
                                "revision": current_revision, "recorded_at": timestamp()}
    if outcome in {"saved", "observed"}:
        action["status"] = "verified"
    elif outcome == "changed":
        action["status"] = "changed"
    return {"subject_person_id": subject, "page_draft_id": target["page_draft_id"],
            "status": outcome, "saved": outcome == "saved", "revision": current_revision,
            "preview_url": "https://hirey.ai/me?" + urlencode({"page_draft_id": target["page_draft_id"],
                            "subject_person_id": subject}) + "#me"}


def status(state):
    rows = []
    for subject, target in state["targets"].items():
        actions = [item for item in state["actions"] if item["subject_person_id"] == subject]
        verification = target["verification"]
        latest_page = next((item for item in reversed(actions)
                            if item["operation"].startswith("page.")), None)
        unresolved = latest_page if latest_page and latest_page["status"] in {
            "prepared", "received", "failed", "unknown"} else None
        rows.append({"subject_person_id": subject, "source_refs": target["source_refs"],
                     "page_draft_id": target["page_draft_id"],
                     "status": unresolved["status"] if unresolved else (
                         verification["status"] if verification else "not_started"),
                     "verification": verification,
                     "actions": [{key: item[key] for key in ("action_id", "operation", "status")}
                                 for item in actions]})
    return {"batch_id": state["batch_id"], "scope": state["scope"],
            "execution": "local_index_only", "targets": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("init", "status", "prepare", "record", "verify", "uncertain"):
        command = commands.add_parser(name)
        command.add_argument("--state", required=True)
        if name == "init":
            command.add_argument("--input", required=True)
        else:
            command.add_argument("--person-id", required=True)
            command.add_argument("--workspace-id", required=True)
        if name in {"prepare", "verify"}:
            command.add_argument("--subject-person-id", required=True)
        if name == "prepare":
            command.add_argument("--operation", required=True, choices=sorted(ALLOWED))
            command.add_argument("--payload", required=True)
        if name in {"record", "uncertain"}:
            command.add_argument("--action-id", required=True)
        if name == "record":
            command.add_argument("--receipt", required=True)
        if name == "verify":
            command.add_argument("--readback", required=True)
    args = parser.parse_args(argv)
    path = Path(args.state).expanduser().resolve()
    try:
        with writer_lock(path):
            if args.command == "init":
                require(not path.exists(), "batch_already_exists")
                state = initialize(read_json(args.input))
                output = status(state)
            else:
                state = read_json(path)
                require(state.get("contract") == CONTRACT, "invalid_batch_contract")
                scope = {"person_id": args.person_id, "workspace_id": args.workspace_id}
                check_scope(scope)
                require(state.get("scope") == scope, "scope_mismatch")
                if args.command == "status":
                    output = status(state)
                elif args.command == "prepare":
                    output = prepare(state, args.subject_person_id, args.operation, read_json(args.payload))
                elif args.command == "record":
                    output = record(state, args.action_id, read_json(args.receipt))
                elif args.command == "uncertain":
                    output = uncertain(state, args.action_id)
                else:
                    output = verify(state, args.subject_person_id, read_json(args.readback))
            if args.command != "status":
                save(path, state)
        # ASCII JSON also works in Windows shells whose stdout code page cannot
        # represent an owner's name. The request/state still round-trip Unicode.
        print(json.dumps({"ok": True, **output}, allow_nan=False))
        return 0
    except (BatchError, OSError, ValueError, KeyError, TypeError) as error:
        # Do not echo payloads or file contents in failures.
        code = str(error) if isinstance(error, BatchError) else "invalid_or_unavailable_local_input"
        print(json.dumps({"ok": False, "error": code}))
        return 2


if __name__ == "__main__":
    sys.exit(main())
