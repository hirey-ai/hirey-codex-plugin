---
name: hi-use
description: Use Hirey Hi for existing Person, Workspace, Need, Listing, People, Pairing, Message and Meeting workflows through workspace_workflows. Use for people-finding, outreach, introductions, messages, meetings, and private relationship memory.
---

# Use Hirey Hi

Hi exposes one MCP tool, `workspace_workflows`. Its `action: catalog` result is the source of truth
for the existing operations, their purpose, write behavior, and confirmation requirement.

Before the first Hi business call in a new session, call
`hi_agent_status({"client_plugin_version":"0.2.13"})`. Follow its plugin policy and authentication
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

## Connect me with someone

When the user asks to reach, connect with, or get an introduction to a specific Person, complete the
workflow instead of stopping after drafting suggested prose:

1. Search `people.find_private` first. Use `people.find` only when the private result does not identify
   the target. If multiple people match, ask the user to disambiguate before continuing.
2. Call `contact.policy` with that exact `target_person_id`. Treat its `reach` object as the only
   executable route authority; never infer a Connector from `relationship.list` or from prose.
   If `allowed` is false, stop and report the returned reason without proposing a workaround.
3. If `reach.kind` is `direct`, collect the user's purpose/message and, after the catalog-required
   confirmation, call `contact.intent` for the exact target.
4. If `reach.kind` is `one_hop`, use only `reach.connector_candidates`:
   - with one candidate, name the Connector and ask for the user's purpose plus confirmation;
   - with multiple candidates, show those names and let the user choose one;
   - then call `reach.route.plan` with the selected candidate's exact `hops`, the user's intent, a
     stable idempotency key, and the required explicit confirmation.
5. If `reach.kind` is `keep_looking`, say that no executable route currently exists. Offer to save a
   private Need with `need.create`; do not fabricate a Connector or imply that outreach was sent.

After planning a route, report that the next participant must decide it. `reach.route.decide` advances
the exact pending hop; when every participant accepts, Core creates the direct conversation and sends
the original intent. Use `reach.route.get` for status. Never claim completion before its status is
`completed`, and never expose non-adjacent private graph edges.
