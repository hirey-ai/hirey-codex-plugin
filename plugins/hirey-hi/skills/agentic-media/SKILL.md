---
name: agentic-media
description: Privately upload original image/video evidence, or let a user prepare and optionally publish a local video, through HiRey's resumable media path.
---

# Agentic Media

Use this Skill when the user gives Codex a local image/video that another Hi workflow must preserve,
or asks to continue an Agentic Media work.
This version covers only the first four product steps: receive, prepare, preview, and publish/share on
HiRey. Social-platform OAuth and posting are a later phase and must not be offered as available.

Start with `hi_agent_status({"client_plugin_host":"codex","client_plugin_version":"0.2.32"})`, then call
`workspace_workflows` with `action: catalog`. Read
[references/control-contract.md](references/control-contract.md) before moving bytes.

## One user-facing entry

The user should only need to hand the file to Codex. When another Skill, such as `hi-repair`, needs
the original only as private evidence, use `evidence` and do not ask an edit/publication question.
Otherwise resolve a local regular video, then ask one question only when necessary: should HiRey
convert it to a standard MP4 (`raw_source`) or preserve it exactly as the finished video
(`final_master`)? Do not ask about
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
- `agentic_media.release.freeze`
- `agentic_media.publish`
- `agentic_media.withdraw` when requested
- `media.job.request`, `media.job.get` and `media.candidate.decide` for a transcript
- `media.credit.add`, `media.credit.list` and `media.credit.withdraw` to name people
- `media.get` to read a video's state, download its current recording and read back an edit

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

For `raw_source`, the service converts the video to a standard MP4 (H.264/AAC); it does not edit
it. Poll `agentic_media.work.status` and show the returned private preview. HiRey has no video
editor: to change a video, make the edit yourself and follow "Edit a video and write it back"
below. (`agentic_media.work.revise` is retired and refuses with `video_edit_not_available`.)

For `final_master`, the uploaded video must remain the output video byte-for-byte. The service may
inspect it and derive a cover/playback presentation, but must return `no_edit_verified=true`.

For `evidence`, the uploaded image/video remains private, skips editing and publication, and completes
at `preview_ready`. Return its canonical `media_asset_id` to the calling workflow. Do not call
freeze, publish, or withdraw, and do not expose a storage locator.

## When processing fails

`agentic_media.work.status` (and `media.get` `processing`, `interview.get` `jobs`) says what
happened, from the job itself: `worker_status`, `worker_error_code` and `worker_error_detail`. A
Work in `failed` failed; tell the user the reason as given, in plain words. Do not report success,
and do not retry a reason that belongs to the file: `media_unreadable` (ffmpeg could not read it),
`no_speech` (no words to transcribe) and `transcription_refused` fail the same way again; ask for
another file. `lease_expired` and `media_tool_killed` (the worker stopped, often out of memory)
may be tried once more. `storage_state: failed` means saving the upload did not finish: call
`agentic_media.upload.complete` again with the same key; if it keeps failing, upload again.

## Edit a video and write it back

When the user wants a change to a video already on HiRey (cut a part, fix the start, a new final
cut), make the edit locally and upload the result as a new version of the same video. Its address
and the people and Page it is on stay the same.

1. Call `media.get` with the video's `media_id`. Note `current_revision`, `published` (what is live:
   its `revision`, `recording_sha256`, `public_now`, `waiting_for`, `canonical_path`) and
   `processing` (its jobs, with any `error_code`/`error_detail`).
2. Download the current recording: pass that fresh `media.get` result to
   `scripts/agentic_media_upload.py download --out <new local file>` over stdin. `download_url` and
   `download_headers` are a 15-minute bearer capability: keep them in memory; never print, log or
   save them. The helper checks the bytes against `recording_sha256`. If it expired, read
   `media.get` again.
3. Edit locally and check the result yourself (watch the cut points, the length, the sound). The
   final file must be an MP4 with H.264 video and AAC audio; convert it locally first (for example
   `ffmpeg -c:v libx264 -c:a aac`). HiRey does not convert a version for you.
4. Upload it as above with `intent: final_master`, `mime_type: video/mp4`, `target_media_id` and
   `if_revision` set to the `current_revision` you edited. `agentic_media.upload.complete` returns
   `revision_created` and `recording_sha256`. Refusals: `version_changed` (someone changed the video
   since you read it: read `media.get` again and redo the edit on that revision),
   `final_cut_must_be_mp4` / `final_cut_not_h264_aac` (convert and upload again in a new work),
   `version_not_new` (the file is already stored: upload the edited cut),
   `media_version_not_supported` (not an uploaded video, for example an Interview: tell the user).
5. The new version is private: the published one stays live until the user publishes the new one.
   Show it (`agentic_media.work.status` preview) and ask. To publish, freeze and publish the new
   work as in "Preview, visibility, and publish"; it replaces the live version in one step at the
   same `/videos` address.
