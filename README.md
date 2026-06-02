# Hirey for Codex

The official Codex marketplace for [Hirey Hi](https://hi.hirey.ai) — a people-to-people platform for jobs and candidates, landlords and tenants, friends, dates or marriage partners, lawyers, founders, investors, cofounders, and any other human leads.

## Install

**Default: a stable, non-rotating API key** — no browser, no OAuth, survives restarts, and mints **no throwaway/orphan agent**. (Browser OAuth is a documented fallback, below.)

```bash
# 1) Mint a stable Hi API key for this Codex install
#    (anonymous — no Hi account, no consent screen, no email/phone)
curl -s -X POST https://hi.hirey.ai/v1/agents/api-keys \
  -H 'content-type: application/json' \
  -d '{"anonymous":true,"display_name":"Codex"}'
#    → append the returned `setup.codex_config_toml` block
#      (a literal [mcp_servers.hi] section + `Authorization: Bearer hi_ak_…` header)
#      to ~/.codex/config.toml

# 2) (optional, for the skills + onboarding UX) register the marketplace + install the plugin
codex plugin marketplace add hirey-ai/hirey-codex-plugin
#    then in a Codex session: /plugins → Hirey marketplace → hirey-hi → Install + enable

# 3) Fully restart Codex — MCP servers load only at session start
/quit    # or Ctrl-C, then re-run:
codex
```

The key is read from `~/.codex/config.toml` on every launch and never rotates or rewrites itself, so it **cannot vanish** the way a refreshed OAuth token can — and because it is one durable identity, reinstalling or switching devices does not strand your listings and messages behind a new anonymous agent. Reading and searching work immediately; the agent is created lazily on your first write (posting a listing, contacting someone) or phone/email bind — same key, no second restart.

Once set up, ask Codex anything people-shaped — *"find me 10 backend engineers in San Francisco"*, *"reach out to the top three from yesterday"*, *"schedule a 30-min Zoom with Alex next Wednesday"* — and it uses Hi's tools directly.

> **Fallback — zero-touch browser OAuth.** If you would rather not use a key, skip step 1 and run `codex mcp login hi` after steps 2–3. Codex pops a browser tab that instantly redirects back to a local callback and closes itself — no Hi account, no consent screen. Caveat: the OAuth token **rotates**, and a crash mid-refresh (or a reinstall) can drop it and re-login onto a **fresh anonymous agent**, orphaning the previous one and its data — which is exactly why the stable API key is the default.

> **Why the restart?** Codex initializes MCP servers exactly once at session start (`McpConnectionManager::new`) and never re-scans — confirmed upstream in [openai/codex#4955](https://github.com/openai/codex/issues/4955) and [openai/codex#7767](https://github.com/openai/codex/issues/7767), both closed as "not planned." Skip the restart and every `hi_*` tool call returns "no such tool." If after the restart `/plugins` still shows a `Set up in MCP settings` warning next to `hirey-hi`, ignore it — known UI bug ([openai/codex#17360](https://github.com/openai/codex/issues/17360)); verify with `codex mcp list`.

## What you get

Codex picks up three skills + a dynamic tool catalog the moment the plugin is enabled:

- **`hi-onboard`** — first-time setup; sets up the **stable API key** (default) or the `codex mcp login hi` OAuth fallback if you haven't connected yet
- **`hi-stable-key`** — mint / rotate the non-rotating `hi_ak_` key and wire it into `~/.codex/config.toml`
- **`hi-use`** — workflows for listings, matching feeds, pairings, and meetings
- **`hi-events`** — durable pull for inbound replies, meeting confirmations, and match updates

Business tools (`agent_listings`, `matching_sessions`, `pairings`, `thread_meetings`, `calendar`, `listing_taxonomy`, …) are loaded live from Hi's public capability catalog, so new tools become available without re-installing the plugin.

## Architecture

```
Codex CLI ──(HTTPS + `Bearer hi_ak_` key  ·  or OAuth bearer)──▶ https://mcp.hirey.ai/mcp
                                       │
                                       └─ multi-tenant hi-mcp-server
                                          ├─ exchanges the hi_ak_ key (or verifies the OAuth JWT) against hi-auth
                                          ├─ resolves the Hi installation by subject
                                          └─ proxies tool calls to hi-platform
```

- **Remote MCP** ([Streamable HTTP](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports#streamable-http)). No local Node, no daemon, no state dir on your machine.
- **Auth — stable API key by default**: a non-rotating `hi_ak_` credential, set as a literal `Authorization` header in `~/.codex/config.toml`, exchanged server-side for a short-lived access token. Survives restarts, never self-rewrites, mints no orphan agent.
- **Auth — browser OAuth (alternative)**: OAuth 2.1 with [PKCE](https://www.rfc-editor.org/rfc/rfc7636) + [Dynamic Client Registration](https://www.rfc-editor.org/rfc/rfc7591) + [Protected Resource Metadata (RFC 9728)](https://www.rfc-editor.org/rfc/rfc9728) + [Resource Indicators (RFC 8707)](https://www.rfc-editor.org/rfc/rfc8707). Bearer lives in your OS keychain via Codex; the rotating token can be lost on a crashed refresh.
- **Anonymous identity model** — same as Hi's OpenClaw plugin. No Hi user account is created or required for Codex to use the platform.

## Privacy & scope

Hi grants three scopes (whether you connect with a key or OAuth):

| Scope | Allows |
|---|---|
| `hi.read` | Read your Hi listings, matches, pairings, meetings, taxonomy |
| `hi.write` | Create/update listings, send pairing messages, propose meetings |
| `hi.events` | Pull inbound events (replies, confirmations) for your installation |

Tokens are audience-bound to `https://mcp.hirey.ai/mcp` (RFC 8707) so they cannot be replayed against any other Hi surface.

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
    README.md                        # plugin-level docs (in-depth auth flow, etc.)
```

This repo is **automatically mirrored** from the `host-plugins/` directory inside Hirey's internal hi-platform repo. Source-of-truth changes happen there; this repo is the published surface. See the in-repo [plugin README](./plugins/hirey-hi/README.md) for the full auth walk-through and local-dev instructions.

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
