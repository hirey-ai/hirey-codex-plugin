"""Synthetic process-level tests for the offline private Page request index."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
HELPER = Path(os.environ.get("HIREY_PAGE_BATCH_TEST_HELPER") or
              Path(__file__).resolve().parents[1] / "plugins/hirey-hi/skills/hi-pages/scripts/page_batch.py")
SCOPE = {"person_id": "per_owner", "workspace_id": "wsp_team"}
BASE = {"name": "Ada Example"}
DRAFT = {"name": "Ada Example", "about": "Synthetic"}


def core_hash(content):
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def envelope(operation, result=None, **extras):
    return {"ok": True, "contract": "hirey.core.workspace.receipt.v1",
            "operation": operation, "result": result, **extras}


class PageBatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.state = self.root / "private" / "batch.json"
        self.counter = 0
        manifest = {"scope": SCOPE, "targets": [
            {"subject_person_id": "per_ada1", "source_refs": ["moment:mom_1", "diary:old/2026/1"]},
            {"subject_person_id": "per_ada2", "source_refs": ["moment:mom_2"]}]}
        self.run_cli("init", "--input", self.file(manifest))

    def tearDown(self):
        self.temp.cleanup()

    def file(self, value):
        self.counter += 1
        path = self.root / (str(self.counter) + ".json")
        path.write_text(json.dumps(value), encoding="utf-8")
        return str(path)

    def command(self, command, *args, scope=None):
        output = [sys.executable, str(HELPER), command, "--state", str(self.state)]
        if command != "init":
            scope = scope or SCOPE
            output.extend(["--person-id", scope["person_id"], "--workspace-id", scope["workspace_id"]])
        return output + list(args)

    def run_cli(self, command, *args, error=None, scope=None):
        process = subprocess.run(self.command(command, *args, scope=scope), text=True,
                                 encoding="utf-8", capture_output=True, timeout=10)
        result = json.loads(process.stdout)
        self.assertEqual(process.returncode, 2 if error else 0, process.stdout + process.stderr)
        if error:
            self.assertEqual(result["error"], error)
        return result

    def prepare(self, operation, payload, subject="per_ada1", **kwargs):
        return self.run_cli("prepare", "--subject-person-id", subject, "--operation", operation,
                            "--payload", self.file(payload), **kwargs)

    def record(self, action, receipt, **kwargs):
        return self.run_cli("record", "--action-id", action["action_id"], "--receipt",
                            self.file(receipt), **kwargs)

    def verify(self, page, subject="per_ada1", **kwargs):
        return self.run_cli("verify", "--subject-person-id", subject, "--readback",
                            self.file(envelope("page.mine", [page])), **kwargs)

    def page(self, subject="per_ada1", page_id="pag_1", rev=1, content=None, **extra):
        content = content if content is not None else (DRAFT if rev == 2 else BASE)
        return {"subject_person_id": subject, "workspace_id": "wsp_team", "page_draft_id": page_id,
                "current_revision": rev, "content_sha256": core_hash(content),
                "content_json": content, **extra}

    def acquired(self, subject="per_ada1", page_id="pag_1"):
        action = self.prepare("page.create", {"subject_person_id": subject, "content": {}}, subject)
        receipt = {"subject_person_id": subject, "workspace_id": "wsp_team", "page_draft_id": page_id,
                   "revision": 1, "content_sha256": core_hash(BASE), "status": "draft"}
        self.record(action, envelope("page.create", receipt))
        observed = self.verify(self.page(subject, page_id), subject)
        self.assertEqual(observed["status"], "observed")
        self.assertFalse(observed["saved"])
        return action

    def draft(self):
        self.acquired()
        action = self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1,
                                           "content": DRAFT})
        result = {"workspace_id": "wsp_team", "page_draft_id": "pag_1", "revision": 2,
                  "content_sha256": core_hash(DRAFT)}
        return action, result

    def test_init_cannot_overwrite_or_import_arbitrary_page_binding(self):
        original = self.state.read_bytes()
        self.run_cli("init", "--input", self.file({}), error="batch_already_exists")
        self.assertEqual(self.state.read_bytes(), original)
        manifest = {"scope": SCOPE, "targets": [{"subject_person_id": "per_ada1",
                    "source_refs": ["mom_1"], "page_draft_id": "pag_somebody_else"}]}
        other = self.root / "other.json"
        process = subprocess.run([sys.executable, str(HELPER), "init", "--state", str(other),
                                  "--input", self.file(manifest)], capture_output=True, text=True)
        self.assertEqual(json.loads(process.stdout)["error"], "invalid_target")
        self.assertFalse(other.exists())

    def test_cross_scope_and_target_refusal_leave_state_unchanged(self):
        original = self.state.read_bytes()
        for scope in ({**SCOPE, "person_id": "per_other"}, {**SCOPE, "workspace_id": "wsp_other"}):
            self.run_cli("status", scope=scope, error="scope_mismatch")
        self.prepare("page.create", {"subject_person_id": "per_outside", "content": {}},
                     error="subject_mismatch")
        self.prepare("page.create", {"subject_person_id": "per_outside", "content": {}},
                     subject="per_outside", error="subject_not_in_batch")
        self.assertEqual(self.state.read_bytes(), original)

    def test_same_named_people_keep_distinct_page_bindings_and_failure_is_local(self):
        self.acquired()
        second = self.prepare("page.create", {"subject_person_id": "per_ada2", "content": {}}, "per_ada2")
        self.record(second, envelope("page.create", ok=False, error={"code": "forbidden"}))
        rows = self.run_cli("status")["targets"]
        self.assertEqual([row["status"] for row in rows], ["observed", "failed"])
        self.assertEqual([row["page_draft_id"] for row in rows], ["pag_1", None])
        self.record(second, envelope("page.create", {"subject_person_id": "per_ada2",
                    "workspace_id": "wsp_team", "page_draft_id": "pag_1", "revision": 1,
                    "status": "draft", "content_sha256": "a" * 64}), error="page_bound_to_other_subject")
        self.acquired("per_ada2", "pag_2")
        rows = self.run_cli("status")["targets"]
        self.assertEqual([row["page_draft_id"] for row in rows], ["pag_1", "pag_2"])

    def test_prepared_request_survives_lost_stdout_and_reuses_exact_key(self):
        payload = {"subject_person_id": "per_ada1", "content": {"name": "Ada Example"}}
        args = ("--subject-person-id", "per_ada1", "--operation", "page.create", "--payload", self.file(payload))
        process = subprocess.run(self.command("prepare", *args), stdout=subprocess.DEVNULL,
                                 stderr=subprocess.PIPE, timeout=10)
        self.assertEqual(process.returncode, 0)
        persisted = json.loads(self.state.read_text())["actions"][0]
        replay = self.prepare("page.create", payload)
        self.assertTrue(replay["replay"])
        self.assertEqual(replay["request"], persisted["request"])
        self.assertEqual(len(json.loads(self.state.read_text())["actions"]), 1)
        self.prepare("page.create", {**payload, "content": {"name": "Changed"}}, error="page_request_unresolved")

    def test_changed_payload_gets_another_key_after_definite_core_failure(self):
        first = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        self.record(first, envelope("page.create", ok=False, error={"code": "invalid_content"}))
        second = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {"name": "Ada"}})
        self.assertNotEqual(first["request"]["payload"]["idempotency_key"],
                            second["request"]["payload"]["idempotency_key"])
        self.assertEqual(len(json.loads(self.state.read_text())["actions"]), 2)

    def test_failure_wrong_operation_scope_subject_and_page_never_succeed(self):
        action, result = self.draft()
        for receipt, error in [
            (envelope("page.create", result), "invalid_receipt_envelope"),
            (envelope("page.draft", {**result, "workspace_id": "wsp_other"}), "scope_mismatch"),
            (envelope("page.draft", {**result, "subject_person_id": "per_ada2"}), "subject_mismatch"),
            (envelope("page.draft", {**result, "page_draft_id": "pag_other"}), "page_mismatch"),
            (envelope("page.draft", {**result, "revision": 1}), "receipt_revision_mismatch"),
        ]:
            self.record(action, receipt, error=error)
        failure = self.record(action, envelope("page.draft", result, ok=False, error={"code": "forbidden"}))
        self.assertEqual(failure["status"], "failed")
        self.assertFalse(failure["saved"])
        self.assertEqual(self.run_cli("status")["targets"][0]["status"], "failed")

    def test_core_historical_revision_gap_requires_forward_revision_and_exact_readback(self):
        create = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        self.record(create, envelope("page.create", {"subject_person_id": "per_ada1",
                    "workspace_id": "wsp_team", "page_draft_id": "pag_1", "revision": 2,
                    "content_sha256": core_hash(BASE), "status": "draft"}))
        self.verify(self.page(rev=2, content=BASE))
        draft = self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 2,
                                           "content": DRAFT})
        result = {"workspace_id": "wsp_team", "page_draft_id": "pag_1",
                  "content_sha256": core_hash(DRAFT)}
        for non_forward_revision in (2, 1):
            self.record(draft, envelope("page.draft", {**result, "revision": non_forward_revision}),
                        error="receipt_revision_mismatch")
        # Core can return 6 when old history occupies 3..5, even if current is 2.
        self.record(draft, envelope("page.draft", {**result, "revision": 6}))
        self.assertFalse(self.verify(self.page(rev=3, content=DRAFT))["saved"])
        saved = self.verify(self.page(rev=6, content=DRAFT))
        self.assertTrue(saved["saved"])
        self.assertEqual(saved["revision"], 6)

    def test_core_page_cas_conflict_recovers_after_fresh_readback(self):
        draft, _ = self.draft()
        # Actual Platform MCP envelope: Core's human-readable detail is absent.
        conflict = {"ok": False, "code": 409, "error_code": "conflict", "error": "conflict"}
        self.assertEqual(self.record(draft, conflict)["status"], "failed")
        self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1,
                     "content": {**DRAFT, "headline": "New role"}}, error="readback_revision_required")
        human_content = {**BASE, "about": "Newer human edit"}
        self.assertEqual(self.verify(self.page(rev=2, content=human_content))["status"], "changed")
        merged = {**human_content, "headline": "New role"}
        next_draft = self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 2,
                                                "content": merged})
        self.assertNotEqual(draft["action_id"], next_draft["action_id"])
        self.record(next_draft, envelope("page.draft", {"workspace_id": "wsp_team",
                    "page_draft_id": "pag_1", "revision": 3, "content_sha256": core_hash(merged)}))
        self.assertTrue(self.verify(self.page(rev=3, content=merged))["saved"])

    def test_core_invalid_request_is_definite_but_generic_conflicts_and_5xx_are_not(self):
        draft, _ = self.draft()
        for code, error_code in ((409, "idempotency_key_payload_conflict"),
                                 (409, "idempotency_key_not_reusable"),
                                 (500, "invalid_request"), (503, "conflict")):
            response = {"ok": False, "code": code, "error_code": error_code, "error": error_code}
            self.assertEqual(self.record(draft, response)["status"], "unknown")
            self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1,
                         "content": {**DRAFT, "headline": "Another write"}}, error="page_request_unresolved")
        validation = {"ok": False, "code": 400, "error_code": "invalid_request", "error": "invalid_request"}
        self.assertEqual(self.record(draft, validation)["status"], "unknown")
        self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1,
                     "content": {**DRAFT, "headline": "Corrected write"}}, error="page_request_unresolved")
        enrichment = self.prepare("enrichment.request", {"subject_person_id": "per_ada2",
                                  "query": {"name": "Ada", "anchors": ["Synthetic Labs"]}}, "per_ada2")
        generic = {"ok": False, "code": 409, "error_code": "conflict", "error": "conflict"}
        self.assertEqual(self.record(enrichment, generic)["status"], "unknown")
        self.prepare("enrichment.request", {"subject_person_id": "per_ada2",
                     "query": {"name": "Ada", "anchors": ["Other Labs"]}}, "per_ada2",
                     error="enrichment_request_unresolved")

    def test_only_exact_readback_saves_and_newer_revision_cannot_be_overwritten_by_old_snapshot(self):
        action, result = self.draft()
        self.record(action, envelope("page.draft", result))
        self.assertEqual(self.run_cli("status")["targets"][0]["status"], "received")
        self.assertEqual(self.verify(self.page())["status"], "stale")
        self.assertEqual(self.verify(self.page(rev=2, content={"name": "Another write"}))["status"], "mismatch")
        saved = self.verify(self.page(rev=2))
        self.assertTrue(saved["saved"])
        self.assertIn("page_draft_id=pag_1&subject_person_id=per_ada1#me", saved["preview_url"])
        self.assertEqual(self.verify(self.page(rev=3))["status"], "changed")
        old = self.verify(self.page(rev=2))
        self.assertEqual(old["status"], "stale")
        self.assertFalse(old["saved"])
        self.assertEqual(json.loads(self.state.read_text())["targets"]["per_ada1"]["observation"]["revision"], 3)
        self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1, "content": {}},
                     error="readback_revision_required")

    def test_wrong_readback_page_scope_or_subject_refused(self):
        self.acquired()
        self.verify(self.page(page_id="pag_other"), error="page_readback_missing_or_ambiguous")
        self.verify(self.page(workspace_id="wsp_other"), error="scope_mismatch")
        self.verify(self.page(subject="per_ada2"), error="subject_mismatch")

    def test_enrichment_queued_never_means_page_saved(self):
        action = self.prepare("enrichment.request", {"subject_person_id": "per_ada1",
                              "query": {"name": "Ada Example", "anchors": ["Synthetic Labs"]}})
        receipt = envelope("enrichment.request", {"enrichment_request_id": "enr_test",
            "subject_person_id": "per_ada1", "status": "queued", "revision": 1,
            "providers": ["monid", "exa"], "privacy": "requester_private_until_explicit_page_publish"})
        self.assertFalse(self.record(action, receipt)["saved"])
        self.assertEqual(self.run_cli("status")["targets"][0]["status"], "not_started")

    def test_uncertain_enrichment_cannot_start_another_paid_request(self):
        payload = {"subject_person_id": "per_ada1", "query": {"name": "Ada", "anchors": ["Test"]}}
        action = self.prepare("enrichment.request", payload)
        changed = {**payload, "query": {"name": "Ada", "anchors": ["Different"]}}
        self.prepare("enrichment.request", changed, error="enrichment_request_unresolved")
        self.record(action, {"ok": False, "code": 504, "error_code": "timeout", "error": "Timed out"})
        self.prepare("enrichment.request", changed, error="enrichment_request_unresolved")
        self.assertEqual(self.prepare("enrichment.request", payload)["request"], action["request"])

    def test_real_hi_error_envelopes_preserve_unknown_requests(self):
        first = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        malformed = {"ok": False, "error": "Something broke"}
        self.record(first, malformed, error="invalid_failure_envelope")
        uncertain = {"ok": False, "code": 502, "error_code": "core_workspace_failed",
                     "error": "Core request failed", "data": {}}
        self.assertEqual(self.record(first, uncertain)["status"], "unknown")
        self.prepare("page.create", {"subject_person_id": "per_ada1", "content": BASE},
                     error="page_request_unresolved")
        replay = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        self.assertEqual(replay["request"], first["request"])
        definite = {"ok": False, "code": 422, "error_code": "invalid_operation_payload",
                    "error": "Invalid operation payload", "data": {}}
        self.assertEqual(self.record(first, definite)["status"], "unknown")
        self.prepare("page.create", {"subject_person_id": "per_ada1", "content": BASE},
                     error="page_request_unresolved")

    def test_unknown_history_survives_retry_refusals_until_original_success(self):
        payload = {"subject_person_id": "per_ada1", "query": {"name": "Ada", "anchors": ["Test"]}}
        first = self.prepare("enrichment.request", payload)
        self.record(first, {"ok": False, "code": 504, "error_code": "timeout", "error": "Timed out"})
        changed = {**payload, "query": {"name": "Ada", "anchors": ["Different"]}}
        for code, error_code in ((400, "invalid_request"), (403, "forbidden")):
            self.assertEqual(self.prepare("enrichment.request", payload)["request"], first["request"])
            refusal = {"ok": False, "code": code, "error_code": error_code, "error": error_code}
            self.assertEqual(self.record(first, refusal)["status"], "unknown")
            self.prepare("enrichment.request", changed, error="enrichment_request_unresolved")
        success = envelope("enrichment.request", {"enrichment_request_id": "enr_test",
            "subject_person_id": "per_ada1", "status": "queued", "revision": 1,
            "providers": ["monid", "exa"], "privacy": "requester_private_until_explicit_page_publish"})
        self.assertEqual(self.record(first, success)["status"], "received")
        self.assertNotEqual(self.prepare("enrichment.request", changed)["action_id"], first["action_id"])

    def test_first_invalid_request_without_uncertainty_allows_corrected_request(self):
        first = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        refusal = {"ok": False, "code": 400, "error_code": "invalid_request", "error": "invalid_request"}
        self.assertEqual(self.record(first, refusal)["status"], "failed")
        changed = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": BASE})
        self.assertNotEqual(changed["action_id"], first["action_id"])

    def test_uncertain_cli_records_local_fact_without_forging_or_downgrading_receipts(self):
        first = self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}})
        original = self.state.read_bytes()
        self.run_cli("uncertain", "--action-id", first["action_id"],
                     scope={**SCOPE, "person_id": "per_other"}, error="scope_mismatch")
        self.assertEqual(self.state.read_bytes(), original)
        self.assertEqual(self.run_cli("uncertain", "--action-id", first["action_id"])["status"], "unknown")
        persisted = json.loads(self.state.read_text())["actions"][0]
        self.assertEqual(persisted["receipts"], [])
        self.assertEqual(persisted["request"], first["request"])
        self.assertIn("uncertain_since", persisted)
        refusal = {"ok": False, "code": 403, "error_code": "forbidden", "error": "forbidden"}
        self.assertEqual(self.record(first, refusal)["status"], "unknown")
        self.prepare("page.create", {"subject_person_id": "per_ada1", "content": BASE},
                     error="page_request_unresolved")
        receipt = envelope("page.create", {"subject_person_id": "per_ada1", "workspace_id": "wsp_team",
                   "page_draft_id": "pag_1", "revision": 1, "content_sha256": core_hash(BASE), "status": "draft"})
        self.record(first, receipt)
        self.verify(self.page())
        resolved = self.run_cli("uncertain", "--action-id", first["action_id"])
        self.assertEqual(resolved["status"], "verified")
        self.assertTrue(resolved["already_resolved"])
        self.assertEqual(json.loads(self.state.read_text())["actions"][0]["receipts"], [refusal, receipt])

    def test_requested_unicode_content_must_match_receipt_and_readback(self):
        self.acquired()
        content = {"name": "王海 🌲", "about": "Met at a synthetic event"}
        action = self.prepare("page.draft", {"page_draft_id": "pag_1", "if_revision": 1,
                                           "content": content})
        receipt = {"workspace_id": "wsp_team", "page_draft_id": "pag_1", "revision": 2,
                   "content_sha256": core_hash(BASE)}
        self.record(action, envelope("page.draft", receipt), error="receipt_content_mismatch")
        receipt["content_sha256"] = core_hash(content)
        self.record(action, envelope("page.draft", receipt))
        self.verify(self.page(rev=2, content=BASE, content_sha256=core_hash(content)),
                    error="readback_content_digest_mismatch")
        self.assertTrue(self.verify(self.page(rev=2, content=content))["saved"])

    def test_caller_cannot_inject_authority_or_idempotency(self):
        for extra in ({"idempotency_key": "reused"}, {"workspace_id": "wsp_other"}):
            self.prepare("page.create", {"subject_person_id": "per_ada1", "content": {}, **extra},
                         error="unexpected_payload_field")
        self.prepare("page.draft", {"page_draft_id": "pag_other", "if_revision": 1, "content": {}},
                     error="page_not_acquired")
        process = subprocess.run(self.command("prepare", "--subject-person-id", "per_ada1",
                    "--operation", "page.publish", "--payload", self.file({})), capture_output=True)
        self.assertEqual(process.returncode, 2)
        self.assertEqual(json.loads(self.state.read_text())["actions"], [])

    def test_single_writer_lock_and_crashed_writer_recovery(self):
        code = ("import importlib.util,sys,time;from pathlib import Path;"
                "s=importlib.util.spec_from_file_location('helper',sys.argv[1]);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
                "lock=m.writer_lock(Path(sys.argv[2]));lock.__enter__();"
                "print('locked',flush=True);time.sleep(30)")
        process = subprocess.Popen([sys.executable, "-c", code, str(HELPER), str(self.state)],
                                   text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertEqual(process.stdout.readline().strip(), "locked")
            self.run_cli("status", error="batch_in_use")
        finally:
            process.kill()
            process.communicate(timeout=5)
        self.assertEqual(self.run_cli("status")["execution"], "local_index_only")


if __name__ == "__main__":
    unittest.main()
