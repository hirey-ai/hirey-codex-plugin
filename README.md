# Hirey for Codex

The official Codex marketplace for [Hirey Hi](https://hi.hirey.ai).

## Install

```bash
codex plugin marketplace add hirey-ai/hirey-codex-plugin
```

Then install and enable `hirey-hi` in `/plugins`, run `codex mcp login hi`, complete the browser
OAuth flow, and fully restart Codex. The plugin connects to the hosted MCP endpoint at
`https://mcp.hirey.ai/mcp`; it does not install an npm package or local daemon.

If a previous install returns `401 invalid_token`, update the plugin first. Its `hi-onboard` skill
will remove an invalid manual Bearer override when present, run the normal Codex OAuth login, and
require one full restart before retrying the original request. It never replaces a broken signed-in
credential with a new anonymous identity.

After the restart, verify that `workspace_workflows` is available and call it with
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
