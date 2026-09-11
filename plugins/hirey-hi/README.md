# Hirey Hi for Codex

This declarative plugin connects Codex to the hosted Hirey Hi MCP endpoint:

```text
https://mcp.hirey.ai/mcp
```

It ships skills and the remote MCP declaration only. There is no npm package, local MCP daemon, or
manually managed API key.

## Install and authenticate

1. Install and enable `hirey-hi` from the Hirey plugin marketplace.
2. Let the `hi-onboard` skill resolve and run Codex's executable; the user completes only the
   browser OAuth page and never needs to type a `codex` command.
3. Fully restart Codex.
4. Verify `hi_agent_status` and `workspace_workflows` are present.
5. Call `hi_agent_status` with `client_plugin_version: "0.2.13"`, then call
   `workspace_workflows` with `action: catalog`.

The live catalog contains the implemented Person, Workspace, Moment, Page, Need, People, Message,
Meeting, Agentic Media, Product Signal, and Repair operations. New operations are added to their owning service and
then appear through the same catalog; the plugin does not create parallel tool names.

## Recover a previous login

If MCP returns `401 invalid_token`, do not create another anonymous API key. Run
`codex mcp logout hi`. If `codex mcp list` shows a manual Bearer-token connection, also run
`codex mcp remove hi` followed by
`codex mcp add hi --url https://mcp.hirey.ai/mcp`. Then run `codex mcp login hi`, complete the
normal browser login, and fully restart Codex. This removes the invalid local override and reconnects
the installation to the user's existing Hi account.

Anonymous access remains available for the bounded public operations documented by the live
catalog. It does not create a Person and it is not used to conceal a broken signed-in credential.

## Runtime ownership

- `hi-agent-gateway`: Agent installation and activation, Endpoint, Subscription, and durable Agent
  event delivery.
- `hi-mcp-server`: MCP protocol adaptation, tool catalog presentation, and capability-call
  forwarding.
- `hi-auth`: Account login, OAuth, tokens, and Agent Session credentials.
- `hi-platform`: Web Agent, `/me`, capability discovery, and public product API.
- Secretary Core: Person, Workspace, Message, Moment, Relationship, and business truth.

The plugin does not own any of those records. It only connects the Codex host to the MCP adapter.

## Release version contract

Every plugin or Skill release must update both:

1. `.codex-plugin/plugin.json` → `version`;
2. `src/services/hireyPluginRelease.ts` → `HIREY_CODEX_PLUGIN_RELEASE.latest` and, only when
   compatibility is intentionally dropped, `minimum_supported`.

The automated contract test fails when the manifest and runtime `latest` version differ. Publishing
the repository alone does not update an installed Codex plugin: refresh the marketplace, reinstall
`hirey-hi@hirey`, and fully restart Codex for end-to-end release verification.

## Repository layout

```text
plugins/hirey-hi/
  .codex-plugin/plugin.json
  .mcp.json
  skills/
    agentic-media/SKILL.md
    hi-onboard/SKILL.md
    hi-use/SKILL.md
    hi-events/SKILL.md
    hi-repair/SKILL.md
```
