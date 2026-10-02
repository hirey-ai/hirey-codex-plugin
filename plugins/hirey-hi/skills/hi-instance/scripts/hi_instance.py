#!/usr/bin/env python3
"""Local Hi host-instance helper: one key pair per verified (Person, Agent).

The MCP control plane stays outside this module.  Hi separates *which Person and
Agent* may act (the OAuth Agent Session) from *which end of the user's work* is
calling (one local instance key pair).  This helper owns only the local half:

* one host installation keeps one stable, random ``local_instance_ref`` that is
  an audit clue only: it ties successive uses of the same installation together,
  but is never proof of physical hardware, of Person identity, or of any
  cross-Person authority;
* inside that installation it keeps one isolated profile per verified
  (Person, logical Agent), each with its own Ed25519 key pair, so one
  installation switching Person A -> B -> A keeps two separate server instances
  and restores A's original identity on return;
* profile names are the server's opaque ``profile_key`` digest, never a name or
  an email, and the helper never trusts a local profile field as server
  authority;
* a legacy single-key ``instance.json`` written by an older helper is read and
  preserved, and its key is migrated lazily into the first profile;
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
import errno
import hashlib
import json
import os
import platform
import random
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from typing import Any, Mapping, Optional

# The removal guard's operating-system file lock: ``fcntl`` on POSIX, ``msvcrt``
# on Windows.  Each import fails on the other platform.
try:
    import fcntl
except ImportError:
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

# --- Frozen local contract -------------------------------------------------

# The on-disk format of the installation and profile files.  A future shape must
# use a new number so an old helper never half-reads a newer file.
FORMAT_VERSION = 2

# A legacy single-key instance file from the previous helper is readable, never
# rewritten, so an upgrade preserves its key and server instance.
LEGACY_FORMAT_VERSION = 1

# Client type grammar matches the server's provenance-only host_type contract.
HOST_TYPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

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

INSTALLATION_FILE_NAME = "installation.json"
PROFILES_DIR_NAME = "profiles"
LEGACY_INSTANCE_FILE_NAME = "instance.json"
LOCK_FILE_NAME = "instance.lock"

# The three legacy-status values an installation can carry.  "unclaimed" means a
# legacy key exists and no profile has inherited it yet; inheriting it marks the
# installation "claimed" so a second Person on the same machine gets a fresh key;
# "refused" records that the migrated key was rejected by the server as already
# bound elsewhere, so no later profile may inherit it.
LEGACY_ABSENT = "absent"
LEGACY_UNCLAIMED = "unclaimed"
LEGACY_CLAIMED = "claimed"
LEGACY_REFUSED = "refused"
LEGACY_STATUSES = (LEGACY_ABSENT, LEGACY_UNCLAIMED, LEGACY_CLAIMED, LEGACY_REFUSED)

# One installation ref is 16 random bytes rendered as 32 lowercase hex characters,
# the same shape the previous single-file helper wrote.
INSTALLATION_REF_LENGTH = 32

# A per-identity profile key is the server's sha256 hex digest, used verbatim as
# a filename and validated before any path join.
PROFILE_KEY_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# The server's instance identifier, recorded locally after a successful bind.
INSTANCE_ID_PATTERN = re.compile(r"^agi_[a-z0-9_-]{12,64}$")

# A first call that loses the initialization race waits for the winner instead of
# creating a second key pair.  The lock file appears atomically with its owner's
# pid and a random token already inside it, so a waiter never sees an ownerless
# lock.  A lock whose owner pid is gone, or which is older than the stale window,
# belongs to a crashed or wedged process and may be broken, so a killed run can
# never wedge the helper.  A lock whose content cannot be read as a complete owner
# record (for example one an older helper created but had not yet written) is
# treated as live until the stale window, never as broken.
#
# Taking the lock needs only its free name.  Every removal of the lock (breaking a
# stale one, a normal release, forget) runs inside one guard,
# ``instance.lock.break``.  Inside it the remover checks the lock that is there
# now and unlinks it; no other remover can free the name in between, and a new
# lock can only take a free name, so a live lock is never removed or moved.
#
# The guard is an operating-system lock on the file ``instance.lock.break``:
# ``flock`` on POSIX, ``msvcrt.locking`` on Windows.  The system frees it when its
# holder exits or is killed, and never takes it from a live holder, however long
# that holder is paused or asleep and whatever the clock does.  On a file system
# that cannot lock files the helper fails closed before it writes any state,
# rather than remove a lock unguarded.
LOCK_TIMEOUT_SECONDS = 20.0
LOCK_STALE_SECONDS = 60.0
LOCK_GUARD_SUFFIX = ".break"
LOCK_POLL_MIN_SECONDS = 0.02
LOCK_POLL_MAX_SECONDS = 0.05
LOCK_RECORD_PATTERN = re.compile(rb"\A([1-9][0-9]{0,9})\n(?:([0-9a-f]{32})\n)?\Z")
LOCK_RECORD_MAX_BYTES = 128

# Errors that mean "this file system cannot make hard links" rather than "the
# name is taken".  Only then does creation fall back: the lock to O_EXCL, a new
# record to the lock alone, as before.
LINK_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in
                      ("EPERM", "ENOTSUP", "EOPNOTSUPP", "ENOSYS", "EXDEV"))
    if code is not None
)

# Windows keeps a deleted lock's name until its last handle closes and reports
# that name as access denied, so there a PermissionError means "still taken".
LOCK_NAME_HELD_ON_PERMISSION_ERROR = os.name == "nt"

# Errors from a non-blocking guard lock that mean "another caller holds it", and
# errors that mean "this file system cannot lock files".  Any other error is a
# real failure and is raised.
GUARD_BUSY_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in
                      ("EAGAIN", "EWOULDBLOCK", "EACCES"))
    if code is not None
)
GUARD_UNSUPPORTED_ERRNOS = frozenset(
    code for code in (getattr(errno, name, None) for name in
                      ("ENOLCK", "ENOTSUP", "EOPNOTSUPP", "EINVAL", "ENOSYS"))
    if code is not None
)

# Without hard links a new record is created exclusively and then written in one
# write, so a reader can briefly find it empty; it waits that moment out.
RECORD_EMPTY_RETRY_SECONDS = 1.0

# Typed exit codes.  Every failure path is distinguishable without parsing text.
EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_SIGNER_UNAVAILABLE = 3
EXIT_STATE_INVALID = 4
EXIT_CHALLENGE_UNREADABLE = 5
EXIT_LOCK_TIMEOUT = 6
EXIT_PROFILE_REQUIRED = 7

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
        host_type: A validated client-reported host type.

    Returns:
        An absolute directory path.
    """

    return os.path.join(data_root(), validate_host_type(host_type))


