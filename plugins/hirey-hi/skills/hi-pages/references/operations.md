# Batch operation guide

These are current Core operation shapes. The live catalog/`describe` remains authoritative; do
not infer new operations from this guide. All business calls go through `workspace_workflows` as
`{action, payload}`. The helper consumes the parsed Core wrapper from MCP `structuredContent`
(or the identical parsed JSON text), not the outer MCP content array. Inspect `ok` before use.

## Source enumeration

| Action | Payload and continuation | Meaning |
|---|---|---|
| `people.snapshot.private` | `{limit: 100}`, then returned `next_cursor` as `cursor` | Personal Workspace people; not proof of who captured them. Preserve returned `person_id` and source refs. |
| `capture.list` | `{limit: 100}`, then `next_cursor` as `cursor` | Current Person's Captures. Use `capture.get` for the exact receipt and `moment_id`; neither is a raw-text export. |
| `moment.list` | `{limit: 100}`, then last returned `moment_id` as `after_moment_id` | May include authorized records by other people. Check `captured_by_person_id` before selecting "mine". |
| `people.detail` | `{subject_person_id}`; continue history with last returned `moment_id` as `history_after_moment_id` | Authorized target detail and recorded context; preserve the same Person across pages. |
| `diary.snapshot` | `{source_namespace:"hirey.met.indexeddb.v1",after_cursor:0,limit:500}` | Existing Who I Met personal Diary source; do not invent another namespace. |

For Diary pagination, freeze the first returned `watermark` as `through_cursor`, pass the
returned `next_cursor` as `after_cursor` until `has_more` is false, and honor tombstones. Read
canonical Person/Moment refs and retained source revisions; a source locator is not a Person ID.
If a user's records came from a different source, inspect its actual supported source contract
instead of treating no results here as an empty network.

## Local batch and scoped commands

Use Python 3 and resolve `scripts/page_batch.py` relative to this skill. No dependency install,
network, token or model is required. Use absolute paths and normal file-writing tools for JSON;
do not interpolate private content into shell commands. `--help` is the executable CLI contract.

Initial input (values are illustrative; replace them with verified tool results):

```json
{
  "scope": {"person_id":"per_connector", "workspace_id":"wsp_personal"},
  "targets": [
    {"subject_person_id":"per_subject", "source_refs":["mom_saved", "source_namespace:source_ref:revision"]}
  ]
}
```

`source_refs` are traceability only. The Agent checks authorship and current source access before
initialization; the local helper cannot authenticate these values or grant access.

```text
python <helper> init --state <batch.json> --input <selection.json>
python <helper> status --state <batch.json> --person-id <current-person> --workspace-id <current-workspace>
python <helper> prepare --state <batch.json> --person-id <current-person> --workspace-id <current-workspace> --subject-person-id <target> --operation page.create --payload <payload.json>
python <helper> record --state <batch.json> --person-id <current-person> --workspace-id <current-workspace> --action-id <returned-action> --receipt <core-receipt.json>
python <helper> uncertain --state <batch.json> --person-id <current-person> --workspace-id <current-workspace> --action-id <returned-action>
python <helper> verify --state <batch.json> --person-id <current-person> --workspace-id <current-workspace> --subject-person-id <target> --readback <page-mine-receipt.json>
```

`prepare` returns `request:{action,payload}` including a persisted idempotency key. Invoke that
request unchanged via MCP. `record` records the actual response; it never submits anything.
Use the stored action/request after interruption. The helper's `status` is an execution report,
not permission or fresh server truth. Use one writer per batch; another client resumes after
the previous one stops. Keep temporary payload/readback files in the same private working area.
If a call might have been sent but no response was retained, call `uncertain` before replay.
This stores a local observation, not a Core receipt. A later rejection cannot clear that earlier
uncertainty; recover the original successful receipt with its original caller and request/key.

## Acquire, research and save

Payload before `prepare` adds the key:

```json
{"subject_person_id":"per_subject","content":{"display_name":"Recorded name"}}
```

`page.create` returns the caller-created draft, even when it already existed. Capture its exact
`page_draft_id`, `workspace_id`, `subject_person_id`, `revision` and `content_sha256`. Then read
`page.mine` (empty payload) and verify the acquired ID. Other editable drafts are not automatically
this Connector's draft. Existing fields must remain untouched by acquisition.

Call `enrichment.list` with `{subject_person_id}` before considering a new request. An exact URL
request has `{subject_person_id,linkedin_url}`. Otherwise use:

```json
{"subject_person_id":"per_subject","query":{"name":"Recorded name","anchors":["Actual company or context"]}}
```

The response returns `enrichment_request_id` and `status:"queued"`, not usable research. Track
that request through `enrichment.list`; statuses include queued, processing, ready, partial,
failed, dead_letter and cancelled. Preserve `failure_code`/`retry_after`. Candidates retain
source refs, provider and decision; use `decided_value_json` for edited candidates and exclude
left-out candidates. Failed or sparse research does not justify guessing.

For `page.draft`, use the latest verified revision and the FULL merged content:

```json
{
  "page_draft_id":"pag_acquired",
  "if_revision":1,
  "content":{
    "display_name":"Recorded name",
    "headline":"Evidence-supported role",
    "bio_markdown":"A factual, source-attributed introduction."
  }
}
```

This example is suitable only for a blank draft. Existing drafts may contain photos, links and
other fields: merge into the whole returned `content_json`, never reduce it to these three
fields. A write receipt has `revision` and `content_sha256`; fresh `page.mine` must match them
as `current_revision` and `content_sha256` for the exact subject and Workspace. A successful
write alone is not a verified usable preview. Read the content for usefulness and factual support.

Core Work cannot hold this manifest: `goal` and `definition_of_done` are bounded strings and
generic transitions do not append arbitrary per-person receipts. Do not stringify a growing
manifest into Work or invent `goal_refs`, `artifact_refs` or a batch API.
