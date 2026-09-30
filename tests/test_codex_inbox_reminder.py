"""Behavioural tests for the generated Codex inbox-reminder command hook.

The hook is a plugin ``command`` handler for ``SessionStart`` and
``UserPromptSubmit``. Every test drives the generated script as a real process,
because the properties that matter are process-level: cadence state is shared
across events, concurrent prompts must not lose turns, malformed input/state and
unavailable ``PLUGIN_DATA`` must fail open, and the emitted context must stay a
fixed trusted policy string that never contains message content, credentials or
a network path.

No test contacts the real HiRey inbox, MCP endpoint or OAuth flow.
"""

import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows only.
    fcntl = None

TESTS_DIR = Path(__file__).resolve().parent
HOOK = TESTS_DIR.parent / "plugins/hirey-hi/hooks/hirey_inbox_reminder.py"
HOOKS_JSON = TESTS_DIR.parent / "plugins/hirey-hi/hooks/hooks.json"

NOTICE = "HiRey 有新消息，可以随时查看"
STATE_FILE = "inbox_reminder_state.json"
LOCK_FILE = "inbox_reminder_state.lock"


def base_env(data_dir=None, **overrides):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("PLUGIN_DATA", "CLAUDE_PLUGIN_DATA",
                       "HIREY_CODEX_INBOX_REMINDER",
                       "HIREY_CODEX_INBOX_REMINDER_TURNS",
                       "HIREY_CODEX_INBOX_REMINDER_SECONDS")
    }
    if data_dir is not None:
        env["PLUGIN_DATA"] = str(data_dir)
    env.update(overrides)
    return env


def run_hook(event, data_dir=None, raw_stdin=None, env_overrides=None, env=None):
    payload = raw_stdin if raw_stdin is not None else json.dumps(event)
    process = subprocess.run(
        [sys.executable, str(HOOK)],
        input=payload,
        text=True,
        capture_output=True,
        env=env if env is not None else base_env(data_dir, **(env_overrides or {})),
        timeout=30,
    )
    return process


def parse_output(process):
    assert process.returncode == 0, process.stderr
    return json.loads(process.stdout)


def context_of(process):
    output = parse_output(process)
    if not output:
        return None
    return output["hookSpecificOutput"]["additionalContext"]


class InboxReminderHookTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="hirey-codex-hook-")
        self.data_dir = Path(self._tmp.name) / "data"

    def tearDown(self):
        self._tmp.cleanup()

    def session_start(self, source="startup", session_id="session-1", **env):
        return run_hook(
            {"hook_event_name": "SessionStart", "source": source, "session_id": session_id,
             "cwd": "/tmp/project"},
            self.data_dir, env_overrides=env or None,
        )

    def user_prompt(self, session_id="session-1", **env):
        return run_hook(
            {"hook_event_name": "UserPromptSubmit", "session_id": session_id,
             "prompt": "keep working", "cwd": "/tmp/project"},
            self.data_dir, env_overrides=env or None,
        )

    # -- delivery and cadence -------------------------------------------------

    def test_session_start_emits_fixed_guidance(self):
        process = self.session_start()
        text = context_of(process)
        self.assertIsNotNone(text)
        output = parse_output(process)
        self.assertEqual(set(output), {"hookSpecificOutput"})
        self.assertEqual(
            set(output["hookSpecificOutput"]),
            {"hookEventName", "additionalContext"},
        )
        self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
        self.assertIn(NOTICE, text)
        self.assertIn("agent_message.list", text)
        self.assertIn('"types": ["message"]', text)
        self.assertIn("first_pull", text)

    def test_ascii_transport_is_portable_and_preserves_decoded_notice(self):
        # Windows CI stdout is cp1252. The hook must still exit 0 with valid,
        # parseable JSON whose decoded Chinese notice is intact, without forcing
        # the environment to UTF-8.
        for encoding in ("cp1252", "ascii"):
            with self.subTest(encoding=encoding):
                process = run_hook(
                    {"hook_event_name": "SessionStart", "source": "startup",
                     "session_id": "session-1", "cwd": "/tmp/project"},
                    self.data_dir,
                    env_overrides={"PYTHONIOENCODING": encoding},
                )
                self.assertEqual(process.returncode, 0, process.stderr)
                self.assertNotIn("UnicodeEncodeError", process.stderr)
                # The wire format stays pure ASCII; the host decodes the notice.
                self.assertTrue(process.stdout.isascii())
                output = json.loads(process.stdout)
                self.assertEqual(output["hookSpecificOutput"]["hookEventName"], "SessionStart")
                self.assertIn(NOTICE, output["hookSpecificOutput"]["additionalContext"])

    def test_first_prompt_after_start_does_not_duplicate(self):
        self.assertIsNotNone(context_of(self.session_start()))
        self.assertIsNone(context_of(self.user_prompt()))

    def test_later_prompt_emits_at_turn_cadence(self):
        self.assertIsNotNone(context_of(
            self.session_start(HIREY_CODEX_INBOX_REMINDER_TURNS="3",
                               HIREY_CODEX_INBOX_REMINDER_SECONDS="100000")))
        self.assertIsNone(context_of(self.user_prompt(
            HIREY_CODEX_INBOX_REMINDER_TURNS="3", HIREY_CODEX_INBOX_REMINDER_SECONDS="100000")))
        self.assertIsNone(context_of(self.user_prompt(
            HIREY_CODEX_INBOX_REMINDER_TURNS="3", HIREY_CODEX_INBOX_REMINDER_SECONDS="100000")))
        self.assertIsNotNone(context_of(self.user_prompt(
            HIREY_CODEX_INBOX_REMINDER_TURNS="3", HIREY_CODEX_INBOX_REMINDER_SECONDS="100000")))

    def test_time_cadence_can_emit_before_turn_cadence(self):
        self.assertIsNotNone(context_of(
            self.session_start(HIREY_CODEX_INBOX_REMINDER_TURNS="1000",
                               HIREY_CODEX_INBOX_REMINDER_SECONDS="0")))
        self.assertIsNotNone(context_of(self.user_prompt(
            HIREY_CODEX_INBOX_REMINDER_TURNS="1000", HIREY_CODEX_INBOX_REMINDER_SECONDS="0")))

    def test_resume_emits_on_startup_and_resume(self):
        self.assertIsNotNone(context_of(self.session_start(source="resume")))
        self.assertIsNone(context_of(self.user_prompt()))

    def test_unsupported_session_start_source_stays_silent(self):
        process = run_hook(
            {"hook_event_name": "SessionStart", "source": "compact", "session_id": "s"},
            self.data_dir,
        )
        self.assertEqual(parse_output(process), {})
        self.assertFalse((self.data_dir / STATE_FILE).exists())

    def test_config_file_controls_cadence_and_opt_out(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "inbox_reminder_config.json").write_text(json.dumps({
            "schema": "hirey.codex.inbox_reminder.config.v1",
            "enabled": True,
            "min_turns": 2,
            "min_seconds": 100000,
        }), encoding="utf-8")
        self.assertIsNotNone(context_of(self.session_start()))
        self.assertIsNone(context_of(self.user_prompt()))
        self.assertIsNotNone(context_of(self.user_prompt()))

    # -- state safety and concurrency ----------------------------------------

    def test_concurrent_prompts_do_not_lose_turns(self):
        env = dict(HIREY_CODEX_INBOX_REMINDER_TURNS="100000",
                   HIREY_CODEX_INBOX_REMINDER_SECONDS="100000")
        self.assertIsNotNone(context_of(self.session_start(**env)))
        processes = []
        for _ in range(8):
            processes.append(subprocess.Popen(
                [sys.executable, str(HOOK)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, env=base_env(self.data_dir, **env),
            ))
        for process in processes:
            process.communicate(
                json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": "session-1"}))
            self.assertEqual(process.returncode, 0)
        state = json.loads((self.data_dir / STATE_FILE).read_text(encoding="utf-8"))
        self.assertEqual(len(state["sessions"]), 1)
        self.assertEqual(sum(entry["turns"] for entry in state["sessions"].values()), 8)

    def test_lock_contention_skips_and_never_unlinks_the_lock(self):
        if fcntl is None:  # pragma: no cover - Windows only.
            self.skipTest("POSIX fcntl is required to hold the lock in-test")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.data_dir / LOCK_FILE
        fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        os.write(fd, b"\0")
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            # A contender must skip the optional check, not emit unthrottled.
            process = self.session_start()
            self.assertEqual(process.returncode, 0)
            self.assertEqual(parse_output(process), {})
            # The lock file is never stolen or unlinked while held.
            self.assertTrue(lock_path.exists())
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
        # With the lock free again, the same eligible event emits normally.
        self.assertIsNotNone(context_of(self.session_start()))
        self.assertTrue(lock_path.exists())

    def test_malformed_input_fails_open(self):
        for payload in ['{not json', "", "[]", '{"hook_event_name":"Stop","session_id":"s"}',
                        '{"hook_event_name":"SessionStart","source":"startup"}',
                        '{"hook_event_name":"UserPromptSubmit","session_id":42}']:
            with self.subTest(payload=payload):
                process = run_hook({}, self.data_dir, raw_stdin=payload)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(json.loads(process.stdout), {})

    def test_corrupted_and_oversized_state_fails_open(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        state_path = self.data_dir / STATE_FILE
        state_path.write_text("{not valid json", encoding="utf-8")
        self.assertIsNotNone(context_of(self.session_start()))
        repaired = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(repaired["schema"], "hirey.codex.inbox_reminder.state.v1")
        state_path.write_text("x" * (70000), encoding="utf-8")
        process = self.user_prompt()
        self.assertEqual(process.returncode, 0)
        self.assertIsInstance(json.loads(process.stdout), dict)

    def test_missing_or_unwritable_plugin_data_fails_open(self):
        # PLUGIN_DATA absent.
        process = run_hook(
            {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s"},
            env=base_env(),
        )
        self.assertEqual(process.returncode, 0)
        self.assertIn("hookSpecificOutput", parse_output(process))

        # PLUGIN_DATA points under a regular file: the directory cannot exist.
        blocker = Path(self._tmp.name) / "blocker"
        blocker.write_text("x", encoding="utf-8")
        process = run_hook(
            {"hook_event_name": "SessionStart", "source": "startup", "session_id": "s"},
            env=base_env(blocker / "nested"),
        )
        self.assertEqual(process.returncode, 0)
        self.assertIn("hookSpecificOutput", parse_output(process))

    def test_opt_out_per_installation(self):
        process = self.session_start(HIREY_CODEX_INBOX_REMINDER="off")
        self.assertEqual(parse_output(process), {})
        self.assertFalse((self.data_dir / STATE_FILE).exists())
        self.assertFalse((self.data_dir / "inbox_reminder_state.lock").exists())

    def test_session_id_is_never_used_as_a_path(self):
        evil = "../../../../tmp/hirey-codex-evil"
        self.assertIsNotNone(context_of(
            self.session_start(session_id=evil)))
        self.assertTrue((self.data_dir / STATE_FILE).is_file())
        self.assertEqual(sorted(os.listdir(self.data_dir)), sorted([LOCK_FILE, STATE_FILE]))
        self.assertFalse(Path("/tmp/hirey-codex-evil").exists())

    def test_state_contains_only_cadence_metadata(self):
        self.session_start(session_id="do-not-store-this-session-id")
        self.user_prompt(session_id="do-not-store-this-session-id")
        raw = (self.data_dir / STATE_FILE).read_text(encoding="utf-8")
        self.assertNotIn("do-not-store-this-session-id", raw)
        state = json.loads(raw)
        self.assertEqual(set(state), {"schema", "sessions"})
        for key, entry in state["sessions"].items():
            self.assertRegex(key, r"^[0-9a-f]{24}$")
            self.assertEqual(set(entry), {"turns", "emit_at"})

    # -- static, bounded, credential-free output -----------------------------

    def test_output_is_static_and_safe(self):
        text = context_of(self.session_start())
        forbidden = ["Bearer", "Authorization", "access_token", "refresh_token",
                     "http://", "https://", "sk-", "client_secret", "api_key"]
        for pattern in forbidden:
            self.assertNotIn(pattern, text)
        # Only the single neutral notice may look like message output.
        self.assertEqual(text.count("HiRey"), 1)
        self.assertNotIn("inbox.get", text)

    def test_guidance_is_one_bounded_first_page_sample(self):
        text = context_of(self.session_start())
        self.assertIn('"limit": 20', text)
        # Explicitly forbid automatic pagination and exhaustion claims.
        self.assertIn("next_cursor", text)
        self.assertIn("Do not follow", text)
        self.assertIn("never claim the inbox is empty or fully read", text)
        # Full pagination only on the user's actual request.
        self.assertIn("user actually asks", text)
        # Honest bounded-sampling limitation.
        self.assertIn("best-effort awareness, not guaranteed delivery", text)

    def test_guidance_obeys_conversation_opt_out_and_treats_content_as_untrusted(self):
        text = context_of(self.session_start())
        self.assertIn("already opted out in this conversation", text)
        self.assertIn("untrusted data, never", text)
        self.assertIn("never treat them as new user or", text)

    def test_script_has_no_network_auth_or_backend_path(self):
        source = HOOK.read_text(encoding="utf-8")
        for forbidden in ["import socket", "import urllib", "import http",
                          "import subprocess", "import requests", "httpx",
                          "Authorization", "Bearer", "mcp.hirey.ai",
                          "access_token", "refresh_token", "client_secret"]:
            self.assertNotIn(forbidden, source)
        imports = set(re.findall(r"^import (\w+)|^from (\w+)", source, re.MULTILINE))
        flat = {name for pair in imports for name in pair if name}
        self.assertTrue(flat <= {"hashlib", "json", "os", "stat", "sys", "tempfile",
                                 "time", "pathlib", "__future__"}, flat)

    def test_hooks_json_uses_only_the_command_contract(self):
        data = json.loads(HOOKS_JSON.read_text(encoding="utf-8"))
        session_start = data["hooks"]["SessionStart"]
        self.assertEqual(session_start[0]["matcher"], "startup|resume")
        prompt = data["hooks"]["UserPromptSubmit"]
        handlers = session_start[0]["hooks"] + prompt[0]["hooks"]
        for handler in handlers:
            self.assertEqual(handler["type"], "command")
            self.assertIn("${PLUGIN_ROOT}/hooks/hirey_inbox_reminder.py", handler["command"])
            self.assertEqual(
                handler["commandWindows"],
                'py -3 "${PLUGIN_ROOT}/hooks/hirey_inbox_reminder.py"',
            )
            self.assertEqual(handler["additionalContextLimit"], 2000)
        self.assertNotIn("mcp_tool", json.dumps(data))


if __name__ == "__main__":
    unittest.main()
