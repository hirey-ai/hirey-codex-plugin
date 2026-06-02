# Hirey Hi for Codex

Codex plugin that gives Codex first-class access to the Hi people-to-people platform — jobs, hiring, housing, friendship, dating, lawyers, founders, investors, cofounders, any human lead.

**Plugin + Remote MCP + a stable API key (OAuth is only a fallback).** Zero local install. No `npm install`, no Node daemon, no state dir. Every Hi tool call goes from Codex over HTTPS to `https://mcp.hirey.ai/mcp`. DEFAULT auth is a stable, non-rotating `hi_ak_…` key written once to `~/.codex/config.toml` — no browser, no `codex mcp login`, survives restarts, mints NO orphan agent. (Browser OAuth still works as a documented fallback — see "Auth" below. The legacy URL `https://hi.hirey.ai/mcp` is a permanent alias for installs published before the cutover.)

## Install

```bash
# 1) Mint a stable hi_ak_ key + append the [mcp_servers.hi] block to ~/.codex/config.toml.
#    Anonymous — no account, no browser, no OAuth, no orphan agent. Idempotent (safe to re-run).
curl -fsSL https://hi.hirey.ai/v1/install/codex.sh | bash

# 2) FULLY RESTART CODEX (quit + relaunch) so the hi MCP server + hi_* tools load.
codex
```

That's the whole install. After the restart, `codex mcp list` shows `hi` with `Auth: Bearer token`, reading/searching work immediately, and your anonymous Hi agent is created on your first write (or phone/email bind) — no account up front, no consent screen, no email/phone needed to start. **Do NOT run `codex mcp login hi`** — that's the OAuth fallback only; the key is your auth.

Optional — the skills bundle (usage guidance; NOT required for the tools to work): `codex plugin marketplace add hirey-ai/hirey-codex-plugin`, then inside Codex `/plugins` → Hirey → `hirey-hi` → install + enable, then restart again. The config.toml key cleanly overrides the plugin's bundled MCP entry (no conflict), so the plugin is safe alongside the key — but the key is what makes Hi work; the plugin just adds the `hi-*` skills.

After install, send Codex any people-finding request — "find me 10 backend engineers in San Francisco", "any replies to my pairings?", "schedule a Zoom with Alex" — and it uses Hi's tools directly.

### Why the restart is mandatory

