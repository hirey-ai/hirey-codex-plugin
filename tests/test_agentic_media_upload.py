import importlib.util
import hashlib
import json
import os
import subprocess
from pathlib import Path
import tempfile
import unittest
from unittest import mock


HELPER = (
    Path(__file__).resolve().parents[1]
    / "plugins/hirey-hi/skills/agentic-media/scripts/agentic_media_upload.py"
)
SPEC = importlib.util.spec_from_file_location("agentic_media_upload", HELPER)
media = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(media)


class AgenticMediaUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def video(self, name="source.mp4", size=40):
        path = self.root / name
        path.write_bytes(b"\x00\x00\x00\x18ftypisom" + b"0" * max(0, size - 12))
        return path

    def image(self, name="evidence.png"):
        path = self.root / name
        path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"image-evidence")
        return path

    def state(self, **changes):
        source = media.describe_local_source(self.video())
        values = media.new_session_state(
            source=source,
            intent="raw_source",
            person_id="person_A",
            session_binding_ref="session_binding_A",
            work_ref="work_1",
            upload_ref="upload_1",
            **changes,
        )
        return values

    def assert_code(self, code, call):
        with self.assertRaises(media.UploadError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_intent_is_exact(self):
        self.assertEqual(media.validate_intent("raw_source"), "raw_source")
        self.assertEqual(media.validate_intent("final_master"), "final_master")
        self.assertEqual(media.validate_intent("evidence"), "evidence")
        for invalid in ("raw", "final", "attachment", "RAW_SOURCE", "raw-source", ""):
            self.assert_code("invalid_intent", lambda value=invalid: media.validate_intent(value))

    def test_private_evidence_accepts_images_but_video_intents_do_not(self):
        path = self.image()
        result = media.preflight_local_file(
            path,
            intent="evidence",
            allowed_suffixes=[".png", ".mp4"],
            max_bytes=100,
            quota_remaining_bytes=100,
            part_size=16,
            max_parts=10,
        )
        self.assertEqual(result["mime_type"], "image/png")
        self.assertEqual(result["container"], "png")
        self.assert_code(
            "unsupported_media_format",
            lambda: media.preflight_local_file(
                path,
                intent="final_master",
                allowed_suffixes=[".png"],
                max_bytes=100,
                quota_remaining_bytes=100,
                part_size=16,
                max_parts=10,
            ),
        )
        disguised = self.root / "disguised.png"
        disguised.write_bytes(b"not-a-png")
        self.assert_code(
            "unsupported_image_format",
            lambda: media.preflight_local_file(
                disguised,
                intent="evidence",
                allowed_suffixes=[".png"],
                max_bytes=100,
                quota_remaining_bytes=100,
                part_size=16,
                max_parts=10,
            ),
        )

    def test_preflight_uses_live_limits_and_fails_closed(self):
        path = self.video()
        result = media.preflight_local_file(
            path,
            intent="final_master",
            allowed_suffixes=[".mp4"],
            max_bytes=100,
            quota_remaining_bytes=40,
            part_size=16,
            max_parts=3,
        )
        self.assertEqual(result["part_count"], 3)
        self.assertEqual(result["container"], "iso-bmff")
        self.assertEqual(result["content_sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
        unlimited = media.preflight_local_file(
            path,
            intent="final_master",
            allowed_suffixes=[".mp4"],
            max_bytes=100,
            quota_remaining_bytes=None,
            part_size=16,
            max_parts=3,
        )
        self.assertEqual(unlimited["total_size"], 40)
        self.assert_code(
            "quota_exceeded",
            lambda: media.preflight_local_file(
                path,
                intent="final_master",
                allowed_suffixes=[".mp4"],
                max_bytes=100,
                quota_remaining_bytes=0,
                part_size=16,
                max_parts=3,
            ),
        )
        self.assert_code(
            "contract_missing_formats",
            lambda: media.preflight_local_file(
                path,
                intent="final_master",
                allowed_suffixes=[],
                max_bytes=100,
                quota_remaining_bytes=100,
                part_size=16,
                max_parts=3,
            ),
        )

    def test_5_20_100_gib_plans_cover_every_byte(self):
        chunk = 64 * 1024 * 1024
        for gib in (5, 20, 100):
            total = gib * 1024 * 1024 * 1024
            plan = media.multipart_plan(total, chunk, 10000)
            self.assertEqual(sum(item["size"] for item in plan), total)
            self.assertEqual(plan[0]["offset"], 0)
            self.assertEqual(plan[-1]["offset"] + plan[-1]["size"], total)
            self.assertEqual(len(plan), gib * 16)

    def test_resume_is_server_completed_part_difference(self):
        plan = media.multipart_plan(40, 10)
        completed = media.normalize_completed(
            [{"part_number": 3, "etag": "e3"}, {"part_number": 1, "etag": "e1"}],
            len(plan),
        )
        self.assertEqual([item["part_number"] for item in media.missing_parts(plan, completed)], [2, 4])
        self.assert_code(
            "completed_part_conflict",
            lambda: media.normalize_completed(
                [{"part_number": 1, "etag": "a"}, {"part_number": 1, "etag": "b"}], 4
            ),
        )

    def test_session_state_rejects_authority_and_secret_material(self):
        state = self.state()
        for key, value in (
            ("user_id", "user_1"),
            ("access_token", "token"),
            ("ciphertext", "encrypted-secret"),
            ("presigned_url", "https://upload.example/part"),
            ("file_content", "video bytes"),
        ):
            unsafe = dict(state)
            unsafe[key] = value
            self.assert_code("unsafe_session_state", lambda value=unsafe: media.validate_session_state(value))

        unsafe_value = dict(state)
        unsafe_value["target_ref"] = "https://upload.example/part?X-Amz-Signature=secret"
        self.assert_code("unsafe_session_state", lambda: media.validate_session_state(unsafe_value))

    def test_state_file_is_private_and_contains_no_grants(self):
        path = self.root / "session.json"
        store = media.SessionStore(path)
        store.save(self.state())
        if os.name == "nt":
            env = dict(os.environ, TEST_STATE_PATH=str(path))
            env.pop("PSModulePath", None)
            acl = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command",
                 "[IO.File]::GetAccessControl($env:TEST_STATE_PATH).Sddl"],
                env=env, text=True,
            ).strip().split("D:", 1)[1]
            self.assertTrue(acl.startswith("P"), acl)
            self.assertEqual(acl[acl.index("("):], "(A;;FA;;;OW)")
        else:
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        saved = path.read_text()
        self.assertNotIn("token", saved.lower())
        self.assertNotIn("presign", saved.lower())
        self.assertNotIn("x-amz", saved.lower())

    def test_permission_failure_closes_file_and_preserves_previous_state(self):
        path = self.root / "session.json"
        store = media.SessionStore(path)
        state = self.state()
        store.save(state)
        previous = path.read_bytes()
        with mock.patch.object(media, "_protect_state_file", side_effect=OSError("permission failed")):
            with self.assertRaisesRegex(OSError, "permission failed"):
                store.save(state)
        self.assertEqual(path.read_bytes(), previous)
        self.assertEqual(list(self.root.glob("session.json.*")), [])
        store.save(state)
        self.assertEqual(store.load(), state)

    def test_replace_failure_preserves_state_and_removes_temporary_file(self):
        store = media.SessionStore(self.root / "session.json")
        state = self.state()
        store.save(state)
        with mock.patch.object(media.os, "replace", side_effect=OSError("replace failed")):
            with self.assertRaisesRegex(OSError, "replace failed"):
                store.save(state)
        self.assertEqual(store.load(), state)
        self.assertEqual(list(self.root.glob("session.json.*")), [])

    def test_live_contract_requires_describable_exact_operations(self):
        roles = {
            "work_create": "agentic_media.work.create",
            "upload_describe": "agentic_media.upload.describe",
            "upload_complete": "agentic_media.upload.complete",
            "upload_cancel": "agentic_media.upload.cancel",
            "work_status": "agentic_media.work.status",
            "release_freeze": "agentic_media.release.freeze",
            "publish": "agentic_media.publish",
        }
        catalog = {
            "operations": [
                {
                    "name": operation,
                    "description": "live %s" % role,
                    "input_schema": {"type": "object"},
                    "result_schema": {"type": "object"},
                    "write": role != "work_status",
                }
                for role, operation in roles.items()
            ]
        }
        inspected = media.inspect_live_contract(catalog, roles)
        self.assertEqual(set(inspected["roles"]), set(roles))
        preview_role = {"work_preview": "media.work_preview"}
        catalog["operations"].append({
            "name": "media.work_preview",
            "description": "describe private preview",
            "input_schema": {"type": "object"},
            "result_schema": {"type": "object"},
        })
        self.assertIn(
            "work_preview",
            media.inspect_live_contract(
                catalog, preview_role, required_roles={"work_preview"}
            )["roles"],
        )
        catalog["operations"][0].pop("result_schema")
        self.assert_code(
            "contract_not_describable",
            lambda: media.inspect_live_contract(catalog, roles),
        )

    def test_upload_sends_only_missing_parts_and_never_persists_grants(self):
        source_path = self.video(size=40)
        store = media.SessionStore(self.root / "upload-state.json")
        store.save(media.new_session_state(
            source=media.describe_local_source(source_path),
            intent="raw_source",
            person_id="person_A",
            session_binding_ref="session_binding_A",
            work_ref="work_1",
            upload_ref="upload_1",
        ))
        description = {
            "upload_ref": "upload_1",
            "total_size": 40,
            "part_size": 10,
            "max_parts": 10,
            "completed_parts": [
                {"part_number": 1, "etag": "server-e1"},
                {"part_number": 3, "etag": "server-e3"},
            ],
            "parts": [
                {
                    "part_number": 2,
                    "url": "https://upload.example/2?X-Amz-Signature=private-two",
                    "required_headers": {"authorization": "secret-two"},
                },
                {
                    "part_number": 4,
                    "url": "https://upload.example/4?X-Amz-Signature=private-four",
                    "required_headers": {"authorization": "secret-four"},
                },
            ],
        }
        calls = []
        events = []

        def fake_put(url, headers, body):
            calls.append((url, headers, body))
            return {"status": 200, "etag": "etag-%d" % len(calls)}

        result = media.upload_from_description(store, description, put=fake_put, emit=events.append)
        self.assertEqual([call[2] for call in calls], [source_path.read_bytes()[10:20], source_path.read_bytes()[30:40]])
        self.assertEqual([item["part_number"] for item in result["completed_parts"]], [1, 2, 3, 4])
        persisted = store.path.read_text()
        self.assertNotIn("private-two", persisted)
        self.assertNotIn("secret-two", persisted)
        self.assertEqual(events[-1]["percent"], 100.0)
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertEqual(result["last_event_seq"], 3)

    def test_live_core_description_aliases_and_local_self_host_upload_work(self):
        source_path = self.video(size=40)
        store = media.SessionStore(self.root / "live-state.json")
        store.save(media.new_session_state(
            source=media.describe_local_source(source_path), intent="final_master",
            person_id="person_A", work_ref="amw_1", upload_ref="amu_1",
        ))
        description = {
            "upload_session_id": "amu_1", "expected_size_bytes": 40,
            "part_size_bytes": 20, "completed_parts": [],
            "missing_parts": [
                {"part_number": 1, "offset_bytes": 0, "length_bytes": 20,
                 "url": "http://127.0.0.1:4012/v1/agentic-media/uploads/amu_1/parts/1",
                 "required_headers": {"x-hi-upload-capability": "transient-1"}},
                {"part_number": 2, "offset_bytes": 20, "length_bytes": 20,
                 "url": "http://localhost:4012/v1/agentic-media/uploads/amu_1/parts/2",
                 "required_headers": {"x-hi-upload-capability": "transient-2"}},
            ],
        }
        result = media.upload_from_description(
            store, description,
            put=lambda url, headers, body: {"status": 200, "etag": hashlib.sha256(body).hexdigest()},
            emit=lambda event: None,
        )
        self.assertEqual(result["status"], "uploading")
        saved = store.path.read_text()
        self.assertNotIn("transient-1", saved)
        self.assertNotIn("x-hi-upload-capability", saved)

    def test_pause_and_cancel_are_observed_between_parts(self):
        source_path = self.video(size=40)
        store = media.SessionStore(self.root / "paused-state.json")
        store.save(media.new_session_state(
            source=media.describe_local_source(source_path),
            intent="raw_source",
            person_id="person_A",
            session_binding_ref="session_binding_A",
            work_ref="work_1",
            upload_ref="upload_1",
        ))
        description = {
            "upload_ref": "upload_1",
            "total_size": 40,
            "part_size": 20,
            "max_parts": 10,
            "completed_parts": [],
            "parts": [
                {"part_number": 1, "url": "https://upload.example/1", "required_headers": {}},
                {"part_number": 2, "url": "https://upload.example/2", "required_headers": {}},
            ],
        }
        calls = []

        def pause_after_first(url, headers, body):
            calls.append(url)
            store.transition("paused")
            return {"status": 200, "etag": "etag-1"}

        result = media.upload_from_description(store, description, put=pause_after_first, emit=lambda event: None)
        self.assertEqual(result["status"], "paused")
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["completed_parts"], [{"part_number": 1, "etag": "etag-1"}])
        cancelled = store.transition("cancel_requested")
        self.assertEqual(cancelled["status"], "cancel_requested")

    def test_canonical_media_never_becomes_a_url_download(self):
        canonical = media.describe_canonical_source("media_123")
        self.assertEqual(canonical["kind"], "hirey_canonical_media")
        self.assert_code(
            "invalid_canonical_media_ref",
            lambda: media.describe_canonical_source("https://youtube.com/watch?v=123"),
        )
        store = media.SessionStore(self.root / "canonical.json")
        store.save(media.new_session_state(
            source=canonical,
            intent="final_master",
            person_id="person_A",
            session_binding_ref="session_binding_A",
        ))
        self.assert_code(
            "canonical_media_is_control_plane_copy",
            lambda: media.upload_from_description(store, {}),
        )


if __name__ == "__main__":
    unittest.main()
