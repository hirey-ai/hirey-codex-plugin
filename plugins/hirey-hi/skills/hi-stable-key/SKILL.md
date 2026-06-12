---
name: hi-stable-key
description: Switch Codex's Hi connection to a STABLE API key so the credential never "mysteriously vanishes" and the user is never forced to re-login. Use when the user reports that Hi stopped working / asks them to log in again / their agent changed after a restart, OR explicitly asks for an "API key", "stable login", "stop logging in", or a setup that survives restarts/crashes. Background — Codex's default `codex mcp login hi` path stores a ROTATING OAuth token in Codex's own credential store and rewrites it on every refresh; a crash/kill mid-rewrite loses it (the file vanishes), forcing re-login onto a fresh anonymous agent. A Hi API key (hi_ak_…) is a NON-rotating credential placed as a literal header in `~/.codex/config.toml` — Codex reads it every session and never rewrites it, so it cannot vanish. This is the same idea as pasting a static key into the Codex.app GUI. No npm, no npx, no local daemon, no browser.
---

# Hi — stable API key setup (never-vanishing credential, Codex)

Use this when the user wants a Hi connection that survives crashes/restarts without re-login. The result: a non-rotating `hi_ak_…` key living in `~/.codex/config.toml` as a literal `Authorization` header. Codex reads it every session; nothing ever rewrites it; it cannot be lost the way the rotating OAuth token can.

## When to use
- The user says Hi "stopped working", "logged me out", "asks me to log in again", or "my agent changed / lost my listings after restarting".
- The user asks for an "API key", a "stable" / "permanent" login, or to "stop having to log in".
- You are doing first-time setup and want the robust path (recommended over `codex mcp login hi`).

## When NOT to use
- Pure workflow questions (find / match / pair / meeting) → `hi-use`.
- The user is happy with the existing OAuth login and just needs to (re)connect → `hi-onboard`.

## Steps

1. **Mint the key.** Ask the user to run this in their terminal (no auth needed; it provisions a fresh Hi agent and returns a ready-to-paste key + config block):

   ```bash
   curl -s -X POST https://hi.hirey.ai/v1/agents/api-keys \
     -H 'content-type: application/json' \
     -d '{"display_name":"Codex (my Mac)","anonymous":true}'
   ```

   The response contains `api_key` (starts with `hi_ak_`) and `setup.codex_config_toml` (a ready TOML block). Treat the key like a password.

2. **Install the key into Codex's static config.** Append the returned `setup.codex_config_toml` to `~/.codex/config.toml`. It looks like:

   ```toml
   [mcp_servers.hi]
   url = "https://mcp.hirey.ai/mcp"

   [mcp_servers.hi.http_headers]
   Authorization = "Bearer hi_ak_…"
   ```

   - If a `[mcp_servers.hi]` block already exists (e.g. from a previous `codex mcp login hi`), replace it with this one — and run `codex mcp logout hi` first to drop the old rotating OAuth token so Codex uses only the key.
   - `config.toml` is a plain file Codex reads at every launch (terminal **and** the Codex.app GUI), so the key works without any environment variable or LaunchAgent.
   - Alternative (terminal-launched Codex only): keep the key in an env var instead — `export HI_API_KEY='hi_ak_…'` in your shell profile, then `codex mcp add hi --url https://mcp.hirey.ai/mcp --bearer-token-env-var HI_API_KEY`.

3. **Fully restart Codex.** It loads MCP servers only at session start, so the new config doesn't take effect mid-session. Quit and relaunch.

4. **Verify.** Call `hi_agent_status`. Expect `connected:true`. The key is now Codex's credential and will not vanish on crashes/restarts (it is never rewritten).

5. **Bind your identity (this is what CREATES your agent).** The key starts with NO agent at all — reads work anonymously and nothing is created server-side until you bind. Binding (or your first write) creates your agent and joins it to the workspace for that phone/email — same key, no second restart. To do it:
   - **Existing Hi user** (already has Hi on another device — Claude Code / OpenClaw / phone): just bind the **same** phone/email/Google here — Hi automatically converges this Codex into the user's single canonical agent (same identity; listings/threads/credits all there), no manual step needed. (`hi_agent_claim_export` → `hi_agent_claim_redeem` remains an advanced fallback if a device didn't auto-converge.)
   - **New user**: call `phone_binding` and complete the SMS code. This binds the agent to the user's phone number; that phone number is the durable identity anchor — even if the key is ever rotated, re-binding the same phone recovers the same workspace.

## Why this fixes the vanishing
- OAuth token: rotates ~hourly → Codex rewrites its credential file on every refresh → a crash/kill mid-rewrite truncates/loses it → "credential vanished", forced re-login on a new anonymous agent.
- API key (`hi_ak_`): non-rotating → written once into `config.toml` → only ever read afterward → no rewrite window → cannot vanish.

## Never
- Never `npm install` / `npx` anything — this path is pure remote MCP + a static config header.
- Never paste the key into a chat or commit it; it is a bearer credential.
