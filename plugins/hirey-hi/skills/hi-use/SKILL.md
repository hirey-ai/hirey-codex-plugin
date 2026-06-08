---
name: hi-use
description: Use Hirey Hi inside Codex for people-to-people work — post listings, match candidates, search, start pairings, schedule meetings. Use whenever the user asks to find, recruit, match, reach out to, pair with, or meet anyone (job candidate, tenant, friend, date, cofounder, investor, lawyer, etc.), AND `hi_agent_status` already reports `connected:true` + `activated:true`. If not connected yet, go to the `hi-onboard` skill first.
---

# Hi Use (post-onboarding workflows, Codex)

Once Hi is connected (the default setup writes a stable `hi_ak_` key into `~/.codex/config.toml` via `curl -fsSL https://hi.hirey.ai/v1/install/codex.sh | bash`, with `codex mcp login hi` only as the OAuth fallback — see `hi-onboard` / `hi-stable-key`) and Codex has been restarted, Codex sees Hi as a first-class tool surface. Every business tool below comes from Hi's live public capability catalog — names and schemas are authoritative.

## Use when

- `hi_agent_status` reports `connected:true` + `activated:true`
- the user asks anything in these shapes:
  - "find me X people for Y" (hiring, housing, friendship, dating, cofounder, investor, lawyer, etc.)
  - "post a job / listing / ad for …"
  - "reach out to candidate N from the last batch"
  - "set up a Zoom / phone call with …"
  - "what came back overnight?" / "any replies?"

## Tool map (loaded dynamically from Hi capability catalog)

| Intent | Tool | Notes |
|---|---|---|
| **Find a specific person / listing by NAME or free text** (anonymous, no listing needed) | `owners` | **`search`** with `q` (e.g. `q:"walter"` or `q:"founder building agent infra"`) — fuzzy + bilingual EN↔中文; searches profiles AND listings; returns `people[]` + `listings[]`. Use this for "搜一个叫 X 的人" / "find someone who does Y", NOT `matching_sessions.search` (that needs a published source listing). |
| Capture / update who the user is (name, headline, bio, location, links) | `owners` | actions: `update_profile`, `get`, `list_listings`, `peers_feed` — **call this first** before the first listing whenever the user has just introduced themselves |
| Publish a search ("I want to find …") | `agent_listings` | actions: `upsert`, `update_status`, `get`, `list`, `browse_recent` |
| **Get the public URL of anything you made** (pages + share links) | `public_pages` | action `get` (no args = ALL your URLs; or `ref={kind,id\|public_id}` for one thing) |
| Create / manage the user's company page | `companies` | actions: `create`, `update`, `get`, `archive`, `list_recent`, `list_listings` |
| Resolve "who is this" + public URLs from any id | `agents` | action `resolve` (`by`=`owner_public_id`/`company_id`/`listing_id`/…) |
| See what matched | `matching_sessions` | actions: `match_feed`, `search`, `contact_match` (source listing must be published first) |
| Open a 1:1 thread with a matched person | `pairings` | actions: `create`, `timeline`, `contact_target` |
| Negotiate / schedule a meeting | `thread_meetings` | actions: `start`, `respond`, `get` |
| Host or discover public multi-party activities | `event_groups` | actions: `create`, `search`, `get`, `mine`, `mine_upcoming`, `join`, `leave`, `invite`, `announce`, `schedule_occurrence`, `cancel_occurrence`, `reschedule_occurrence`, `rsvp`, `rsvp_summary` |
| Browse the listing taxonomy (job kinds, housing kinds, …) | `listing_taxonomy` | read-only |
| Check credits / billing | `agent_credits` | read-only for most flows |
| Inbound events (replies, confirmations) | `hi_agent_events_wait` | long-poll; see `hi-events` skill |

If a tool you remember from this map is not in your live inventory, trust the live inventory — the catalog is the source of truth, this map may lag.

## Binding / connecting your identity to Hi (proactive)

When the user wants to **bind / connect / add / save** their **email, phone, or Google account to Hi** — to keep their identity, recover their workspace across devices/reinstalls, or unlock writes — use **Hi's OWN tools**:

- **Email → default `google_link`** (one-click Sign in with Google): `google_link({action:"start"})` → give the user the `verification_url` → `google_link({action:"poll"})` until `status:"verified"`. If they'd rather not use Google, **`email_binding`** (`bind` → `verify` with the emailed code).
- **Phone → `phone_binding`** (`bind` → `verify` with the SMS code).

⚠️ **This is Hi's identity binding, NOT a host-native email/Gmail/calendar connector.** Never tell the user to reauthorize or reconnect a host app (e.g. a Codex/OpenClaw "Gmail connector") for this — that's a different thing and won't bind them to Hi. If a host shows a "reauthenticate this app" message for some Gmail/email connector, that is NOT how you bind email to Hi; call `google_link` / `email_binding` instead.

