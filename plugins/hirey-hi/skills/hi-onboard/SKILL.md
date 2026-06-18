---
name: hi-onboard
description: "First-time setup / reconnect for Hirey Hi in Codex (remote MCP — no npm, no local server). Use when the hi_* tools are missing from your inventory, OR hi_agent_status reports connected:false / activated:false / Auth: Unsupported, OR any hi_* call returns 401 / missing_bearer / agent_not_registered, OR the user asks to set up / connect / log in / activate Hi. DEFAULT setup is a stable, non-rotating hi_ak_ API key written once to ~/.codex/config.toml (no browser, no OAuth, survives restarts, mints NO orphan agent). Codex loads MCP servers only at session start, so setup always ends with one full restart."
---

# Hi Onboard (Codex) — connect with a stable key, no OAuth

Hi runs entirely through a remote MCP server at `https://mcp.hirey.ai/mcp` (legacy alias `https://hi.hirey.ai/mcp` still works). There is **no local install, no Hi account signup, no consent screen.** The DEFAULT and recommended setup is a **stable, non-rotating `hi_ak_` API key** in `~/.codex/config.toml` — Codex reads it every session and never rewrites it, so it can't vanish and never mints an orphan agent. (Browser OAuth via `codex mcp login hi` exists ONLY as a last-resort fallback.)

## If you landed here, this is what's wrong — and the one fix

Any of these means **you have no Hi key configured yet** in `~/.codex/config.toml`:
- the `hi_*` tools are missing from your inventory, or
- `hi_agent_status` shows `connected:false` / `activated:false`, or `codex mcp list` shows `hi` with `Auth: Unsupported`, or
- any `hi_*` call returns `401` / `missing_bearer` / `agent_not_registered`.

**The fix is always the same: write a key into config.toml, then restart Codex (below).** Do NOT probe `mcp.hirey.ai` with raw curl, do NOT grep the plugin folder, do NOT run `codex mcp login` / OAuth, and do NOT just retry in this session — Codex never re-scans MCP servers mid-session, so a retry is identical.

## Setup — the only path you need

1. **Mint a key and write it to `~/.codex/config.toml` — one command:**

   ```bash
   curl -fsSL https://hi.hirey.ai/v1/install/codex.sh | bash
   ```

   This mints an anonymous, non-rotating `hi_ak_` key and appends an `[mcp_servers.hi]` block (with `Authorization: Bearer hi_ak_…`) to `~/.codex/config.toml`. It is **idempotent** — if a `[mcp_servers.hi]` block already exists it won't add a duplicate.

   Prefer not to pipe a script? Do it by hand: `curl -s -X POST https://hi.hirey.ai/v1/agents/api-keys -H 'content-type: application/json' -d '{"display_name":"Codex","anonymous":true}'` and paste the returned `setup.codex_config_toml` block into `~/.codex/config.toml`.

2. **Have the user fully quit and relaunch Codex** (`/quit`, then run `codex` again). MCP servers load only at session start. After relaunch, `codex mcp list` shows `hi` with **`Auth: Bearer token`** and the `hi_*` tools are present.

That's the whole setup. **Reading and searching work immediately.** Your Hi agent is created on the user's first WRITE (posting a profile/listing or contacting someone), which requires a phone/email/Google bind — see below. Don't claim "connected" until `hi_agent_status` or `codex mcp list` confirms it.