Codex initializes its MCP connections exactly once in `McpConnectionManager::new` at session start and never revisits them. A server you just added (the `[mcp_servers.hi]` key block, or a plugin you enabled) is registered in config but will not be spawned until your **next** session. This is upstream Codex behavior, not a Hi quirk — see [openai/codex#4955](https://github.com/openai/codex/issues/4955) and [openai/codex#7767](https://github.com/openai/codex/issues/7767) (closed as "not planned"). If you skip the restart, every `hi_*` tool call will fail with "no such tool."

## Auth — stable key (default) or browser OAuth (fallback)

DEFAULT is the stable `hi_ak_…` key from step 3 (no browser, can't vanish, mints no orphan agent). The table below is the **OAuth fallback** — what Codex does automatically on the first `/mcp` call *only if you did not install a key*:

| Step | What Codex does | What Hi does | What the user sees |
|---|---|---|---|
| 1. First `/mcp` call | Sends no Authorization header | 401 + `WWW-Authenticate: Bearer resource_metadata="https://mcp.hirey.ai/.well-known/oauth-protected-resource"` | Nothing |
| 2. Discovery | Fetches RFC 9728 metadata, then RFC 8414 AS metadata | Returns JSON with `authorization_endpoint` / `token_endpoint` / `registration_endpoint` | Nothing |
| 3. DCR | `POST /oauth/register` with loopback `redirect_uris` | Mints `client_id` + (silently) provisions a fresh anonymous Hi subject for this Codex install | Nothing |
| 4. `/authorize` | Opens browser at `/oauth/authorize?...` | **No HTML rendered.** Issues an auth code bound to the new Hi identity and 302-redirects to Codex's loopback callback | Browser tab opens then closes (~200ms) |
| 5. Token exchange | `POST /oauth/token` with code + PKCE verifier + RFC 8707 `resource` | Returns access token (RS256 JWT, `aud=https://mcp.hirey.ai/mcp`) + rotating refresh token | Nothing |
| 6. Subsequent `/mcp` calls | `Authorization: Bearer <token>` on every request | Verifies signature + `aud` exact-match, resolves the Hi installation by `sub`, dispatches tool | Nothing |

This is the same identity model as OpenClaw: agent self-registers, no human identity is bound. If you want to later tie an installation to a phone-verified human, that's a follow-up workflow inside Hi — it has nothing to do with the OAuth flow above.

## What the plugin actually ships

```
plugins/hirey-hi/
  .codex-plugin/plugin.json     # Codex plugin manifest
  .mcp.json                     # remote MCP server config (url + OAuth scopes)
  skills/
    hi-onboard/SKILL.md         # first-time setup (the `codex mcp login hi` flow)
    hi-use/SKILL.md             # post-onboarding workflows (listings/matching/pairings/meetings)
    hi-events/SKILL.md          # durable pull for inbound events
  README.md                     # this file
```

The marketplace entry at `.agents/plugins/marketplace.json` points to this folder with `source.path: "./plugins/hirey-hi"`.

**No code**, no `package.json`, no `dist/`. Codex plugins are declarative — the manifest tells Codex where Hi lives, the skills tell the LLM how to use Hi, and the MCP server runs in Hi's cloud.

## How this differs from the OpenClaw path

| | OpenClaw (existing) | Codex (this plugin) |
|---|---|---|
| Distribution | ClawHub package `clawhub:hirey` + npm fallback `hirey` | `codex plugin marketplace add hirey-ai/hirey-codex-plugin` |
| Local install | `npm install` of `@hirey/hi-mcp-server` + `@hirey/hi-agent-receiver` | none |
| Process model | local stdio MCP child + local hi-agent-receiver | remote HTTPS, no local process |
| Auth | client_credentials baked into local state | OAuth 2.1 (DCR + PKCE) via `codex mcp login hi` |
| Event delivery | local receiver hooks + durable claim | durable claim via `hi_agent_events_wait` (Codex has no persistent push) |
| State on user machine | `~/.openclaw/hi-mcp/<profile>/` | Codex keychain entry only |
| Updates | user re-runs `openclaw plugins install` | Hi backend deploy — no user action |

The two paths are sibling adapters over the same Hi public capability catalog. Tool names, schemas, and semantics are identical. The plugin shell is what differs.

## Backing service

The remote MCP endpoint at `https://mcp.hirey.ai/mcp` is served by `hi-mcp-server` running in HTTP mode (`HI_MCP_TRANSPORT=http`). Multi-tenant: each Codex installation's OAuth subject resolves to a Hi installation server-side. Tools are loaded dynamically from Hi's public capability catalog (`/v1/capabilities`) so the Codex tool inventory stays in sync with Hi without a plugin re-release. The legacy alias at `https://hi.hirey.ai/mcp` is preserved for installs whose `.mcp.json` was published before the dedicated subdomain cutover — both URLs share the same OAuth subjects and tokens, audience-validated against either.

## Local development / staging

Point this plugin at a staging Hi:

```bash
codex mcp set hi --url https://staging.hi.hirey.ai/mcp
codex mcp login hi
```

For fully local Hi development, run `hi-mcp-server` in HTTP mode on your laptop and point Codex at it:

```bash
HI_PLATFORM_BASE_URL=http://127.0.0.1:3000 \
HI_MCP_TRANSPORT=http \
HI_MCP_HOST=127.0.0.1 \
HI_MCP_PORT=8788 \
hi-mcp-server

codex mcp set hi --url http://127.0.0.1:8788/mcp
codex mcp login hi   # will fail OAuth against local; for local dev use the bearer fallback below
```

For local dev without OAuth, use the bearer-token fallback the plugin's `.mcp.json` ignores by default:

```bash
codex mcp set hi --bearer-token-env-var HI_DEV_TOKEN
export HI_DEV_TOKEN=<a token minted from gateway /oauth/token client_credentials>
```

Never use bearer-token mode in production — it is the OpenClaw-path auth model and bypasses Hi's per-user installation binding.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Codex says `no such tool: hi_agent_status` (or any other `hi_*` tool) | The `hirey-hi` MCP server didn't load into this Codex session. Almost always: you enabled the plugin in `/plugins` but didn't restart Codex afterwards. | Quit Codex fully (`/quit`, `Ctrl-C`, or close the window) and relaunch. The MCP only spawns at session start ([openai/codex#4955](https://github.com/openai/codex/issues/4955) — not planned). |
| `/plugins` shows `hirey-hi` enabled but with a `Set up in MCP settings` warning | Known Codex UI bug — the plugin page does not reconcile against the runtime MCP registry, so it always shows the manual-setup prompt even when the MCP is already loaded ([openai/codex#17360](https://github.com/openai/codex/issues/17360)). | Ignore the UI warning. Run `codex mcp list` in a shell — if `hi` shows up with `Auth: OAuth`, you're fine. |
| `hi_*` tool returns `401` / `oauth_required` / `agent_not_registered` | The MCP **is** loaded but you haven't run OAuth (or your token expired and refresh failed). | Run `codex mcp login hi`. If that fails repeatedly, check that loopback ports 1455–1465 aren't blocked by a firewall or proxy. |
| `codex mcp login hi` opens a browser that does NOT redirect+close (it shows a real consent screen, login form, or Hi error page) | Something broke the zero-touch DCR + silent `/authorize` flow — usually wrong `auth.hi.hirey.ai` discovery or a proxy intercepting the redirect. | Surface the rendered page contents (it'll quote a Hi error code) to support@hirey.com. Do NOT paste tokens manually — there's no manual path on Codex. |
| Tools work but `hi_agent_doctor` fails on the events leg | OAuth scope was issued without `hi.events`, or the AS DB lost the refresh token. | Re-run `codex mcp login hi` and accept whatever the AS returns (it will request the full scope set declared in `.mcp.json`). |
| `codex plugin marketplace add hirey-ai/hirey-codex-plugin` fails with "marketplace not found" | Codex < 0.30 doesn't have plugin support. | Upgrade Codex CLI (`npm i -g @openai/codex@latest`). The plugin manifest pins `compat.codex: ">=0.30"`. |

## Versioning

This plugin's `version` is independent from `hi-mcp-server` and `hi-platform`. The plugin only needs to be re-released when:

- the bundled SKILL.md instructions change
- a new top-level skill (e.g. `hi-billing`) is added
- the OAuth scope set changes
- `compat.codex` minimum changes

Tool-level additions to Hi's capability catalog show up in Codex's tool inventory automatically — no plugin release required.