The three anchors (phone / email / Google) are **equivalent and additive in ANY order**: a user who already bound one can bind another later and it **converges to the same workspace** — never a second account. So "I bound my phone, now I also want to add my email/Google" (and vice-versa) just works — go ahead and bind the additional anchor. Full mechanics (start → URL → poll, the "verified" payload, claim re-attach) live in `hi-onboard`.

## Profile collection (call before the first listing)

The first time a user tells you anything profile-shaped — their name, role, where they are, a 1-line introduction, a website / LinkedIn — parse it and call `owners(action: "update_profile", …)` with whatever fields you can extract. Don't invent fields you weren't given (no fake titles, no fake locations). Bare minimum to write is `display_name` + `headline`; bio_markdown and location_text are nice-to-have but optional.

Why this matters: matching feeds, the first contact message, AND meeting invites all include the sender's profile snippet on the wire. Use the user's **real name** (plus a one-line headline) — the platform's outbound gate now rejects generic agent/device labels like "Hi OAuth agent" or "Hi agent" (the other person sees a robot instead of a human, and Zoom invites get declined). Without a real `display_name` + `headline` the counterpart sees "someone with a listing" instead of "Alex, San Francisco backend engineer who is hiring." Reply rates drop visibly.

A single user turn can carry both a profile and a listing in one breath — "I'm Alex, San Francisco backend 8y, looking to hire a senior frontend." Handle that as two tool calls in the same turn: `owners.update_profile` first (display_name="Alex", headline="San Francisco backend engineer, 8y"), then `agent_listings.upsert` for the hiring intent.

`update_profile` is self-scoped: caller can only edit their own owner profile. Don't pass `customer_id` trying to edit someone else — gateway returns 403.

## Public pages & share links — every published thing has a shareable URL

Everything the user creates on Hi has a public web page they can open and forward (no login to view), all cross-linked to each other:
- **owner / personal page** — `hi.hirey.ai/owner/<id>` (also the "agent page" — same page, aliased),
- **company page** — `hi.hirey.ai/company/<id>`,
- each **listing / demand page** — `hi.hirey.ai/listing/<id>`.

**Hand the URL back after every publish** — each write already returns its link, so surface it:
- `agent_listings` `upsert` / `update_status` / `get` → `listing_public_url` (+ `listing_public_url_status`: `public` / `unlisted` / `private_not_shareable`; null when the listing is private or not open).
- `owners` `update_profile` / `get` → `owner_public_url`.
- `companies` `create` / `update` / `get` → `company.public_url` (+ `company.owner_public_url`).

**When the user asks "what's my page / link?" call `public_pages`** — the single place to fetch any/all URLs:
- `public_pages({action:"get"})` → `{owner_public_url, company_public_url, listings:[{listing_id, summary, status, listing_public_url, listing_public_url_status}]}` — all of the user's links at once.
- `public_pages({action:"get", ref:{kind:"listing", id:"<listing_id>"}})` → `{public_url, public_url_status}` — one thing (kind = `listing` | `owner` | `agent` | `company`).

A private or closed listing has no shareable URL (`public_url_status` says why) — tell the user that instead of inventing a link.

## Discovery — "people you might be interested in"

If the user has finished onboarding and asks anything along the lines of "show me what's on Hi" / "any interesting people I could talk to?" / "browse around a bit," call `owners(action: "peers_feed", limit: 10)`. Returns `{items[], caller_profile_ready}`:

- `items[]` — owner profile cards (display_name + headline + location_text + avatar_url + owner_public_url + `suggested_because`). Surface 5–10 to the user. Don't paraphrase the fields — quote them as-is.
- `caller_profile_ready` — if `false`, the user's own profile is too sparse for the other side to take them seriously. Suggest a quick `owners.update_profile` before proceeding.

**Discovery is not a contact entry.** `peers_feed` returns owner identity, not listing IDs or selection keys. To actually reach out to one of these owners, both sides still need a listing → matching → contact_match flow. Don't try to pass `owner_public_id` into `pairings.create`; it won't bind.

## Default workflow (find people)

0. **Set up: outline the plan, then capture the user's real identity.** For a new user, first tell them in one line how Hi works so setup isn't confusing: *"Here's how this works: I'll set up your Hi profile (your real name + a one-line headline), post what you're looking for, show you matches, and connect you — we can even schedule a Zoom, all from this chat."* Then capture their profile (see "Profile collection" above — use their **real name**, never a generic/agent label). One `owners(action: "update_profile")` call, then continue.

1. **Clarify intent before calling anything.** Hi listings are durable and visible — do not publish on a vague hint. Ask the user for:
   - what kind of person (role, relationship, criteria)
   - hard filters (location, language, level, budget, age range as applicable)
   - any soft preferences worth ranking on
   - whether to post the listing now or hold off (a successful upsert is immediately live)

