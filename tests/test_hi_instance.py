"""Behavioural tests for the shared ``hi-instance`` local instance helper.

Every test drives the helper as a real process, because the properties that
matter here are process-level ones: two simultaneous first calls must produce
exactly one key pair, one installation switching Person A -> B -> A must keep two
separate keys under one shared installation ref, a plugin upgrade must not change
the profile, a legacy single-key file must be migrated without being rewritten,
and the private seed must never leave the machine.  The lock and creation-safety
tests also call the helper in process, to stage the exact race states (an
ownerless lock, a lock name pending deletion, a late racer, another caller
breaking or taking the lock at the moment one call removes it) that real
processes hit only by timing.

The helper itself imports no third-party module, so this file imports nothing
third-party either; the Ed25519 checks are skipped when the environment cannot
provide a signer or the cross-repository verifier is not checked out.
"""

import base64
import contextlib
import errno
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock
import importlib.util


REPO_ROOT = Path(__file__).resolve().parents[2]
TESTS_DIR = Path(__file__).resolve().parent
HELPER = TESTS_DIR.parent / "plugins/hirey-hi/skills/hi-instance/scripts/hi_instance.py"

# The verifier that owns the frozen challenge shape lives in the Core repository.
# It is a sibling checkout, not a package on this repository's path.
CORE_CHECKOUT = Path(
    os.environ.get("HIREY_CORE_CHECKOUT")
    or "/Users/dujuan/hirey/.worktrees/agent-instances-core-20260919"
)

EXIT_SIGNER_UNAVAILABLE = 3
EXIT_PROFILE_REQUIRED = 7

# Two distinct opaque profile keys, as the server would issue for two verified
# (Person, logical Agent) pairs on one installation.
PROFILE_A = "a" * 64
PROFILE_B = "b" * 64
PROFILE_C = "c" * 64
LEGACY_REF = "0123456789abcdef0123456789abcdef"


# A separate process that takes the removal guard of one lock and keeps it until
# it is killed, as a remover killed inside the guard would.
GUARD_HOLDER_SCRIPT = r"""
import importlib.util, sys, time
spec = importlib.util.spec_from_file_location("hi_instance_guard_holder", sys.argv[1])
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
handle = helper._take_guard(sys.argv[2], time.monotonic() + 10)
print("held" if handle is not None else "missed", flush=True)
time.sleep(300)
"""

