"""Behavioural tests for the shared ``hi-instance`` local instance helper.

Every test drives the helper as a real process, because the properties that
matter here are process-level ones: two simultaneous first calls must produce
exactly one key pair, one installation switching Person A -> B -> A must keep two
separate keys under one shared installation ref, a plugin upgrade must not change
the profile, a legacy single-key file must be migrated without being rewritten,
and the private seed must never leave the machine.

The helper itself imports no third-party module, so this file imports nothing
third-party either; the Ed25519 checks are skipped when the environment cannot
provide a signer or the cross-repository verifier is not checked out.
"""

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
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