def installation_path(host_type: str) -> str:
    """Return the absolute path of one host's installation record.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        ``<root>/<host_type>/installation.json``.
    """

    return os.path.join(host_dir(host_type), INSTALLATION_FILE_NAME)


def profiles_dir(host_type: str) -> str:
    """Return the absolute directory that holds one host's per-identity profiles.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        ``<root>/<host_type>/profiles``.
    """

    return os.path.join(host_dir(host_type), PROFILES_DIR_NAME)


def profile_path(host_type: str, profile_key_value: str) -> str:
    """Return the absolute path of one validated profile file.

    The profile key is validated to the server's 64-hex digest before any path
    join, so a caller-supplied value can never escape the profiles directory.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server-issued opaque profile key.

    Returns:
        ``<root>/<host_type>/profiles/<profile_key>.json``.
    """

    return os.path.join(profiles_dir(host_type),
                        validate_profile_key(profile_key_value) + ".json")


def instance_path(host_type: str) -> str:
    """Return the absolute path of one host's legacy instance file.

    This is the file the previous single-key helper wrote.  It is read on upgrade
    and preserved verbatim, never rewritten, so a rollback still finds the key it
    left behind.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        ``<root>/<host_type>/instance.json``.
    """

    return os.path.join(host_dir(host_type), LEGACY_INSTANCE_FILE_NAME)


