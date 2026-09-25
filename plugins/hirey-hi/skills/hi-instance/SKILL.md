---
name: hi-instance
description: Bind this Codex installation on this computer to one Hi local instance and prove it with a local Ed25519 key. Use when Hi reports instance_binding_required, when binding must be recovered after instance_revoked, when the user asks which computer this Agent is running on, or when a private handoff between the user's own computers must be addressed by name.
---

# Hi local instance

Hi separates two different questions. OAuth answers *which Person and Agent* may act. A local
instance key pair answers *which end of the user's work* is calling. This skill owns the local half:
one key pair per verified Person and logical Agent, created and kept on the user's machine, while the
installation keeps one stable audit-only reference.

`scripts/hi_instance.py` is a stdlib-only helper. It never prints the private key, never derives an
instance from a hostname, an IP address, an OAuth client or a plugin version, and it never needs the
user to type a device id or edit an MCP configuration.

## Local state and its location

The helper keeps one installation record and one profile per identity:

```text
<data root>/<host type>/installation.json          # stable installation_ref, audit clue only
<data root>/<host type>/profiles/<profile_key>.json # one key pair per verified Person + Agent
<data root>/<host type>/instance.json              # legacy single-key file, read and preserved
```

The data root is `$HIREY_INSTANCE_DATA_DIR` when set; otherwise macOS
`~/Library/Application Support/hirey-hi`, otherwise Windows `%APPDATA%\hirey-hi`, otherwise
`${XDG_DATA_HOME:-~/.local/share}/hirey-hi`. Every file is mode `0600` and every directory is `0700`.

This location is deliberately **outside every plugin version cache and outside any repository or
worktree**, so a plugin upgrade, a plain uninstall and reinstall, an OAuth re-login and a brand-new
Task all reuse the same material. Only an explicit `forget`, a wiped user data directory or a new
computer creates new material.

The per-host split is required: Codex and any other host on the same computer are **two
different installations**, with two different installation references.

The installation reference is the same for every Person who uses this installation. It is only a
clue that the same installation was involved, and it is never proof of physical hardware, of Person
identity or of permission to reach another Person's data. The key pair, not the reference, is what
the server verifies.

**This is a label, not the identity.** When the helper runs inside a container, over SSH or in a
cloud sandbox it reports *that* environment — its filesystem, its user account and its computer
name — not the computer in front of the user. The reported display name is therefore only a
convenience for the user; the instance identity is the key pair, never the name.

## Check this identity's local profile

The server tells you which local profile belongs to the verified Person and Agent: call
`agent_instance.current`, read its `profile_key`, and pass exactly that value.

```text
python3 scripts/hi_instance.py status --host codex --profile <profile_key>
```

`status --profile` creates that identity's profile when it does not exist yet and prints one JSON
object with `installation_ref`, `profile_key`, `os_type`, `public_key`, `public_key_fingerprint`,
`display_name`, `server_instance_id` and the signer. It never prints the private key. A bare
`status --host codex` with no profile reports `profile_status: "profile_required"` and the
opaque profile keys, and creates no key material.

A legacy single-key `instance.json` is adopted only when the server confirms that the *verified*
identity already owns an instance under this installation ref whose fingerprint is exactly the
legacy key's. Ask for that confirmation by passing the ref to `agent_instance.current` and feeding
its `existing_public_key_fingerprint` back to `status`:

```text
python3 scripts/hi_instance.py status --host codex \
  --profile <profile_key> --confirmed-fingerprint <existing_public_key_fingerprint>
```

Omit `--confirmed-fingerprint` when the server returns `null`. A profile created without a matching
confirmation never inherits the legacy key: the first Person to log in after an upgrade cannot
displace the old owner, and the legacy file stays `unclaimed` so its real owner can still adopt it
on a later login.

Switching Person on the same installation is not a special command: after the new OAuth login the
server returns that Person's `profile_key`, so `status --profile <new key>` creates a separate
profile with its own key pair. Returning to a former Person selects their existing profile and
restores their original key. Never copy one Person's profile onto another and never pass a
`profile_key` the server did not return for the current session.

## First bind

A local key pair is not yet authority. The server binds an instance to the current Agent Session
only after the helper signs the exact server-issued challenge.

1. Run `python3 scripts/hi_instance.py status --host codex` once. It creates the installation
   record when absent and returns the stable `installation_ref`. It creates no key pair.
2. Call `workspace_workflows` with `action: agent_instance.current` and payload
   `{local_instance_ref: <installation_ref>}`. If it returns `binding_status: "bound"`, an instance
   is already bound to this Agent Session: report it and stop. Otherwise keep the returned
   `profile_key` and `existing_public_key_fingerprint`.
3. Run `status --host codex --profile <profile_key>` and, when the server returned a
   non-null `existing_public_key_fingerprint`, add
   `--confirmed-fingerprint <existing_public_key_fingerprint>`. Keep `public_key`, `os_type` and
   `display_name` from the result. This is what lets a returning legacy owner keep the exact server
   instance they already had while a different first login gets a fresh key.