2. **Check taxonomy if you are unsure of category.** Call `listing_taxonomy` with the broadest hint; pick the closest `listing_kind` / `subkind`. Do not invent kinds.

3. **Create the listing.** `agent_listings(action: "upsert", listing_type_id, idempotency_key, text, target: { roles: [{role_type_id}], requirements: [{attribute_label, value_kind: "text", raw_value_text}] }, semantic_readiness_status: "ready")` → returns `listing_id`. There is **no separate draft/publish step** — a successful `upsert` is live. Use `update_status` (paused/closed) to retract. Report `listing_id` + public link if returned; never fabricate.

4. **Pull the match feed.** `matching_sessions(action: "match_feed", listing_id)` (or `action: "search", listing_id, query` to narrow by free text) returns `items[]` — ranked counterpart listings, each with a `selection_key`, the counterpart `listing_id` + `published_by_agent_id`, and an owner profile snippet. Surface the top 5–10 with structured fields, not paraphrase.

5. **On user pick → contact.** `matching_sessions(action: "contact_match", listing_id, selection_key (or selected_listing_id), text)` — this opens the pairing **and** sends the first message in one call. There is no separate `select_for_contact` / `pairings.start`. Confirm the thread is open.

6. **On reply → meeting (optional).** `thread_meetings(action: "start", pairing_id, flow_kind: "propose_slot", modality: "zoom"|"phone", requested_windows: [{start_at, end_at, timezone}])`. Real ISO datetimes, never placeholders. The counterpart answers via `thread_meetings(action: "respond", …)`.

## Tool-call discipline

- Always pass real values returned by the previous tool — never reuse a `listing_id` you saw in a prior session unless the user is explicitly resuming that listing.
- All `*_id` shapes are `<prefix>_<12+ chars>`; if you do not have one from a tool result, you do not have one. Do not guess.
- A successful `agent_listings(action: "upsert")` is durable + immediately live. Never "test-publish" — use `update_status` (paused/closed) to retract.
- `matching_sessions(action: "contact_match")` sends the `text` to a real person. Confirm the body with the user when it is the first outbound, requests a meeting, or discloses anything sensitive.
- For a by-NAME / free-text "find a specific person or listing" (no source listing needed), use `owners(action: "search", q: "…")` — NOT `matching_sessions.search` (that requires a published source `listing_id`).

## Reading match results

`matching_sessions(action: "match_feed" | "search")` returns `items[]`, each with a `selection_key`, the counterpart `listing_id` + `published_by_agent_id`, a `target_preview_text`, and an owner profile snippet (`display_name`, `headline`, `owner_public_url`). Surface `display_name` + `headline` + the preview; do not invent details. Reuse the `selection_key` for `contact_match`.

## Common multi-tool patterns

- **"Find me 20 backend engineers in San Francisco … reach out to the best three."** → `listing_taxonomy` → `agent_listings(action: "upsert")` (recruiting listing with target requirements) → `matching_sessions(action: "match_feed", listing_id)` → present candidates → user picks → for each: `matching_sessions(action: "contact_match", listing_id, selection_key, text)`.
- **"Find someone named Walter / a founder building agent infra."** → `owners(action: "search", q: "walter")` — bilingual fuzzy, no listing needed.
- **"Did anyone reply overnight?"** → `hi_agent_events_wait(timeout_ms: 5000)`; group by `pairing_id`. See `hi-events`.
- **"Schedule a 30-min Zoom with the senior PM thread."** → get the `pairing_id` (from your prior `contact_match` result or `pairings(action: "timeline")`) → `thread_meetings(action: "start", pairing_id, flow_kind: "propose_slot", modality: "zoom", requested_windows: [<ISO ranges>])`.

## Anti-patterns

- ❌ Using `matching_sessions.search` for a by-name lookup — that needs a published source listing; use `owners(action: "search")`.
- ❌ Inventing match cards / candidates the model "thinks would fit". Only surface what Hi returned.
- ❌ Putting raw scores / internal fields into the outbound `contact_match` `text` — keep it human.
- ❌ Using `hi_agent_install` mid-workflow to "reset" things. If a tool fails, surface the error; do not reinstall.
- ❌ Asking the user to paste a random API token to "make it work". A `hi_*` auth failure is fixed by the documented connection setup — the stable `hi_ak_` key in `~/.codex/config.toml` (or `codex mcp login hi` as the OAuth fallback) followed by a Codex restart — not by inventing a token to paste. See `hi-onboard` / `hi-stable-key`.
- ❌ Treating "tool not found" / "no such tool `hi_*`" as a login problem. If a `hi_*` tool literally isn't in your inventory, the `hirey-hi` MCP server didn't load into this Codex session — bounce to `hi-onboard` step 1. The fix is a Codex **restart** (Codex only spawns MCP servers at session start; openai/codex#4955, #7767), not `codex mcp login hi`.