def lock_path(host_type: str) -> str:
    """Return the absolute path of one host's initialization lock file.

    Args:
        host_type: A validated client-reported host type.

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
    if not HOST_TYPE_PATTERN.fullmatch(text):
        raise InstanceError(
            "instance_host_type_invalid",
            "--host must be a client type token (1-64 lowercase letters, digits, dot, underscore or hyphen)",
            EXIT_USAGE,
        )
    return text


def validate_profile_key(value: Any) -> str:
    """Return one accepted profile key or fail closed.

    Args:
        value: The ``--profile`` argument, normally the server's opaque
            ``profile_key`` for the verified (Person, logical Agent).

    Returns:
        The validated lowercase sha256 hex digest.

    Raises:
        InstanceError: With ``instance_profile_required`` when the value is
            missing or malformed, so a caller never falls back to a guessed or
            shared profile.
    """

    text = str(value or "").strip().lower()
    if not text:
        raise InstanceError(
            "instance_profile_required",
            "a verified profile key is required; call agent_instance.current and pass "
            "its profile_key to --profile",
            EXIT_PROFILE_REQUIRED,
        )
    if not PROFILE_KEY_PATTERN.match(text):
        raise InstanceError(
            "instance_profile_invalid",
            "the profile key is not the server's 64-character hex digest",
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
        host_type: A validated client-reported host type.

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


def _json_file_payload(path: str, description: str) -> Optional[dict]:
    """Read one small JSON object from disk, or return ``None`` when absent.

    Args:
        path: The file to read.
        description: Human-readable label used in a typed failure.

    Returns:
        The decoded object, or ``None`` when the file does not exist.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the file is
            unreadable or is not a JSON object.
    """

    if not os.path.exists(path):
        return None
    deadline = time.monotonic() + RECORD_EMPTY_RETRY_SECONDS
    try:
        while True:
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            if text or time.monotonic() >= deadline:
                break
            time.sleep(random.uniform(LOCK_POLL_MIN_SECONDS, LOCK_POLL_MAX_SECONDS))
        payload = json.loads(text)
    except (OSError, ValueError) as exc:
        raise InstanceError(
            "instance_state_invalid",
            "the local %s at %s is unreadable; run 'forget' and bind again" % (description, path),
            EXIT_STATE_INVALID,
        ) from exc
    if not isinstance(payload, Mapping):
        raise InstanceError(
            "instance_state_invalid",
            "the local %s at %s is not an object" % (description, path),
            EXIT_STATE_INVALID,
        )
    return dict(payload)


def _record_text(payload: Mapping[str, Any], key: str, description: str) -> str:
    """Return one required non-empty string field of a local record, or fail closed.

    Args:
        payload: The parsed record.
        key: The required field name.
        description: Human-readable record label for the error message.

    Returns:
        The stripped field value.

    Raises:
        InstanceError: When the field is missing or empty.
    """

    value = str(payload.get(key) or "").strip()
    if not value:
        raise InstanceError("instance_state_invalid",
                            "%s is missing %s" % (description, key), EXIT_STATE_INVALID)
    return value


def _valid_ref(value: str) -> bool:
    """Return whether ``value`` is one canonical 32-character hex installation ref."""

    return len(value) == INSTALLATION_REF_LENGTH and all(
        character in "0123456789abcdef" for character in value)


def _validate_installation(payload: Mapping[str, Any], host_type: str) -> dict:
    """Return one validated installation record or fail closed.

    Args:
        payload: The parsed installation file.
        host_type: The host type the file must belong to.

    Returns:
        The validated record.

    Raises:
        InstanceError: With ``instance_state_invalid`` for any local corruption.
    """

    if int(payload.get("format_version") or 0) != FORMAT_VERSION:
        raise InstanceError("instance_state_invalid", "unsupported installation file format",
                            EXIT_STATE_INVALID)
    if _record_text(payload, "host_type", "installation file") != host_type:
        raise InstanceError("instance_state_invalid",
                            "installation file belongs to another host type", EXIT_STATE_INVALID)
    reference = _record_text(payload, "installation_ref", "installation file")
    if not _valid_ref(reference):
        raise InstanceError("instance_state_invalid", "stored installation_ref is malformed",
                            EXIT_STATE_INVALID)
    legacy = str(payload.get("legacy_status") or LEGACY_ABSENT).strip()
    if legacy not in LEGACY_STATUSES:
        raise InstanceError("instance_state_invalid", "stored legacy_status is malformed",
                            EXIT_STATE_INVALID)
    return {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "os_type": _record_text(payload, "os_type", "installation file"),
        "installation_ref": reference,
        "legacy_status": legacy,
        "created_at": str(payload.get("created_at") or ""),
    }


def _validate_profile(payload: Mapping[str, Any], host_type: str, profile_key_value: str) -> dict:
    """Return one validated per-identity profile or fail closed.

    Args:
        payload: The parsed profile file.
        host_type: The host type the profile must belong to.
        profile_key_value: The exact profile key the filename encodes.

    Returns:
        The validated profile.

    Raises:
        InstanceError: With ``instance_state_invalid`` for any local corruption.
    """

    description = "profile file"
    if int(payload.get("format_version") or 0) != FORMAT_VERSION:
        raise InstanceError("instance_state_invalid", "unsupported %s format" % description,
                            EXIT_STATE_INVALID)
    if _record_text(payload, "host_type", description) != host_type:
        raise InstanceError("instance_state_invalid",
                            "%s belongs to another host type" % description, EXIT_STATE_INVALID)
    if _record_text(payload, "profile_key", description) != profile_key_value:
        raise InstanceError("instance_state_invalid",
                            "%s belongs to another profile" % description, EXIT_STATE_INVALID)
    seed = base64url_decode(_record_text(payload, "private_key_seed", description))
    if len(seed) != SEED_BYTES:
        raise InstanceError("instance_state_invalid",
                            "stored private seed has the wrong size", EXIT_STATE_INVALID)
    public_key = _record_text(payload, "public_key", description)
    if len(base64url_decode(public_key)) != PUBLIC_KEY_BYTES:
        raise InstanceError("instance_state_invalid",
                            "stored public key has the wrong size", EXIT_STATE_INVALID)
    signer = _record_text(payload, "signer", description)
    if signer not in SIGNERS:
        raise InstanceError("instance_state_invalid", "stored signer is unsupported",
                            EXIT_STATE_INVALID)
    instance_id = str(payload.get("server_instance_id") or "").strip() or None
    if instance_id is not None and not INSTANCE_ID_PATTERN.match(instance_id):
        raise InstanceError("instance_state_invalid", "stored server_instance_id is malformed",
                            EXIT_STATE_INVALID)
    return {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "profile_key": profile_key_value,
        "os_type": _record_text(payload, "os_type", description),
        "public_key": public_key,
        "private_key_seed": base64url_encode(seed),
        "display_name": _record_text(payload, "display_name", description),
        "signer": signer,
        "server_instance_id": instance_id,
        "source": str(payload.get("source") or "created"),
        "created_at": str(payload.get("created_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _validate_legacy_instance(payload: Mapping[str, Any], host_type: str) -> dict:
    """Return one validated legacy single-key instance record or fail closed.

    The legacy record is read only.  It is never rewritten, so a rollback to the
    previous helper still finds the exact key and reference it left behind.

    Args:
        payload: The parsed legacy ``instance.json``.
        host_type: The host type the file must belong to.

    Returns:
        The validated legacy record.

    Raises:
        InstanceError: With ``instance_state_invalid`` for any local corruption.
    """

    description = "legacy instance file"
    if int(payload.get("format_version") or 0) != LEGACY_FORMAT_VERSION:
        raise InstanceError("instance_state_invalid", "unsupported %s format" % description,
                            EXIT_STATE_INVALID)
    if _record_text(payload, "host_type", description) != host_type:
        raise InstanceError("instance_state_invalid",
                            "%s belongs to another host type" % description, EXIT_STATE_INVALID)
    seed = base64url_decode(_record_text(payload, "private_key_seed", description))
    if len(seed) != SEED_BYTES:
        raise InstanceError("instance_state_invalid",
                            "legacy private seed has the wrong size", EXIT_STATE_INVALID)
    public_key = _record_text(payload, "public_key", description)
    if len(base64url_decode(public_key)) != PUBLIC_KEY_BYTES:
        raise InstanceError("instance_state_invalid",
                            "legacy public key has the wrong size", EXIT_STATE_INVALID)
    reference = _record_text(payload, "local_instance_ref", description)
    if not _valid_ref(reference):
        raise InstanceError("instance_state_invalid",
                            "legacy local_instance_ref is malformed", EXIT_STATE_INVALID)
    return {
        "format_version": LEGACY_FORMAT_VERSION,
        "host_type": host_type,
        "os_type": _record_text(payload, "os_type", description),
        "local_instance_ref": reference,
        "public_key": public_key,
        "private_key_seed": base64url_encode(seed),
        "display_name": _record_text(payload, "display_name", description),
        "signer": _record_text(payload, "signer", description),
        "created_at": str(payload.get("created_at") or ""),
    }


def read_installation(host_type: str) -> Optional[dict]:
    """Read and validate one host installation record when it exists.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        The validated installation, or ``None`` when absent.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the file is unreadable.
    """

    payload = _json_file_payload(installation_path(host_type), "installation file")
    return None if payload is None else _validate_installation(payload, host_type)


def read_profile(host_type: str, profile_key_value: str) -> Optional[dict]:
    """Read and validate one per-identity profile when it exists.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server-issued opaque profile key.

    Returns:
        The validated profile, or ``None`` when absent.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the file is unreadable.
    """

    key = validate_profile_key(profile_key_value)
    payload = _json_file_payload(profile_path(host_type, key), "profile file")
    return None if payload is None else _validate_profile(payload, host_type, key)


def read_legacy_instance(host_type: str) -> Optional[dict]:
    """Read and validate the legacy single-key instance file when it exists.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        The validated legacy record, or ``None`` when absent.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the file is unreadable.
    """

    payload = _json_file_payload(instance_path(host_type), "legacy instance file")
    return None if payload is None else _validate_legacy_instance(payload, host_type)


def iter_profiles(host_type: str) -> list[dict]:
    """Return every valid per-identity profile in one host installation.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        A list of validated profiles, ordered by profile key.
    """

    directory = profiles_dir(host_type)
    if not os.path.isdir(directory):
        return []
    profiles = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".json") or not PROFILE_KEY_PATTERN.match(name[:-5]):
            continue
        profile = read_profile(host_type, name[:-5])
        if profile is not None:
            profiles.append(profile)
    return profiles


def _remove_quietly(path: str) -> None:
    """Remove one file if it exists, ignoring any failure."""

    try:
        os.unlink(path)
    except OSError:
        pass


def _temporary_json(path: str, record: Mapping[str, Any]) -> str:
    """Write one JSON record to a new owner-only temporary file beside ``path``.

    Args:
        path: The final destination the temporary file will be moved to.
        record: The JSON-serializable record.

    Returns:
        The temporary file path, fully written and flushed to disk.
    """

    _ensure_directory(os.path.dirname(path))
    handle, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _protect_file(temporary)
    except BaseException:
        _remove_quietly(temporary)
        raise
    return temporary


def _atomic_write_json(path: str, record: Mapping[str, Any]) -> dict:
    """Write one JSON record atomically with owner-only permissions.

    Args:
        path: Destination path.
        record: The JSON-serializable record.

    Returns:
        The record that was written.
    """

    temporary = _temporary_json(path, record)
    try:
        os.replace(temporary, path)
    except BaseException:
        _remove_quietly(temporary)
        raise
    return dict(record)


def _place_new_file(temporary: str, path: str) -> Optional[bool]:
    """Give a finished temporary file the name ``path`` only if that name is free.

    The name appears atomically with the complete content, and an existing file
    at ``path`` is never replaced.  The temporary name is gone afterwards in every
    case.

    Args:
        temporary: A fully written file in the same directory as ``path``.
        path: The destination name.

    Returns:
        True when this call created ``path``; False when the name was already
        taken; None when this file system cannot create a name exclusively and
        atomically (it has no hard links), so the caller must fall back.

    Raises:
        OSError: Any other failure.  On Windows a ``PermissionError`` means the
            name still belongs to a file that is being deleted.
    """

    try:
        if os.name == "nt":
            # A Windows rename never replaces an existing name.
            os.rename(temporary, path)
        elif hasattr(os, "link"):
            os.link(temporary, path)
        else:
            return None
        return True
    except FileExistsError:
        return False
    except OSError as exc:
        if os.name != "nt" and exc.errno in LINK_UNSUPPORTED_ERRNOS:
            return None
        raise
    finally:
        _remove_quietly(temporary)


def _publish_new_json(path: str, record: Mapping[str, Any]) -> bool:
    """Create one JSON record file, never replacing a record that already exists.

    This is what keeps one installation ref and one key pair per identity even if
    two calls ever reach creation together: the second call finds the first
    call's file and must adopt it instead of overwriting it.

    Args:
        path: Destination path.
        record: The JSON-serializable record.

    Returns:
        True when this call created the file; False when a file already existed
        and was left untouched.
    """

    created = _place_new_file(_temporary_json(path, record), path)
    if created is not None:
        return created
    # No hard links: claim the name exclusively and write the whole record in one
    # write.  A reader that meets the brief empty file waits for the content.
    data = (json.dumps(record, sort_keys=True, indent=2) + "\n").encode("utf-8")
    return _create_exclusive(path, data, durable=True)


def _create_exclusive(path: str, data: bytes, durable: bool = False) -> bool:
    """Create ``path`` holding ``data`` only if the name is free, without hard links.

    Args:
        path: Destination path.
        data: The complete content, written in one write.
        durable: Whether to flush the content to disk before returning.

    Returns:
        True when this call created the file; False when the name was taken.
    """

    try:
        handle = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        return False
    try:
        if os.write(handle, data) != len(data):
            raise OSError(errno.EIO, "short write", path)
        if durable:
            os.fsync(handle)
    except BaseException:
        os.close(handle)
        _remove_quietly(path)
        raise
    os.close(handle)
    _protect_file(path)
    return True


def write_installation(host_type: str, installation: Mapping[str, Any]) -> dict:
    """Write one installation record atomically.

    Args:
        host_type: A validated client-reported host type.
        installation: The record to write.

    Returns:
        The record that was written.
    """

    return _atomic_write_json(installation_path(host_type), installation)


def write_profile(host_type: str, profile: Mapping[str, Any]) -> dict:
    """Write one profile record atomically.

    Args:
        host_type: A validated client-reported host type.
        profile: The record to write; its ``profile_key`` names the file.

    Returns:
        The record that was written.
    """

    return _atomic_write_json(profile_path(host_type, profile["profile_key"]), profile)


def _new_installation(host_type: str, legacy: Optional[Mapping[str, Any]]) -> dict:
    """Build one new installation record, adopting the legacy ref when present.

    Adopting the legacy reference is what lets the server reuse the instance a
    Person already bound with the old helper.  The ref remains an audit clue
    only, never an identity or an authority.
    """

    if legacy is not None:
        reference = legacy["local_instance_ref"]
        legacy_status = LEGACY_UNCLAIMED
    else:
        reference = os.urandom(16).hex()
        legacy_status = LEGACY_ABSENT
    return {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "os_type": os_type_value(),
        "installation_ref": reference,
        "legacy_status": legacy_status,
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def _read_or_create_installation_locked(host_type: str) -> dict:
    """Return the installation record, creating it while the caller holds the lock."""

    existing = read_installation(host_type)
    if existing is not None:
        return existing
    installation = _new_installation(host_type, read_legacy_instance(host_type))
    if _publish_new_json(installation_path(host_type), installation):
        return dict(installation)
    # Another call created the installation first: adopt it, never replace it.
    return _adopt_existing(read_installation(host_type), "installation")


def _adopt_existing(record: Optional[dict], description: str) -> dict:
    """Return a record another call created first, or fail closed if it vanished.

    Args:
        record: The re-read record, or ``None`` when it no longer exists.
        description: Human-readable label used in a typed failure.

    Returns:
        The existing record.

    Raises:
        InstanceError: With ``instance_state_invalid`` when the record that
            blocked creation disappeared before it could be read.
    """

    if record is None:
        raise InstanceError(
            "instance_state_invalid",
            "the local %s changed while this call was creating it; retry" % description,
            EXIT_STATE_INVALID,
        )
    return record


def _new_profile(host_type: str, key: str, signer: str, seed: bytes, public_key: str,
                 display_name: Optional[str], source: str) -> dict:
    """Build one profile record with its own key pair and no cached server id."""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "profile_key": key,
        "os_type": os_type_value(),
        "public_key": public_key,
        "private_key_seed": base64url_encode(seed),
        "display_name": (str(display_name or "").strip() or default_display_name(host_type))[:120],
        "signer": signer,
        "server_instance_id": None,
        "source": source,
        "created_at": now,
        "updated_at": now,
    }


def _create_profile_locked(host_type: str, key: str, display_name: Optional[str],
                           confirmed_legacy_fingerprint: Optional[str] = None) -> dict:
    """Create one profile while the caller holds the lock.

    A legacy single-key file is never inherited on the strength of local state
    alone: the first Person to log in after an upgrade could be someone other
    than the old owner, and giving them the old key both fails the server's
    key-ownership check and consumes the migration slot.  The key is adopted
    only when the server has confirmed, for this verified (Person, Agent) and
    this installation ref, the exact fingerprint the legacy file holds.  Every
    other identity gets a fresh key of its own, and the legacy file stays
    ``unclaimed`` so its true owner can still adopt it on a later login.

    An existing profile file is never replaced here: if another call created
    this identity's profile first, its key pair is the one returned.  The
    installation is marked ``claimed`` only after this call's profile, holding
    the legacy key, actually exists.
    """

    installation = _read_or_create_installation_locked(host_type)
    legacy = read_legacy_instance(host_type)
    inherit = (
        legacy is not None
        and installation["legacy_status"] == LEGACY_UNCLAIMED
        and confirmed_legacy_fingerprint is not None
        and confirmed_legacy_fingerprint == public_key_fingerprint(legacy["public_key"])
        and not any(profile["public_key"] == legacy["public_key"]
                    for profile in iter_profiles(host_type))
    )
    if inherit:
        profile = _new_profile(host_type, key, legacy["signer"],
                               base64url_decode(legacy["private_key_seed"]),
                               legacy["public_key"],
                               display_name or legacy["display_name"], "legacy_migrated")
    else:
        signer = resolve_signer()
        seed = generate_seed()
        profile = _new_profile(host_type, key, signer, seed,
                               derive_public_key(signer, seed), display_name, "created")
    if not _publish_new_json(profile_path(host_type, key), profile):
        # Another call created this identity's key first: adopt it, never replace it.
        return _adopt_existing(read_profile(host_type, key), "profile")
    if inherit:
        installation["legacy_status"] = LEGACY_CLAIMED
        write_installation(host_type, installation)
    return dict(profile)


def _owner_record() -> bytes:
    """Return a new complete owner record: this process's pid and a random token."""

    return ("%d\n%s\n" % (os.getpid(), os.urandom(16).hex())).encode("ascii")


