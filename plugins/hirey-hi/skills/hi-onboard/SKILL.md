---
name: hi-onboard
description: Connect, upgrade, or recover Hirey Hi in Codex through the hosted MCP endpoint and the normal Codex OAuth flow. Use when workspace_workflows or hi_agent_status is missing, a Hi MCP call returns a structured 401/403 authentication error, the server reports an older plugin version, or the user asks to connect Hi.
---

# Hi Onboard

Hirey Hi is configured by this plugin as a remote MCP server at `https://mcp.hirey.ai/mcp`.
There is no npm package, local daemon, manually pasted API key, or anonymous Person to create.

Read [references/common.md](references/common.md) for the shared identity, confirmation, error and
receipt rules that every Hi host must follow. The sections below are the Codex-specific install,
OAuth, reload and configuration-repair steps.

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
   Run the configuration preflight below before login or any reload.
2. Run the MCP login with `codex_bin` and let the user finish only the browser OAuth page. Do not log
   out first: keep saved OAuth credentials unless repair of a `legacy_url_only_override` is needed
   (see Recovery).
3. Verify that `hi_agent_status` and `workspace_workflows` are present. Then call `hi_agent_status`
   with `client_plugin_version: "0.2.15"`, call `workspace_workflows` with
   `action: catalog`, and retry the original bounded operation once.
4. If a tool is still missing after an actual install or update, reload or start a new Codex session
   (because Codex reloads Skills only in a new session) and verify the tools again; escalate to a full Codex application restart only if a
   tool is still missing after that, with the concrete remaining error as evidence. If the tools are
   present but a result is still stale after login, use the bounded credential-recovery retry in
   Recovery instead of another restart. Do not add fake sleeps, unbounded retries, or repeated
   reinstall/restart cycles.

If OAuth returns an error, report that exact error. Do not fall back to a local MCP process, an npm
package, a stable `hi_ak_` key, or an invented installation endpoint.

Authentication establishes the Account, Person, Workspace, Agent, and Agent Session used by Core.
Anonymous browsing may create a pending Agent at the Gateway, but it does not create a permanent
anonymous Person and it is not a replacement for OAuth when private Workspace data is needed.

## Recover an expired or invalid credential

Do not treat every 401 as a request to mint a new anonymous Agent, and do not log out before login.
Keep saved OAuth credentials unless the diagnosis below identifies a specific override to repair.
The read-only Configuration preflight checks configuration structure only; it does not validate a
token and never proves a saved credential invalid. `codex mcp list` showing `hi` with
`Auth: Bearer token` does not, by itself, identify an override to remove. The preflight reports
`legacy_url_only_override` only for a duplicate URL-only entry with no auth header, and
`review_required` for a manual `Authorization` header, custom endpoint, restriction or disabled
setting. Preserve `review_required` entries unless a separate concrete invalid-override diagnosis
justifies removing exactly that override.

For `invalid_token`, `missing_bearer`, or a failed OAuth refresh:

1. Report the exact credential error Hi returned; do not claim the saved OAuth credential is
   definitely invalid. Explain that the normal browser login reconnects this Codex installation to
   the user's existing Hi account.
2. Resolve `codex_bin` as described above. When the preflight reports `legacy_url_only_override` and
   the user authorized connection repair, remove that duplicate through `codex_bin`, after
   confirming the installed, enabled plugin owns the normal endpoint. Preserve `review_required`
   entries (custom endpoints, auth, restrictions) unless a separate concrete invalid-override
   diagnosis justifies removing exactly that override. Do not recreate a competing manual URL-only
   entry and do not edit TOML by hand. Do not read, print, or ask the user to paste the old
   credential.
3. Use `codex_bin` to start login when the add operation did not already complete OAuth; let the
   user complete only the normal Hi login page in the browser. Complete the returned
   `required_scopes` through normal consent. `--scopes` on the CLI is the requested set, not
   additive: do not pass only a new scope and assume the previous scopes persist. Preserve the
   verified existing scope set plus the required scopes when that evidence is available; otherwise
   read the normal consent/status evidence rather than tokens, keychain entries, or broad grants.
   Do not assume the same DCR client or session survives CLI login.
