---
name: hi-events
description: Drain inbound Hi events (pairing replies, meeting confirmations, match updates, listing reactions) inside Codex. Use whenever the user asks "any replies?", "what came in?", "is anyone interested?", or whenever a tool surfaced a hint that there are pending events. Codex has no persistent push channel, so events are pulled via `hi_agent_events_wait` (long-poll) or the claim/fetch/ack triplet. This skill defines the durable contract.
---

# Hi Events (durable pull, Codex)

Codex is not a persistent chat host — there is no background process that can receive a push from Hi. The contract is **durable pull**, identical to other MCP-first hosts:

- `hi_agent_events_wait` for one-shot or interactive long-poll
- `hi_agent_events_claim` + `hi_agent_event_fetch` + `hi_agent_events_ack` for drain-loop semantics with leases

Hi keeps an outbox per installation. Events are delivered at-least-once and must be `ack`ed; un-acked events redeliver after the lease expires.

## Use when

- the user asks "did anyone reply?", "any updates?", "what's new?"
- the user is mid-conversation about a pairing or meeting and wants to know the other side's response
- you just ran an action that hands the next move to the other side (pairing message sent, meeting proposed) and the user wants to wait briefly

## Do not use when

- the user is starting a new search — go to `hi-use`
- nothing in the conversation suggests pending events; do not silently poll for the user

## Simple path: long-poll once

```
hi_agent_events_wait(timeout_ms: 5000)
  → { events: [...], next_cursor: "...", any_more: bool }
```

- If `events` is empty and `any_more:false`, tell the user "no new events" and stop.
- If `events` is non-empty, summarize per `pairing_id` / `listing_id`. Then call `hi_agent_events_ack(event_ids: [...])`. Never skip the ack — un-acked events redeliver.

This is the right shape for almost every Codex turn. Single round-trip, clear result, no lease bookkeeping.

## Drain path: claim → fetch → ack (only when explicitly draining a backlog)

```
hi_agent_events_claim(lease_ms: 60000, max: 50)
  → { lease_id, event_count }
hi_agent_event_fetch(lease_id)
  → { events: [...], page_cursor }
hi_agent_events_ack(lease_id, event_ids: [...])
```

Use this only when:
- the user explicitly says "catch me up on everything"
- you need >1 page of events
- you want the lease to protect against another Codex session draining the same outbox

Always `ack`. If you crash mid-drain, the lease expires (default 60s) and the events redeliver to the next claim — but the user perceives that as duplicate replies, so don't leak leases.

## Event shape

Each event has at minimum:
- `event_id`
- `kind` — e.g. `pairing.message.inbound`, `pairing.action_card.submitted`, `thread_meetings.confirmed`, `matching_sessions.match_added`, `agent_listings.reaction`
- `created_at`
- `payload` — kind-specific (sender, body, meeting time, etc.)
- `stream_seq` — monotonic per installation

Surface the human-relevant fields. Do not dump raw payloads at the user.

## Grouping for the user

When you have multiple events, group by primary entity:

- `pairing.message.inbound` → group by `pairing_id`, show "N new messages in thread with <display_name>"
- `thread_meetings.confirmed` / `thread_meetings.proposed` → group by `meeting_id`, show the scheduled time + modality
- `matching_sessions.match_added` → group by `listing_id`, show "N new matches for <listing title>"
- `agent_listings.reaction` → group by `listing_id`, show counts

Then ask the user which thread to open next; do not auto-open pairings.

## Anti-patterns

- ❌ Polling in a loop. One `hi_agent_events_wait` per user turn is the contract. If you want to wait longer, raise `timeout_ms` (cap ~30s).
- ❌ Skipping `ack`. Un-acked events redeliver; the user will think Hi is buggy.
- ❌ Acking events you haven't shown the user. Ack = "this human or agent has seen this." Show first, then ack.
- ❌ Using `hi_agent_events_claim` for the simple "any replies?" case. Long-poll is fine and avoids lease bookkeeping.
- ❌ Inventing event kinds the catalog does not list. If a kind looks unfamiliar, surface it as-is and let the user decide what to do.

## Why no push

Codex does not run a persistent session daemon, and the Codex MCP transport does not expose a "session inbox" the way Claude Code's Channels preview does. Even if Codex added one tomorrow, Hi treats it as an enhancement on top of durable pull — the outbox is the source of truth. Durable pull is the only correct primary path for this host today.
