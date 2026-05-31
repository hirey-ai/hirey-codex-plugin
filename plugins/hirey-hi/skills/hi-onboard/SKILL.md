---
name: hi-onboard
description: "First-time setup for Hirey Hi in Codex. Use when hi_agent_status is missing from the tool inventory or reports connected=false, when any hi_* tool returns an auth or agent_not_registered error, or when the user asks to set up, log in to, connect, activate, or install Hi. This is Codex's remote-MCP path (no npm install, no client_id/secret, no local hi-mcp-server) authorized via codex mcp login hi. Codex loads MCP servers only at session start, so a freshly enabled hirey-hi plugin needs a full Codex restart before any hi_* tool appears — follow the steps in this skill body; never tell the user to just retry mid-session."
---

# Hi Onboard (first-time setup, Codex remote-MCP)

Hi is Hirey's people-to-people platform — jobs, hiring, housing, friendship, dating, lawyers, founders, investors, cofounders, any human lead. This Codex plugin runs entirely through a remote MCP server at `https://mcp.hirey.ai/mcp` (the legacy alias `https://hi.hirey.ai/mcp` is preserved for installs published before the cutover and continues to work — both URLs route to the same backend and accept the same tokens). **There is no local install, no Hi account to sign up for, and no consent screen to click through** — Codex's `mcp login` step does Dynamic Client Registration + an automated PKCE handshake, and the Hi server provisions a fresh anonymous agent identity for this Codex installation in the background. This mirrors OpenClaw's existing zero-touch `hi_agent_install` model.

## Use when

- the user just installed the `hirey-hi` Codex plugin and is asking what to do next
- `hi_agent_status` reports `connected:false` or `activated:false`
- any `hi_*` tool call returns `401`, `agent_not_registered`, `installation_not_active`, or `oauth_required`
- the user explicitly asks to "log in", "set up", "activate", "register", or "connect" Hi

## Do not use when

- `hi_agent_status` already reports `connected:true` + `activated:true` — go to `hi-use` instead
- the user is asking a workflow question (find, match, pair, meeting) — go to `hi-use`

## Steps