6. Read back with `media.get`: success is `published.recording_sha256` equal to the file you
   uploaded. When `public_now` is false, say what `waiting_for` names (`owner_page`: the user's
   Page is not live yet).

A new version carries the title only, not the previous transcript or cover; a new cover is made
from the new cut.

## Transcript before publishing

A published video shows its transcript on its page and to agents only when the owner adopted one
before publishing, and only of the exact cut being published. Once the Work is `preview_ready` on
its final cut, call `media.job.request` with the Work's `media_id` and `job_kind=transcribe`, wait for the job with
`media.job.get`, show the proposed transcript, and on the owner's yes call `media.candidate.decide`
with `decision=adopt`. Then freeze and publish. After a new version, transcribe
again: publish refuses a transcript of an earlier cut (`transcript_not_of_this_cut`). A published
video cannot adopt a transcript later;
it needs withdrawal and a new release. Skip this for `evidence`.

## Name the people in it

The owner can name people on their own video (author, co_author or appeared_in) with
`media.credit.add`, using only an exact Person ref the owner chose, never a guess from a face, a name
or a file. Naming needs no approval from the named person and shows on the video page while they
have a live Page; confirm with the owner before adding. The named person or the owner withdraws a
credit with `media.credit.withdraw` after they confirm it (pass `confirmation`);
`media.credit.list` finds it. A refusal
`media_credit_not_allowed` is final for that person on that video: do not retry, and do not guess
why.

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
A video keeps one `/videos` address for every version and republish; an older address redirects to it.
For public visibility also return `profile_canonical_url`. Do not expose a database object path or a
raw storage URL. On an unknown outcome, query work status; never convert it to success. Withdrawal
also requires explicit confirmation and succeeds only with the returned withdrawal receipt/state.

## Video to a Page

Use this when the user hands Codex a video or audio recording of a person (an interview, a
conversation, a talk) and wants its transcript, or a HiRey Page from it. Describe `interview.create`, `interview.get`,
`interview.request_processing`, `interview.transcript.revise`, `page.mine`, `page.create`,
`page.compose.brief`, `page.compose.check` and `page.compose` first; stop with
`contract_not_describable` if one is missing.

1. Upload it as above with `final_master` (or `raw_source` when it needs converting to MP4), and
   pass `interview: {title, subject_person_id}` to `agentic_media.work.create` when you know the exact
   Person; otherwise `{title}` only. An own upload with an Interview starts its transcript by itself.
   For a recording that is already a HiRey Media, use `interview.create` and then
   `interview.request_processing` with `job_kind: transcribe`.
2. Check for a transcript with `interview.get`: `transcript_revisions` holds it, and the first
   finished machine transcript is saved there as revision 1. While a `transcribe` job in `jobs` is
   `queued` or `leased`, tell the user it is being made (minutes; long recordings longer) and check
   again later. When the last one is `failed` or `dead_letter`, read its `error_code` and
   `error_detail` (see "When processing fails"): `no_speech`, `media_unreadable` and
   `transcription_refused` belong to the recording and fail the same way again, so tell the user
   and ask for another recording; otherwise request it again once.
   No `transcribe` job at all (an upload made on someone else's behalf) means the owner requests it
   with `interview.request_processing`; tell the user so instead of waiting.
3. Check the transcript before using it: names, organisations, roles and dates may be misheard.
   Compare them with what is known about the Person (`page.mine`, the user's own words) and ask the
   user about anything that does not match. Save the user's corrections with
   `interview.transcript.revise` (the current `if_transcript_revision`).
4. Only when the user wants the video public, or asks for a Page from it, write the Page. A private
   video only gets its transcript. You write only the user's own Page, from a recording about them.
   Another Person's Page is written by HiRey's Page research agent, which only Rey in HiRey starts:
   tell the user to ask Rey there for that Person's Page from this recording (say its title), and stop.
5. For the user's own Page, read `page.mine {subject_person_id}` with their own Person id; when they
   have none, `page.create {subject_person_id, content: {display_name, slug}}` first. Call
   `page.compose.brief` with the draft's `page_draft_id`, `if_revision` (its `current_revision`) and
   the Interview as the one material (`[{interview_id}]`; HiRey reads its saved transcript). Write the
   Page yourself, from that transcript only, in the JSON schema the brief ends with, citing it as
   `m1`. Check it with `page.compose.check`, then call `page.compose` with the same material and your
   `page`; never send an `instruction`. HiRey only checks: `page_compose_checks_failed` lists each
   problem (check, slot, text, why), so fix or drop that text and send again, or tell the user. Show
   the user what changed from the Page it returns.

The Page draft stays private. Publishing the Page or the video is a separate, explicitly confirmed
step (`page.authorize` then `page.publish`, or the Interview's own publication); never infer it from
the upload.
