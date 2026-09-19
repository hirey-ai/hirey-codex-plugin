---
name: hi-events
description: Read and process the current Person's Hirey Hi business inbox through workspace_workflows. Use when the user asks about messages, replies, new activity, tasks, notifications, or work that needs attention.
---

# Hi business inbox

Call `workspace_workflows` with `action: agent_message.list` to read messages,
related tasks and notifications. The default is every currently authorized
Workspace. Never loop over `workspace.focus`, replace caller identity, or silently
retry in only the focused Workspace.

Optional payload fields: `types` (`message`, `task`, `event`), `workspace_ids`,
`since`, `until`, `limit` (1–100, default 50), and `cursor`. Workspace filters only
narrow access. Resolve relative time using an explicit timezone and report the
returned bounds. Keep filters and limit identical when continuing a cursor.

Read `hirey.person.inbox.v2` `items`; label each with its server-returned
`workspace.name` and `workspace.type`. `visible_in_workspaces` lists authorized
mappings, not extra ownership. Reconcile repeated results by stable `item_ref`.
Use `page.next_cursor` while `page.has_more` is true. Only
`coverage.status=complete` AND `page.has_more=false` establish exhaustion of this
query range. A failed read is never “no messages.” A stale-authority cursor requires
a fresh first page; it does not justify falling back to the focused Workspace.

Open details with `action: inbox.get`, payload `{item_ref}`. Details use current
object authority without changing focus. Message history is bounded;
`item.detail.history.next` supplies the next exact read operation/payload. Do not
interpret an old notification as a pending Ask: inspect `referenced_state` and
`needs_action`. `unknown` means responsibility could not be established.

Reads never claim, mark read, acknowledge, reply, change a task or move focus.
`source_refs` preserve original read states, including merged notifications.
Only the recorded authority Agent receives pending presentation intake; another
Agent or a real Web/iOS Client must not inherit it.

Subsequent writes use existing business operations, confirmations and authority.
Action descriptors include the original Workspace and object. If a descriptor
requires Workspace selection, use the existing controlled selection flow after
an explicit user write request; a payload Workspace field is not authority.
Never claim merely to inspect an Agent request. Complete/fail only an exact held
lease after processing its content. Transport retries and leases are not messages.

These are live pages, not a multi-request snapshot. Refresh page one for arrivals
and updated tasks; withdrawals and revoked access may remove items. Do not promise
exactly-once monitoring. `agent_message.history` preserves the existing focused
Agent chat-history route; it is not an inbox fallback.
