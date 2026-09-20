---
name: hi-instance
description: Bind this Codex installation on this computer to one Hi local instance and prove it with a local Ed25519 key. Use when Hi reports instance_binding_required, when binding must be recovered after instance_revoked, when the user asks which computer this Agent is running on, or when a private handoff between the user's own computers must be addressed by name.
---

# Hi local instance

Hi separates two different questions. OAuth answers *which Person and Agent* may act. A local
instance key pair answers *which end of the user's work* is calling. This skill owns the local half:
one key pair per host per computer, created and kept on the user's machine.

`scripts/hi_instance.py` is a stdlib-only helper. It never prints the private key, never derives an
instance from a hostname, an IP address, an OAuth client or a plugin version, and it never needs the
user to type a device id or edit an MCP configuration.

## Local state and its location

The helper keeps one file per host:

```text
<data root>/<host type>/instance.json
```

The data root is `$HIREY_INSTANCE_DATA_DIR` when set; otherwise macOS
`~/Library/Application Support/hirey-hi`, otherwise Windows `%APPDATA%\hirey-hi`, otherwise
`${XDG_DATA_HOME:-~/.local/share}/hirey-hi`. The file is mode `0600` and its directory is `0700`.

This location is deliberately **outside every plugin version cache and outside any repository or
worktree**, so a plugin upgrade, a plain uninstall and reinstall, an OAuth re-login and a brand-new
Task all reuse the same file. Only an explicit `forget`, a wiped user data directory or a new
computer creates a new instance.

The per-host split is required: Codex and any other host on the same computer are **two
different instances**, with two different key pairs and two different local instance references.

**This is a label, not the identity.** When the helper runs inside a container, over SSH or in a
cloud sandbox it reports *that* environment — its filesystem, its user account and its computer
name — not the computer in front of the user. The reported display name is therefore only a
convenience for the user; the instance identity is the key pair, never the name.

## Check the local instance

```text
python3 scripts/hi_instance.py status --host codex
```

`status` initializes the instance when none exists, then prints one JSON object with
`format_version`, `host_type`, `os_type`, `local_instance_ref`, `public_key`,
`public_key_fingerprint`, `display_name`, `data_dir` and the signer that was selected. It never
prints the private key. Report the readable `display_name`; pass `local_instance_ref` and
`public_key` to the server exactly as returned.

## First bind

A local key pair is not yet authority. The server binds an instance to the current Agent Session
only after the helper signs the exact server-issued challenge.

1. Call `workspace_workflows` with `action: agent_instance.current`. If it returns
   `binding_status: "bound"`, an instance is already bound to this Agent Session: report it and stop.
2. When it returns `binding_status: "instance_binding_required"`, run `status` as above and keep
   `local_instance_ref`, `public_key`, `os_type` and `display_name`.
3. Call `workspace_workflows` with `action: agent_instance.binding.begin` and payload
   `{idempotency_key, local_instance_ref, public_key, host_type: "codex", os_type,
   display_name}`. Use one stable `idempotency_key` and reuse it only for the exact retry.
4. Take the returned `challenge_text` and write it to a private temporary file **byte for byte**,
   with no added newline and no re-quoting. Never pass the challenge as a command-line argument.
5. Sign that file:

   ```text
   python3 scripts/hi_instance.py sign --host codex --challenge-file <challenge path>
   ```

   The result is `{"signature": ..., "public_key": ...}`. The challenge is time-bounded; if it has
   expired, call `binding.begin` again for a fresh one instead of reusing the old file.
6. Call `workspace_workflows` with `action: agent_instance.binding.finish` and payload
   `{idempotency_key, challenge_id, signature}`. Delete the temporary challenge file afterwards.
7. Report the bound instance from the result: its `instance_id`, its readable `display_name`, its
   `host_type` and `os_type`. Confirm the binding by calling `agent_instance.current` once and
   reporting the returned instance, not a remembered one.

## Recovery

- **`instance_revoked`.** The user revoked this instance from another client. Run
  `python3 scripts/hi_instance.py forget --host codex`, then initialize a fresh instance with
  `status` and bind it again from step 2 above. A revoked key pair is never reused.
- **Local material missing.** When `status` reports `instance_state_invalid`, the local file is
  unreadable or corrupt: run `forget`, then `status` to initialize a new instance, then bind again.
  Never edit `instance.json` by hand and never copy one machine's file onto another.
- **Signer unavailable.** `instance_signer_unavailable` means neither the Python `cryptography`
  package nor an OpenSSL build with real Ed25519 support is available. Report the remedy the helper
  returned and stop. No instance state was written, so a later attempt starts clean.
- **`instance_binding_proof_invalid` / `instance_key_mismatch`.** The signature did not match the
  key the challenge was issued for. Re-read `status`, confirm the same `public_key` is sent, sign the
  exact challenge bytes again, and use a new `binding.begin` challenge rather than an old one.

## Handoffs between the user's own instances

A private handoff is addressed to one instance id, and the readable name is what the user actually
said. Follow the rules in the `hi-use` skill for sending and the `hi-events` skill for reading: match
by readable name with `agent_instance.list`, never guess between candidates, display a note before
marking it read, never poll in the background, and never turn a note into work. A handoff is a
private note between the user's own computers, not an instruction channel and not a way to make
another machine act.

## Boundaries

- The helper only creates, reads, signs with and deletes *this* computer's instance material. It
  never lists, renames or revokes instances; those are server operations on the user's account.
- Never print, echo, log, email or paste the private seed, and never ask the user to paste it.
- Never invent an instance id, and never claim a binding succeeded before `binding.finish` returned
  it. A local key pair alone proves nothing to the server.