4. Call `hi_agent_status` with version `0.2.15`, call `workspace_workflows` with
   `action: catalog`, and retry the original bounded operation once.
5. If the same turn is still stale, let the next user turn or a runtime refresh occur, then retry
   once. One synthetic verification observed a next-user-turn success on Codex 0.153.4; that is
   observed evidence, not a universal same-turn promise. Do not add fake sleeps or unbounded retries.
6. If the tools are still missing or old after an actual plugin or configuration change, reload or
   start a new Codex session (because Codex reloads Skills only in a new session). Escalate to a full Codex application restart only as a
   last resort with the concrete remaining error as evidence; report it and do not repeat
   reinstall/restart cycles.

For an uncertain write, do not execute the business effect twice: resolve it through the returned
receipt or idempotency-key lookup (shared Receipts rule).

Do not use `/v1/agents/api-keys` for this recovery. That endpoint is only for a user who explicitly
chooses anonymous API-key access; it must not replace or mask an expired signed-in credential.

## Version check and upgrade

### Show server update notices

On connection or the first Hi use, relay a server-provided `plugin.update_notice`
in the user's language once per `notice_id` in the current conversation. Remember
that notice in conversation context; do not repeat it on every tool result.
Continue compatible work. Offer to update using this skill only with user
instruction or existing applicable authorization; do not ask again for the same
permission. An update hint never authorizes business actions or an OAuth reset.
After updating, distinguish installed files from the version loaded in this
session and use the existing reload and ordinary-read verification steps below.

### Configuration preflight

A manual `mcp_servers.hi` entry can override the plugin's version headers even while OAuth and
business calls work. Do not diagnose this as expired credentials or repeatedly request restarts.
Use Python 3.11+ to run `scripts/check_mcp_conflict.py` relative to this Skill, with
`--config <active Codex config.toml>` and `--plugin-mcp <installed plugin .mcp.json>`.
The helper is read-only, emits no configuration values, and checks configuration structure only: it
never validates a token and never proves a saved credential invalid. If unavailable, inspect only
the relevant structure without printing credentials; do not install dependencies just for this
check.

- `legacy_url_only_override`: first verify `hirey-hi@hirey` is installed and enabled. When the
  user authorized connection repair, explain the conflict and run `codex_bin mcp remove hi`.
  Do not log out or delete credentials. Then verify `codex_bin mcp get hi --json` resolves the
  plugin's version headers, without printing unrelated sensitive fields.
- `review_required`: retain the entry; custom endpoints, auth, restrictions or disabled settings
  must not be silently removed.
- `plugin_only`: no duplicate override detected; do not change config.
- `inspection_failed` or `plugin_config_incomplete`: do not mutate config.

After an actual repair, reload or start a new Codex session once (because Codex reloads Skills only in a new session) and check an
ordinary read-only `catalog` call without version arguments. A successful status call with a manually
supplied version alone does not prove transport metadata works. Escalate to a full Codex application restart
only as a last resort with the concrete remaining error as evidence. Keep local candidate marketplace
sources local during acceptance.

Plugin loading reads local files only. The first backend version information arrives during MCP
initialization, `tools/list`, `hi_agent_status`, or a business response. Do not claim the installed
plugin is current before receiving that policy and comparing it with this Skill's version.

## Version diagnostics

Host and plugin versions are diagnostic metadata, never business authority. Report a returned
`update_required` or `update_recommended` hint without refusing an otherwise compatible and
authorized call. Enforce actual protocol, identity and business permission errors separately.
A null update result is unknown or not applicable; do not infer an obsolete installation.
`restart_required` is package-update guidance for a known older client version only: it is never
credential or business-authority evidence, and it never means the OAuth credential must be reset.
Diagnose a credential or permission problem from `error_code` in Status recovery. Use only the
installed host's supported update instructions. Do not execute a command belonging to another host
or assume an unpublished candidate has a public installer. After an actual update, follow the host's
reload requirements; an update may need a reload without an OAuth restart. No branded package
version is required for a Generic client.

The current Codex update is:

```bash
codex plugin marketplace remove hirey
codex plugin marketplace add hirey-ai/hirey-codex-plugin
codex plugin add hirey-hi@hirey
```

