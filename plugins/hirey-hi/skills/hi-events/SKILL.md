---
name: hi-events
description: Read and process the current Person's Hirey Hi business inbox through workspace_workflows. Use when the user asks about messages, replies, new activity, tasks, notifications, or work that needs attention.
---

# Hi business inbox

Use the same `workspace_workflows` tool as every other Hi capability. Do not look for separate
message, task, notification, or system-event inbox tools.

1. For “what is new?”, messages, replies, tasks, notifications, or work needing attention, first call
   `action: agent_message.list`. Use its optional `types` filter only when the user narrows the request
   to `message`, `task`, or `event`.
2. Read `result.items`. The item `type` distinguishes a message, task, or user-visible business event.
   Use `available_actions` and the live catalog for the exact next operation.
3. Listing is read-only. Do not claim an item merely to inspect it.
4. Call `action: agent_message.claim` only when an `agent_request` item must actually be processed.
   Show the human-relevant content before completing it.
5. Complete or fail only the exact lease returned by that claim. Tasks, direct messages, and
   notifications use their listed existing actions rather than an Agent-message lease.

Core hides transport-only events such as pending, leased, retry, delivery attempts, and dead-letter
bookkeeping. Never describe those as user messages or activity. A zero-item response means there is
nothing user-visible in the requested types; do not infer that from one Agent conversation alone.

Claims are leases and delivery is at least once. Never complete unseen content, invent an item,
reuse identifiers from another session, or put names, phone numbers, email addresses, tokens, or
account-specific examples into reusable instructions.