def _acquire_lock(host_type: str) -> bytes:
    """Take the exclusive initialization lock for one host, or fail closed.

    The lock file appears atomically with this process's complete owner record
    (pid and a random token) already in it.  A waiter breaks a lock only when it
    can tell the owner is gone (dead pid, or older than the stale window), and
    only inside the removal guard.  Every retry path is bounded by the same
    deadline.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        The exact owner record written into the lock, to pass to
        ``_release_lock``.

    Raises:
        InstanceError: With ``instance_lock_timeout`` when the lock never frees,
            or ``instance_lock_unsupported`` when this file system cannot hold
            the removal guard.
    """

    path = lock_path(host_type)
    # A file system that cannot lock files fails here, before any state exists,
    # rather than at the release after this call has written its records.
    probe = _try_guard(path)
    if probe is not None:
        _drop_guard(path, probe)
    record = _owner_record()
    deadline = time.monotonic() + LOCK_TIMEOUT_SECONDS
    while True:
        if _create_lock_file(path, record):
            return record
        state = _inspect_lock(path)
        retry_now = state == "gone" or (state == "stale" and _remove_lock(path, None, deadline))
        if time.monotonic() >= deadline:
            raise InstanceError(
                "instance_lock_timeout",
                "another process is still creating the local instance; retry shortly",
                EXIT_LOCK_TIMEOUT,
            )
        if not retry_now:
            time.sleep(random.uniform(LOCK_POLL_MIN_SECONDS, LOCK_POLL_MAX_SECONDS))


