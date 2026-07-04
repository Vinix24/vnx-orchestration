# Operator Key Provisioning — VNX Signing Authority

This document describes the one-time operator step to provision a VNX signing key.
This is executed by the operator (Vincent), not by any worker or automated process.

## Prerequisites

- macOS with Secure Enclave (Touch ID) present, OR a machine-scoped key is acceptable
- `ssh-keygen` (ships with macOS)
- Git ≥ 2.34 (SSH signing support)

---

## Step 1 — Generate the signing key

**Option A — Secure Enclave key (Touch ID; strongest, non-exportable):**

```bash
ssh-keygen -t ed25519-sk -O resident -O application=ssh:vnx-sign \
  -C "vnx-signing-key@$(hostname -s)" \
  -f ~/.ssh/vnx_sign_ed25519_sk
```

The private key material never leaves the Secure Enclave.  Touch ID confirms each signing operation.

**Option B — Machine key (simpler; key is exportable):**

```bash
ssh-keygen -t ed25519 -C "vnx-signing-key@$(hostname -s)" \
  -f ~/.ssh/vnx_sign_ed25519
```

Set file permissions: `chmod 600 ~/.ssh/vnx_sign_ed25519`.

> **Honest residual:** a machine key (Option B) is exportable.  If the worker process can read
> `~/.ssh/vnx_sign_ed25519`, the guarantee degrades from preventive to detective.  Option A
> eliminates that residual — the Secure Enclave private key is hardware-bound to this machine
> and requires Touch ID confirmation.

---

## Step 2 — Register the public key in `allowed_signers`

```bash
# In the repo root:
echo "vnx-operator@$(hostname -s) $(cat ~/.ssh/vnx_sign_ed25519.pub)" \
  >> allowed_signers
```

Or for the SK key:

```bash
echo "vnx-operator@$(hostname -s) $(cat ~/.ssh/vnx_sign_ed25519_sk.pub)" \
  >> allowed_signers
```

Commit `allowed_signers` to the repository.  It is a trust-root path — any change
to it must be signed by a pre-existing trusted key and reviewed (see `CODEOWNERS`).

---

## Step 3 — Configure Git to use SSH signing

```bash
git config --global gpg.format ssh
git config --global user.signingkey ~/.ssh/vnx_sign_ed25519     # or _sk variant
git config --global gpg.ssh.allowedSignersFile "$(pwd)/allowed_signers"
git config --global commit.gpgsign true
```

Verify the configuration:

```bash
git config --global gpg.format       # → ssh
git config --global user.signingkey  # → path to your key
```

---

## Step 4 — Test sign + verify

```bash
# Create a test file and sign it
echo "vnx test" > /tmp/vnx_test_sign.txt
ssh-keygen -Y sign -f ~/.ssh/vnx_sign_ed25519 -n "vnx-attestation" /tmp/vnx_test_sign.txt

# Verify the signature
ssh-keygen -Y verify \
  -f "$(pwd)/allowed_signers" \
  -I "vnx-operator@$(hostname -s)" \
  -n "vnx-attestation" \
  -s /tmp/vnx_test_sign.txt.sig \
  < /tmp/vnx_test_sign.txt
```

Expected output: `Good "vnx-attestation" signature for vnx-operator@<hostname> with ED25519 key ...`

---

## Step 5 — Set the key path for the attestation module

Pass the key path when calling `emit_governed_attestation`:

```python
from scripts.lib.attestation import emit_governed_attestation

rec = emit_governed_attestation(
    dispatch_id="D-my-feature",
    deliverable_id="D1",
    track_id="my-track",
    plan_gate_ref="plan-gate-pass-ref",
    signer_identity="vnx-operator@mymachine",
    timestamp="2026-07-04T12:00:00Z",
    key_path="~/.ssh/vnx_sign_ed25519",   # ← operator-provided
    repo_root=".",
)
```

The attestation module **never provisions, stores, or looks up keys**.
Key custody is entirely the operator's responsibility.

---

## Key rotation and revocation

- To revoke a key: remove its line from `allowed_signers` (base-branch side).
  Past attestations in `.vnx-attest/governed.ndjson` remain valid via the
  receipt chain timestamp — they were signed when the key was trusted.
- To rotate: add the new key line to `allowed_signers`, generate new key,
  update `user.signingkey` in git config.  Sign the `allowed_signers` change
  with a **pre-existing** trusted key (the new key cannot self-authorize).
- Validity windows: add a comment next to each key noting its active-from date
  so an auditor can match attestation timestamps against key validity.

---

## Single-machine residual

This setup binds the signing trust to this machine.  A remote signing service
(where the door process signs and the worker never holds the key) would
eliminate this residual.  That hardening path is named as a future slice.
