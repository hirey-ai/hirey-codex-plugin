"""Contract tests for the Codex inbox reminder hook source."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
TEMP_ROOT = "/private/tmp" if Path("/private/tmp").is_dir() else None
HOOK_DIR = ROOT / "plugins/hirey-hi/hooks"
sys.path.insert(0, str(HOOK_DIR))
spec = importlib.util.spec_from_file_location("inbox_reminder_under_test", HOOK_DIR / "hirey_inbox_reminder.py")
hook = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hook)

PROFILE = "a" * 64
STATUS = {"identity_bound": True, "instance_status": "bound", "profile_key": PROFILE,
          "current_instance": {"instance_id": "local-one", "status": "active"}}


def receipt(checkpoint="cp-1", new=0, historical=0, requests_new=0, requests_historical=0):
    return {"contract": "hirey.person.inbox.latest.v1", "scope": "authorized_workspaces",
            "checked_at": "2026-10-09T09:00:00Z", "window_started_at": "2026-10-06T09:00:00Z",
            "checkpoint": checkpoint,
            "messages": {"new_count": new, "historical_count": historical,
                         "total_count": new + historical, "new_sender_count": 1 if new else 0,
                         "new_system_count": 0,
                         "senders": ([{"person_id": "person-a", "display_name": "王某",
                                      "message_count": new}] if new else [])},
            "contact_requests": {"new_count": requests_new,
                                 "historical_count": requests_historical,
                                 "total_count": requests_new + requests_historical},
            "snapshots": []}


class Clock:
    def __init__(self):
        self.epoch = datetime(2026, 10, 9, 9, tzinfo=timezone.utc).timestamp()

    def advance(self, seconds):
        self.epoch += seconds

    def now(self):
        return datetime.fromtimestamp(self.epoch, timezone.utc).isoformat().replace("+00:00", "Z")


class FakeRemote:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            self.calls.append(("connect", None))
        return self

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return None

    def status(self, installation):
        with self.lock:
            self.calls.append(("status", installation))
        return STATUS

    def latest(self, checkpoint):
        with self.lock:
            self.calls.append(("latest", checkpoint))
            result = self.results.pop(0)
        if isinstance(result, BaseException):
            raise result
        return result

    def latest_inputs(self):
        return [value for name, value in self.calls if name == "latest"]


class InboxReminderHookTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="hirey-inbox-contract-", dir=TEMP_ROOT)
        self.addCleanup(self.tmp.cleanup)
        self.data = Path(self.tmp.name) / "data"
        env = dict(os.environ)
        for name in ("HIREY_CODEX_INBOX_HOOK_ACTIVE", "HIREY_CODEX_INBOX_REMINDER",
                     "HIREY_CODEX_INBOX_REMINDER_SECONDS", "CODEX_SESSION_ID"):
            env.pop(name, None)
        env["PLUGIN_DATA"] = str(self.data)
        self.env_patch = patch.dict(os.environ, env, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.clock = Clock()
        self.now_patch = patch.object(hook, "_now", self.clock.now)
        self.time_patch = patch.object(hook.time, "time", lambda: self.clock.epoch)
        self.now_patch.start()
        self.time_patch.start()
        self.addCleanup(self.now_patch.stop)
        self.addCleanup(self.time_patch.stop)

    def event(self, session="session-a", kind="UserPromptSubmit"):
        result = {"hook_event_name": kind, "session_id": session}
        if kind == "SessionStart":
            result["source"] = "startup"
        else:
            result["prompt"] = "PRIVATE PROMPT MUST NOT BE LOGGED"
        return result

    def run_hook(self, remote, session="session-a", kind="UserPromptSubmit"):
        return hook.run(self.event(session, kind), client_factory=remote,
                        installation=lambda: "installation-one")

    def config(self, **values):
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / hook.CONFIG_FILENAME).write_text(json.dumps({
            "schema": hook.SCHEMA_CONFIG, **values}), encoding="utf-8")

    def state(self):
        return json.loads((self.data / hook.STATE_FILENAME).read_text(encoding="utf-8"))

    def journal(self):
        path = self.data / hook.JOURNAL_FILENAME
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def events(self, name):
        return [record for record in self.journal() if record["event"] == name]

    def scope(self):
        return next(iter(self.state()["scopes"].values()))

    def hint(self):
        return self.events("hint_offered")[-1]

    def pad_journal_past_recent_window(self):
        path = self.data / hook.JOURNAL_FILENAME
        for _ in range(hook.RECENT_JOURNAL_RECORDS + 1):
            hook._append(path, "user_activity", None, hook._ref("session-b"),
                         {"hook_event": "UserPromptSubmit", "pending_batch_id": None})
        return path

    def installed_hook(self):
        plugin_root = Path(self.tmp.name) / "plugins"
        hooks = plugin_root / "cache" / "openai-curated-remote" / "hirey-hi" / "1.2.3" / "hooks"
        hooks.mkdir(parents=True)
        shutil.copy2(HOOK_DIR / "hirey_inbox_reminder.py", hooks / "hirey_inbox_reminder.py")
        shutil.copy2(HOOK_DIR / "hi_hook_client.py", hooks / "hi_hook_client.py")
        sys.path.insert(0, str(hooks))
        self.addCleanup(lambda: sys.path.remove(str(hooks)) if str(hooks) in sys.path else None)
        installed_spec = importlib.util.spec_from_file_location(
            f"inbox_reminder_installed_{id(self)}", hooks / "hirey_inbox_reminder.py")
        installed = importlib.util.module_from_spec(installed_spec)
        installed_spec.loader.exec_module(installed)
        data_dir = plugin_root / "data" / "hirey-hi-openai-curated-remote"
        return installed, data_dir

    # [IR-04]
    def test_ir04_unchanged_batch_without_due_cold_hint_is_silent(self):
        remote = FakeRemote([receipt("cp-1"), receipt("cp-2")])
        self.assertEqual(self.run_hook(remote), {})
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote), {})
        self.assertEqual(remote.latest_inputs(), [None, "cp-1"])
        self.assertEqual(self.scope()["last_seen_checkpoint"], "cp-2")
        self.assertEqual(self.events("hint_offered"), [])

    # [IR-10]
    def test_ir10_five_minute_installation_throttle_precedes_remote(self):
        remote = FakeRemote([receipt("cp-1", new=1)])
        self.assertIn("hookSpecificOutput", self.run_hook(remote, "session-a"))
        self.clock.advance(60)
        self.assertEqual(self.run_hook(remote, "session-b"), {})
        self.assertEqual([name for name, _ in remote.calls], ["connect", "status", "latest"])
        self.assertEqual(len(self.events("check_started")), 1)

    def test_invalid_installation_is_logged_without_remote_request(self):
        class InstanceError(RuntimeError):
            code = "instance_state_invalid"

        class InvalidInstallation:
            @staticmethod
            def read_installation(_host):
                raise InstanceError("malformed installation")

        InvalidInstallation.InstanceError = InstanceError
        remote = FakeRemote([])
        with patch.object(hook, "_instance_helper", return_value=InvalidInstallation):
            self.assertEqual(hook.run(self.event(), client_factory=remote,
                                      installation=hook._installation), {})
        self.assertEqual(remote.calls, [])
        self.assertEqual(self.events("check_failed")[-1]["data"],
                         {"stage": "identity", "error_code": "instance_state_invalid"})

    # [IR-11]
    def test_ir11_session_silence_precedes_network_and_other_session_can_query(self):
        remote = FakeRemote([receipt("cp-1"), receipt("cp-2", new=1)])
        self.run_hook(remote, "session-a")
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            self.assertTrue(hook.command_silence("on")["silent"])
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote, "session-a"), {})
        self.assertEqual(remote.latest_inputs(), [None])
        self.assertIn("hookSpecificOutput", self.run_hook(remote, "session-b"))
        self.assertEqual(remote.latest_inputs(), [None, "cp-1"])
        self.assertTrue(self.state()["sessions"][hook._ref("session-a")]["silent"])
        self.assertFalse(self.state()["sessions"][hook._ref("session-b")]["silent"])

    # [IR-12]
    def test_ir12_timeout_403_and_invalid_contract_preserve_checkpoint_and_batch(self):
        for failure in (TimeoutError("deadline"), ValueError("403"), {**receipt("cp-bad"), "checkpoint": ""}):
            with self.subTest(failure=str(failure)):
                with tempfile.TemporaryDirectory(prefix="ir12-", dir=TEMP_ROOT) as path:
                    with patch.dict(os.environ, PLUGIN_DATA=path):
                        original_data = self.data
                        self.data = Path(path)
                        try:
                            remote = FakeRemote([receipt("cp-1", new=2), failure])
                            self.assertIn("hookSpecificOutput", self.run_hook(remote))
                            prior = self.scope().copy()
                            self.clock.advance(301)
                            self.assertEqual(self.run_hook(remote), {})
                            self.assertEqual(self.scope(), prior)
                            failed = self.events("check_failed")[-1]
                            self.assertIn(failed["data"]["stage"], ("request", "contract"))
                            self.assertEqual(remote.latest_inputs(), [None, None])
                        finally:
                            self.data = original_data

    # [IR-13]
    def test_ir13_concurrent_sessions_form_one_batch_and_one_bounded_hint(self):
        remote = FakeRemote([receipt("cp-1", new=3, requests_new=1)])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda sid: self.run_hook(remote, sid), ("session-a", "session-b")))
        contexts = [r["hookSpecificOutput"]["additionalContext"] for r in results if r]
        self.assertEqual(len(contexts), 1)
        self.assertIn("新增未读消息 3 条", contexts[0])
        self.assertIn("新增待处理联系申请 1 条", contexts[0])
        self.assertNotIn("PRIVATE PROMPT", contexts[0])
        self.assertEqual(len(remote.latest_inputs()), 1)
        self.assertEqual(len(self.events("hint_offered")), 1)
        self.assertEqual(len(self.state()["scopes"]), 1)
        self.assertEqual(self.scope()["pending_batch"]["hint_count"], 1)

    # [IR-14]
    def test_ir14_unattempted_batch_can_hint_again_after_hot_interval(self):
        self.config(hot_repeat_seconds=60)
        remote = FakeRemote([receipt("cp-1", new=2), receipt("cp-2", new=2)])
        self.assertTrue(self.run_hook(remote))
        batch = self.scope()["pending_batch"]["batch_id"]
        self.clock.advance(301)
        self.assertTrue(self.run_hook(remote, "session-b"))
        hints = self.events("hint_offered")
        self.assertEqual([r["data"]["batch_id"] for r in hints], [batch, batch])
        self.assertEqual(self.events("reminder_attempted"), [])
        self.assertEqual(self.scope()["pending_batch"]["hint_count"], 2)
        self.assertEqual(remote.latest_inputs(), [None, None])

    # [IR-15]
    def test_ir15_attempted_items_become_cold_and_repeat_only_after_interval(self):
        self.config(cold_repeat_seconds=600)
        remote = FakeRemote([receipt("cp-1", new=2), receipt("cp-2", historical=2),
                             receipt("cp-3", historical=2)])
        self.run_hook(remote)
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            hook.command_record(self.hint()["record_id"], "attempted", "提醒两条消息")
        self.assertIsNone(self.scope()["pending_batch"])
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote), {})
        self.clock.advance(301)
        self.assertIn("历史积压", self.run_hook(remote)["hookSpecificOutput"]["additionalContext"])
        self.assertEqual([r["data"]["hint_kind"] for r in self.events("hint_offered")], ["hot", "cold"])

    # [IR-22]
    def test_ir22_window_rollover_closes_hot_batch_without_claiming_completion(self):
        remote = FakeRemote([receipt("cp-1", new=1), receipt("cp-2", historical=1)])
        self.run_hook(remote)
        self.clock.advance(301)
        self.run_hook(remote)
        self.assertIsNone(self.scope()["pending_batch"])
        succeeded = self.events("check_succeeded")[-1]
        self.assertEqual(succeeded["data"]["close_reason"], "no_hot_items")
        self.assertEqual(succeeded["data"]["messages"]["historical_count"], 1)
        self.assertNotIn("processed", json.dumps(succeeded).lower())

    # [IR-16]
    def test_ir16_complete_journal_restores_checkpoint_and_batch_after_state_loss(self):
        remote = FakeRemote([receipt("cp-1", new=1), receipt("cp-2", new=1)])
        self.run_hook(remote)
        batch = self.scope()["pending_batch"]["batch_id"]
        (self.data / hook.STATE_FILENAME).write_text("{incomplete", encoding="utf-8")
        self.clock.advance(301)
        self.run_hook(remote)
        self.assertEqual(remote.latest_inputs(), [None, None])
        self.assertEqual(self.scope()["pending_batch"]["batch_id"], batch)
        self.assertEqual(self.scope()["last_seen_checkpoint"], "cp-2")
        self.assertTrue(self.events("state_recovered"))

    # [IR-17]
    def test_ir17_record_attempt_and_silence_only_exact_hook_session_id(self):
        remote = FakeRemote([receipt("cp-1", new=1)])
        self.run_hook(remote, "session-a")
        hint = self.hint()
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            first = hook.command_record(hint["record_id"], "attempted", "提醒一条消息")
            again = hook.command_record(hint["record_id"], "attempted", "提醒一条消息")
            silence = hook.command_silence("on")
        self.assertFalse(first["already_recorded"])
        self.assertTrue(again["already_recorded"])
        self.assertEqual(first["record_id"], again["record_id"])
        self.assertEqual(len(self.events("reminder_attempted")), 1)
        attempt = self.events("reminder_attempted")[0]
        self.assertEqual(attempt["session_ref"], hook._ref("session-a"))
        self.assertEqual(attempt["data"]["batch_id"], hint["data"]["batch_id"])
        self.assertTrue(attempt["record_id"] and attempt["at"])
        self.assertEqual(silence["session_ref"], hook._ref("session-a"))
        self.assertTrue(self.state()["sessions"][hook._ref("session-a")]["silent"])
        self.assertNotIn("session-a", (self.data / hook.STATE_FILENAME).read_text())

    # [IR-18]
    def test_ir18_missing_or_mismatched_command_session_fails_without_writes(self):
        remote = FakeRemote([receipt("cp-1", new=1)])
        self.run_hook(remote)
        hint_id = self.hint()["record_id"]
        before = self.journal()
        for sid in (None, "other-session"):
            with self.subTest(sid=sid):
                env = dict(os.environ)
                env.pop("CODEX_SESSION_ID", None)
                if sid:
                    env["CODEX_SESSION_ID"] = sid
                with patch.dict(os.environ, env, clear=True):
                    commands = (("record", "--hint-id", hint_id, "--outcome", "attempted", "--summary", "x"),
                                ("session-silence", "--value", "on"))
                    for command in commands:
                        process = subprocess.run(
                            [sys.executable, str(HOOK_DIR / "hirey_inbox_reminder.py"), *command],
                            text=True, capture_output=True, env=env, timeout=10)
                        self.assertEqual(process.returncode, 1, process.stderr)
                        self.assertEqual(json.loads(process.stdout)["ok"], False)
                    for action in (lambda: hook.command_record(hint_id, "attempted", "x"),
                                   lambda: hook.command_silence("on")):
                        with self.assertRaises(hook.LocalError):
                            action()
                self.assertEqual(self.journal(), before)
        self.assertFalse(self.state()["sessions"][hook._ref("session-a")]["silent"])

    # [IR-23]
    def test_ir23_cold_hint_records_once_without_refreshing_hint_time(self):
        remote = FakeRemote([receipt("cp-1", historical=3)])
        self.assertTrue(self.run_hook(remote))
        hint = self.hint()
        self.assertIsNone(hint["data"]["batch_id"])
        offered_at = self.scope()["last_any_hint_at"]
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            first = hook.command_record(hint["record_id"], "attempted", "历史未读三条")
            again = hook.command_record(hint["record_id"], "attempted", "历史未读三条")
        self.assertEqual(first["record_id"], again["record_id"])
        self.assertTrue(again["already_recorded"])
        self.assertEqual(len(self.events("reminder_attempted")), 1)
        self.assertIsNone(self.events("reminder_attempted")[0]["data"]["batch_id"])
        self.assertEqual(self.scope()["last_any_hint_at"], offered_at)

    # [IR-24]
    def test_ir24_unwritable_journal_preflight_stays_silent_without_remote(self):
        remote = FakeRemote([receipt("cp-1", new=1)])
        with patch.object(hook, "_preflight", side_effect=hook.LocalError("local_write", "unwritable_local_file")):
            self.assertEqual(self.run_hook(remote), {})
        self.assertEqual(remote.calls, [])
        self.assertEqual(self.events("check_started"), [])
        self.assertEqual(self.state()["scopes"], {})

    # [IR-25]
    def test_ir25_journal_restores_session_silence_after_state_loss(self):
        remote = FakeRemote([receipt("cp-1")])
        self.run_hook(remote, "session-a")
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            hook.command_silence("on")
        (self.data / hook.STATE_FILENAME).write_text("{incomplete", encoding="utf-8")
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote, "session-a"), {})
        self.assertEqual(len(remote.latest_inputs()), 1)
        self.assertTrue(self.state()["sessions"][hook._ref("session-a")]["silent"])
        self.assertTrue(self.events("state_recovered"))
        self.assertFalse(self.state()["sessions"].get(hook._ref("session-b"), {"silent": False})["silent"])

    # [IR-26]
    def test_ir26_existing_config_and_old_state_do_not_supply_checkpoint(self):
        self.config(enabled=False, min_seconds="600")
        (self.data / "inbox_reminder_state.json").write_text(json.dumps({"schema": "old", "checkpoint": "stale"}))
        remote = FakeRemote([receipt("cp-1"), receipt("cp-2")])
        self.assertEqual(self.run_hook(remote), {})
        self.assertEqual(remote.calls, [])
        with patch.dict(os.environ, HIREY_CODEX_INBOX_REMINDER="on"):
            self.run_hook(remote)
            self.clock.advance(301)
            self.run_hook(remote)
            self.assertEqual(remote.latest_inputs(), [None])
            self.clock.advance(300)
            self.run_hook(remote)
        self.assertEqual(remote.latest_inputs(), [None, "cp-1"])
        self.assertEqual(json.loads((self.data / "inbox_reminder_state.json").read_text())["checkpoint"], "stale")

    # [IR-27]
    def test_ir27_invalid_new_config_fields_fail_silently_before_remote(self):
        for config in ({"hot_repeat_seconds": "60"}, {"max_hot_hints": 11}):
            with self.subTest(config=config):
                self.config(**config)
                remote = FakeRemote([receipt("cp-1", new=1)])
                self.assertEqual(self.run_hook(remote), {})
                self.assertEqual(remote.calls, [])
                failed = self.events("check_failed")[-1]
                self.assertEqual(failed["data"]["stage"], "config")
                self.assertEqual(self.events("check_started"), [])

    # [IR-28]
    def test_ir28_rotation_restores_checkpoint_batch_silence_and_archived_hint(self):
        remote = FakeRemote([receipt("cp-1", new=1), receipt("cp-2")])
        self.assertTrue(self.run_hook(remote, "session-a"))
        hint_id = self.hint()["record_id"]
        batch_id = self.scope()["pending_batch"]["batch_id"]
        self.assertEqual(self.run_hook(remote, "session-b"), {})
        path = self.pad_journal_past_recent_window()
        with patch.object(hook, "ROTATE_JOURNAL_BYTES", path.stat().st_size):
            with patch.dict(os.environ, CODEX_SESSION_ID="session-b"):
                self.assertTrue(hook.command_silence("on")["silent"])

        archives = list(self.data.glob("inbox_reminder_journal.*.jsonl"))
        self.assertEqual(len(archives), 1)
        if os.name != 'nt':
            self.assertEqual(archives[0].stat().st_mode & 0o777, 0o600)
        archived = [json.loads(line) for line in archives[0].read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(record["record_id"] == hint_id for record in archived))
        active = self.journal()
        self.assertEqual(active[0]["event"], "state_snapshot")
        self.assertFalse(any(record["record_id"] == hint_id for record in active[0]["data"]["recent_records"]))
        self.assertEqual(active[0]["data"]["state"]["scopes"], self.state()["scopes"])

        (self.data / hook.STATE_FILENAME).write_text("{incomplete", encoding="utf-8")
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote, "session-b"), {})
        self.assertEqual(remote.latest_inputs(), [None])
        self.assertTrue(self.state()["sessions"][hook._ref("session-b")]["silent"])
        self.assertEqual(self.scope()["pending_batch"]["batch_id"], batch_id)
        self.assertEqual(self.scope()["last_seen_checkpoint"], "cp-1")
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            result = hook.command_record(hint_id, "attempted", "一条新增消息")
        self.assertFalse(result["already_recorded"])
        self.assertIsNone(self.scope()["pending_batch"])
        self.assertEqual(self.run_hook(remote, "session-a"), {})
        self.assertEqual(remote.latest_inputs(), [None, "cp-1"])

    # [IR-28]
    def test_ir28_archived_result_retry_does_not_append_attempt(self):
        remote = FakeRemote([receipt("cp-1", new=1)])
        self.run_hook(remote, "session-a")
        hint_id = self.hint()["record_id"]
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            first = hook.command_record(hint_id, "attempted", "一条新增消息")
        self.run_hook(remote, "session-b")
        path = self.pad_journal_past_recent_window()
        with patch.object(hook, "ROTATE_JOURNAL_BYTES", path.stat().st_size):
            with patch.dict(os.environ, CODEX_SESSION_ID="session-b"):
                hook.command_silence("on")
        before = path.read_bytes()
        with patch.dict(os.environ, CODEX_SESSION_ID="session-a"):
            retry = hook.command_record(hint_id, "attempted", "一条新增消息")
        self.assertEqual(retry, {"ok": True, "record_id": first["record_id"], "already_recorded": True})
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(any(record["event"] == "reminder_attempted" for record in self.journal()[1:]))

    # [IR-28]
    def test_ir28_archive_failure_preserves_journal_and_skips_network(self):
        remote = FakeRemote([receipt("cp-1", new=1), receipt("cp-2", new=1)])
        self.run_hook(remote)
        path = self.pad_journal_past_recent_window()
        before = path.read_bytes()
        self.clock.advance(301)
        with patch.object(hook, "ROTATE_JOURNAL_BYTES", path.stat().st_size):
            with patch.object(hook, "_write_new_file", side_effect=OSError("disk full")):
                self.assertEqual(self.run_hook(remote), {})
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(self.data.glob("inbox_reminder_journal.*.jsonl")), [])
        self.assertEqual(remote.latest_inputs(), [None])
        self.assertEqual(self.scope()["last_seen_checkpoint"], "cp-1")

    # [IR-28]
    def test_ir28_windows_rotation_never_opens_directory_as_a_file(self):
        remote = FakeRemote([receipt("cp-1", new=1), receipt("cp-2")])
        self.run_hook(remote)
        path = self.pad_journal_past_recent_window()
        original_open = os.open
        binary_flag = 0x8000
        binary_paths = set()

        def windows_open(target, flags, *args, **kwargs):
            if os.fspath(target) == str(self.data) and flags == os.O_RDONLY:
                raise PermissionError("Windows cannot open a directory for fsync")
            name = os.path.basename(os.fspath(target))
            if name == hook.JOURNAL_FILENAME or name.startswith(("inbox_reminder_journal.", ".inbox-journal-")):
                self.assertTrue(flags & binary_flag, name)
                binary_paths.add(name)
            return original_open(target, flags & ~binary_flag, *args, **kwargs)

        with patch.object(hook, "ROTATE_JOURNAL_BYTES", path.stat().st_size):
            with patch.object(hook.os, "name", "nt"):
                with patch.object(hook, "BINARY_OPEN_FLAG", binary_flag):
                    with patch.object(hook.os, "open", side_effect=windows_open):
                        hook._append(path, "user_activity", None, hook._ref("session-a"),
                                     {"hook_event": "UserPromptSubmit", "pending_batch_id": None})

        self.assertEqual(len(list(self.data.glob("inbox_reminder_journal.*.jsonl"))), 1)
        self.assertIn(hook.JOURNAL_FILENAME, binary_paths)
        self.assertTrue(any(name.startswith("inbox_reminder_journal.") for name in binary_paths))
        self.assertTrue(any(name.startswith(".inbox-journal-") for name in binary_paths))
        self.assertEqual(self.journal()[0]["event"], "state_snapshot")
        self.clock.advance(301)
        self.assertEqual(self.run_hook(remote), {})
        self.assertEqual(remote.latest_inputs(), [None, None])
        self.assertEqual(self.scope()["last_seen_checkpoint"], "cp-2")

    def test_installed_plugin_hook_and_commands_share_plugin_data_without_command_env(self):
        installed, data_dir = self.installed_hook()
        remote = FakeRemote([receipt("cp-1", new=1)])
        with patch.dict(os.environ, PLUGIN_DATA=str(data_dir)):
            result = installed.run(self.event(), client_factory=remote,
                                   installation=lambda: "installation-one")
        self.assertIn("hookSpecificOutput", result)
        state_path = data_dir / installed.STATE_FILENAME
        journal_path = data_dir / installed.JOURNAL_FILENAME
        state_after_hook = json.loads(state_path.read_text(encoding="utf-8"))
        journal_after_hook = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertIn(installed._ref("session-a"), state_after_hook["sessions"])
        hint = next(record for record in journal_after_hook if record["event"] == "hint_offered")

        with patch.dict(os.environ, {"CODEX_SESSION_ID": "session-a"}, clear=True):
            attempted = installed.command_record(hint["record_id"], "attempted", "提醒一条消息")
            silenced = installed.command_silence("on")

        self.assertFalse(attempted["already_recorded"])
        self.assertTrue(silenced["silent"])
        state_after_commands = json.loads(state_path.read_text(encoding="utf-8"))
        journal_after_commands = [json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()]
        self.assertTrue(state_after_commands["sessions"][installed._ref("session-a")]["silent"])
        self.assertIsNone(next(iter(state_after_commands["scopes"].values()))["pending_batch"])
        self.assertEqual([record["event"] for record in journal_after_commands].count("reminder_attempted"), 1)
        self.assertEqual([record["event"] for record in journal_after_commands].count("session_silence_changed"), 1)
        self.assertEqual(remote.latest_inputs(), [None])

    def test_installed_plugin_rejects_mismatched_plugin_data_before_remote(self):
        installed, data_dir = self.installed_hook()
        mismatched_data = Path(self.tmp.name) / "wrong-data"
        remote = FakeRemote([receipt("cp-1", new=1)])
        with patch.dict(os.environ, PLUGIN_DATA=str(mismatched_data)):
            self.assertEqual(installed.run(self.event(), client_factory=remote,
                                           installation=lambda: "installation-one"), {})
        self.assertEqual(remote.calls, [])
        self.assertFalse(data_dir.exists())
        self.assertFalse(mismatched_data.exists())


if __name__ == "__main__":
    unittest.main()