def _create_lock_file(path: str, record: bytes) -> bool:
    """Create the lock file holding ``record``, or report that it is taken.

    Args:
        path: The lock file path.
        record: The complete owner record.

    Returns:
        True when this call now holds the lock; False when another holder has it.
    """

    handle, temporary = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=os.path.dirname(path))
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(record)
    except BaseException:
        _remove_quietly(temporary)
        raise
    try:
        created = _place_new_file(temporary, path)
    except PermissionError:
        if LOCK_NAME_HELD_ON_PERMISSION_ERROR:
            return False
        raise
    if created is not None:
        return created
    # This file system has no hard links: create exclusively, then write.  A
    # waiter that reads the brief empty file treats it as live, never as broken.
    return _create_exclusive(path, record)


def _read_lock_owner(path: str) -> Optional[int]:
    """Return the owner pid recorded in one lock, or ``None`` when it is not complete.

    Args:
        path: The lock file path.

    Returns:
        The pid of a complete owner record; ``None`` for a missing, unreadable,
        empty, partial or otherwise unparseable lock.
    """

    try:
        with open(path, "rb") as handle:
            content = handle.read(LOCK_RECORD_MAX_BYTES + 1)
    except OSError:
        return None
    match = LOCK_RECORD_PATTERN.match(content)
    return int(match.group(1)) if match else None


def _inspect_lock(path: str) -> str:
    """Classify an existing lock as ``gone``, ``live`` or ``stale``.

    Anything uncertain is ``live``: the caller then waits, bounded by its
    deadline, and the stale window still frees a lock whose owner died before it
    could be identified.

    Args:
        path: The lock file path.

    Returns:
        ``gone``, ``live`` or ``stale``.
    """

    try:
        info = os.stat(path)
    except FileNotFoundError:
        return "gone"
    except OSError:
        # Windows reports a lock that is being deleted as access denied.
        return "live"
    if time.time() - info.st_mtime > LOCK_STALE_SECONDS:
        return "stale"
    if os.name == "nt":
        # Windows has no harmless liveness probe (os.kill terminates the
        # process), and reading the file would hold a handle that blocks the
        # owner's delete, so age alone decides there.
        return "live"
    pid = _read_lock_owner(path)
    if pid is None:
        return "live"
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "stale"
    except (OSError, OverflowError):
        # PermissionError: the pid is alive under another user.
        return "live"
    return "live"