def load_helper():
    """Import the helper module in-process for direct function tests.

    Returns:
        The imported ``hi_instance`` module.
    """

    spec = importlib.util.spec_from_file_location("hi_instance_under_test", HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_core_proof():
    """Import the cross-repository challenge verifier, or return ``None``.

    Returns:
        The ``hirey_core.agent_instance_proof`` module when the Core checkout and
        its ``cryptography`` dependency are both present, otherwise ``None``.
    """

    if not (CORE_CHECKOUT / "hirey_core/agent_instance_proof.py").exists():
        return None
    if str(CORE_CHECKOUT) not in sys.path:
        sys.path.insert(0, str(CORE_CHECKOUT))
    try:
        from hirey_core import agent_instance_proof
    except BaseException as exc:  # noqa: BLE001 - a broken extension can panic
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        return None
    return agent_instance_proof


class HiInstanceTests(unittest.TestCase):
    """Process-level behaviour of one host's local instance material."""

    def setUp(self):
        """Create an isolated data root so no test touches the real user data."""

        self.temp = tempfile.TemporaryDirectory()
        self.data_root = Path(self.temp.name) / "data"

    def test_any_valid_client_declared_type_can_be_used(self):
        helper = load_helper()
        self.assertEqual(helper.validate_host_type(" Example.Client-1 "), "example.client-1")
        self.assertEqual(helper.validate_host_type("future_host"), "future_host")
        for invalid in ("", "../escape", "host/other", "host space", "x" * 65):
            with self.subTest(invalid=invalid), self.assertRaises(helper.InstanceError):
                helper.validate_host_type(invalid)

    def tearDown(self):
        """Remove the isolated data root."""

        self.temp.cleanup()

    def run_helper(self, *args, cwd=None, data_root=None, env=None):
        """Run the helper as a real process and return the completed process.

        Args:
            *args: Command and arguments passed to the helper.
            cwd: Optional working directory.
            data_root: Optional ``HIREY_INSTANCE_DATA_DIR`` override.
            env: Optional extra environment entries.

        Returns:
            The ``subprocess.CompletedProcess`` with text output.
        """

        environment = dict(os.environ)
        environment["HIREY_INSTANCE_DATA_DIR"] = str(data_root or self.data_root)
        environment.pop("PYTHONPATH", None)
        environment.update(env or {})
        return subprocess.run(
            [sys.executable, str(HELPER), *args],
            capture_output=True,
            text=True,
            cwd=str(cwd or REPO_ROOT),
            env=environment,
            timeout=120,
        )

    def status(self, host="codex", profile=PROFILE_A, *extra, **kwargs):
        """Run ``status`` for one profile and return its parsed JSON object.

        Args:
            host: The host type.
            profile: The opaque profile key, or ``None`` for the bare view.
            *extra: Extra arguments appended to the command.

        Returns:
            The parsed status object.
        """

        arguments = ["status", "--host", host]
        if profile is not None:
            arguments += ["--profile", profile]
        completed = self.run_helper(*arguments, *extra, **kwargs)
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        return json.loads(completed.stdout)

    def installation_file(self, host="codex"):
        """Return the path of one host's installation record inside the data root."""

        return self.data_root / host / "installation.json"

    def profile_file(self, profile=PROFILE_A, host="codex"):
        """Return the path of one identity's profile file inside the data root."""

        return self.data_root / host / "profiles" / ("%s.json" % profile)

    def legacy_file(self, host="codex"):
        """Return the path of one host's legacy single-key instance file."""

        return self.data_root / host / "instance.json"

    def write_legacy(self, host="codex", signer=None):
        """Write one valid legacy v1 instance file and return its record.

        Args:
            host: The host type.
            signer: Optional signer override.

        Returns:
            The legacy record that was written.
        """

        helper = load_helper()
        signer = signer or helper.resolve_signer()
        seed = helper.generate_seed()
        record = {
            "format_version": 1,
            "host_type": host,
            "os_type": "mac",
            "local_instance_ref": LEGACY_REF,
            "public_key": helper.derive_public_key(signer, seed),
            "private_key_seed": helper.base64url_encode(seed),
            "display_name": "Old Mac \u00b7 Codex",
            "signer": signer,
            "created_at": "2025-01-01T00:00:00Z",
        }
        path = self.legacy_file(host)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return record

    # -- data directory contract ------------------------------------------

    def test_state_lives_per_host_outside_the_plugin(self):
        """Installation and profile files live under the per-host data root."""

        status = self.status("codex")
        self.assertEqual(status["data_dir"], str(self.data_root))
        self.assertEqual(Path(status["instance_path"]), self.installation_file("codex"))
        self.assertTrue(self.installation_file("codex").is_file())
        self.assertTrue(self.profile_file(PROFILE_A, "codex").is_file())
        self.assertEqual(status["format_version"], 2)
        self.assertEqual(status["host_type"], "codex")
        self.assertIn(status["os_type"], ("mac", "windows", "linux", "ios", "android", "unknown"))
        self.assertEqual(len(status["installation_ref"]), 32)
        self.assertEqual(status["profile_key"], PROFILE_A)
        self.assertEqual(len(status["public_key_fingerprint"]), 64)
        # The plugin's own package directory never receives instance material.
        for plugin_root in (REPO_ROOT / "host-plugins/plugins/hirey-hi", REPO_ROOT / "agent-packages"):
            self.assertEqual(list(plugin_root.rglob("instance.json")), [])
            self.assertEqual(list(plugin_root.rglob("installation.json")), [])
            self.assertEqual(list(plugin_root.rglob("profiles")), [])

    def test_bare_status_reports_profile_required_without_key_material(self):
        """A status with no verified profile creates no key and reports the typed state."""

        status = self.status("codex", profile=None)
        self.assertEqual(status["profile_status"], "profile_required")
        self.assertEqual(len(status["installation_ref"]), 32)
        self.assertNotIn("public_key", status)
        self.assertNotIn("private_key_seed", status)
        self.assertFalse(self.profile_file(PROFILE_A, "codex").exists())
        self.assertFalse((self.data_root / "codex" / "profiles").exists())
        # A sign without the server-issued profile key fails typed.
        challenge = Path(self.temp.name) / "challenge.bin"
        challenge.write_bytes(b"hirey-agent-instance-binding-v1\nneeds-a-profile")
        missing = self.run_helper("sign", "--host", "codex", "--challenge-file", str(challenge))
        self.assertEqual(missing.returncode, EXIT_PROFILE_REQUIRED,
                         missing.stderr or missing.stdout)
        self.assertEqual(json.loads(missing.stdout)["error_code"], "instance_profile_required")

    def test_status_never_prints_the_private_key(self):
        """No command output may contain the stored private seed."""

        status = self.status("codex")
        stored = json.loads(self.profile_file(PROFILE_A, "codex").read_text(encoding="utf-8"))
        seed = stored["private_key_seed"]
        self.assertTrue(seed)
        self.assertNotIn(seed, json.dumps(status))
        self.assertNotIn("private_key_seed", json.dumps(status))
        self.assertNotIn("private", json.dumps(status).lower())
        # The public half is reported, and it is not the private half.
        self.assertEqual(stored["public_key"], status["public_key"])
        self.assertNotEqual(status["public_key"], seed)
        # The installation record carries no key material at all.
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertNotIn("public_key", installation)
        self.assertNotIn("private_key_seed", installation)
        self.assertNotIn("public_key", self.status("codex", profile=None))

    def test_two_hosts_on_one_machine_are_two_installations(self):
        """A different ``--host`` produces a different installation and key pair."""

        codex = self.status("codex")
        claude = self.status("claude")
        self.assertNotEqual(codex["installation_ref"], claude["installation_ref"])
        self.assertNotEqual(codex["public_key"], claude["public_key"])
        self.assertTrue(self.installation_file("codex").is_file())
        self.assertTrue(self.installation_file("claude").is_file())
        self.assertIn("Codex", codex["display_name"])
        self.assertIn("Claude", claude["display_name"])

    # -- one installation, many identities ---------------------------------

    def test_switching_person_a_to_b_to_a_keeps_two_keys_and_restores_a(self):
        """One installation, two identities: shared ref, distinct keys/bindings, A restored."""

        first_a = self.status("codex", PROFILE_A)
        recorded_a = self.run_helper("record", "--host", "codex", "--profile", PROFILE_A,
                                     "--instance-id", "agi_aaaaaaaaaaaa")
        self.assertEqual(recorded_a.returncode, 0, recorded_a.stderr or recorded_a.stdout)
        first_b = self.status("codex", PROFILE_B)
        recorded_b = self.run_helper("record", "--host", "codex", "--profile", PROFILE_B,
                                     "--instance-id", "agi_bbbbbbbbbbbb")
        self.assertEqual(recorded_b.returncode, 0, recorded_b.stderr or recorded_b.stdout)
        self.assertEqual(first_a["installation_ref"], first_b["installation_ref"],
                         "the installation ref is shared across Persons for audit")
        self.assertNotEqual(first_a["public_key"], first_b["public_key"],
                            "each verified Person mints a separate key")
        self.assertTrue(self.profile_file(PROFILE_A, "codex").is_file())
        self.assertTrue(self.profile_file(PROFILE_B, "codex").is_file())
        # Only opaque profile keys are listed, never another identity's material.
        self.assertEqual(sorted(first_b["profiles"]), sorted([PROFILE_A, PROFILE_B]))

        # Returning to A restores A's exact key and A's own bound instance.
        second_a = self.status("codex", PROFILE_A)
        self.assertEqual(second_a["public_key"], first_a["public_key"])
        self.assertEqual(second_a["public_key_fingerprint"], first_a["public_key_fingerprint"])
        self.assertEqual(second_a["installation_ref"], first_a["installation_ref"])
        self.assertEqual(second_a["server_instance_id"], "agi_aaaaaaaaaaaa",
                         "switching away and back never rotates A's bound instance")
        # B is unchanged by A's return: B keeps its own key and its own instance.
        second_b = self.status("codex", PROFILE_B)
        self.assertEqual(second_b["public_key"], first_b["public_key"])
        self.assertEqual(second_b["server_instance_id"], "agi_bbbbbbbbbbbb",
                         "A's return never erases or rotates B's binding")

    def test_forget_one_profile_leaves_the_others_and_the_installation(self):
        """Deleting one identity's profile never deletes another identity or the ref."""

        a = self.status("codex", PROFILE_A)
        self.status("codex", PROFILE_B)
        forgotten = self.run_helper("forget", "--host", "codex", "--profile", PROFILE_A)
        self.assertEqual(forgotten.returncode, 0, forgotten.stderr or forgotten.stdout)
        payload = json.loads(forgotten.stdout)
        self.assertTrue(payload["forgotten"])
        self.assertEqual(payload["scope"], "profile")
        self.assertFalse(self.profile_file(PROFILE_A, "codex").exists())
        self.assertTrue(self.profile_file(PROFILE_B, "codex").is_file())
        self.assertTrue(self.installation_file("codex").is_file())
        # The installation ref is preserved: forgetting a Person is not forgetting the Mac.
        self.assertEqual(self.status("codex", PROFILE_B)["installation_ref"],
                         a["installation_ref"])

    # -- durability across a plugin upgrade --------------------------------

    def test_a_plugin_upgrade_reuses_the_same_profile(self):
        """Renaming the version directory and changing cwd keep the same profile."""

        first = self.status("codex")
        recorded = self.run_helper("record", "--host", "codex", "--profile", PROFILE_A,
                                   "--instance-id", "agi_upgrade000001")
        self.assertEqual(recorded.returncode, 0, recorded.stderr or recorded.stdout)
        upgrade_root = Path(self.temp.name) / "plugin-v2"
        (upgrade_root / "scripts").mkdir(parents=True)
        shutil.copy2(HELPER, upgrade_root / "scripts/hi_instance.py")
        completed = subprocess.run(
            [sys.executable, str(upgrade_root / "scripts/hi_instance.py"), "status",
             "--host", "codex", "--profile", PROFILE_A],
            capture_output=True,
            text=True,
            cwd=str(upgrade_root),
            env={**os.environ, "HIREY_INSTANCE_DATA_DIR": str(self.data_root)},
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr or completed.stdout)
        after = json.loads(completed.stdout)
        self.assertEqual(after["installation_ref"], first["installation_ref"])
        self.assertEqual(after["public_key"], first["public_key"])
        self.assertEqual(after["display_name"], first["display_name"])
        # The upgrade keeps the existing server instance id too: the Person and
        # logical Agent retain the instance they already bound, so an upgrade
        # never looks like a fresh, unbound installation.
        self.assertEqual(after["server_instance_id"], "agi_upgrade000001")
        # The upgrade did not scatter instance files into the new version directory.
        self.assertEqual(list(upgrade_root.rglob("instance.json")), [])
        self.assertEqual(list(upgrade_root.rglob("installation.json")), [])

    # -- legacy migration ---------------------------------------------------

    def test_legacy_single_key_file_is_migrated_without_being_rewritten(self):
        """The confirmed owner inherits the legacy key; the legacy file stays byte-identical."""

        legacy = self.write_legacy()
        before = self.legacy_file().read_bytes()
        legacy_fingerprint = load_helper().public_key_fingerprint(legacy["public_key"])

        migrated = self.status("codex", PROFILE_A, "--confirmed-fingerprint", legacy_fingerprint)
        self.assertEqual(migrated["installation_ref"], legacy["local_instance_ref"],
                         "the legacy ref becomes the shared installation ref")
        self.assertEqual(migrated["public_key"], legacy["public_key"],
                         "the existing key survives the upgrade")
        self.assertEqual(migrated["display_name"], legacy["display_name"])
        self.assertEqual(migrated["source"], "legacy_migrated")
        self.assertEqual(self.legacy_file().read_bytes(), before,
                         "a legacy file is read, never rewritten")

        # A second Person on the same installation gets a fresh key: the legacy key
        # already belongs to A, and B has no server confirmation for it.
        second = self.status("codex", PROFILE_B)
        self.assertNotEqual(second["public_key"], legacy["public_key"])
        self.assertEqual(second["source"], "created")
        self.assertEqual(second["installation_ref"], legacy["local_instance_ref"])
        self.assertEqual(self.legacy_file().read_bytes(), before)

    def test_legacy_key_is_never_claimed_without_server_confirmation(self):
        """B logs in first after an upgrade and must not consume A's legacy key.

        The local helper cannot know who owns the legacy key, so it must never
        hand that key to the first profile.  Only the fingerprint the server
        confirms for this verified identity enables adoption, and the unclaimed
        legacy file remains available for A on a later login.
        """

        legacy = self.write_legacy()
        before = self.legacy_file().read_bytes()
        legacy_fingerprint = load_helper().public_key_fingerprint(legacy["public_key"])

        # B logs in first: no confirmation for B's (Person, Agent), so B mints a
        # fresh key and the legacy file is left unclaimed.
        first_b = self.status("codex", PROFILE_B)
        self.assertNotEqual(first_b["public_key"], legacy["public_key"])
        self.assertEqual(first_b["source"], "created")
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["legacy_status"], "unclaimed",
                         "B's first login must not consume the legacy migration slot")
        self.assertEqual(first_b["installation_ref"], legacy["local_instance_ref"])
        self.assertEqual(self.legacy_file().read_bytes(), before)

        # A returns and the server confirms the legacy fingerprint for A's
        # identity: A still adopts the original key.
        first_a = self.status("codex", PROFILE_A, "--confirmed-fingerprint", legacy_fingerprint)
        self.assertEqual(first_a["public_key"], legacy["public_key"])
        self.assertEqual(first_a["source"], "legacy_migrated")
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["legacy_status"], "claimed")
        # B's profile is untouched by A's return.
        second_b = self.status("codex", PROFILE_B)
        self.assertEqual(second_b["public_key"], first_b["public_key"])
        self.assertEqual(self.legacy_file().read_bytes(), before)

    def test_legacy_adoption_ignores_a_fingerprint_that_is_not_the_legacy_key(self):
        """A confirmation for some other instance never releases the legacy key."""

        legacy = self.write_legacy()
        before = self.legacy_file().read_bytes()
        # The server reports an existing instance with a different key, so the
        # helper must not substitute the unconfirmed legacy key.
        other_fingerprint = load_helper().public_key_fingerprint("A" * 43)
        created = self.status("codex", PROFILE_A,
                              "--confirmed-fingerprint", other_fingerprint)
        self.assertNotEqual(created["public_key"], legacy["public_key"])
        self.assertEqual(created["source"], "created")
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["legacy_status"], "unclaimed")
        self.assertEqual(self.legacy_file().read_bytes(), before)

    def test_forget_profile_does_not_rotate_the_installation_ref(self):
        """Forgetting one Person keeps the shared installation reference stable."""

        legacy = self.write_legacy()
        self.status("codex", PROFILE_A)
        self.run_helper("forget", "--host", "codex", "--profile", PROFILE_A)
        # The legacy file is still present and still owns the ref, so B can bind
        # its own key but the installation audit ref does not rotate.
        second = self.status("codex", PROFILE_B)
        self.assertEqual(second["installation_ref"], legacy["local_instance_ref"])

    # -- recovery -----------------------------------------------------------

    def test_recover_is_profile_scoped_and_never_poisons_the_legacy_key(self):
        """Recovery mints fresh material for one identity and retains the legacy file."""

        legacy = self.write_legacy()
        before = self.legacy_file().read_bytes()
        legacy_fingerprint = load_helper().public_key_fingerprint(legacy["public_key"])
        adopted = self.status("codex", PROFILE_A, "--confirmed-fingerprint", legacy_fingerprint)
        self.assertEqual(adopted["public_key"], legacy["public_key"])

        recovered = self.run_helper("recover", "--host", "codex", "--profile", PROFILE_A)
        self.assertEqual(recovered.returncode, 0, recovered.stderr or recovered.stdout)
        payload = json.loads(recovered.stdout)
        self.assertEqual(payload["source"], "recovered")
        self.assertNotEqual(payload["public_key"], legacy["public_key"])
        self.assertIsNone(payload["server_instance_id"])
        self.assertEqual(payload["installation_ref"], legacy["local_instance_ref"],
                         "profile recovery never rotates the shared installation ref")
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["legacy_status"], "claimed",
                         "recovery does not rewrite the confirmed legacy status")
        self.assertEqual(self.legacy_file().read_bytes(), before,
                         "recovery never rewrites the legacy file")

        # Another identity is untouched and still gets its own fresh key.
        other = self.status("codex", PROFILE_B)
        self.assertNotEqual(other["public_key"], legacy["public_key"])
        self.assertEqual(other["source"], "created")
        self.assertEqual(self.legacy_file().read_bytes(), before)

    def test_recover_after_an_unconfirmed_login_leaves_legacy_adoptable(self):
        """A rejection on one identity never consumes the legacy key for its owner."""

        legacy = self.write_legacy()
        before = self.legacy_file().read_bytes()
        legacy_fingerprint = load_helper().public_key_fingerprint(legacy["public_key"])

        # B logs in first and never gets the legacy key; recovering B must not
        # mark the legacy key refused, so A can still adopt it later.
        b = self.status("codex", PROFILE_B)
        self.assertNotEqual(b["public_key"], legacy["public_key"])
        self.run_helper("recover", "--host", "codex", "--profile", PROFILE_B)
        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["legacy_status"], "unclaimed",
                         "B's recovery must not poison the legacy key for its owner")
        a = self.status("codex", PROFILE_A, "--confirmed-fingerprint", legacy_fingerprint)
        self.assertEqual(a["public_key"], legacy["public_key"])
        self.assertEqual(a["source"], "legacy_migrated")
        self.assertEqual(self.legacy_file().read_bytes(), before)

    def test_record_persists_the_server_instance_id(self):
        """A successful bind's server id is remembered and reported back."""

        self.status("codex", PROFILE_A)
        recorded = self.run_helper("record", "--host", "codex", "--profile", PROFILE_A,
                                   "--instance-id", "agi_abcdefghijklmnop")
        self.assertEqual(recorded.returncode, 0, recorded.stderr or recorded.stdout)
        self.assertEqual(json.loads(recorded.stdout)["server_instance_id"],
                         "agi_abcdefghijklmnop")
        self.assertEqual(self.status("codex", PROFILE_A)["server_instance_id"],
                         "agi_abcdefghijklmnop")
        # A malformed server id is refused rather than stored.
        bad = self.run_helper("record", "--host", "codex", "--profile", PROFILE_A,
                              "--instance-id", "not-an-instance")
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout)["error_code"], "instance_state_invalid")

    # -- concurrency --------------------------------------------------------

    def test_simultaneous_first_calls_create_exactly_one_instance(self):
        """Six concurrent first calls converge on one installation and one profile key."""

        environment = dict(os.environ)
        environment["HIREY_INSTANCE_DATA_DIR"] = str(self.data_root)
        processes = [
            subprocess.Popen(
                [sys.executable, str(HELPER), "status", "--host", "codex",
                 "--profile", PROFILE_A],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(REPO_ROOT),
                env=environment,
            )
            for _ in range(6)
        ]
        results = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=120)
            self.assertEqual(process.returncode, 0, stderr or stdout)
            results.append(json.loads(stdout))

        references = {item["installation_ref"] for item in results}
        keys = {item["public_key"] for item in results}
        self.assertEqual(len(references), 1, "concurrent first calls created more than one installation")
        self.assertEqual(len(keys), 1, "concurrent first calls created more than one key pair")

        installation = json.loads(self.installation_file("codex").read_text(encoding="utf-8"))
        self.assertEqual(installation["installation_ref"], references.pop())
        profile = json.loads(self.profile_file(PROFILE_A, "codex").read_text(encoding="utf-8"))
        self.assertEqual(profile["public_key"], keys.pop())
        # Exactly one installation and one profile, and no leftover lock or temp file.
        self.assertEqual(
            sorted(path.name for path in (self.data_root / "codex").iterdir()),
            ["installation.json", "profiles"],
        )
        self.assertEqual(
            sorted(path.name for path in (self.data_root / "codex" / "profiles").iterdir()),
            ["%s.json" % PROFILE_A],
        )

    # -- lock and creation safety --------------------------------------------

    def in_process_helper(self):
        """Load the helper with this test's data root and a short lock deadline."""

        patcher = mock.patch.dict(os.environ, {"HIREY_INSTANCE_DATA_DIR": str(self.data_root)})
        patcher.start()
        self.addCleanup(patcher.stop)
        helper = load_helper()
        helper.LOCK_TIMEOUT_SECONDS = 0.5
        (self.data_root / "codex").mkdir(parents=True, exist_ok=True)
        return helper

    def lock_file(self, host="codex"):
        """Return the path of one host's initialization lock inside the data root."""

        return self.data_root / host / "instance.lock"

    def test_a_fresh_lock_without_a_complete_owner_is_live_not_broken(self):
        """A lock whose owner record is empty or partial is waited for, never broken."""

        helper = self.in_process_helper()
        for content in (b"", b"12", b"not a pid\n", b"0\n"):
            with self.subTest(content=content):
                self.lock_file().write_bytes(content)
                with self.assertRaises(helper.InstanceError) as raised:
                    helper.ensure_installation("codex")
                self.assertEqual(raised.exception.code, "instance_lock_timeout")
                self.assertEqual(self.lock_file().read_bytes(), content)
                self.assertFalse(self.installation_file().exists())

    def test_an_ownerless_lock_past_the_stale_window_is_broken(self):
        """The stale window still frees a lock whose owner died before writing."""

        helper = self.in_process_helper()
        self.lock_file().write_bytes(b"")
        old = time.time() - 2 * helper.LOCK_STALE_SECONDS
        os.utime(self.lock_file(), (old, old))
        installation = helper.ensure_installation("codex")
        self.assertEqual(len(installation["installation_ref"]), 32)
        self.assertFalse(self.lock_file().exists())

    @unittest.skipIf(os.name == "nt", "Windows has no harmless process liveness probe")
    def test_a_lock_whose_owner_process_exited_is_broken(self):
        """A complete owner record naming a dead process is broken at once."""

        helper = self.in_process_helper()
        finished = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"],
                                  capture_output=True, text=True, check=True)
        self.lock_file().write_bytes(("%d\n%s\n" % (int(finished.stdout), "e" * 32)).encode("ascii"))
        installation = helper.ensure_installation("codex")
        self.assertEqual(len(installation["installation_ref"]), 32)
        self.assertFalse(self.lock_file().exists())

    def test_a_lock_name_held_by_a_pending_delete_is_retried(self):
        """Windows' access-denied on a lock being deleted is a busy lock, not a failure."""

        helper = self.in_process_helper()
        real_place = helper._place_new_file
        denials = []

        def deny_twice(temporary, path):
            if path.endswith("instance.lock") and len(denials) < 2:
                denials.append(path)
                os.unlink(temporary)
                raise PermissionError(13, "Permission denied", path)
            return real_place(temporary, path)

        with mock.patch.object(helper, "LOCK_NAME_HELD_ON_PERMISSION_ERROR", True), \
                mock.patch.object(helper, "_place_new_file", deny_twice):
            installation = helper.ensure_installation("codex")
        self.assertEqual(len(denials), 2)
        self.assertEqual(len(installation["installation_ref"]), 32)
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                         ["installation.json"])

        def deny_always(temporary, path):
            os.unlink(temporary)
            raise PermissionError(13, "Permission denied", path)

        with mock.patch.object(helper, "LOCK_NAME_HELD_ON_PERMISSION_ERROR", True), \
                mock.patch.object(helper, "_place_new_file", deny_always), \
                self.assertRaises(helper.InstanceError) as raised:
            helper.ensure_profile("codex", PROFILE_A)
        self.assertEqual(raised.exception.code, "instance_lock_timeout")
        # Elsewhere an access-denied lock directory is a real failure, reported at once.
        with mock.patch.object(helper, "LOCK_NAME_HELD_ON_PERMISSION_ERROR", False), \
                mock.patch.object(helper, "_place_new_file", deny_always), \
                self.assertRaises(PermissionError):
            helper.ensure_profile("codex", PROFILE_A)
        self.assertFalse(self.profile_file(PROFILE_A).exists())

    def test_creation_never_replaces_an_existing_key_or_installation(self):
        """A racer that reached creation late adopts the first record, never overwrites it."""

        helper = self.in_process_helper()
        first = helper._create_profile_locked("codex", PROFILE_A, None)
        installation_bytes = self.installation_file().read_bytes()
        profile_bytes = self.profile_file(PROFILE_A).read_bytes()

        second = helper._create_profile_locked("codex", PROFILE_A, None)
        self.assertEqual(second["public_key"], first["public_key"])
        self.assertEqual(self.profile_file(PROFILE_A).read_bytes(), profile_bytes)

        real_read = helper.read_installation
        reads = []

        def missed_first_read(host_type):
            reads.append(host_type)
            return None if len(reads) == 1 else real_read(host_type)

        with mock.patch.object(helper, "read_installation", missed_first_read):
            again = helper._read_or_create_installation_locked("codex")
        self.assertEqual(again["installation_ref"], json.loads(installation_bytes)["installation_ref"])
        self.assertEqual(self.installation_file().read_bytes(), installation_bytes)
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                         ["installation.json", "profiles"])
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex" / "profiles").iterdir()),
                         ["%s.json" % PROFILE_A])

    @unittest.skipIf(os.name == "nt", "Windows creates names exclusively by rename")
    def test_a_file_system_without_hard_links_still_creates_one_instance(self):
        """Without hard links, lock and records are created by O_EXCL and never replaced."""

        helper = self.in_process_helper()
        unsupported = OSError(errno.EPERM, "Operation not permitted")
        with mock.patch.object(os, "link", side_effect=unsupported):
            profile = helper.ensure_profile("codex", PROFILE_A)
            again = helper.ensure_profile("codex", PROFILE_A)
            profile_bytes = self.profile_file(PROFILE_A).read_bytes()
            installation_bytes = self.installation_file().read_bytes()
            # A late racer reaching creation must adopt, never replace.
            late = helper._create_profile_locked("codex", PROFILE_A, None)
            # Even a racer whose existence check ran before the first write landed.
            with mock.patch.object(os.path, "exists", return_value=False):
                self.assertFalse(helper._publish_new_json(str(self.installation_file()),
                                                          {"installation_ref": "f" * 32}))
        self.assertEqual(again["public_key"], profile["public_key"])
        self.assertEqual(late["public_key"], profile["public_key"])
        self.assertEqual(self.profile_file(PROFILE_A).read_bytes(), profile_bytes)
        self.assertEqual(self.installation_file().read_bytes(), installation_bytes)
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                         ["installation.json", "profiles"])
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex" / "profiles").iterdir()),
                         ["%s.json" % PROFILE_A])

    def test_releasing_never_removes_another_holders_lock(self):
        """A holder whose lock was broken and re-taken leaves the new holder's lock alone."""

        helper = self.in_process_helper()
        mine = helper._acquire_lock("codex")
        theirs = ("%d\n%s\n" % (os.getpid(), "f" * 32)).encode("ascii")
        os.unlink(self.lock_file())
        self.lock_file().write_bytes(theirs)
        helper._release_lock("codex", mine)
        self.assertEqual(self.lock_file().read_bytes(), theirs)

        os.unlink(self.lock_file())
        mine = helper._acquire_lock("codex")
        helper._release_lock("codex", mine)
        self.assertFalse(self.lock_file().exists())

    def make_stale_lock(self, helper, content=b""):
        """Leave a lock as a crashed holder would: past the stale window."""

        self.lock_file().write_bytes(content)
        old = time.time() - 2 * helper.LOCK_STALE_SECONDS
        os.utime(self.lock_file(), (old, old))

    def link_cases(self):
        """Run a race with hard links and, where the platform has them, without."""

        cases = [("hard links", None)]
        if os.name != "nt":
            cases.append(("no hard links", OSError(errno.EPERM, "Operation not permitted")))
        return cases

    @contextlib.contextmanager
    def racing_lock_removals(self, holders, before_free=None, after_free=None,
                             link_error=None):
        """Let other callers race every removal or move of the lock name.

        Each time any caller is about to unlink or rename away ``instance.lock``,
        ``before_free`` runs, then the file about to go is compared with the
        records of holders that have not released, then the name is freed and
        ``after_free`` runs.  Callers the hooks start are not raced again.

        Args:
            holders: Owner records of holders that have not released, by name.
            before_free: Called just before the name is freed.
            after_free: Called just after the name is freed.
            link_error: When set, ``os.link`` fails with it (no hard links).

        Yields:
            The list that collects every live lock that was removed or moved.
        """

        lock = os.path.abspath(str(self.lock_file()))
        real_unlink, real_rename = os.unlink, os.rename
        violations = []
        racing = []

        def free(kind, path, operation):
            if os.path.abspath(os.fspath(path)) != lock:
                return operation()
            outermost = not racing
            racing.append(kind)
            try:
                if outermost and before_free is not None:
                    before_free()
                try:
                    with open(lock, "rb") as handle:
                        content = handle.read()
                except OSError:
                    content = None
                if content in holders.values():
                    violations.append((kind, content))
                result = operation()
                if outermost and after_free is not None:
                    after_free()
                return result
            finally:
                racing.pop()

        def unlink(path, *args, **kwargs):
            return free("unlink", path, lambda: real_unlink(path, *args, **kwargs))

        def rename(source, target, *args, **kwargs):
            return free("rename", source, lambda: real_rename(source, target, *args, **kwargs))

        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(os, "unlink", unlink))
            stack.enter_context(mock.patch.object(os, "rename", rename))
            if link_error is not None:
                stack.enter_context(mock.patch.object(os, "link", side_effect=link_error))
            yield violations

    def racer(self, helper, name, holders, log):
        """Return a caller that takes the lock if it frees within a short deadline."""

        def run():
            saved = helper.LOCK_TIMEOUT_SECONDS
            helper.LOCK_TIMEOUT_SECONDS = 0.2
            try:
                record = helper._acquire_lock("codex")
            except helper.InstanceError:
                log.append((name, "waited"))
                return None
            finally:
                helper.LOCK_TIMEOUT_SECONDS = saved
            holders[name] = record
            log.append((name, "acquired"))
            return record

        return run

    def test_a_stale_break_never_frees_the_name_of_a_live_lock(self):
        """Two breakers judge one stale lock; the slower never removes the lock that replaced it.

        Between the slower breaker's last check and its removal, the faster one
        frees the stale lock and caller A asks for the lock.  Right after the
        slower breaker frees the name, either caller C asks for it or A finishes
        and releases.  A live lock is never removed or moved, at most one caller
        ever holds the lock, and a released lock never comes back.
        """

        for label, link_error in self.link_cases():
            for after in ("third caller", "owner releases"):
                with self.subTest(label, after=after):
                    helper = self.in_process_helper()
                    helper.LOCK_TIMEOUT_SECONDS = 0.6
                    self.make_stale_lock(helper)
                    holders, log = {}, []
                    real_inspect = helper._inspect_lock
                    lock = os.path.abspath(str(self.lock_file()))
                    checks = []

                    def last_check(path, *args, **kwargs):
                        state = real_inspect(path, *args, **kwargs)
                        if os.path.abspath(path) == lock:
                            checks.append(state)
                            if len(checks) == 2:
                                self.racer(helper, "A", holders, log)()
                        return state

                    def after_free():
                        if after == "third caller":
                            self.racer(helper, "C", holders, log)()
                        elif "A" in holders:
                            helper._release_lock("codex", holders.pop("A"))
                            log.append(("A", "released"))

                    with self.racing_lock_removals(holders, after_free=after_free,
                                                   link_error=link_error) as violations, \
                            mock.patch.object(helper, "_inspect_lock", last_check):
                        try:
                            holders["B"] = helper._acquire_lock("codex")
                            log.append(("B", "acquired"))
                        except helper.InstanceError:
                            log.append(("B", "waited"))

                    self.assertEqual(violations, [], log)
                    self.assertLessEqual(len(holders), 1, log)
                    if holders:
                        (record,) = holders.values()
                        self.assertEqual(self.lock_file().read_bytes(), record, log)
                    for name in list(holders):
                        helper._release_lock("codex", holders.pop(name))
                    self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                                     [], log)

    def test_a_release_never_removes_a_lock_that_replaced_it(self):
        """A holder past the stale window releases while another caller breaks its lock.

        Between the release's check of its own record and its unlink, caller C
        judges the lock stale and asks for it.  The release never removes C's
        lock, and C never gets in before the release has finished.
        """

        for label, link_error in self.link_cases():
            with self.subTest(label):
                helper = self.in_process_helper()
                holders, log = {}, []
                mine = helper._acquire_lock("codex")
                old = time.time() - 2 * helper.LOCK_STALE_SECONDS
                os.utime(self.lock_file(), (old, old))
                with self.racing_lock_removals(holders,
                                               before_free=self.racer(helper, "C", holders, log),
                                               link_error=link_error) as violations:
                    helper._release_lock("codex", mine)

                self.assertEqual(violations, [], log)
                if holders:
                    self.assertEqual(self.lock_file().read_bytes(), holders["C"], log)
                    helper._release_lock("codex", holders.pop("C"))
                self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                                 [], log)

    def guard_file(self):
        """Return the path of the guard every removal of the codex lock runs inside."""

        return self.data_root / "codex" / "instance.lock.break"

    @contextlib.contextmanager
    def clock_jump(self, helper, seconds):
        """Make the helper's wall clock jump ``seconds`` ahead, and the guard file that old.

        This is what a system sleep or a forward clock adjustment looks like to a
        caller: every age it measures is suddenly ``seconds`` larger.  The
        monotonic clock that bounds waiting is left alone.
        """

        real_time = time.time
        if self.guard_file().exists():
            old = real_time() - seconds
            os.utime(self.guard_file(), (old, old))
        jumped = types.SimpleNamespace(time=lambda: real_time() + seconds,
                                       monotonic=time.monotonic, sleep=time.sleep)
        with mock.patch.object(helper, "time", jumped):
            yield

    def test_a_paused_remover_keeps_the_guard_and_its_check_stays_true(self):
        """A remover that pauses inside the guard after its check keeps the guard.

        While it is paused the clock jumps 30 days, as after a system sleep, and
        another caller finds the lock stale.  That caller keeps waiting, because
        a live holder keeps the guard however long it is paused, so the lock the
        remover unlinks when it resumes is still the stale one it checked, never
        the other caller's new lock.
        """

        helper = self.in_process_helper()
        helper.LOCK_TIMEOUT_SECONDS = 2.0
        self.make_stale_lock(helper)
        holders, log = {}, []
        real_inspect = helper._inspect_lock
        lock = os.path.abspath(str(self.lock_file()))
        checks = []

        def paused(path, *args, **kwargs):
            state = real_inspect(path, *args, **kwargs)
            if os.path.abspath(path) == lock:
                checks.append(state)
                if len(checks) == 2:
                    with self.clock_jump(helper, 30 * 86400):
                        self.racer(helper, "B", holders, log)()
            return state

        with self.racing_lock_removals(holders) as violations, \
                mock.patch.object(helper, "_inspect_lock", paused):
            holders["A"] = helper._acquire_lock("codex")

        self.assertEqual(violations, [], log)
        self.assertEqual(log, [("B", "waited")])
        self.assertEqual(self.lock_file().read_bytes(), holders["A"])
        helper._release_lock("codex", holders.pop("A"))
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()), [])

    def test_a_live_guard_holds_back_every_removal_at_any_age(self):
        """While a live holder has the guard no lock is broken or released.

        The guard file is 30 days old and the clock has jumped 30 days, as after
        a system sleep: no age ever takes the guard from a live holder.
        """

        helper = self.in_process_helper()
        helper.LOCK_TIMEOUT_SECONDS = 0.3
        lock = str(self.lock_file())
        held = helper._take_guard(lock, time.monotonic() + 1)
        self.assertIsNotNone(held)
        with self.clock_jump(helper, 30 * 86400):
            self.make_stale_lock(helper)
            with self.assertRaises(helper.InstanceError) as raised:
                helper._acquire_lock("codex")
            self.assertEqual(raised.exception.code, "instance_lock_timeout")
            self.assertEqual(self.lock_file().read_bytes(), b"")
            self.assertFalse(helper.forget_instance("codex"))
            self.assertEqual(self.lock_file().read_bytes(), b"")

            # A release is never done outside the guard: the lock is left for the stale rules.
            os.unlink(self.lock_file())
            mine = helper._acquire_lock("codex")
            helper._release_lock("codex", mine)
            self.assertEqual(self.lock_file().read_bytes(), mine)

        # Its holder lets go; then the release goes through and nothing is left.
        helper._drop_guard(lock, held)
        helper._release_lock("codex", mine)
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()), [])

    def test_the_guard_of_a_holder_killed_inside_it_is_freed_by_the_system(self):
        """A remover killed while it holds the guard delays nobody once it is gone."""

        helper = self.in_process_helper()
        helper.LOCK_TIMEOUT_SECONDS = 0.3
        environment = dict(os.environ)
        environment["HIREY_INSTANCE_DATA_DIR"] = str(self.data_root)
        holder = subprocess.Popen(
            [sys.executable, "-c", GUARD_HOLDER_SCRIPT, str(HELPER), str(self.lock_file())],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=environment)
        try:
            self.assertEqual(holder.stdout.readline().strip(), "held")
            self.make_stale_lock(helper)
            with self.assertRaises(helper.InstanceError) as raised:
                helper.ensure_installation("codex")
            self.assertEqual(raised.exception.code, "instance_lock_timeout")
            self.assertEqual(self.lock_file().read_bytes(), b"")
        finally:
            holder.kill()
            holder.communicate(timeout=60)

        # Windows may take a moment to release a terminated process's locks.
        helper.LOCK_TIMEOUT_SECONDS = 10.0
        installation = helper.ensure_installation("codex")
        self.assertEqual(len(installation["installation_ref"]), 32)
        self.assertEqual(sorted(path.name for path in (self.data_root / "codex").iterdir()),
                         ["installation.json"])

    @unittest.skipIf(os.name == "nt", "Windows never removes a file that a caller has open")
    def test_a_waiter_never_holds_a_guard_file_removed_under_it(self):
        """A caller that locks the old guard file never holds the guard beside its new holder.

        Between a waiter's open and its lock, the holder lets go (removing the
        file while it still holds it) and a third caller takes the guard on a
        new file.  The waiter's lock on the old file succeeds, but the name no
        longer refers to that file, so the waiter does not hold the guard.
        """

        helper = self.in_process_helper()
        lock = str(self.lock_file())
        first = helper._take_guard(lock, time.monotonic() + 1)
        real_lock = helper._lock_guard_file
        seen = {}

        def holder_hands_over_first(handle, guard):
            if not seen:
                seen["old"] = os.fstat(handle).st_ino
                helper._drop_guard(lock, first)
                seen["third"] = helper._take_guard(lock, time.monotonic() + 1)
            return real_lock(handle, guard)

        with mock.patch.object(helper, "_lock_guard_file", holder_hands_over_first):
            waiter = helper._take_guard(lock, time.monotonic() + 0.3)
        self.assertIsNone(waiter)
        self.assertIsNotNone(seen["third"])
        self.assertNotEqual(self.guard_file().stat().st_ino, seen["old"])
        self.assertEqual(self.guard_file().stat().st_ino, os.fstat(seen["third"]).st_ino)
        helper._drop_guard(lock, seen["third"])
        self.assertFalse(self.guard_file().exists())

    def test_a_file_system_that_cannot_lock_files_fails_closed_before_writing(self):
        """Without file locks the helper says so at once and writes no instance state."""

        helper = self.in_process_helper()
        if os.name == "nt":
            module, function, codes = helper.msvcrt, "locking", (errno.EINVAL,)
        else:
            module, function, codes = helper.fcntl, "flock", (errno.ENOLCK, errno.EOPNOTSUPP,
                                                             errno.EINVAL)
        for code in codes:
            with self.subTest(errno=errno.errorcode[code]):
                with mock.patch.object(module, function, side_effect=OSError(code, os.strerror(code))), \
                        self.assertRaises(helper.InstanceError) as raised:
                    helper.ensure_profile("codex", PROFILE_A)
                self.assertEqual(raised.exception.code, "instance_lock_unsupported")
                # At most the empty guard file the check opened; no lock, installation or key.
                self.assertEqual([path.name for path in (self.data_root / "codex").iterdir()
                                  if path.name != "instance.lock.break"], [])

    def test_forget_removes_a_stale_lock_but_never_a_live_one(self):
        """Forget is a removal like any other: a lock of a call still running stays."""

        helper = self.in_process_helper()
        helper.ensure_installation("codex")
        mine = helper._acquire_lock("codex")
        self.assertTrue(helper.forget_instance("codex"))
        self.assertEqual(self.lock_file().read_bytes(), mine)
        self.assertFalse(self.installation_file().exists())
        helper._release_lock("codex", mine)

        self.make_stale_lock(helper)
        self.assertTrue(helper.forget_instance("codex"))
        self.assertFalse((self.data_root / "codex").exists())

    def test_a_reader_waits_out_a_record_that_is_still_empty(self):
        """A record created without hard links is briefly empty; readers wait, then fail closed."""

        helper = self.in_process_helper()
        helper.ensure_installation("codex")
        content = self.installation_file().read_bytes()
        self.installation_file().write_bytes(b"")
        writer = threading.Timer(0.2, self.installation_file().write_bytes, args=(content,))
        writer.start()
        try:
            installation = helper.read_installation("codex")
        finally:
            writer.join()
        self.assertEqual(installation["installation_ref"], json.loads(content)["installation_ref"])

        self.installation_file().write_bytes(b"")
        helper.RECORD_EMPTY_RETRY_SECONDS = 0.2
        with self.assertRaises(helper.InstanceError) as raised:
            helper.read_installation("codex")
        self.assertEqual(raised.exception.code, "instance_state_invalid")

    # -- forget -------------------------------------------------------------

    def test_forget_then_status_produces_a_new_installation(self):
        """An explicit installation ``forget`` is the only local action that rotates the ref."""

        first = self.status("codex")
        forgotten = self.run_helper("forget", "--host", "codex")
        self.assertEqual(forgotten.returncode, 0, forgotten.stderr or forgotten.stdout)
        self.assertTrue(json.loads(forgotten.stdout)["forgotten"])
        self.assertFalse(self.installation_file("codex").exists())
        self.assertFalse(self.profile_file(PROFILE_A, "codex").exists())

        second = self.status("codex")
        self.assertNotEqual(second["installation_ref"], first["installation_ref"])
        self.assertNotEqual(second["public_key"], first["public_key"])

        # Forgetting a host that has no instance is idempotent, not an error.
        again = self.run_helper("forget", "--host", "hermes")
        self.assertEqual(again.returncode, 0, again.stderr or again.stdout)
        self.assertFalse(json.loads(again.stdout)["forgotten"])
        second_again = self.run_helper("forget", "--host", "hermes")
        self.assertEqual(second_again.returncode, 0, second_again.stderr or second_again.stdout)
        self.assertFalse(json.loads(second_again.stdout)["forgotten"])

    # -- signing ------------------------------------------------------------

    def test_signature_verifies_with_the_core_challenge_verifier(self):
        """A real canonical challenge verifies, and a tampered message does not."""

        proof = load_core_proof()
        if proof is None:
            self.skipTest("the Core verifier checkout is not available")

        status = self.status("codex")
        challenge = proof.canonical_binding_challenge(
            challenge_id="challenge_test_1",
            agent_session_id="agent_session_test_1",
            operation="agent_instance.binding.begin",
            public_key_fingerprint_value=status["public_key_fingerprint"],
            local_instance_ref=status["installation_ref"],
            parameters_digest="0" * 64,
            expires_at="2026-09-19T10:00:00Z",
        )
        challenge_file = Path(self.temp.name) / "challenge.bin"
        challenge_file.write_bytes(challenge)

        signed = self.run_helper("sign", "--host", "codex", "--profile", PROFILE_A,
                                 "--challenge-file", str(challenge_file))
        self.assertEqual(signed.returncode, 0, signed.stderr or signed.stdout)
        result = json.loads(signed.stdout)
        self.assertEqual(result["public_key"], status["public_key"])
        self.assertEqual(result["profile_key"], PROFILE_A)
        self.assertEqual(
            proof.normalize_base64url(result["public_key"], "public_key", 32),
            result["public_key"],
        )
        self.assertEqual(
            proof.normalize_base64url(result["signature"], "signature", 64),
            result["signature"],
        )
        self.assertTrue(
            proof.verify_signature(result["public_key"], challenge, result["signature"]),
            "the instance signature did not verify against the canonical challenge",
        )
        self.assertFalse(
            proof.verify_signature(result["public_key"], challenge + b"\n", result["signature"]),
            "a tampered challenge must not verify",
        )
        # The reported fingerprint is the digest the challenge binds.
        self.assertEqual(
            proof.public_key_fingerprint(result["public_key"]),
            status["public_key_fingerprint"],
        )

    def test_two_profiles_sign_with_their_own_keys(self):
        """Signing with one identity never uses another identity's key."""

        proof = load_core_proof()
        if proof is None:
            self.skipTest("the Core verifier checkout is not available")
        a = self.status("codex", PROFILE_A)
        b = self.status("codex", PROFILE_B)
        challenge_file = Path(self.temp.name) / "challenge.bin"
        challenge_file.write_bytes(b"hirey-agent-instance-binding-v1\nprofile-selection")
        signed_a = json.loads(self.run_helper(
            "sign", "--host", "codex", "--profile", PROFILE_A,
            "--challenge-file", str(challenge_file)).stdout)
        signed_b = json.loads(self.run_helper(
            "sign", "--host", "codex", "--profile", PROFILE_B,
            "--challenge-file", str(challenge_file)).stdout)
        self.assertEqual(signed_a["public_key"], a["public_key"])
        self.assertEqual(signed_b["public_key"], b["public_key"])
        self.assertTrue(proof.verify_signature(a["public_key"],
                                               challenge_file.read_bytes(), signed_a["signature"]))
        self.assertTrue(proof.verify_signature(b["public_key"],
                                               challenge_file.read_bytes(), signed_b["signature"]))
        self.assertFalse(proof.verify_signature(a["public_key"],
                                                challenge_file.read_bytes(), signed_b["signature"]))

    def test_sign_rejects_a_missing_or_empty_challenge_file(self):
        """Signing fails closed instead of signing an empty message."""

        missing = self.run_helper("sign", "--host", "codex", "--profile", PROFILE_A,
                                  "--challenge-file", str(Path(self.temp.name) / "absent.bin"))
        self.assertNotEqual(missing.returncode, 0)
        self.assertEqual(json.loads(missing.stdout)["error_code"], "instance_challenge_unreadable")

        empty = Path(self.temp.name) / "empty.bin"
        empty.write_bytes(b"")
        blank = self.run_helper("sign", "--host", "codex", "--profile", PROFILE_A,
                                "--challenge-file", str(empty))
        self.assertNotEqual(blank.returncode, 0)
        self.assertEqual(json.loads(blank.stdout)["error_code"], "instance_challenge_empty")

    # -- signer layer -------------------------------------------------------

    def test_the_openssl_cli_path_signs_the_same_key(self):
        """The CLI signer derives and signs with the same Ed25519 key."""

        helper = load_helper()
        if not helper.openssl_supported():
            self.skipTest("no openssl CLI with real Ed25519 support")
        seed = helper.generate_seed()
        message = b"hirey-agent-instance-binding-v1\nchallenge_id=cli"
        cryptography_key = helper.derive_public_key("cryptography", seed) \
            if helper.cryptography_available() else ""
        openssl_key = helper.derive_public_key("openssl", seed)
        if cryptography_key:
            self.assertEqual(cryptography_key, openssl_key)
        signature = helper.sign_message("openssl", seed, message)
        proof = load_core_proof()
        if proof is None:
            self.skipTest("the Core verifier checkout is not available")
        self.assertTrue(proof.verify_signature(openssl_key, message, signature))
        self.assertFalse(proof.verify_signature(openssl_key, message + b"x", signature))

    def test_a_broken_cryptography_extension_falls_through_to_openssl(self):
        """A native-extension failure must degrade to openssl, never crash.

        A half-installed ``cryptography`` can fail at import time with an
        exception that derives from ``BaseException`` rather than ``Exception``
        (pyo3 reports a Rust panic that way).  Catching only ``Exception`` let
        that escape and killed the command before any fallback ran.
        """

        helper = load_helper()
        if not helper.openssl_supported():
            self.skipTest("this machine has no Ed25519-capable openssl to fall back to")
        bootstrap = (
            "import sys, importlib.util\n"
            "sys.path.insert(0, %r)\n"
            "class PanicException(BaseException):\n"
            "    pass\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'cryptography':\n"
            "            raise PanicException('Rust panic: _cffi_backend is missing')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "spec = importlib.util.spec_from_file_location('hi_instance', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "code = module.main(['status', '--host', 'codex'])\n"
            "print('EXIT', code)\n"
        ) % (str(TESTS_DIR), str(HELPER))
        completed = subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True,
            text=True,
            cwd=str(self.temp.name),
            env={**os.environ, "HIREY_INSTANCE_DATA_DIR": str(self.data_root)},
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("PanicException", completed.stderr)
        self.assertIn("EXIT 0", completed.stdout)
        status = json.loads(completed.stdout.split("EXIT", 1)[0].strip())
        self.assertEqual(status["signer"], "openssl",
                         "a broken cryptography install must fall through to openssl")

        # The fallback signature must still be one the server can verify.
        challenge = Path(self.temp.name) / "challenge.txt"
        challenge.write_bytes(b"hirey-agent-instance-binding-v1\nbroken-extension-check")
        signed = subprocess.run(
            [sys.executable, "-c",
             bootstrap.replace(
                 "['status', '--host', 'codex']",
                 "['sign', '--host', 'codex', '--profile', %r, '--challenge-file', %r]"
                 % (PROFILE_A, str(challenge)))]
            + [],
            capture_output=True, text=True, cwd=str(self.temp.name),
            env={**os.environ, "HIREY_INSTANCE_DATA_DIR": str(self.data_root)},
            timeout=120,
        )
        self.assertEqual(signed.returncode, 0, signed.stderr)
        proof = load_core_proof()
        if proof is None:
            self.skipTest("the Core verifier checkout is not available")
        payload = json.loads(signed.stdout.split("EXIT", 1)[0].strip())
        self.assertTrue(proof.verify_signature(
            payload["public_key"], challenge.read_bytes(), payload["signature"]))

    def test_fatal_exceptions_are_never_swallowed_by_the_signer_probe(self):
        """``KeyboardInterrupt``/``SystemExit`` must propagate, not become False."""

        helper = load_helper()
        self.assertFalse(helper.is_fatal(ValueError("x")))
        self.assertFalse(helper.is_fatal(RuntimeError("x")))
        self.assertTrue(helper.is_fatal(KeyboardInterrupt()))
        self.assertTrue(helper.is_fatal(SystemExit(0)))
        # The probe must re-raise a fatal error rather than report "unavailable".
        original = helper._cryptography_probe
        try:
            helper._CRYPTOGRAPHY_SUPPORTED = None

            def panic():
                raise KeyboardInterrupt()

            helper._cryptography_probe = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                helper.resolve_signer()
        finally:
            helper._cryptography_probe = original
            helper._CRYPTOGRAPHY_SUPPORTED = None

    def test_no_signer_available_fails_closed_without_writing_state(self):
        """With neither signer usable the helper exits typed and writes nothing."""

        empty_path = Path(self.temp.name) / "empty-bin"
        empty_path.mkdir()
        bootstrap = (
            "import json, sys\n"
            "sys.path.insert(0, %r)\n"
            "import importlib.util\n"
            "class _Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] == 'cryptography':\n"
            "            raise ImportError('blocked for the test')\n"
            "        return None\n"
            "sys.meta_path.insert(0, _Block())\n"
            "spec = importlib.util.spec_from_file_location('hi_instance', %r)\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            "code = module.main(['status', '--host', 'codex'])\n"
            "print(code)\n"
        ) % (str(TESTS_DIR), str(HELPER))
        completed = subprocess.run(
            [sys.executable, "-c", bootstrap],
            capture_output=True,
            text=True,
            cwd=str(self.temp.name),
            env={
                **os.environ,
                "PATH": str(empty_path),
                "HIREY_INSTANCE_DATA_DIR": str(self.data_root),
            },
            timeout=120,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(int(completed.stdout.strip().splitlines()[-1]), EXIT_SIGNER_UNAVAILABLE)
        self.assertFalse(self.data_root.exists(), "a failed signer probe must not create state")


if __name__ == "__main__":
    unittest.main()
