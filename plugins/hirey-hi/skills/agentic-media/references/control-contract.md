# Agentic Media first-four control contract

The live `workspace_workflows` catalog and each operation's `describe` response are authoritative.
This reference fixes orchestration invariants only.

## State sequence

```text
awaiting_upload -> uploading -> processing? -> preview_ready
preview_ready -> private (stop) | frozen(unlisted/public) -> published -> withdrawn
```

`processing` is required for `raw_source` and skipped for `final_master` and `evidence`. Evidence is
always private and stops at `preview_ready`. Cancellation is allowed
before publication. Failure and unknown outcome remain explicit terminal/recovery states.

## Source and integrity

- Source is a user-local regular video, a supported raster image for `evidence`, or a stable HiRey
  canonical media ref.
- `raw_source` invokes the existing edit flow. An edit the Agent makes locally comes back as a new
  version of the same video: `final_master` MP4 (H.264/AAC) with `target_media_id` and
  `if_revision`; a stale revision is refused (`version_changed`), never merged.
- `final_master` keeps `source_asset_id == output_asset_id` and requires `no_edit_verified=true`.
- `evidence` keeps the original private and returns one canonical media asset for a calling workflow;
  it never enters release or publication.
- Work creation binds filename, MIME type, size, whole-file SHA-256, intent, Person, Workspace,
  current Agent Session, original task ref, and idempotency key.
- Completion is valid only when every exact part exists and assembled size/SHA-256 match the work.

## Upload capability boundary

`agentic_media.upload.describe` may return a short-lived URL and required headers for each missing
part. They are bearer capabilities: use only in memory, never print, save, include in state JSON, or
place in a URL query string. Durable state may contain only stable refs, safe file identity,
completed part receipts, counts, and progress.

Production grants and endpoints require HTTPS. Plain HTTP is accepted only for `localhost` or
`127.0.0.1`, preserving self-hosted local development without a third-party object store.

## Download capability boundary

`media.get` gives the owner a short-lived `download_url` plus `download_headers` for the current
recording. They are bearer capabilities, like part grants: use only in memory through the helper's
`download` command, which checks `recording_sha256`; never print, save or put them in a URL.

## Visibility truth

- `private`: no publication and no public media byte access.
- `unlisted`: direct canonical video page and media bytes; `noindex`; absent from public Profile/feed.
- `public`: direct canonical video page and public Profile/HiRey Moments.

The release receipt binds the exact media revision, output asset, publication digest, visibility,
publisher, and canonical path. The canonical path belongs to the video: every version and
republish keeps it, and publishing a new version replaces the live one in one step. Publish and withdrawal require separate explicit confirmations.
Only an active receipt plus active canonical publication can serve video/poster bytes.

## Out of scope

YouTube, X, TikTok, LinkedIn, provider OAuth, account/target selection, provider privacy, and remote
posting receipts are step 5. Do not describe them as implemented by this first-four workflow.