def _unlink(path: str) -> bool:
    """Unlink one file and report whether its name is now free.

    Args:
        path: The file path.

    Returns:
        True when the file was removed or was already absent; False when it
        could not be removed.
    """

    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _remove_lock(path: str, record: Optional[bytes], deadline: float) -> bool:
    """Remove the lock inside the removal guard: a stale lock, or the caller's own.

    Inside the guard no other remover can free the lock name, and a new lock can
    only appear on a free name, so the lock checked here is the lock unlinked.  A
    live lock is never removed or moved.

    Args:
        path: The lock file path.
        record: The caller's own owner record, to release its lock; ``None`` to
            remove the lock only if it is stale.
        deadline: When to stop waiting for the guard.

    Returns:
        True when the lock is gone (removed here, or already absent); False when
        it is live, belongs to another holder, could not be removed, or the guard
        did not free before the deadline.
    """

    guard = _take_guard(path, deadline)
    if guard is None:
        return False
    try:
        if record is None:
            state = _inspect_lock(path)
            if state != "stale":
                return state == "gone"
        else:
            try:
                with open(path, "rb") as handle:
                    content = handle.read(LOCK_RECORD_MAX_BYTES + 1)
            except FileNotFoundError:
                return True
            except OSError:
                # Unverifiable: release it as before rather than wedge every later call.
                content = record
            if content != record:
                # This lock was broken as stale and another call holds it now.
                return False
        return _unlink(path)
    finally:
        _drop_guard(path, guard)


def _take_guard(path: str, deadline: float) -> Optional[int]:
    """Take the guard that every removal of the lock at ``path`` runs inside.

    The guard is an operating-system lock on ``<path>.break``.  The system frees
    it when its holder exits or is killed and never takes it from a live
    holder, so a slow, paused or sleeping holder simply keeps it, and a waiter
    stops at its deadline instead.

    Args:
        path: The lock file path.
        deadline: When to stop waiting.

    Returns:
        The open guard file, to pass to ``_drop_guard``; ``None`` when the guard
        did not free before the deadline.

    Raises:
        InstanceError: With ``instance_lock_unsupported`` when this file system
            cannot lock files.
    """

    while True:
        handle = _try_guard(path)
        if handle is not None:
            return handle
        if time.monotonic() >= deadline:
            return None
        time.sleep(random.uniform(LOCK_POLL_MIN_SECONDS, LOCK_POLL_MAX_SECONDS))


