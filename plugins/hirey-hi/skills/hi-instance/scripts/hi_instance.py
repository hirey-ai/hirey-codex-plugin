#!/usr/bin/env python3
"""Local Hi host-instance helper: one Ed25519 key pair per host per computer.

The MCP control plane stays outside this module.  Hi separates *which Person and
Agent* may act (the OAuth Agent Session) from *which end of the user's work* is
calling (one local instance key pair).  This helper owns only the local half:

* it keeps one instance file per host type outside every plugin version cache,
  so a plugin upgrade, an uninstall/reinstall, an OAuth re-login and a brand-new
  Task all reuse the same instance;
* it never prints, returns or logs the private key;
* it signs the exact server-issued challenge bytes read from a file, never from
  argv, so the challenge can never be truncated or re-quoted by a shell.

The package is identical for every user: this module contains no instance id,
no machine id and no per-user value.  All instance material is created on the
user's own computer, at run time, under the data directory resolved by
``data_root()``.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import hashlib
import json
import os
import platform
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

# --- Frozen local contract -------------------------------------------------

# The on-disk format of the instance file.  A future shape must use a new number
# so an old helper never half-reads a newer file.
FORMAT_VERSION = 1

# Host types the server accepts for ``agent_instance.binding.begin.host_type``.
HOST_TYPES = ("codex", "claude", "hermes", "openclaw", "generic")

# Readable labels used only to build a default display name.
HOST_LABELS = {
    "codex": "Codex",
    "claude": "Claude",
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "generic": "Generic host",
}

# A neutral label used when the operating system will not name the computer.
NEUTRAL_COMPUTER_NAME = "This computer"

INSTANCE_FILE_NAME = "instance.json"
LOCK_FILE_NAME = "instance.lock"

# A first call that loses the initialization race waits for the winner instead of
# creating a second key pair.  A lock older than the stale window belongs to a
# crashed process and may be broken, so a killed run can never wedge the helper.
LOCK_TIMEOUT_SECONDS = 20.0
LOCK_STALE_SECONDS = 60.0
LOCK_POLL_MIN_SECONDS = 0.02
LOCK_POLL_MAX_SECONDS = 0.05

# Typed exit codes.  Every failure path is distinguishable without parsing text.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SIGNER_UNAVAILABLE = 3
EXIT_STATE_INVALID = 4
EXIT_CHALLENGE_UNREADABLE = 5
EXIT_LOCK_TIMEOUT = 6

# Ed25519 sizes: one private seed, one raw public key and one detached signature.
SEED_BYTES = 32
PUBLIC_KEY_BYTES = 32
SIGNATURE_BYTES = 64

# PKCS#8 prefix for a raw Ed25519 seed (RFC 8410 PrivateKeyInfo, 16 bytes of DER).
PKCS8_ED25519_PREFIX = bytes.fromhex("302e020100300506032b657004220420")

# The only content this helper is ever pointed at is the server-issued binding
# challenge.  The signing subcommand still signs whatever exact bytes it is
# given, because a silent rewrite of the message would break verification; the
# skill that calls it documents that rule.
SIGNERS = ("cryptography", "openssl")


class InstanceError(RuntimeError):
    """A stable, typed local failure.

    Args:
        code: Machine-readable ``error_code`` for the caller.
        message: Human-readable remedy, never containing key material.
        exit_code: Process exit code that identifies this failure class.
    """

    def __init__(self, code: str, message: str, exit_code: int = EXIT_FAILURE):
        """Record the typed code, the user-facing remedy and the exit code.

        Args:
            code: Machine-readable ``error_code`` for the caller.
            message: Human-readable remedy, never containing key material.
            exit_code: Process exit code that identifies this failure class.

        Returns:
            None.
        """

        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.exit_code = exit_code


# --- Encoding helpers ------------------------------------------------------


def base64url_encode(raw: bytes) -> str:
    """Return unpadded base64url for ``raw``.

    Args:
        raw: Arbitrary bytes.

    Returns:
        The canonical unpadded base64url spelling the server expects.
    """

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def base64url_decode(text: str) -> bytes:
    """Decode one padded or unpadded base64url string.

    Args:
        text: The encoded value.

    Returns:
        The decoded bytes.

    Raises:
        InstanceError: When the value is not valid base64.
    """

    padded = str(text or "").strip().replace("-", "+").replace("_", "/")
    padded += "=" * (-len(padded) % 4)
    try:
        return base64.b64decode(padded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise InstanceError("instance_state_invalid", "stored key material is not valid base64",
                            EXIT_STATE_INVALID) from exc


def public_key_fingerprint(public_key: str) -> str:
    """Return the lowercase sha256 hex digest of one raw Ed25519 public key.

    Args:
        public_key: Canonical unpadded base64url public key.

    Returns:
        The hex digest the challenge binds.
    """

    return hashlib.sha256(base64url_decode(public_key)).hexdigest()


# --- Data directory contract ----------------------------------------------


def data_root() -> str:
    """Resolve the directory that holds every host's local instance material.

    The root deliberately lives outside every plugin version cache and outside
    any repository or worktree, so it survives a plugin upgrade, a plain
    uninstall/reinstall, an OAuth re-login and a brand-new Task.

    Order: ``$HIREY_INSTANCE_DATA_DIR``, then the macOS Application Support
    directory, then ``%APPDATA%`` on Windows, then the XDG data directory.

    Note that a plugin running inside a container, over SSH or in a cloud
    sandbox resolves the *sandbox* user's home, not the desktop in front of the
    user; the reported location is a label, never this instance's identity.

    Returns:
        An absolute path to the per-user data root.
    """

    override = str(os.environ.get("HIREY_INSTANCE_DATA_DIR") or "").strip()
    if override:
        return os.path.abspath(os.path.expanduser(override))
    if sys.platform == "darwin":
        return os.path.join(os.path.expanduser("~"), "Library", "Application Support", "hirey-hi")
    if os.name == "nt":
        appdata = str(os.environ.get("APPDATA") or "").strip()
        base = appdata or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")
        return os.path.join(base, "hirey-hi")
    xdg = str(os.environ.get("XDG_DATA_HOME") or "").strip()
    base = xdg or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "hirey-hi")


def host_dir(host_type: str) -> str:
    """Return the per-host instance directory ``<root>/<host_type>``.

    The per-host split is required: Codex and Claude running on the same
    computer are two different instances with two different key pairs.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        An absolute directory path.
    """

    return os.path.join(data_root(), validate_host_type(host_type))


def instance_path(host_type: str) -> str:
    """Return the absolute path of one host's instance file.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        ``<root>/<host_type>/instance.json``.
    """

    return os.path.join(host_dir(host_type), INSTANCE_FILE_NAME)


def lock_path(host_type: str) -> str:
    """Return the absolute path of one host's initialization lock file.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        ``<root>/<host_type>/instance.lock``.
    """

    return os.path.join(host_dir(host_type), LOCK_FILE_NAME)


def validate_host_type(value: str) -> str:
    """Return one accepted host type or fail closed.

    Args:
        value: The raw ``--host`` argument.

    Returns:
        The validated host type.

    Raises:
        InstanceError: When the value is not an accepted host type.
    """

    text = str(value or "").strip().lower()
    if text not in HOST_TYPES:
        raise InstanceError(
            "instance_host_type_invalid",
            "--host must be one of %s" % ", ".join(HOST_TYPES),
            EXIT_USAGE,
        )
    return text


def os_type_value() -> str:
    """Return the server's ``os_type`` value for the running operating system.

    Returns:
        One of ``mac``, ``windows``, ``linux``, ``ios``, ``android``, ``unknown``.
    """

    if sys.platform == "darwin":
        return "mac"
    if sys.platform in ("win32", "cygwin", "msys"):
        return "windows"
    if sys.platform == "ios":
        return "ios"
    if sys.platform == "android" or os.environ.get("ANDROID_ROOT"):
        return "android"
    if sys.platform.startswith("linux"):
        return "linux"
    return "unknown"


def computer_display_name() -> str:
    """Return the operating system's name for this computer, or a neutral label.

    macOS asks ``scutil --get ComputerName`` (the name the user sees in Sharing),
    Windows reads ``COMPUTERNAME``, and every other platform falls back to the
    hostname.  When the host is a container, an SSH target or a cloud sandbox
    this reports *that* environment, so the value is a display label only.

    Returns:
        A non-empty human-readable computer name.
    """

    # Inside a container, over SSH or in a cloud sandbox these answer for THAT
    # environment, not for the computer in front of the user. The value is a
    # display label only; the instance identity is the key pair, never the name.
    if sys.platform == "darwin":
        value = _run_text(["scutil", "--get", "ComputerName"])
        if value:
            return value
    if os.name == "nt":
        value = str(os.environ.get("COMPUTERNAME") or "").strip()
        if value:
            return value
    for candidate in (
        str(os.environ.get("HOSTNAME") or "").strip(),
        _run_text(["hostname"]),
        str(platform.node() or "").strip(),
    ):
        if candidate:
            return candidate
    return NEUTRAL_COMPUTER_NAME


def default_display_name(host_type: str) -> str:
    """Build the default ``display_name`` ``<computer> · <Host>``.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        A display name of at most 120 characters.
    """

    label = HOST_LABELS.get(host_type, host_type)
    return ("%s · %s" % (computer_display_name(), label))[:120]


def _run_text(argv: list[str]) -> str:
    """Run a short read-only command and return its stripped stdout, or ``""``.

    Args:
        argv: The exact argv to execute without a shell.

    Returns:
        Stripped standard output, or an empty string when the command is missing
        or fails.
    """

    try:
        completed = subprocess.run(argv, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


# --- Signer layer ----------------------------------------------------------
#
# Three layered signers are tried in order.  Path 1 is the Python
# ``cryptography`` package; path 2 is the ``openssl`` CLI, used only after a real
# Ed25519 probe proves that this build can generate, sign and verify with the
# algorithm.  When neither is available the helper fails closed with
# ``instance_signer_unavailable`` and writes no partial state.

_OPENSSL_SUPPORTED: Optional[bool] = None
_CRYPTOGRAPHY_SUPPORTED: Optional[bool] = None


def is_fatal(exc: BaseException) -> bool:
    """Return whether one exception must never be swallowed by a fallback.

    A broken third-party extension can raise anything, including an exception
    that derives from ``BaseException`` rather than ``Exception`` (pyo3 reports a
    Rust panic that way).  Those must be treated as "this signer is unavailable"
    and fall through, but an interpreter shutdown or a user's Ctrl-C must always
    propagate.

    Args:
        exc: The exception that was caught.

    Returns:
        True for ``KeyboardInterrupt`` and ``SystemExit``, otherwise False.
    """

    return isinstance(exc, (KeyboardInterrupt, SystemExit))


def cryptography_available() -> bool:
    """Return whether the ``cryptography`` package really works, not just imports.

    Importing alone is not proof: an installation whose native extension failed
    to load can raise an exception derived from ``BaseException`` at import time
    or on first use.  This runs the same kind of real round trip the openssl
    probe runs, so a half-installed package falls through to the next signer
    instead of crashing the command.  The answer is cached per process.

    Returns:
        True only after a complete derive/sign round trip succeeded.
    """

    global _CRYPTOGRAPHY_SUPPORTED
    if _CRYPTOGRAPHY_SUPPORTED is not None:
        return _CRYPTOGRAPHY_SUPPORTED
    _CRYPTOGRAPHY_SUPPORTED = _cryptography_probe()
    return _CRYPTOGRAPHY_SUPPORTED


def _cryptography_probe() -> bool:
    """Run the real Ed25519 round trip that gates the cryptography signer.

    Returns:
        True when deriving a public key and signing both succeed.  A fatal
        exception such as ``KeyboardInterrupt`` is re-raised rather than turned
        into a False answer.
    """

    try:
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.from_private_bytes(os.urandom(SEED_BYTES))
        raw = private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        signature = private.sign(b"hirey-agent-instance-binding-probe-v1")
        return len(raw) == PUBLIC_KEY_BYTES and len(signature) == SIGNATURE_BYTES
    except BaseException as exc:  # noqa: BLE001 - a broken extension may raise anything
        if is_fatal(exc):
            raise
        return False


def openssl_executable() -> str:
    """Return the path of the ``openssl`` CLI, or an empty string when absent.

    Returns:
        An absolute executable path, or ``""``.
    """

    return shutil.which("openssl") or ""


def openssl_supported() -> bool:
    """Return whether the ``openssl`` CLI really implements Ed25519.

    The probe generates a throwaway key, signs a fixed message and verifies the
    signature, so a build that merely advertises the algorithm is rejected.  The
    answer is cached per process.

    Returns:
        True only after a complete generate/sign/verify round trip succeeded.
    """

    global _OPENSSL_SUPPORTED
    if _OPENSSL_SUPPORTED is not None:
        return _OPENSSL_SUPPORTED
    _OPENSSL_SUPPORTED = _openssl_probe()
    return _OPENSSL_SUPPORTED


def _openssl_probe() -> bool:
    """Run the real Ed25519 round trip that gates the openssl signer.

    Returns:
        True when generation, signing and verification all succeed.
    """

    binary = openssl_executable()
    if not binary:
        return False
    message = b"hirey-agent-instance-binding-probe-v1"
    try:
        with tempfile.TemporaryDirectory() as work:
            key = os.path.join(work, "probe.pem")
            pub = os.path.join(work, "probe.pub.pem")
            source = os.path.join(work, "probe.bin")
            signature = os.path.join(work, "probe.sig")
            with open(source, "wb") as handle:
                handle.write(message)
            if _openssl(binary, ["genpkey", "-algorithm", "ED25519", "-out", key]) is None:
                return False
            if _openssl(binary, ["pkey", "-in", key, "-pubout", "-out", pub]) is None:
                return False
            if _openssl(binary, ["pkeyutl", "-sign", "-inkey", key, "-rawin",
                                 "-in", source, "-out", signature]) is None:
                return False
            with open(signature, "rb") as handle:
                raw = handle.read()
            if len(raw) != SIGNATURE_BYTES:
                return False
            verified = _openssl(binary, ["pkeyutl", "-verify", "-pubin", "-inkey", pub, "-rawin",
                                         "-in", source, "-sigfile", signature])
            return verified is not None and b"Verified" in verified
    except OSError:
        return False


def _openssl(binary: str, args: list[str]) -> Optional[bytes]:
    """Run one ``openssl`` subcommand and return its stdout.

    Args:
        binary: The resolved openssl executable.
        args: Subcommand and arguments, without a shell.

    Returns:
        Standard output on exit status 0, otherwise ``None``.
    """

    try:
        completed = subprocess.run([binary, *args], capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout


def resolve_signer(preferred: Optional[str] = None) -> str:
    """Return the first signer that is really usable, or fail closed.

    Args:
        preferred: A previously recorded signer to try before the default order.

    Returns:
        ``"cryptography"`` or ``"openssl"``.

    Raises:
        InstanceError: With ``instance_signer_unavailable`` when no signer works.
    """

    order = [preferred] if preferred in SIGNERS else []
    order += [name for name in SIGNERS if name not in order]
    for name in order:
        if name == "cryptography" and cryptography_available():
            return name
        if name == "openssl" and openssl_supported():
            return name
    raise InstanceError(
        "instance_signer_unavailable",
        "No Ed25519 signer is available. Install the Python 'cryptography' package "
        "(python3 -m pip install cryptography) or an OpenSSL build that supports "
        "Ed25519, then run the command again. No local instance state was written.",
        EXIT_SIGNER_UNAVAILABLE,
    )


def generate_seed() -> bytes:
    """Return 32 fresh bytes of operating-system randomness.

    Returns:
        The Ed25519 private seed.
    """

    return os.urandom(SEED_BYTES)


def _pkcs8_pem(seed: bytes) -> str:
    """Wrap one raw Ed25519 seed as a PKCS#8 PEM for the openssl CLI.

    Args:
        seed: The 32-byte private seed.

    Returns:
        A PEM-encoded ``PRIVATE KEY`` block.
    """

    der = PKCS8_ED25519_PREFIX + seed
    body = base64.b64encode(der).decode("ascii")
    lines = "\n".join(body[index:index + 64] for index in range(0, len(body), 64))
    return "-----BEGIN PRIVATE KEY-----\n%s\n-----END PRIVATE KEY-----\n" % lines


def derive_public_key(signer: str, seed: bytes) -> str:
    """Derive the raw Ed25519 public key for one seed.

    Args:
        signer: ``"cryptography"`` or ``"openssl"``.
        seed: The 32-byte private seed.

    Returns:
        The canonical unpadded base64url public key.

    Raises:
        InstanceError: When the signer cannot derive the key.
    """

    if signer == "cryptography":
        try:
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            private = Ed25519PrivateKey.from_private_bytes(seed)
            raw = private.public_key().public_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PublicFormat.Raw,
            )
        except BaseException as exc:  # noqa: BLE001 - a broken extension may raise anything
            if is_fatal(exc):
                raise
            raise InstanceError("instance_signer_failed", "cryptography could not derive the key",
                                EXIT_FAILURE) from exc
        return base64url_encode(raw)

    binary = openssl_executable()
    with tempfile.TemporaryDirectory() as work:
        key = os.path.join(work, "key.pem")
        _write_private_file(key, _pkcs8_pem(seed).encode("ascii"))
        output = _openssl(binary, ["pkey", "-in", key, "-pubout", "-outform", "DER"])
    if output is None or len(output) < PUBLIC_KEY_BYTES:
        raise InstanceError("instance_signer_failed", "openssl could not derive the public key")
    return base64url_encode(output[-PUBLIC_KEY_BYTES:])


def sign_message(signer: str, seed: bytes, message: bytes) -> str:
    """Sign the exact message bytes with the instance seed.

    Args:
        signer: ``"cryptography"`` or ``"openssl"``.
        seed: The 32-byte private seed.
        message: The exact bytes to sign.

    Returns:
        The canonical unpadded base64url detached signature.

    Raises:
        InstanceError: When signing fails or the signature has the wrong size.
    """

    if signer == "cryptography":
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

            raw = Ed25519PrivateKey.from_private_bytes(seed).sign(message)
        except BaseException as exc:  # noqa: BLE001 - a broken extension may raise anything
            if is_fatal(exc):
                raise
            raise InstanceError("instance_signer_failed", "cryptography could not sign") from exc
    else:
        binary = openssl_executable()
        with tempfile.TemporaryDirectory() as work:
            key = os.path.join(work, "key.pem")
            source = os.path.join(work, "message.bin")
            signature = os.path.join(work, "message.sig")
            _write_private_file(key, _pkcs8_pem(seed).encode("ascii"))
            _write_private_file(source, message)
            if _openssl(binary, ["pkeyutl", "-sign", "-inkey", key, "-rawin",
                                 "-in", source, "-out", signature]) is None:
                raise InstanceError("instance_signer_failed", "openssl could not sign the challenge")
            with open(signature, "rb") as handle:
                raw = handle.read()
    if len(raw) != SIGNATURE_BYTES:
        raise InstanceError("instance_signer_failed", "the signer returned a malformed signature")
    return base64url_encode(raw)


# --- Local state -----------------------------------------------------------


def _protect_file(path: str) -> None:
    """Best-effort restrict one local file to its owner (mode 0600).

    Args:
        path: The file to restrict.

    Returns:
        None. A platform that cannot express the mode is ignored rather than
        failing the command.
    """

    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _write_private_file(path: str, data: bytes) -> None:
    """Create a file that only the current user may read.

    Args:
        path: Destination path.
        data: Exact bytes to write.

    Returns:
        None.
    """

    handle = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(handle, data)
    finally:
        os.close(handle)
    _protect_file(path)


def _ensure_directory(path: str) -> None:
    """Create a directory that only the current user may enter (mode 0700).

    Args:
        path: Destination directory.

    Returns:
        None.
    """

    os.makedirs(path, mode=0o700, exist_ok=True)
    if os.name == "nt":
        return
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _validate_instance(payload: Mapping[str, Any], host_type: str) -> dict:
    """Return a validated instance record or fail closed.

    Args:
        payload: The parsed instance file.
        host_type: The host type the file must belong to.

    Returns:
        The validated record.

    Raises:
        InstanceError: With ``instance_state_invalid`` for any local corruption.
    """

    def _require_text(key: str) -> str:
        """Return one required non-empty string field, or fail closed.

        Args:
            key: The field name in the instance file.

        Returns:
            The stripped field value.
        """

        value = str(payload.get(key) or "").strip()
        if not value:
            raise InstanceError("instance_state_invalid", "instance file is missing %s" % key,
                                EXIT_STATE_INVALID)
        return value

    if int(payload.get("format_version") or 0) != FORMAT_VERSION:
        raise InstanceError("instance_state_invalid", "unsupported instance file format",
                            EXIT_STATE_INVALID)
    if _require_text("host_type") != host_type:
        raise InstanceError("instance_state_invalid", "instance file belongs to another host type",
                            EXIT_STATE_INVALID)
    seed = base64url_decode(_require_text("private_key_seed"))
    if len(seed) != SEED_BYTES:
        raise InstanceError("instance_state_invalid", "stored private seed has the wrong size",
                            EXIT_STATE_INVALID)
    public_key = _require_text("public_key")
    if len(base64url_decode(public_key)) != PUBLIC_KEY_BYTES:
        raise InstanceError("instance_state_invalid", "stored public key has the wrong size",
                            EXIT_STATE_INVALID)
    local_ref = _require_text("local_instance_ref")
    if len(local_ref) != 32 or any(character not in "0123456789abcdef" for character in local_ref):
        raise InstanceError("instance_state_invalid", "stored local_instance_ref is malformed",
                            EXIT_STATE_INVALID)
    return {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "os_type": _require_text("os_type"),
        "local_instance_ref": local_ref,
        "public_key": public_key,
        "private_key_seed": base64url_encode(seed),
        "display_name": _require_text("display_name"),
        "signer": _require_text("signer"),
        "created_at": str(payload.get("created_at") or ""),
    }


def read_instance(host_type: str) -> Optional[dict]:
    """Read and validate one host's instance file when it exists.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        The validated instance record, or ``None`` when no file exists.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the file is unreadable.
    """

    path = instance_path(host_type)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError) as exc:
        raise InstanceError(
            "instance_state_invalid",
            "the local instance file at %s is unreadable; run 'forget' and bind again" % path,
            EXIT_STATE_INVALID,
        ) from exc
    if not isinstance(payload, Mapping):
        raise InstanceError("instance_state_invalid", "the local instance file is not an object",
                            EXIT_STATE_INVALID)
    return _validate_instance(payload, host_type)


def _create_instance(host_type: str, display_name: Optional[str]) -> dict:
    """Create exactly one instance record and write it atomically.

    The signer is resolved before anything is created, so a machine with no
    usable signer leaves no partial state behind.

    Args:
        host_type: One of ``HOST_TYPES``.
        display_name: An optional caller-supplied name.

    Returns:
        The newly written instance record.
    """

    signer = resolve_signer()
    seed = generate_seed()
    public_key = derive_public_key(signer, seed)
    requested_name = str(display_name).strip() if display_name is not None else ""
    record = {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "os_type": os_type_value(),
        "local_instance_ref": os.urandom(16).hex(),
        "public_key": public_key,
        "private_key_seed": base64url_encode(seed),
        "display_name": (requested_name or default_display_name(host_type))[:120],
        "signer": signer,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _ensure_directory(host_dir(host_type))
    path = instance_path(host_type)
    handle, temporary = tempfile.mkstemp(prefix=".instance-", suffix=".tmp",
                                         dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _protect_file(temporary)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise
    return record


def _acquire_lock(host_type: str) -> int:
    """Take the exclusive initialization lock for one host, or fail closed.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        The open lock file descriptor.

    Raises:
        InstanceError: With ``instance_lock_timeout`` when the lock never frees.
    """

    path = lock_path(host_type)
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        try:
            handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            if _lock_is_stale(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass
                continue
            if time.monotonic() >= deadline:
                raise InstanceError(
                    "instance_lock_timeout",
                    "another process is still creating the local instance; retry shortly",
                    EXIT_LOCK_TIMEOUT,
                )
            time.sleep(random.uniform(LOCK_POLL_MIN_SECONDS, LOCK_POLL_MAX_SECONDS))
            continue
        try:
            os.write(handle, ("%d\n" % os.getpid()).encode("ascii"))
        except OSError:
            pass
        return handle


def _lock_is_stale(path: str) -> bool:
    """Return whether an existing lock belongs to a dead or wedged process.

    Args:
        path: The lock file path.

    Returns:
        True when the lock may be broken safely.
    """

    try:
        info = os.stat(path)
    except OSError:
        return True
    if time.time() - info.st_mtime > LOCK_STALE_SECONDS:
        return True
    if os.name == "nt":
        return False
    try:
        with open(path, "r", encoding="ascii") as handle:
            pid = int(handle.read().strip() or "0")
    except (OSError, ValueError):
        return False
    if pid <= 0:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _release_lock(host_type: str, handle: int) -> None:
    """Release one initialization lock.

    Args:
        host_type: One of ``HOST_TYPES``.
        handle: The open lock file descriptor.

    Returns:
        None.
    """

    try:
        os.close(handle)
    except OSError:
        pass
    try:
        os.unlink(lock_path(host_type))
    except OSError:
        pass


def ensure_instance(host_type: str, display_name: Optional[str] = None) -> dict:
    """Return the existing instance, creating exactly one when none exists.

    Two simultaneous first calls must produce exactly one key pair and one
    ``local_instance_ref``: the loser of the race waits for the exclusive lock
    and then re-reads the winner's file.

    Args:
        host_type: One of ``HOST_TYPES``.
        display_name: An optional name used only when creating the instance.

    Returns:
        The instance record for this host.
    """

    existing = read_instance(host_type)
    if existing is not None:
        return existing
    # Fail closed before creating a directory when no signer exists at all.
    resolve_signer()
    _ensure_directory(host_dir(host_type))
    handle = _acquire_lock(host_type)
    try:
        existing = read_instance(host_type)
        if existing is not None:
            return existing
        return _create_instance(host_type, display_name)
    finally:
        _release_lock(host_type, handle)


def forget_instance(host_type: str) -> bool:
    """Delete this host's local instance material.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        True when a file was removed, False when nothing existed.
    """

    removed = False
    for path in (instance_path(host_type), lock_path(host_type)):
        try:
            os.unlink(path)
            removed = removed or path.endswith(INSTANCE_FILE_NAME)
        except OSError:
            pass
    try:
        os.rmdir(host_dir(host_type))
    except OSError:
        pass
    return removed


# --- Commands --------------------------------------------------------------


def status_payload(instance: Mapping[str, Any]) -> dict:
    """Build the public status object for one instance.

    The private seed is never part of this payload.

    Args:
        instance: A validated instance record.

    Returns:
        The JSON-serializable status object.
    """

    return {
        "format_version": FORMAT_VERSION,
        "host_type": instance["host_type"],
        "os_type": instance["os_type"],
        "local_instance_ref": instance["local_instance_ref"],
        "public_key": instance["public_key"],
        "public_key_fingerprint": public_key_fingerprint(instance["public_key"]),
        "display_name": instance["display_name"],
        "data_dir": data_root(),
        "signer": instance["signer"],
        "instance_path": instance_path(instance["host_type"]),
        "created_at": instance["created_at"],
    }


def command_status(host_type: str, display_name: Optional[str] = None) -> dict:
    """Initialize the instance when absent and report its public fields.

    Args:
        host_type: One of ``HOST_TYPES``.
        display_name: An optional name used only on first creation.

    Returns:
        The status object.
    """

    return status_payload(ensure_instance(host_type, display_name))


def command_sign(host_type: str, challenge_file: str) -> dict:
    """Sign the exact challenge bytes read from a file.

    Args:
        host_type: One of ``HOST_TYPES``.
        challenge_file: Path to a file holding the exact challenge bytes.

    Returns:
        ``{"signature": ..., "public_key": ...}``.

    Raises:
        InstanceError: When the challenge cannot be read or signing fails.
    """

    try:
        with open(challenge_file, "rb") as handle:
            message = handle.read()
    except OSError as exc:
        raise InstanceError("instance_challenge_unreadable",
                            "cannot read the challenge file: %s" % exc, EXIT_CHALLENGE_UNREADABLE) from exc
    if not message:
        raise InstanceError("instance_challenge_empty", "the challenge file is empty",
                            EXIT_CHALLENGE_UNREADABLE)
    instance = ensure_instance(host_type)
    seed = base64url_decode(instance["private_key_seed"])
    signature = sign_message(resolve_signer(instance["signer"]), seed, message)
    return {"signature": signature, "public_key": instance["public_key"]}


def command_forget(host_type: str) -> dict:
    """Delete this host's local instance material and report the outcome.

    Args:
        host_type: One of ``HOST_TYPES``.

    Returns:
        ``{"forgotten": <bool>, ...}``.
    """

    removed = forget_instance(host_type)
    return {
        "forgotten": removed,
        "host_type": host_type,
        "data_dir": data_root(),
        "instance_path": instance_path(host_type),
    }


# --- CLI -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured argument parser.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="initialize when absent, then report the local instance")
    status.add_argument("--host", required=True, choices=sorted(HOST_TYPES))
    status.add_argument("--display-name", default=None)

    sign = sub.add_parser("sign", help="sign the exact challenge bytes read from a file")
    sign.add_argument("--host", required=True, choices=sorted(HOST_TYPES))
    sign.add_argument("--challenge-file", required=True)

    forget = sub.add_parser("forget", help="delete the local instance material")
    forget.add_argument("--host", required=True, choices=sorted(HOST_TYPES))
    return parser


def _json_out(value: Any) -> None:
    """Print one compact, key-sorted JSON object to stdout.

    Args:
        value: The JSON-serializable value.

    Returns:
        None.
    """

    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: Optional[list[str]] = None) -> int:
    """Run one subcommand and return its typed exit code.

    Args:
        argv: Optional argument list; defaults to ``sys.argv[1:]``.

    Returns:
        The process exit code.
    """

    args = _parser().parse_args(argv)
    try:
        if args.command == "status":
            _json_out(command_status(validate_host_type(args.host), args.display_name))
        elif args.command == "sign":
            _json_out(command_sign(validate_host_type(args.host), args.challenge_file))
        elif args.command == "forget":
            _json_out(command_forget(validate_host_type(args.host)))
        return EXIT_OK
    except InstanceError as exc:
        _json_out({"ok": False, "error_code": exc.code, "message": exc.message})
        return exc.exit_code
    except (OSError, ValueError) as exc:
        _json_out({"ok": False, "error_code": "instance_local_failure", "message": str(exc)})
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
