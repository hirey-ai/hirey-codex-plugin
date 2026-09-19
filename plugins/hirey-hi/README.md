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
   browser OAuth page and never needs to type a `codex` command. Saved OAuth credentials are kept;
   only a `legacy_url_only_override` is removed as an authorized connection repair.
3. Verify `hi_agent_status` and `workspace_workflows` are present, then call `hi_agent_status` with
   `client_plugin_version: "0.2.15"` and `workspace_workflows` with
   `action: catalog`. A missing tool after an actual install or update can follow a host loading or
   auth startup failure; it is not proof of anything about credential validity. Inspect the host
   loading state and do a supported reload or start a new Codex session first, then verify the tools
   again. Restart Codex only if a tool is still missing after that, with the concrete remaining error
   as evidence. The next-user-turn recovery in "Recover a previous login" applies only after OAuth
   login, not to loading a newly installed tool.

The live catalog contains the implemented Person, Workspace, Moment, Page, Need, People, Message,
Meeting, Agentic Media, Product Signal, and Repair operations. New operations are added to their owning service and
then appear through the same catalog; the plugin does not create parallel tool names.

## Recover a previous login

If MCP returns `401 invalid_token` (or another credential error), report that exact error and do not
claim the saved OAuth credential is definitely invalid. Do not create another anonymous API key and
do not log out first. Run `codex mcp login hi` and complete the normal browser login, even when the
preflight reports `plugin_only` or finds no override; this reconnects the installation to the user's
existing Hi account. `hi-onboard`'s read-only preflight checks configuration structure only, not
token validity: only a `legacy_url_only_override` (a URL-only duplicate with no auth header) is
removed as an authorized connection repair with `codex mcp remove hi`. A `review_required` entry (a
manual `Authorization` header, custom endpoint, restriction or disabled setting) is preserved unless
a separate concrete invalid-override diagnosis justifies removing exactly that override. Retry the
original bounded operation once; if the result is still stale after login, let the next user turn or
a Codex reload or new session refresh it. Restart Codex only as a last resort with the concrete
remaining error as evidence.

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

Prepare and release a plugin version in this order:

1. `.codex-plugin/plugin.json` → `version`;
2. `src/services/hireyPluginRelease.ts` → `HIREY_PLUGIN_CANDIDATE_VERSIONS.codex` while the
   candidate is being reviewed and tested;
3. publish the public marketplace, complete real-host acceptance, then promote
   `HIREY_CODEX_PLUGIN_RELEASE.latest`. Change `minimum_supported` only when compatibility is
   intentionally dropped.

The server recommends an upgrade only after `latest` is promoted, so an old test installation can
verify the prompt without being silently replaced. Publishing the repository alone does not update
an installed Codex plugin: refresh the marketplace, reinstall `hirey-hi@hirey`, and reload or start
a new Codex session for end-to-end release verification; restart the full application only as a last
resort with evidence. A package update may need a reload but does not require resetting the OAuth
credential.

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

This package is generated from `agent-integration/` by
`node scripts/build-agent-packages.mjs`. Edit the source there, not this copy.