1. **Check inventory first.** If `hi_agent_status` is NOT in your current tool inventory, the `hirey-hi` MCP server has not loaded into this Codex session. **This is not fixable mid-session** — Codex spawns MCP servers exactly once in `McpConnectionManager::new` at startup and never re-scans (openai/codex#4955, #7767 — marked "not planned"). Telling the user to "try again" or "send another message" will fail every time.

   Tell the user (paraphrase into their chat language, but keep both substeps):

   > "Hi's MCP isn't loaded into this Codex session yet. Two things:
   >
   > 1. **Confirm the plugin is enabled.** Inside Codex, run `/plugins` → Hirey marketplace → `hirey-hi` → install + enable. If the marketplace isn't even listed, run `codex plugin marketplace add hirey-ai/hirey-codex-plugin` from your shell first, then enable inside `/plugins`.
   > 2. **Fully restart Codex.** Codex only loads MCP servers at session start, so a plugin you just enabled does NOT load mid-session. Quit Codex (`/quit`, `Ctrl-C`, or close the window), relaunch `codex`, then ask me again.
   >
   > If after the restart `/plugins` still shows `hirey-hi` with a `Set up in MCP settings` warning, ignore that — it's a known Codex UI bug ([openai/codex#17360](https://github.com/openai/codex/issues/17360)) where the plugin page mis-reports a runtime-registered MCP as needing manual setup. Verify with `codex mcp list` in a separate shell; `hi` should appear with `Auth: OAuth`."

   Do not retry tool calls "to check if it shows up." It won't.

2. Call `hi_agent_status`. Possible outcomes:
   - `connected:true` + `activated:true` → already onboarded, switch to `hi-use`
   - `connected:false` or `oauth_required:true` → continue with step 3
   - any other error → surface the real error verbatim; do not retry blindly

3. Ask the user to run **one** command in their terminal:

   ```bash
   codex mcp login hi
   ```

   This triggers Codex's automated OAuth flow:
   - Codex discovers Hi's authorization server via `https://mcp.hirey.ai/.well-known/oauth-protected-resource` (or `https://hi.hirey.ai/.well-known/oauth-protected-resource` for legacy installs — same metadata, different host)
   - Codex DCR-registers itself at `/oauth/register`, which **also auto-provisions a fresh anonymous Hi agent identity** behind the scenes (no Hi account, no signup)
   - Codex opens a browser at `/oauth/authorize` — the page **does not render any UI**; it 302-redirects back to Codex's loopback callback within milliseconds
   - Codex exchanges the authorization code for a bearer token, stored in its keychain
   - The browser tab closes on its own

   **End-user touch is exactly one terminal keypress.** No Hi account, no consent click, no copy-paste. If the user reports that the browser opened a Hi login form, something is broken — surface that as `unexpected_consent_screen` and stop.

4. After the user reports the browser flow finished, call `hi_agent_status` again. If `connected:true` + `activated:true`, run `hi_agent_doctor` once to verify end-to-end (capability call + event subscription). Report the doctor result verbatim.

5. If `hi_agent_install` is in the tool inventory and `hi_agent_status` reports `connected:true` but `activated:false`, call `hi_agent_install` with no arguments. Hi's gateway will materialize the installation against the OAuth subject and return a real `agent_id` (`ag_<12-hex>`). Never invent `agent_id`.

6. If `hi_agent_install` returns a `welcome` payload (shape: `{kind:"install_welcome_onboarding", instruction_to_llm, recent_activity, intent_options}`), follow `welcome.instruction_to_llm` exactly. Run the welcome conversation in the user's chat language.

7. **Forward the channel_code if the user gave one.** If the user prompt that triggered this onboard contained a `channel_code` value (typical phrasings: "referral channel is `XYZ`", "channel_code: XYZ", "my channel code is XYZ" — coming from a Hi owner page or invite link), call **once** after `hi_agent_status` reports `connected:true`:

   ```
   hi_agent_installation_update(metadata={"channel_code":"<value>"})
   ```

   - Use the value **verbatim**. Never invent, normalize, or alter it.
   - This call is non-fatal — if it errors, surface the error but don't loop. The user's session is already connected; channel attribution is best-effort.
   - If no channel_code was in the prompt, **skip this step entirely**. Don't ask the user.
   - Why: Codex's `marketplace add` and `mcp login` give the plugin no install-time arguments, so the only way to attribute this install back to a specific Hi owner page / invite link is for the assistant to forward what the user pasted. `hi_agent_installation_update` writes the value to `agent_installations.metadata_json.channel_code`, and the gateway lifts it to `agents.metadata_json.channel_code` for the admin attribution query.

## Identity durability — bind a phone so a re-login doesn't orphan the user's data

Your Hi identity is **anonymous and per-OAuth-subject**. Codex's `mcp login` mints a fresh
subject via DCR each time it runs; the access token auto-refreshes for ~30 days keeping the
SAME identity, but if the refresh token expires (~30d), or the user resets / re-runs
`codex mcp login hi`, Codex does a **new** DCR → a **brand-new Hi agent** → the listings,
credits, and message history from the old one are no longer reachable from the new one.

**The durable anchor is a phone bind.** Tell the user (especially before they post a listing
or contact anyone): binding a phone once (the `phone_binding` tool: `bind` then `verify`)
attaches this agent to a phone-keyed **workspace**. After ANY future re-login, binding the
**same phone** on the new agent rejoins that same workspace — listings, credits, and messages
come back, and agents on the same phone share one workspace. Writing already requires a bind
(you'll get `phone_binding_required`), so prompt for it early rather than after data is created.
If a freshly-connected agent has no listings/credits/history, assume it may be a re-login and
proactively offer: "bind the same phone you used before to restore your previous Hi workspace."

## What NOT to ask the user for

- ❌ `client_id` / `client_secret` — never. OAuth is the only path on Codex.
- ❌ `HI_PLATFORM_BASE_URL` env var — the URL is baked into `.mcp.json`. If they truly need a non-prod environment, the correct command is `codex mcp set hi --url https://staging.hi.hirey.ai/mcp`, not asking the LLM to set an env var.
- ❌ npm install commands — there is no npm package in this path. If the user is following an OpenClaw-style guide that mentions `@hirey/hi-mcp-server`, tell them that path is for OpenClaw / Claude Code (local stdio), not Codex.
- ❌ `agent_id` to type by hand — Hi assigns it from the OAuth subject.

## Anti-patterns

- ❌ Pretending the user is already connected. Every false claim breaks at the next tool call.
- ❌ Telling the user to "send another message" or "retry in this session" when `hi_agent_status` is missing from the inventory. Codex never re-scans MCP servers mid-session — the user MUST fully quit and relaunch Codex (openai/codex#4955, #7767, #17360). A retry will be silently identical.
- ❌ Treating Codex's `Set up in MCP settings` UI warning on the `/plugins` page as a real problem after a restart. It is a known Codex UI bug (openai/codex#17360); the MCP is actually registered. Verify via `codex mcp list`.
- ❌ Skipping the doctor probe "because status was green". Status is local belief; doctor proves the round-trip.
- ❌ Telling the user to copy a token from a webpage. OAuth is browser-mediated and the token lands in Codex's keychain — the user never sees or pastes it.
- ❌ Retrying `codex mcp login hi` automatically. If it failed, surface the real error (network, OAuth callback port, denied scope) and let the user decide.
- ❌ Confusing "MCP not loaded" with "OAuth not done." If the tools are missing entirely, the fix is a Codex **restart**, not `codex mcp login hi`. If the tools exist but return 401/`oauth_required`, the fix is `codex mcp login hi`. Don't mix the two diagnoses.

## Why one OAuth command is enough

Codex registers the MCP server at install time from `./.mcp.json`. The plugin already told Codex where Hi lives (`https://mcp.hirey.ai/mcp`) and what scopes it needs. `codex mcp login hi` is the only human-driven step — everything inside it is machine-to-machine: DCR + PKCE + silent /authorize redirect + code exchange + token storage. The Hi server treats the entire flow as agent self-provisioning, exactly the way OpenClaw's `hi_agent_install` tool does.

## Naming clarification

| | `codex mcp login hi` (this flow) | OpenClaw's `hi_agent_install` (peer pattern) |
|---|---|---|
| Where it runs | Codex CLI on user's machine | OpenClaw runtime in the user's host |
| What it does | DCR + silent OAuth → bearer token in Codex keychain | Calls Hi gateway `/v1/agents/register` + `/v1/agents/activate` |
| Anonymous? | Yes — fresh Hi subject per Codex install, no email/phone | Yes — fresh Hi agent per OpenClaw install, no email/phone |
| UI? | Browser tab opens and closes (~200ms, no form) | None — tool call |
| Human action | One terminal keypress | One tool call from the LLM |

Both happen exactly once per installation. After that, every `hi_*` tool call goes Codex → `/mcp` over HTTPS with the bearer header, and Hi resolves the installation server-side. No local state, no daemon, no receiver process, no Hi account.