def _try_guard(path: str) -> Optional[int]:
    """Try once, without waiting, to take the guard of the lock at ``path``.

    On POSIX the holder removes the guard file while it still holds it (see
    ``_drop_guard``), so a caller that opened the file earlier can lock a file
    that no longer has the name.  This call therefore holds the guard only when
    the name still refers to the very file it locked.  Windows never removes a
    file that any caller has open, so there the file locked is always the one
    at the name.

    Args:
        path: The lock file path.

    Returns:
        The open, locked guard file; ``None`` when another caller holds the
        guard.

    Raises:
        InstanceError: With ``instance_lock_unsupported`` when this file system
            cannot lock files.
    """

    guard = path + LOCK_GUARD_SUFFIX
    try:
        handle = os.open(guard, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
    except PermissionError:
        # Windows reports a guard file that is being deleted as access denied.
        if LOCK_NAME_HELD_ON_PERMISSION_ERROR:
            return None
        raise
    try:
        if _lock_guard_file(handle, guard) and _names_this_file(guard, handle):
            return handle
    except BaseException:
        _close_unheld_guard(guard, handle)
        raise
    _close_unheld_guard(guard, handle)
    return None


def _lock_guard_file(handle: int, guard: str) -> bool:
    """Take the operating-system lock on the open guard file without waiting.

    Args:
        handle: The open guard file.
        guard: The guard file path, for the error message.

    Returns:
        True when this call now holds the lock; False when another caller does.

    Raises:
        InstanceError: With ``instance_lock_unsupported`` when this file system
            cannot lock files.
    """

    try:
        if msvcrt is not None:
            # Lock the first byte; Windows allows a lock past the end of a file.
            msvcrt.locking(handle, msvcrt.LK_NBLCK, 1)
        elif fcntl is not None:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        else:
            raise OSError(errno.ENOSYS, "no file locking on this platform", guard)
    except OSError as exc:
        if exc.errno in GUARD_BUSY_ERRNOS:
            return False
        if exc.errno in GUARD_UNSUPPORTED_ERRNOS:
            raise InstanceError(
                "instance_lock_unsupported",
                "the file system that holds %s cannot lock files, so the local instance "
                "cannot be changed safely there; keep the Hi data directory on a local disk"
                % os.path.dirname(guard),
                EXIT_FAILURE,
            ) from exc
        raise
    return True


def _names_this_file(guard: str, handle: int) -> bool:
    """Return whether the guard path still names the open file ``handle``.

    Args:
        guard: The guard file path.
        handle: The open guard file.

    Returns:
        True when the path names this very file.
    """

    if os.name == "nt":
        return True
    try:
        named = os.stat(guard)
    except FileNotFoundError:
        return False
    opened = os.fstat(handle)
    return (named.st_dev, named.st_ino) == (opened.st_dev, opened.st_ino)


def _close_unheld_guard(guard: str, handle: int) -> None:
    """Close a guard file this call does not hold.

    On Windows the file is then removed if nobody else has it open: a holder
    always has it open and Windows never removes such a file, so this never
    removes a held guard, and an idle guard file never outlives its last user.
    On POSIX only a holder removes the file.

    Args:
        guard: The guard file path.
        handle: The open guard file.

    Returns:
        None.
    """

    os.close(handle)
    if os.name == "nt":
        _remove_quietly(guard)


def _drop_guard(path: str, handle: int) -> None:
    """Let go of the guard and remove its file.

    On POSIX the file is removed while it is still locked.  This holder checked
    that the name refers to its own file, and only a holder ever removes the
    name, so the file removed is this holder's own; a caller that locks the old
    file afterwards finds the name gone or reused and tries again.  On Windows
    the lock is released and the file closed first, then the file is removed
    if nobody else has it open.

    Args:
        path: The lock file path.
        handle: The open guard file ``_take_guard`` returned.

    Returns:
        None.
    """

    guard = path + LOCK_GUARD_SUFFIX
    if os.name == "nt":
        try:
            msvcrt.locking(handle, msvcrt.LK_UNLCK, 1)
        except OSError:
            pass  # Closing the file releases the lock as well.
        _close_unheld_guard(guard, handle)
    else:
        _remove_quietly(guard)
        os.close(handle)


def _release_lock(host_type: str, record: bytes) -> None:
    """Release one initialization lock, but never another holder's lock.

    The release runs inside the removal guard like every other removal.  If the
    guard never frees before the deadline, the lock is left for the stale rules
    to free rather than removed unguarded.

    Args:
        host_type: A validated client-reported host type.
        record: The owner record ``_acquire_lock`` returned.

    Returns:
        None.
    """

    _remove_lock(lock_path(host_type), record, time.monotonic() + LOCK_TIMEOUT_SECONDS)


def ensure_installation(host_type: str) -> dict:
    """Return one host's installation record, creating it exactly once.

    Two simultaneous first calls must produce exactly one installation ref: the
    loser of the race waits for the exclusive lock and then re-reads the winner's
    file.

    Args:
        host_type: A validated client-reported host type.

    Returns:
        The installation record for this host.
    """

    existing = read_installation(host_type)
    if existing is not None:
        return existing
    # Fail closed before creating a directory when no signer exists at all, so a
    # machine that cannot sign never leaves half-written instance state behind.
    resolve_signer()
    _ensure_directory(host_dir(host_type))
    handle = _acquire_lock(host_type)
    try:
        return _read_or_create_installation_locked(host_type)
    finally:
        _release_lock(host_type, handle)


def ensure_profile(host_type: str, profile_key_value: str,
                   display_name: Optional[str] = None,
                   confirmed_legacy_fingerprint: Optional[str] = None) -> dict:
    """Return one identity's profile, creating it exactly once when absent.

    When the server has confirmed that the verified identity already owns an
    existing instance under the installed ref, and that instance's fingerprint
    is the legacy file's key, the new profile inherits the legacy key so the
    Person keeps the exact server instance they already had.  Any other identity
    gets a fresh key pair of its own; the unconfirmed legacy key is never handed
    to whoever logs in first.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key for the verified
            (Person, logical Agent).
        display_name: An optional name used only when creating the profile.
        confirmed_legacy_fingerprint: The exact fingerprint the server reported
            for this identity's existing instance under this ref, or ``None``.

    Returns:
        The profile record for this identity.
    """

    key = validate_profile_key(profile_key_value)
    existing = read_profile(host_type, key)
    if existing is not None:
        return existing
    # Fail closed before writing anything when no signer is available.
    resolve_signer()
    _ensure_directory(host_dir(host_type))
    handle = _acquire_lock(host_type)
    try:
        existing = read_profile(host_type, key)
        if existing is not None:
            return existing
        return _create_profile_locked(host_type, key, display_name,
                                      confirmed_legacy_fingerprint)
    finally:
        _release_lock(host_type, handle)


def recover_profile(host_type: str, profile_key_value: str,
                    display_name: Optional[str] = None) -> dict:
    """Mint fresh local key material for one identity after a server refusal.

    The profile keeps its identity and readable name but receives a new key pair
    and loses its cached server instance id.  Recovery is profile-scoped: it
    never deletes another identity's profile, never rotates the shared
    installation ref, and never consumes or poisons the legacy single-key file.
    The legacy file stays available, so the identity the server confirms as its
    owner can still adopt it on a later login; the caller here is simply minting
    a new key for itself.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key.
        display_name: An optional replacement name.

    Returns:
        The recovered profile record.
    """

    key = validate_profile_key(profile_key_value)
    signer = resolve_signer()
    _ensure_directory(host_dir(host_type))
    handle = _acquire_lock(host_type)
    try:
        existing = read_profile(host_type, key)
        seed = generate_seed()
        name = display_name or (existing["display_name"] if existing else None)
        profile = _new_profile(host_type, key, signer, seed,
                               derive_public_key(signer, seed), name, "recovered")
        return write_profile(host_type, profile)
    finally:
        _release_lock(host_type, handle)


def record_instance(host_type: str, profile_key_value: str, instance_id: str) -> dict:
    """Record the server instance id a successful bind returned for one profile.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key.
        instance_id: The exact ``agi_...`` identifier the server returned.

    Returns:
        The updated profile record.
    """

    key = validate_profile_key(profile_key_value)
    text = str(instance_id or "").strip()
    if not INSTANCE_ID_PATTERN.match(text):
        raise InstanceError("instance_state_invalid", "the reported instance id is malformed",
                            EXIT_STATE_INVALID)
    profile = ensure_profile(host_type, key)
    profile["server_instance_id"] = text
    profile["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return write_profile(host_type, profile)


def forget_instance(host_type: str, profile_key_value: Optional[str] = None) -> bool:
    """Delete one identity's local profile, or the whole installation.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: When given, remove only that profile.  When omitted,
            remove the installation, every profile and the legacy file.  This is
            the explicit "remove this device" action; a plugin upgrade never
            calls it.

    Returns:
        True when at least one file was removed, False when nothing existed.
    """

    if profile_key_value is not None:
        key = validate_profile_key(profile_key_value)
        try:
            os.unlink(profile_path(host_type, key))
            removed = True
        except OSError:
            removed = False
        try:
            os.rmdir(profiles_dir(host_type))
        except OSError:
            pass
        return removed
    removed = False
    for path in (installation_path(host_type), instance_path(host_type)):
        try:
            os.unlink(path)
            removed = True
        except OSError:
            pass
    # A live lock belongs to a call that is still running.  Like every removal of
    # the lock, forget removes only a stale one, inside the removal guard.
    lock = lock_path(host_type)
    if _inspect_lock(lock) == "stale" and _remove_lock(
            lock, None, time.monotonic() + LOCK_TIMEOUT_SECONDS):
        removed = True
    directory = profiles_dir(host_type)
    if os.path.isdir(directory):
        for name in os.listdir(directory):
            try:
                os.unlink(os.path.join(directory, name))
                removed = True
            except OSError:
                pass
        try:
            os.rmdir(directory)
        except OSError:
            pass
    try:
        os.rmdir(host_dir(host_type))
    except OSError:
        pass
    return removed


# --- Commands --------------------------------------------------------------


def status_payload(installation: Mapping[str, Any], profile: Optional[Mapping[str, Any]] = None) -> dict:
    """Build the public status object for one installation or one profile.

    The private seed is never part of this payload, and a profile for another
    identity is never expanded into this answer.  Only opaque profile keys are
    listed so one Person is never shown another Person's identity.

    Args:
        installation: A validated installation record.
        profile: The validated profile to report, or ``None`` for the typed
            unbound installation view.

    Returns:
        The JSON-serializable status object.
    """

    host_type = installation["host_type"]
    payload = {
        "format_version": FORMAT_VERSION,
        "host_type": host_type,
        "installation_ref": installation["installation_ref"],
        "data_dir": data_root(),
        "instance_path": installation_path(host_type),
        "legacy_status": installation["legacy_status"],
        "legacy_path": instance_path(host_type),
        "profiles": sorted(item["profile_key"] for item in iter_profiles(host_type)),
    }
    if profile is None:
        # A pure installation view never invents a profile.  It reports the typed
        # state and the exact server-verified key the caller must pass back in.
        payload.update({
            "profile_status": "profile_required",
            "signer": resolve_signer(),
            "recovery": ("call agent_instance.current with local_instance_ref, then pass its "
                         "profile_key and existing_public_key_fingerprint to "
                         "status --profile <profile_key> --confirmed-fingerprint <fingerprint>"),
        })
        return payload
    payload.update({
        "profile_status": "ready",
        "profile_key": profile["profile_key"],
        "os_type": profile["os_type"],
        "public_key": profile["public_key"],
        "public_key_fingerprint": public_key_fingerprint(profile["public_key"]),
        "display_name": profile["display_name"],
        "signer": profile["signer"],
        "server_instance_id": profile["server_instance_id"],
        "source": profile["source"],
        "created_at": profile["created_at"],
    })
    return payload


def command_status(host_type: str, profile_key_value: Optional[str] = None,
                   display_name: Optional[str] = None,
                   confirmed_legacy_fingerprint: Optional[str] = None) -> dict:
    """Report the installation, and initialize one identity's profile when named.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key, or ``None`` for the
            typed unbound installation view.
        display_name: An optional name used only on first creation.
        confirmed_legacy_fingerprint: The exact fingerprint the server reported
            for this verified identity's existing instance under this ref, used
            only to adopt a legacy key that provably belongs to this identity.

    Returns:
        The status object.
    """

    installation = ensure_installation(host_type)
    if profile_key_value is None:
        return status_payload(installation)
    profile = ensure_profile(host_type, profile_key_value, display_name,
                             confirmed_legacy_fingerprint)
    return status_payload(installation, profile)


def command_sign(host_type: str, profile_key_value: str, challenge_file: str) -> dict:
    """Sign the exact challenge bytes with one identity's profile key.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key.
        challenge_file: Path to a file holding the exact challenge bytes.

    Returns:
        ``{"signature": ..., "public_key": ..., "profile_key": ...}``.

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
    profile = ensure_profile(host_type, profile_key_value)
    seed = base64url_decode(profile["private_key_seed"])
    signature = sign_message(resolve_signer(profile["signer"]), seed, message)
    return {
        "signature": signature,
        "public_key": profile["public_key"],
        "profile_key": profile["profile_key"],
    }


def command_record(host_type: str, profile_key_value: str, instance_id: str) -> dict:
    """Persist the server instance id a successful bind returned.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key.
        instance_id: The exact ``agi_...`` identifier the server returned.

    Returns:
        The updated status object.
    """

    profile = record_instance(host_type, profile_key_value, instance_id)
    return status_payload(read_installation(host_type), profile)


def command_recover(host_type: str, profile_key_value: str,
                    display_name: Optional[str] = None) -> dict:
    """Mint fresh local key material for one identity after a server refusal.

    This is the explicit recovery path for ``instance_key_mismatch``,
    ``instance_key_already_bound`` and ``instance_revoked``.  The old server row
    is retained, never reassigned, and the local profile keeps its identity.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: The server's opaque profile key.
        display_name: An optional replacement name.

    Returns:
        The recovered status object.
    """

    profile = recover_profile(host_type, profile_key_value, display_name)
    return status_payload(read_installation(host_type), profile)


def command_forget(host_type: str, profile_key_value: Optional[str] = None) -> dict:
    """Delete one identity's profile, or the whole installation, and report it.

    Args:
        host_type: A validated client-reported host type.
        profile_key_value: When given, remove only that profile.  When omitted,
            remove the installation, every profile and the legacy file.

    Returns:
        ``{"forgotten": <bool>, ...}``.
    """

    removed = forget_instance(host_type, profile_key_value)
    return {
        "forgotten": removed,
        "scope": "profile" if profile_key_value else "installation",
        "host_type": host_type,
        "data_dir": data_root(),
        "instance_path": (profile_path(host_type, profile_key_value)
                          if profile_key_value else installation_path(host_type)),
    }


# --- CLI -------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    """Build the command-line parser.

    Returns:
        The configured argument parser.
    """

    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status", help="report one identity's local profile, creating it when absent")
    status.add_argument("--host", required=True)
    status.add_argument("--profile", default=None)
    status.add_argument("--display-name", default=None)
    status.add_argument(
        "--confirmed-fingerprint", default=None,
        help="fingerprint the server reported for this identity's existing instance")

    sign = sub.add_parser("sign", help="sign the exact challenge bytes read from a file")
    sign.add_argument("--host", required=True)
    sign.add_argument("--profile", default=None)
    sign.add_argument("--challenge-file", required=True)

    record = sub.add_parser("record", help="remember the server instance id a bind returned")
    record.add_argument("--host", required=True)
    record.add_argument("--profile", default=None)
    record.add_argument("--instance-id", required=True)

    recover = sub.add_parser("recover", help="mint fresh local key material for one identity")
    recover.add_argument("--host", required=True)
    recover.add_argument("--profile", default=None)
    recover.add_argument("--display-name", default=None)

    forget = sub.add_parser(
        "forget", help="delete one identity's profile, or the whole installation")
    forget.add_argument("--host", required=True)
    forget.add_argument("--profile", default=None)
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
        host_type = validate_host_type(args.host)
        if args.command == "status":
            _json_out(command_status(host_type, args.profile, args.display_name,
                                     args.confirmed_fingerprint))
        elif args.command == "sign":
            _json_out(command_sign(host_type, args.profile, args.challenge_file))
        elif args.command == "record":
            _json_out(command_record(host_type, args.profile, args.instance_id))
        elif args.command == "recover":
            _json_out(command_recover(host_type, args.profile, args.display_name))
        elif args.command == "forget":
            _json_out(command_forget(host_type, args.profile))
        return EXIT_OK
    except InstanceError as exc:
        _json_out({"ok": False, "error_code": exc.code, "message": exc.message})
        return exc.exit_code
    except (OSError, ValueError) as exc:
        _json_out({"ok": False, "error_code": "instance_local_failure", "message": str(exc)})
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())