4. Call `workspace_workflows` with `action: agent_instance.binding.begin` and payload
   `{idempotency_key, local_instance_ref: <installation_ref>, public_key, host_type: "codex",
   os_type, display_name}`. The installation reference is sent as `local_instance_ref`. Use one
   stable `idempotency_key` and reuse it only for the exact retry.
5. Take the returned `challenge_text` and write it to a private temporary file **byte for byte**,
   with no added newline and no re-quoting. Never pass the challenge as a command-line argument.
6. Sign that file with the same profile:

   ```text
   python3 scripts/hi_instance.py sign --host codex --profile <profile_key> --challenge-file <challenge path>
   ```

   The result is `{"signature": ..., "public_key": ..., "profile_key": ...}`. The challenge is
   time-bounded; if it has expired, call `binding.begin` again for a fresh one instead of reusing the
   old file.
7. Call `workspace_workflows` with `action: agent_instance.binding.finish` and payload
   `{idempotency_key, challenge_id, signature}`. Delete the temporary challenge file afterwards.
8. Record the returned `instance_id` locally, then confirm the binding:

   ```text
   python3 scripts/hi_instance.py record --host codex --profile <profile_key> --instance-id <instance_id>
   ```

   Call `agent_instance.current` once more and report the instance it returns, not a remembered one.

Every ordinary Hi operation keeps working before this binding, including token refresh on an
existing Session. An existing logged-in Agent is transitioned lazily: the very first
instance-directed operation runs this flow, and a legacy single-key file is migrated into its
confirmed owner's profile without losing its key or its server instance.

## Recovery

- **`instance_key_already_bound`.** The local key is already bound to another Person or Agent under
  the same installation ref. Mint fresh key material for exactly this identity and bind normally:
  `python3 scripts/hi_instance.py recover --host codex --profile <profile_key>`, then bind
  again from step 4 above. Other identities and the shared installation ref are untouched.
- **`instance_revoked`.** The user revoked this instance from another client. Mint fresh material
  with `recover`, then use the explicit recovery route below so a new active row is registered while
  the revoked row is retained as evidence.
- **`instance_key_mismatch`.** The server already has an active instance for this identity and ref
  under a different key, for example after the local profile was replaced or its key was lost.
  `recover` alone mints a new local key but the server still holds the old row, so `recover` then
  bind would repeat the same refusal. Run `recover`, then use the explicit recovery route: call
  `agent_instance.recovery.begin` with the same metadata instead of `binding.begin`, sign its
  challenge, and call `binding.finish`. The server retires this identity's own conflicting row as
  `revoked` evidence and registers the new active row for the same audit ref; other Persons' rows
  under that ref are never read or changed. Never `forget` the whole installation to escape this,
  because that deletes other identities' working material.
- **Local material missing.** When `status` reports `instance_state_invalid`, the profile is
  unreadable or corrupt: run `forget --profile <profile_key>`, then `status --profile <profile_key>`
  to initialize a fresh profile, then bind again. Never edit the files by hand and never copy one
  machine's profile onto another.
- **Signer unavailable.** `instance_signer_unavailable` means neither the Python `cryptography`
  package nor an OpenSSL build with real Ed25519 support is available. Report the remedy the helper
  returned and stop. No instance state was written, so a later attempt starts clean.
- **`instance_binding_proof_invalid`.** The signature did not match the key the challenge was issued
  for. Re-read `status`, confirm the same `public_key` is sent, sign the exact challenge bytes again,
  and use a new `binding.begin` challenge rather than an old one.

## Handoffs between the user's own instances

A private handoff is addressed to one instance id, and the readable name is what the user actually
said. Follow the rules in the `hi-use` skill for sending and the `hi-events` skill for reading: match
by readable name with `agent_instance.list`, never guess between candidates, display a note before
marking it read, never poll in the background, and never turn a note into work. A handoff is a
private note between the user's own computers, not an instruction channel and not a way to make
another machine act.

## Boundaries

- The helper only creates, reads, signs with and deletes *this* computer's local material. It never
  lists, renames or revokes instances; those are server operations on the user's account.
- Never print, echo, log, email or paste the private seed, and never ask the user to paste it.
- Never invent an instance id, and never claim a binding succeeded before `binding.finish` returned
  it. A local key pair alone proves nothing to the server.
- The installation reference is shared audit evidence, not an identity. Never treat it as proof of a
  physical machine or as permission to act for another Person.

## Limits of the local clue

- **Another OS user is another installation.** The data root is under the current user's home, so two
  OS accounts on one computer have separate installation refs, profiles and keys and never share an
  instance.
- **Another host type is another installation.** Each host type keeps its own per-host directory
  even on one computer.
- **Copied local state copies the identity.** Copying the whole data directory (not just OAuth
  credentials) copies the profile key, so the copy can act as that instance until the owner revokes
  it. This is a client instance identity, not hardware attestation.
- **A lost directory loses the ref and key.** The server-side instance is retained, not deleted; the
  next use creates a fresh local identity instead of silently re-adopting the old one.
