---
name: hi-use
description: Use Hirey Hi inside Codex for people-to-people work — post listings, match candidates, search, start pairings, schedule meetings. Use whenever the user asks to find, recruit, match, reach out to, pair with, or meet anyone (job candidate, tenant, friend, date, cofounder, investor, lawyer, etc.), AND `hi_agent_status` already reports `connected:true` + `activated:true`. If not connected yet, go to the `hi-onboard` skill first.
---

# Hi Use (post-onboarding workflows, Codex)

Once the plugin is installed and `codex mcp login hi` has been run, Codex sees Hi as a first-class tool surface. Every business tool below comes from Hi's live public capability catalog — names and schemas are authoritative.

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
| Publish a search ("I want to find …") | `agent_listings` | actions: `upsert_draft`, `publish`, `list`, `get`, `archive`, `browse_recent` |
| See what matched | `matching_sessions` | actions: `feed`, `search`, `select_for_contact`, `dismiss` |
| Open a 1:1 thread with a matched person | `pairings` | actions: `start`, `message`, `list`, `get`, `action_card_*` |
| Negotiate / schedule a meeting | `thread_meetings` | actions: `propose`, `confirm`, `cancel`, `list` |
| Browse the listing taxonomy (job kinds, housing kinds, …) | `listing_taxonomy` | read-only |
| Check credits / billing | `agent_credits` | read-only for most flows |
| Calendar / availability | `calendar` | for meeting proposal windows |
| Inbound events (replies, confirmations) | `hi_agent_events_wait` | long-poll; see `hi-events` skill |

If a tool you remember from this map is not in your live inventory, trust the live inventory — the catalog is the source of truth, this map may lag.

## Default workflow (find people)

1. **Clarify intent before calling anything.** Hi listings are durable and visible — do not publish on a vague hint. Ask the user for:
   - what kind of person (role, relationship, criteria)
   - hard filters (location, language, level, budget, age range as applicable)
   - any soft preferences worth ranking on
   - whether to publish now or just draft

2. **Check taxonomy if you are unsure of category.** Call `listing_taxonomy` with the broadest hint; pick the closest `listing_kind` / `subkind`. Do not invent kinds.

3. **Upsert + publish the listing.**
   - `agent_listings(action: "upsert_draft", …)` → returns `listing_id`
   - `agent_listings(action: "publish", listing_id)` → goes live
   - Report `listing_id` and the public link if returned. Never fabricate either.

4. **Pull the match feed.** `matching_sessions(action: "feed", listing_id)` returns ranked candidates with profile cards. Surface the top N (typically 5–10) to the user with structured fields, not paraphrase.

5. **On user pick → contact.** `matching_sessions(action: "select_for_contact", listing_id, match_id)` then `pairings(action: "start", match_id, opening_message)`. Hi handles the outbound; you just confirm the thread is open.

6. **On reply → meeting (optional).** `thread_meetings(action: "propose", pairing_id, windows, modality)`. Pass real datetime windows in ISO 8601, never placeholders.

## Tool-call discipline

- Always pass real values returned by the previous tool — never reuse a `listing_id` you saw in a prior session unless the user is explicitly resuming that listing.
- All `*_id` shapes are `<prefix>_<12+ hex>`; if you do not have one from a tool result, you do not have one. Do not guess.
- `agent_listings(action: "publish")` is durable. Never publish "to test" — use `upsert_draft` for drafts and `archive` to retract.
- `pairings(action: "message")` sends to a real person. Confirm the message body with the user when it is the first outbound, when it requests a meeting, or when it discloses anything sensitive.

## Reading match results

Match results from `matching_sessions(action: "feed")` include:

- `match_id`, `profile_card { display_name, headline, summary, badges }`, `score`, `reasons[]`, `compliance_flags[]`

Surface `display_name` + `headline` + 1–2 `reasons`. Do not invent details outside the returned card; if the user asks for more, call `matching_sessions(action: "search", filters)` or open a pairing.

## Common multi-tool patterns

- **"Find me 20 backend engineers in Tokyo, JLPT N2+, ¥10–15M, and reach out to the best three."** → taxonomy → upsert/publish listing with filters → feed → present 20 → user picks 3 → for each: select_for_contact → start pairing with a tailored opening message.
- **"Did anyone reply overnight?"** → `hi_agent_events_wait(timeout_ms: 5000)`; if events returned, group by `pairing_id`, summarize per thread. See `hi-events` for the full pattern.
- **"Schedule a 30-min Zoom with Alex from the senior PM thread."** → resolve the `pairing_id` via `pairings(action: "list", filters)` → `thread_meetings(action: "propose", pairing_id, windows: [<3 ISO ranges>], modality: "zoom", duration_minutes: 30)`.

## Anti-patterns

- ❌ Calling `agent_listings(action: "publish")` without `listing_id` from a prior `upsert_draft`. Publish requires a real draft id.
- ❌ Inventing match cards / candidates the model "thinks would fit". Only surface what Hi returned.
- ❌ Sending a pairing message that includes raw match scores or internal `reasons[]` — those are operator-visible, not for the outbound message.
- ❌ Using `hi_agent_install` mid-workflow to "reset" things. If a tool fails, surface the error; do not reinstall.
- ❌ Asking the user for an API token to "make it work". OAuth is the only path; if a tool fails on auth, the fix is `codex mcp login hi` again, not a token paste.
