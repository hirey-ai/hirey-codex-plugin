---
name: hi-use
description: Use Hirey Hi for existing Person, Workspace, Need, Listing, People, Pairing, Message and Meeting workflows through workspace_workflows. Use for people-finding, outreach, introductions, messages, meetings, and private relationship memory.
---

# Use Hirey Hi

Hi exposes one MCP tool, `workspace_workflows`. Its `action: catalog` result is the source of truth
for the existing operations, their purpose, write behavior, and confirmation requirement.

Before the first Hi business call in a new session, call
`hi_agent_status({"client_plugin_version":"0.2.10"})`. Follow its plugin policy and authentication
state exactly. A recommended update does not block a compatible call; a required update ends the
current session after upgrading because Codex reloads Skills only in a new session.

A pending Agent installation credential may use only `people.find`, `people.explain`, and
`capture.record`. Anonymous `capture.record` is retained under that Agent and returns a
`pending_capture_id`; after verified login, repeat the same action with the returned ID and the same
`idempotency_key` to place it in the real Workspace. Do not attempt messages, contact, publication,
or private reads before login.

## Call discipline

- Call `action: catalog` before using an operation you have not inspected in this session.
- Pass business inputs under `payload`; never supply Account, Person, Workspace, Agent, or Agent
  Session authority fields. Authority comes from the verified session.
- Every write or external effect requires a stable `idempotency_key`, reused only for the exact
  retry.
- When the catalog requires explicit user confirmation, ask first and pass
  `confirmation: { approved: true, operation: "<exact action>" }`.
- Use identifiers returned by the preceding call. Never guess IDs or results.
- On failure, branch on `error_code`: recover a 401 credential state, follow a 403 binding/scope
  action, and never turn an anonymous public operation into a login requirement.

## Existing workflow families

- Private network: `person.observe`, `person.network.save`, `person.note.add`,
  `person.private_contact.set`, `commitment.create`, `people.find_private`, `people.detail`.
- Finding people: `need.create`, `listing.create`, `listing.change_status`,
  `discovery.find_for_need`, `people.find`, `match.record`, `match.select`.
- Contact: `pairing.create`, `pairing.decide`, `message.send`, `message.reply`,
  `contact.introduction_decide`, and the `reach.*` actions.
- Meetings: `meeting.propose`, `meeting.decide`, `meeting.reschedule`, `meeting.cancel`,
  `meeting.list`, and `meeting_link.*`.

These are existing Core operation names, not aliases. If an action is absent from the live catalog,
do not call it. Searches and messages affect real people; surface returned facts and confirm external
effects exactly as the catalog requires.
