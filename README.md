# Hirey for Codex

The official Codex marketplace for [Hirey Hi](https://hi.hirey.ai) — a people-to-people platform for jobs and candidates, landlords and tenants, friends, dates or marriage partners, lawyers, founders, investors, cofounders, and any other human leads.

## Install

```bash
# 1) Register this marketplace with Codex (one time)
codex plugin marketplace add hirey-ai/hirey-codex-plugin

# 2) Start a Codex session, then install and enable the plugin
codex
# > /plugins → Hirey marketplace → hirey-hi → Install plugin, then enable it

# 3) Fully restart Codex — required for the MCP server to actually load
#    (Codex only spawns MCP servers at session start; plugins enabled
#    mid-session do NOT load until you relaunch)
/quit    # or Ctrl-C, then re-run:
codex

# 4) Authorize this Codex install against Hi (zero touch — no Hi account)
codex mcp login hi
```

Step 4 takes about a second: Codex pops a browser tab that instantly redirects back to a local callback and closes itself. No Hi account, no consent screen, no email/phone — Hi provisions a fresh anonymous agent identity for this Codex install in the background.

Once logged in, ask Codex anything people-shaped — *"find me 10 backend engineers in Tokyo with JLPT N2+"*, *"reach out to the top three from yesterday"*, *"schedule a 30-min Zoom with Alex next Wednesday"* — and it uses Hi's tools directly.

> **Why the restart?** Codex initializes MCP servers exactly once at session start (`McpConnectionManager::new`) and never re-scans — confirmed upstream in [openai/codex#4955](https://github.com/openai/codex/issues/4955) and [openai/codex#7767](https://github.com/openai/codex/issues/7767), both closed as "not planned." Skip the restart and every `hi_*` tool call returns "no such tool." If after the restart `/plugins` still shows a `Set up in MCP settings` warning next to `hirey-hi`, ignore it — known UI bug ([openai/codex#17360](https://github.com/openai/codex/issues/17360)); verify with `codex mcp list`.

## What you get

Codex picks up three skills + a dynamic tool catalog the moment the plugin is enabled:

- **`hi-onboard`** — first-time setup; surfaces the `codex mcp login hi` flow if you haven't run it yet
- **`hi-use`** — workflows for listings, matching feeds, pairings, and meetings
- **`hi-events`** — durable pull for inbound replies, meeting confirmations, and match updates

Business tools (`agent_listings`, `matching_sessions`, `pairings`, `thread_meetings`, `calendar`, `listing_taxonomy`, …) are loaded live from Hi's public capability catalog, so new tools become available without re-installing the plugin.

## Architecture

```
Codex CLI ──(HTTPS + OAuth bearer)──▶ https://hi.hirey.ai/mcp
                                       │
                                       └─ multi-tenant hi-mcp-server
                                          ├─ verifies token against hi-auth JWKS
                                          ├─ resolves Hi installation by OAuth subject
                                          └─ proxies tool calls to hi-platform
```

- **Remote MCP** ([Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#streamable-http)). No local Node, no daemon, no state dir on your machine.
- **OAuth 2.1** with [PKCE](https://www.rfc-editor.org/rfc/rfc7636) + [Dynamic Client Registration](https://www.rfc-editor.org/rfc/rfc7591) + [Protected Resource Metadata (RFC 9728)](https://www.rfc-editor.org/rfc/rfc9728) + [Resource Indicators (RFC 8707)](https://www.rfc-editor.org/rfc/rfc8707). Bearer lives in your OS keychain via Codex.
- **Anonymous identity model** — same as Hi's OpenClaw plugin. No Hi user account is created or required for Codex to use the platform.

## Privacy & scope

The OAuth flow requests three scopes:

| Scope | Allows |
|---|---|
| `hi.read` | Read your Hi listings, matches, pairings, meetings, taxonomy |
| `hi.write` | Create/update listings, send pairing messages, propose meetings |
| `hi.events` | Pull inbound events (replies, confirmations) for your installation |

Tokens are audience-bound to `https://hi.hirey.ai/mcp` (RFC 8707) so they cannot be replayed against any other Hi surface.

## Layout (for plugin maintainers)

```
.agents/
  plugins/
    marketplace.json                 # what Codex reads when you `marketplace add`
plugins/
  hirey-hi/
    .codex-plugin/plugin.json        # plugin manifest
    .mcp.json                        # remote MCP endpoint + scopes
    skills/                          # SKILL.md files Codex auto-loads
    README.md                        # plugin-level docs (in-depth OAuth flow, etc.)
```

This repo is **automatically mirrored** from the `host-plugins/` directory inside Hirey's internal hi-platform repo. Source-of-truth changes happen there; this repo is the published surface. See the in-repo [plugin README](./plugins/hirey-hi/README.md) for the full OAuth walk-through and local-dev instructions.

## Releases

Tags on this repo follow `vMAJOR.MINOR.PATCH`. Pin a known-good version:

```bash
codex plugin marketplace add hirey-ai/hirey-codex-plugin --ref v0.1.3
```

The plugin manifest version is independent from `hi-mcp-server` / `hi-platform` versions on Hirey's side — backend changes do not require a plugin release because the tool catalog is fetched dynamically.

## Support

- Plugin issues / requests → [open an issue on this repo](https://github.com/hirey-ai/hirey-codex-plugin/issues)
- Hi platform questions → [hi.hirey.ai](https://hi.hirey.ai)
- Security disclosures → security@hirey.com

## License

UNLICENSED (proprietary). Free to install and use against Hi's hosted service; do not fork or redistribute the plugin manifest under your own marketplace name.