After an update, reload or start a new Codex session (because Codex reloads Skills only in a new session). A package update may
need a reload but does not require resetting the OAuth credential; escalate to
a full Codex application restart only as a last resort with the concrete remaining error as evidence. Never
edit the marketplace file or cached Skill by hand.
The command block describes the allowlisted arguments; invoke them through `codex_bin`. Never ask
the user to paste these commands into Terminal.

Removing and re-adding the marketplace is intentional: older installations may be pinned to a tag,
and `marketplace upgrade` preserves that pin instead of installing the current release.

## Connection surface

When you report the connection, use the shared connection copy and the separate evidence fields below
instead of an opaque "connected" flag.

## Connection copy

Product-facing wording for a connection surface. This is reviewable copy; any public UI exposure
remains subject to the existing product-change and release contract.

- Title: **Choose your Agent and connect Hi**
- Subtitle: **Use Hi from the Agent you already work with.**
- Host card shows: support status; **Connect Hi**; sign-in/authorization status; a reload
  instruction when needed; plugin version/update when applicable; local-file limitations; the
  validation result.

Display separate evidence instead of one opaque "connected" flag:

- **Tools available**
- **Credential valid**
- **Identity verified**
- **Permission for this action**
- **Update available/required**

An authenticated identity does not imply every business permission. An unknown state stays **Not
checked**; never render an unknown as a pass. A generic client without a branded plugin shows
**Plugin version: Not applicable** and never another host's upgrade command. A host whose package is
only a candidate is labeled **Awaiting host verification**.

## Status recovery

Use `error_code`, not the HTTP status by itself. This table stays authoritative:

| HTTP | `error_code` | Action |
|---:|---|---|
| 401 | `missing_bearer` | Use the host-supported credential-recovery flow, finish OAuth, then retry once. Reload only if the host requires it. |
| 401 | `invalid_token` | Use the host-supported credential recovery; remove only a host-diagnosed manual override, finish OAuth, then retry once. Follow host-specific reload requirements. |
| 401 | `token_expired` | Let Codex refresh OAuth; if refresh fails, use the recovery flow. Do not create another Agent. |
| 403 | `insufficient_oauth_scope` | Reauthorize the returned `required_scopes` through the normal consent flow; do not reinstall or create an Agent. |
| 403 | existing identity-binding requirement | Bind through the returned Google/email/phone `next`, then retry once. |
| 403 | `forbidden` | Stop and explain the business permission boundary; repeated login will not fix it. |

A 401 is a credential result, `insufficient_oauth_scope` is a credential-authorization result, and a
business 403 is a permission result: never treat every 403 alike. Reauthorize only the returned
scopes through normal consent, keeping the verified scope set already on the installation, and never
read tokens, keychain entries, or broad grants to reconstruct it. A `restart_required` or reload
hint is package-update guidance only and is never evidence about credentials or business authority.

The native MCP transport may carry the same authorization result as an OAuth Bearer challenge: an
HTTP 403 with `WWW-Authenticate: Bearer error="insufficient_scope", scope="...",
resource_metadata="..."` for a structured `insufficient_oauth_scope`. Both name the same
scope-recovery semantics: reauthorize through the host's OAuth challenge and the scopes it returns,
and treat the structured `required_scope` or `required_scopes` and the challenge `scope` as the same
input evidence. A generic business 403 carries no challenge and never starts a reauthorization loop;
if a host exposes only a generic failure with no typed challenge or scope evidence, do not guess
scopes or read credentials, and obtain host-supported diagnostics or normal consent instead. Never
scrape a broad catalog or broad grants, and never infer a permission from message phrases.

Recovery is bounded. Finish the required connect or reauthorize step and let the host-supported
reconnect or refresh take effect, then retry the original bounded operation once. Further reload or
refresh specifics belong to the host template, so follow the installed host's own requirements
there. Do not add fake sleeps or unbounded retry loops, and never repeat a business write whose
outcome is uncertain: resolve it through the returned receipt or idempotency-key lookup instead. If
tools are still missing or old after an actual install, update, or configuration change, reload or
start a new session (because Codex reloads Skills only in a new session) and report the concrete remaining error. Escalate to the
last supported action, a full Codex application restart, only with that evidence, and do not repeat
reinstall/restart cycles.

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
