# Staged-only pre-commit security guard

AttestMesh commits run a fail-closed security gate implemented as a Smithers process. It protects
against the incident classes that prompted it: operational backups, database exports, private key
material, access keys, API tokens, bearer credentials, and less obvious sensitive runtime data.

## Install

From the repository root:

```bash
deploy/install-precommit-security.sh
```

The installer uses the checked-in `deploy/bun.lock`, verifies
`smithers-orchestrator@0.22.0`, checks that Codex is installed and authenticated, and sets this
clone's `core.hooksPath` to `.githooks`. Run it once for every fresh clone.

To invoke the same guard without committing:

```bash
deploy/precommit-security.sh
```

## Security boundary

The deterministic guard reads Git's index, not filesystem copies of tracked files. Partial staging
therefore behaves correctly: staged hunks are checked, while later unstaged edits are absent. The
snapshot contains new staged paths and added staged lines only. Deleted text is deliberately omitted
because removing an old credential must not resend it anywhere. Git attributes cannot suppress the
index diff: staged non-binary blobs are always treated as text for inspection.

Before an agent runs, local rules reject:

- exact backup or dump directory components;
- archives, database/disk images, key containers, private-key filenames, and non-template `.env`
  files;
- known provider-token, JWT, bearer-credential, credential-URL, and private-key shapes;
- non-placeholder literals assigned to password, token, secret, and access-key fields;
- binary, oversized, or otherwise incomplete staged inputs.

Local findings print only a rule, location, explanation, and short one-way fingerprint. A detected
credential value is neither printed nor sent to a model. Current and rename-source paths are scanned
for credential shapes, and every path is token-redacted before display or model evaluation.

If deterministic checks pass, token-like literals are redacted and the private snapshot is passed to
`deploy/workflows/precommit-security.tsx`. Smithers runs three ephemeral, read-only Codex
specialists in parallel for backup, credential, and adversarial-evasion review, then gives their
structured results to a fourth Codex arbiter. Every agent uses `gpt-5.6-sol` with
`model_reasoning_effort="ultra"`. Agents start in an empty temporary directory, ignore user
configuration and rules, have no repository working directory, and are instructed to use only the
embedded staged snapshot. Smithers owns the delegation boundary; nested agent spawning, shell,
browser, connector, hook, local-image, and web-search tools are disabled. The subprocess receives a
minimal allowlisted environment so ambient access keys and tokens are not inherited by the
reviewers.

The snapshot, Smithers database, agent directory, and logs live in a mode-0700 temporary directory
and are deleted after each run. Codex uses an ephemeral session. The guard compares the Git index
tree before and after evaluation and rejects the commit if it changed. It also reads the private
Smithers database directly and requires all three specialist rows, one clean arbiter verdict, an
empty final findings array, and a successful enforcement row. Every specialist must independently
return clear with no findings; the arbiter cannot override a specialist denial. A zero CLI exit
status alone cannot approve a commit.

## Failure behavior

The commit is rejected when:

- a deterministic rule finds a backup or credential;
- the agent returns any finding or denies the change;
- Smithers, Codex authentication, structured output, or the network fails;
- a staged binary or oversized change cannot be completely reviewed;
- the index changes while evaluation is running.

Split staged payloads larger than 512 KiB into smaller commits. As with every Git hook,
`git commit --no-verify` can bypass it; protected branches should pair this local guard with a
server-side policy before treating it as enforcement against a malicious committer.

Run deterministic regression tests with:

```bash
( cd deploy && bun run security:test )
```
