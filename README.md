# Hirey for Codex

The official Codex marketplace for [Hirey Hi](https://hi.hirey.ai).

## Install

```bash
codex plugin marketplace add hirey-ai/hirey-codex-plugin
```

Then install and enable `hirey-hi` in `/plugins`, run `codex mcp login hi`, and complete the browser
OAuth flow. The plugin connects to the hosted MCP endpoint at `https://mcp.hirey.ai/mcp`; it does not
install an npm package or local daemon. The bundled `hi-onboard` skill is the canonical connect and
recovery procedure; the notes below summarize it.

A missing tool after an actual install or update can follow a host loading or auth startup failure. It
is not proof of anything about credential validity: inspect the host loading state, do a supported
reload or start a new Codex session, and verify the tools again. Restart the full Codex application
only if a tool is still missing after that, with the concrete remaining error as evidence.

If a previous install returns `401 invalid_token`, follow the credential recovery in `hi-onboard`:
report the exact error, keep saved OAuth credentials, never log out first, and run the normal Codex
OAuth login; it never claims the saved credential is definitely invalid. Update the plugin only when
the version policy actually says an update is needed. The skill's read-only preflight checks
configuration structure only, not token validity: it may remove a URL-only duplicate
(`legacy_url_only_override`, no auth header) as an authorized connection repair, while a
`review_required` entry (manual `Authorization` header, custom endpoint, restriction or disabled
setting) is preserved unless a separate concrete invalid-override diagnosis justifies removing
exactly that override. Credential errors and failed refreshes need the normal login, not a blanket
restart. It never replaces a broken signed-in credential with a new anonymous identity.

Once the tools are loaded, verify that `workspace_workflows` is available and call it with
`action: catalog`. The live catalog is authoritative for the existing Person, Workspace, Moment,
Page, Need, People, Message, Meeting, Product Signal, and Repair operations.

## What the plugin ships

- `hi-onboard`: normal Codex OAuth setup and readiness verification.
- `hi-use`: existing people, relationship, messaging, and meeting workflows.
- `hi-events`: one typed business inbox for messages, tasks, and user-visible events, plus safe Agent-message leases.
- `hi-repair`: Product Signal and scoped Repair Case workflow.
- `.mcp.json`: the hosted MCP URL and OAuth resource.

The MCP service exposes one existing tool, `workspace_workflows`. Business operations are actions
inside that tool; the plugin does not introduce parallel tool names or maintain business state.

## Service ownership

- `hi-agent-gateway`: Agent installation and activation, Endpoint, Subscription, and durable Agent
  event delivery.
- `hi-mcp-server`: MCP protocol adaptation, tool catalog presentation, and capability-call
  forwarding.
- `hi-auth`: Account login, OAuth, tokens, and Agent Session credentials.
- `hi-platform`: Web Agent, `/me`, capability discovery, and public product API.
- Secretary Core: Person, Workspace, Message, Moment, Relationship, and business truth.

## Repository layout

```text
.agents/plugins/marketplace.json
plugins/hirey-hi/
  .codex-plugin/plugin.json
  .mcp.json
  skills/
  README.md
```

This published marketplace is mirrored from `host-plugins/` in the internal `hi-platform`
repository.

## Support

- Plugin issues: [hirey-ai/hirey-codex-plugin](https://github.com/hirey-ai/hirey-codex-plugin/issues)
- Product: [hi.hirey.ai](https://hi.hirey.ai)
- Security: security@hirey.com

UNLICENSED (proprietary).
