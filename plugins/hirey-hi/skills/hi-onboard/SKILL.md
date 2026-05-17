---
name: hi-onboard
description: First-time setup for Hirey Hi inside Codex. Use whenever `hi_agent_status` reports `connected:false`, any `hi_*` tool returns an auth or `agent_not_registered` error, or the user explicitly asks to "set up", "log in to", "connect", "activate", or "install" Hi. CRITICAL — this plugin is Codex's remote-MCP path; never tell the user to `npm install`, never ask for a client_id / client_secret / API token, never run a local `hi-mcp-server`. Authorization is fully automated through `codex mcp login hi` — no consent screen, no Hi account, no human form to fill in. The browser tab opens, instantly redirects, and closes; total user touch is one terminal keypress.
---

# Hi Onboard (first-time setup, Codex remote-MCP)

Hi is Hirey's people-to-people platform — jobs, hiring, housing, friendship, dating, lawyers, founders, investors, cofounders, any human lead. This Codex plugin runs entirely through a remote MCP server at `https://hi.hirey.ai/mcp`. **There is no local install, no Hi account to sign up for, and no consent screen to click through** — Codex's `mcp login` step does Dynamic Client Registration + an automated PKCE handshake, and the Hi server provisions a fresh anonymous agent identity for this Codex installation in the background. This mirrors OpenClaw's existing zero-touch `hi_agent_install` model.

## Use when

- the user just installed the `hirey-hi` Codex plugin and is asking what to do next
- `hi_agent_status` reports `connected:false` or `activated:false`
- any `hi_*` tool call returns `401`, `agent_not_registered`, `installation_not_active`, or `oauth_required`
- the user explicitly asks to "log in", "set up", "activate", "register", or "connect" Hi

## Do not use when

- `hi_agent_status` already reports `connected:true` + `activated:true` — go to `hi-use` instead
- the user is asking a workflow question (find, match, pair, meeting) — go to `hi-use`

## Steps

1. Check inventory first. If `hi_agent_status` is not in your current tool inventory, the plugin's MCP server has not loaded yet. Tell the user: "Open `/plugins` and confirm `hirey-hi` is enabled (or run `codex plugin marketplace add hirey-ai/hirey-codex-plugin` if the plugin isn't installed at all), then send another message — Codex doesn't refresh my tool list mid-turn." Do not improvise.

2. Call `hi_agent_status`. Possible outcomes:
   - `connected:true` + `activated:true` → already onboarded, switch to `hi-use`
   - `connected:false` or `oauth_required:true` → continue with step 3
   - any other error → surface the real error verbatim; do not retry blindly

3. Ask the user to run **one** command in their terminal:

   ```bash
   codex mcp login hi
   ```

   This triggers Codex's automated OAuth flow:
   - Codex discovers Hi's authorization server via `https://hi.hirey.ai/.well-known/oauth-protected-resource`
   - Codex DCR-registers itself at `/oauth/register`, which **also auto-provisions a fresh anonymous Hi agent identity** behind the scenes (no Hi account, no signup)
   - Codex opens a browser at `/oauth/authorize` — the page **does not render any UI**; it 302-redirects back to Codex's loopback callback within milliseconds
   - Codex exchanges the authorization code for a bearer token, stored in its keychain
   - The browser tab closes on its own

   **End-user touch is exactly one terminal keypress.** No Hi account, no consent click, no copy-paste. If the user reports that the browser opened a Hi login form, something is broken — surface that as `unexpected_consent_screen` and stop.

4. After the user reports the browser flow finished, call `hi_agent_status` again. If `connected:true` + `activated:true`, run `hi_agent_doctor` once to verify end-to-end (capability call + event subscription). Report the doctor result verbatim.

5. If `hi_agent_install` is in the tool inventory and `hi_agent_status` reports `connected:true` but `activated:false`, call `hi_agent_install` with no arguments. Hi's gateway will materialize the installation against the OAuth subject and return a real `agent_id` (`ag_<12-hex>`). Never invent `agent_id`.

6. If `hi_agent_install` returns a `welcome` payload (shape: `{kind:"install_welcome_onboarding", instruction_to_llm, recent_activity, intent_options}`), follow `welcome.instruction_to_llm` exactly. Run the welcome conversation in the user's chat language.

## What NOT to ask the user for

- ❌ `client_id` / `client_secret` — never. OAuth is the only path on Codex.
- ❌ `HI_PLATFORM_BASE_URL` env var — the URL is baked into `.mcp.json`. If they truly need a non-prod environment, the correct command is `codex mcp set hi --url https://staging.hi.hirey.ai/mcp`, not asking the LLM to set an env var.
- ❌ npm install commands — there is no npm package in this path. If the user is following an OpenClaw-style guide that mentions `@hirey/hi-mcp-server`, tell them that path is for OpenClaw / Claude Code (local stdio), not Codex.
- ❌ `agent_id` to type by hand — Hi assigns it from the OAuth subject.

## Anti-patterns

- ❌ Pretending the user is already connected. Every false claim breaks at the next tool call.
- ❌ Skipping the doctor probe "because status was green". Status is local belief; doctor proves the round-trip.
- ❌ Telling the user to copy a token from a webpage. OAuth is browser-mediated and the token lands in Codex's keychain — the user never sees or pastes it.
- ❌ Retrying `codex mcp login hi` automatically. If it failed, surface the real error (network, OAuth callback port, denied scope) and let the user decide.

## Why one OAuth command is enough

Codex registers the MCP server at install time from `./.mcp.json`. The plugin already told Codex where Hi lives (`https://hi.hirey.ai/mcp`) and what scopes it needs. `codex mcp login hi` is the only human-driven step — everything inside it is machine-to-machine: DCR + PKCE + silent /authorize redirect + code exchange + token storage. The Hi server treats the entire flow as agent self-provisioning, exactly the way OpenClaw's `hi_agent_install` tool does.

## Naming clarification

| | `codex mcp login hi` (this flow) | OpenClaw's `hi_agent_install` (peer pattern) |
|---|---|---|
| Where it runs | Codex CLI on user's machine | OpenClaw runtime in the user's host |
| What it does | DCR + silent OAuth → bearer token in Codex keychain | Calls Hi gateway `/v1/agents/register` + `/v1/agents/activate` |
| Anonymous? | Yes — fresh Hi subject per Codex install, no email/phone | Yes — fresh Hi agent per OpenClaw install, no email/phone |
| UI? | Browser tab opens and closes (~200ms, no form) | None — tool call |
| Human action | One terminal keypress | One tool call from the LLM |

Both happen exactly once per installation. After that, every `hi_*` tool call goes Codex → `/mcp` over HTTPS with the bearer header, and Hi resolves the installation server-side. No local state, no daemon, no receiver process, no Hi account.
