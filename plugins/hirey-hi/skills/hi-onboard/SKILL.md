---
name: hi-onboard
description: Connect, upgrade, or recover Hirey Hi in Codex through the hosted MCP endpoint and the normal Codex OAuth flow. Use when workspace_workflows or hi_agent_status is missing, a Hi MCP call returns a structured 401/403 authentication error, the server reports an older plugin version, or the user asks to connect Hi.
---

# Hi Onboard

Hirey Hi is configured by this plugin as a remote MCP server at `https://mcp.hirey.ai/mcp`.
There is no npm package, local daemon, manually pasted API key, or anonymous Person to create.

## Connect

Before running any Codex command, resolve the executable yourself. Do not ask the user to type a
`codex` command and do not assume it is on the shell PATH. Use the first executable match:

1. `command -v codex`;
2. `$CODEX_CLI_PATH`, when set;
3. `/Applications/ChatGPT.app/Contents/Resources/codex`;
4. `/Applications/Codex.app/Contents/Resources/codex`.

Call this resolved path `codex_bin` in your work; never modify `PATH`, never print credentials, and
never make the user edit `~/.codex/config.toml` by hand. If none exists, explain that this Codex
installation has no callable CLI and stop without changing the Hi configuration.

1. Make sure the `hirey-hi` plugin is installed and enabled. Run the marketplace commands yourself
   with `codex_bin`; do not hand them to the user.
2. Run the MCP login with `codex_bin` and let the user finish only the browser OAuth page.
3. Fully quit and relaunch Codex so the MCP server and its tools load in the new session.
4. Verify that `hi_agent_status` and `workspace_workflows` are present.
5. Call `hi_agent_status` with `client_plugin_version: "0.2.10"`, then call
   `workspace_workflows` with `action: catalog` before continuing.

If OAuth returns an error, report that exact error. Do not fall back to a local MCP process, an npm
package, a stable `hi_ak_` key, or an invented installation endpoint.

Authentication establishes the Account, Person, Workspace, Agent, and Agent Session used by Core.
Anonymous browsing may create a pending Agent at the Gateway, but it does not create a permanent
anonymous Person and it is not a replacement for OAuth when private Workspace data is needed.

## Recover an expired or invalid credential

Do not treat every 401 as a request to mint a new anonymous Agent. If Hi worked before, or
`codex mcp list` shows `hi` with `Auth: Bearer token`, an old manual `Authorization` header may be
overriding OAuth. For `invalid_token`, `missing_bearer`, or a failed OAuth refresh:

1. Tell the user that the saved Hi credential is no longer valid and that the browser login will
   reconnect this Codex installation to their existing Hi account.
2. Resolve `codex_bin` as described above and run the logout yourself. Do not read, print, or ask
   the user to paste the old credential.
3. If `hi` is a manual Bearer-token entry, use `codex_bin` to remove only the `hi` MCP entry, then
   recreate the normal OAuth entry at `https://mcp.hirey.ai/mcp`. Do not edit TOML by hand.
4. Use `codex_bin` to start login when the add operation did not already complete OAuth; let the
   user complete only the normal Hi login page in the browser.
5. Fully quit and relaunch Codex. In the new session call `hi_agent_status` with version `0.2.10`,
   then call `workspace_workflows` with `action: catalog` and retry the original request once.

Do not use `/v1/agents/api-keys` for this recovery. That endpoint is only for a user who explicitly
chooses anonymous API-key access; it must not replace or mask an expired signed-in credential.

## Version check and upgrade

Plugin loading reads local files only. The first backend version information arrives during MCP
initialization, `tools/list`, `hi_agent_status`, or a business response. Do not claim the installed
plugin is current before receiving that policy and comparing it with this Skill's version.

Read `_meta.hirey_plugin` (or `structuredContent.plugin` from `hi_agent_status`):

- `update_required: true`: resolve `codex_bin` and run `update_command` yourself only when its
  command names and arguments exactly match the allowlist below; otherwise display it and stop for review. Fully quit and relaunch Codex,
  then stop this session. Continue the original task once in the new session.
- `update_recommended: true` with `update_required: false`: tell the user an update is available but
  do not block a compatible anonymous read or business operation.
- `update_required: null`: the server did not receive the local version. Compare this Skill's
  version (`0.2.10`) with `minimum_supported` and `latest` locally.

The current Codex update is:

```bash
codex plugin marketplace remove hirey
codex plugin marketplace add hirey-ai/hirey-codex-plugin
codex plugin add hirey-hi@hirey
```

After an update, fully restart Codex. Never edit the marketplace file or cached Skill by hand.
The command block describes the allowlisted arguments; invoke them through `codex_bin`. Never ask
the user to paste these commands into Terminal.

Removing and re-adding the marketplace is intentional: older installations may be pinned to a tag,
and `marketplace upgrade` preserves that pin instead of installing the current release.

## Status recovery

Use `error_code`, not the HTTP status by itself:

| HTTP | `error_code` | Action |
|---:|---|---|
| 401 | `missing_bearer` | Use the credential-recovery flow above, finish OAuth, then fully restart Codex. |
| 401 | `invalid_token` | Remove an invalid manual Bearer override when present, run normal OAuth, restart, then retry once. |
| 401 | `token_expired` | Let Codex refresh OAuth; if refresh fails, use the recovery flow. Do not create another Agent. |
| 403 | `insufficient_oauth_scope` | Reauthorize the returned `required_scopes`; do not reinstall or create an Agent. |
| 403 | existing identity-binding requirement | Bind through the returned Google/email/phone `next`, then retry once. |
| 403 | `forbidden` | Stop and explain the business permission boundary; repeated login will not fix it. |

A valid pending Agent may continue with the existing anonymous operations `people.find`,
`people.explain`, and staged `capture.record`. Do not require login merely because the user is
anonymous. Login is required only when the requested operation needs private Workspace data or an
authenticated write.

## Boundaries

- The plugin only declares the remote MCP connection and usage skills.
- `hi-mcp-server` adapts MCP and forwards the live capability call.
- `hi-auth` performs OAuth and issues the Agent Session credential.
- `hi-platform` exposes the public capability catalog and call endpoint.
- Secretary Core owns Person, Workspace, Message, Moment, Relationship and the other business
  records.

Readiness requires both: `hi_agent_status` reports a valid credential, and
`workspace_workflows(action: "catalog")` succeeds. `activated:false` may still be a valid anonymous
pending Agent; apply the anonymous-operation boundary above instead of treating it as disconnected.