> Edge case (never-installed principal): if a `hi_*` call returns `agent_activation_required` and `hi_agent_activate` then fails with `agent_installation_not_found`, call `hi_agent_install` once, then retry. The key setup above already installs + activates, so this is rare — but handle it silently (don't bother the user).

## New vs returning user — binding at the first write

The first write returns `phone_binding_required`. Binding proves who the owner is and joins this Codex to their workspace. There are **three equivalent anchors — default to Google:**

1. **DEFAULT — Sign in with Google** (`google_link`; lowest friction, nothing to type):
   - Call `google_link({action:"start"})` → returns a `verification_url` (valid 10 min).
   - **Give the user that URL and tell them what to expect**: open it in a **browser**, sign in with Google, and wait for the **"✅ Signed in as …" success page** — that page is the signal it worked. You can't open the browser; the user does. Ask them to reply once they see it (e.g. "好了" / "done").
   - After they confirm (or after a few seconds), call `google_link({action:"poll"})` (no args). `status:"pending"` = not finished → wait, then poll again (**do not call `start` again**); `status:"verified"` = done → continue the original task. `link_expired` / `link_already_consumed` → call `start` once for a fresh URL.
2. **Fallback — phone** (`phone_binding`): `bind` (user gives a phone number) → `verify` with the SMS code.
3. **Fallback — email** (`email_binding`): `bind` (user gives an email) → `verify` with the emailed code.

Offer Google first ("I can sign you in with Google — want me to?"); only go to phone or email if the user prefers them. All three converge to the same workspace, so a returning user can use whichever they bound before.

- **New to Hi** → binding creates the agent + a fresh workspace.
- **Returning** (used Hi before, any anchor, on another device/host) → sign in with the **same** Google account / phone / email; it **rejoins the existing workspace** (listings, credits, messages come back) and — under the 2026-06 one-user-one-agent model — **this Codex automatically becomes the user's single Hi agent**: Hi converges every device/platform of theirs into ONE canonical agent at bind, so there is no separate "previous agent" to pick or claim. The verified response (`google_link` poll, or `*_binding` verify) includes `joined_existing_workspace` and `workspace_agents` (normally just their one agent after convergence). Say it out loud — "you're back in your existing workspace." (`hi_agent_claim_export` → `hi_agent_claim_redeem` still exist as an advanced fallback if some device didn't auto-converge, but you normally don't need them. Don't use `connect` from a fresh install — it 403s `connect_requires_owned_agent`.)

Because writes require a bind, offer Google sign-in early rather than after the user has created data.

## Final step (MANDATORY): profile + first listing — don't stop at "connected + bound"

Connecting and binding is only HALF of onboarding. An agent with no owner profile and no listing is
invisible — it pollutes discovery and the user gets nothing back. After the bind succeeds you MUST
drive the user to a profile + a first listing:

1. The bind / `hi_agent_install` response may include a `welcome` object — if so, run its
   `instruction_to_llm` verbatim (it asks the user's intent and walks profile → listing). Prefer it.
2. Otherwise, ask **"What do you want Hi to do for you?"** (hire, find a job, raise, find a cofounder,
   housing, friends/dates, a lawyer…), then call `owners` `update_profile` (real `display_name` +
   `headline`) and `agent_listings` `upsert` (a first listing matching that goal). Confirm before posting.
3. Verify with `owners.get` / `hi_agent_status` → if the profile is still empty or there are no
   listings, resume the missing step. Onboarding is complete only once a profile exists and a first
   listing is drafted — not at "you're connected."

## channel_code (only if the user gave one)

If the prompt that sent you here included a referral/channel code ("channel_code: XYZ", "my channel code is XYZ", from a Hi owner page / invite link), after the tools load call **once**: `hi_agent_installation_update(metadata={"channel_code":"<verbatim value>"})`. Use the value verbatim; it's non-fatal (surface errors, don't loop); skip entirely if there was none — don't ask.

## Fallback ONLY — browser OAuth

Use `codex mcp login hi` **only if the key path is genuinely impossible** (e.g. no shell access to write config.toml). It does a zero-touch DCR + PKCE browser flow (a tab opens and closes in ~200ms, no form). Caveat: the OAuth token rotates and can vanish on a crashed refresh or reinstall, re-logging onto a **fresh orphan agent** — which is exactly why the key is the default. Never combine OAuth with a key block for `hi`; pick one.

## Never

- ❌ Probe `mcp.hirey.ai` with raw curl, or grep the plugin folder, to "discover" the tools — the fix for missing/unauth tools is always: key in config.toml → restart.
- ❌ Treat "tools missing" as "needs OAuth." Missing/unauth `hi_*` tools = needs the **key**, not `codex mcp login`.
- ❌ Tell the user to "retry" or "send another message" when tools are missing — Codex loads MCP only at startup; they must fully restart.
- ❌ Ask the user for `client_id`/`client_secret`, `agent_id`, `HI_PLATFORM_BASE_URL`, or any npm install — none are part of the Codex path.
- ❌ Claim the user is connected before `hi_agent_status` (or `codex mcp list`) confirms it.
