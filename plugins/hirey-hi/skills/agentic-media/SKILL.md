---
name: agentic-media
description: Privately upload original image/video evidence, or let a user prepare and optionally publish a local video, through HiRey's resumable media path.
---

# Agentic Media

Use this Skill when the user gives Codex a local image/video that another Hi workflow must preserve,
or asks to continue an Agentic Media work.
This version covers only the first four product steps: receive, prepare, preview, and publish/share on
HiRey. Social-platform OAuth and posting are a later phase and must not be offered as available.

Start with `hi_agent_status({"client_plugin_host":"codex","client_plugin_version":"0.2.15"})`, then call
`workspace_workflows` with `action: catalog`. Read
[references/control-contract.md](references/control-contract.md) before moving bytes.

## One user-facing entry

The user should only need to hand the file to Codex. When another Skill, such as `hi-repair`, needs
the original only as private evidence, use `evidence` and do not ask an edit/publication question.
Otherwise resolve a local regular video, then ask one question only when necessary: should HiRey edit
it (`raw_source`) or preserve it exactly as the finished video (`final_master`)? Do not ask about
internal tables, workers, storage, MCP calls, part sizes, or cover generation.

Reject remote watch/share URLs and every scraping or URL-to-file workaround. A stable existing HiRey
media reference may move only through a live catalog-described Core copy operation.

## Require the live contract

Before acting, require these exact operations in the fresh catalog and call `describe` for each one
used by the requested flow:

- `agentic_media.work.create`
- `agentic_media.upload.describe`
- `agentic_media.upload.complete`
- `agentic_media.upload.cancel`
- `agentic_media.work.list`
- `agentic_media.work.status`
- `agentic_media.work.revise` for `raw_source` changes only
- `agentic_media.release.freeze`
- `agentic_media.publish`
- `agentic_media.withdraw` when requested

Stop with `contract_not_describable` if an operation, payload, result, confirmation requirement,
idempotency rule, or limit is missing. The live service is authoritative; this list is routing
guidance, not permission to invent a missing operation.

## Upload the local file

Require `agentic_media.work.create` describe to return `transport_policy` with
`accepted_suffixes_by_intent`, maximum bytes, default/minimum/maximum part sizes, maximum parts, and
explicit quota enforcement. Select only the suffix list for the exact requested intent; never let an
`evidence` image broaden `raw_source` or `final_master` video input. Run
`scripts/agentic_media_upload.py preflight` using those live values. When quota enforcement is
false use `--quota-unlimited`; when it is true require a numeric remaining-byte value and use
`--quota-remaining-bytes`. Stop with `contract_not_describable` if either policy is incomplete.
Compute the whole-file SHA-256 before `agentic_media.work.create`; send the filename, MIME type, byte
size, digest, title, exact intent, original task ref, and a stable idempotency key.

Save only stable work/upload refs plus safe local file identity in the helper's mode-0600 state file.
When the local state file is missing or the user asks to continue an earlier upload, call
`agentic_media.work.list` first and let the user identify the returned work by its safe title,
filename, state and progress. Never guess or reconstruct a work ID from a filename.
Call `agentic_media.upload.describe` immediately before uploading. Pass that fresh result to the
helper's `upload` command; its short-lived URLs and `x-hi-upload-capability` headers stay in memory
only. Never display, log, or persist them. On timeout, restart, or unknown part outcome, describe
again and upload only the server-reported missing parts.

After no parts are missing, call `agentic_media.upload.complete` with a stable idempotency key. Report
progress using only byte/part counts, percent, rate, ETA, work ref, upload ref, and state.

## Edit or preserve

For `raw_source`, the service enters the existing edit pipeline. Poll `agentic_media.work.status` and
show the returned private preview. A modification uses `agentic_media.work.revise` and creates a new
processing attempt; do not rebuild or imitate the editor locally.

For `final_master`, the uploaded video must remain the output video byte-for-byte. The service may
inspect it and derive a cover/playback presentation, but must return `no_edit_verified=true`. Never
call revise for this intent.

For `evidence`, the uploaded image/video remains private, skips editing and publication, and completes
at `preview_ready`. Return its canonical `media_asset_id` to the calling workflow. Do not call revise,
freeze, publish, or withdraw, and do not expose a storage locator.

## Preview, visibility, and publish

Keep every result private by default. Let the user choose:

- `private`: keep the preview; do not call publish.
- `unlisted`: direct `/videos/...` link with `noindex`; omit it from the Profile/feed.
- `public`: direct video link plus the user's public Profile/HiRey Moments.

Call `agentic_media.release.freeze` with the exact visibility and a stable idempotency key. Immediately
before `agentic_media.publish`, show the selected visibility and ask for explicit confirmation. Pass
the tool confirmation object bound to `agentic_media.publish`; upload or preview approval is not
publish approval.

Success requires the returned `release_receipt_id`, `video_canonical_url`, and state `published`.
For public visibility also return `profile_canonical_url`. Do not expose a database object path or a
raw storage URL. On an unknown outcome, query work status; never convert it to success. Withdrawal
also requires explicit confirmation and succeeds only with the returned withdrawal receipt/state.
