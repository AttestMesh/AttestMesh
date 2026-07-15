# Restart-safe Hindsight recovery

This package contains a tunnel, guard, watchdog, and persistent recovery
controller. The controller is the only automatic authority that can close the
Agent circuit; the guard and watchdog can only fail closed. Recovery state is
stored under `~/.local/state/hindsight-recovery`.

## Current production posture

Only the read tunnel is approved for continuous operation. The default
installer deliberately stops, disables, and masks the controller, guard, and
watchdog. This is incident containment, not a complete controller rollout.
Those services can mutate recovery state and, after extensive runtime proofs,
the controller can resume work. They must remain quarantined until every
activation gate below is met.

The installed runtime is independent of the checkout name and of the Agent
server's Python environment. `install.sh` creates a content-addressed release
under `~/.local/share/hindsight-recovery/releases`, installs the exact packages
from `requirements.lock` into that release, and atomically points `current` at
it. The generated `recovery.env` records the absolute checkout as
`RECOVERY_ROOT`; an existing configuration is never silently repointed.

Install or restore the containment posture:

```bash
deploy/hindsight-recovery/install.sh
systemd-analyze --user verify \
  ~/.config/systemd/user/hindsight-recovery-tunnel.service
systemctl --user start hindsight-recovery-tunnel.service
```

The installer enables the tunnel for future user-manager starts but does not
start it. It does not enable or start any controller service.

For a fresh CI environment, install only the pinned test lock and disable
pytest's on-disk cache:

```bash
python3 -m venv .venv-hindsight-recovery
.venv-hindsight-recovery/bin/python -m pip install --no-cache-dir \
  -r deploy/hindsight-recovery/requirements-test.lock
PYTHONDONTWRITEBYTECODE=1 \
  .venv-hindsight-recovery/bin/python -m pytest -p no:cacheprovider \
  deploy/hindsight-recovery/tests
```

## Read-only inspection

Use the package's pinned interpreter, not the Agent server virtualenv:

```bash
PY=~/.local/share/hindsight-recovery/current/venv/bin/python
RECOVERY=~/.local/share/hindsight-recovery/current/runtime/recovery.py
set -a
source ~/.config/hindsight-recovery/recovery.env
set +a
$PY "$RECOVERY" audit --full --legacy-held
$PY "$RECOVERY" incident-status --full
$PY "$RECOVERY" spot-check --json
```

`recovery.env` is assignment-only. A bare `source` without `set -a` does not
export its values to Python. `spot-check` is read-only unless the operator adds
`--apply-pause`; that flag may open the Agent circuit after two matching reads
and never closes it.

## Configuration migration

The full-batch configuration is cap 5,142, Agent max-two, Hindsight LLM
concurrency three, provider ceiling six, and a $30 hard budget. Migrate an
existing sealed configuration only with reviewed immutable Agent identities:

```bash
deploy/hindsight-recovery/install.sh --migrate-config \
  --expected-agent-image-digest 'sha256:<64 lowercase hex>' \
  --expected-agent-compose-hash '<64 lowercase hex>' \
  --expected-agent-outbox-build-id 'outbox.py:sha256:<reviewed source digest>'
```

Migration validates the reviewed local `outbox.py`, production `Config`, and
all immutable identities before atomically replacing the mode-0600 file. It
also repoints a retired checkout path to the checkout running the installer.
The installer preserves an existing file in every other mode and fails if it
names a different checkout.

## Manual controller activation

There is no automatic controller activation path. Do not stage the quarantined
units until all of these are true:

- `BUDGET_MAINTENANCE_HOLD_URL` implements the reviewed authenticated contract:
  a successful POST durably leaves `circuit_open=true`, the exact reason
  `hindsight backfill complete: maintenance hold`, and zero in-flight
  reservations, and `/health` proves the same predicate.
- The configuration migration above succeeds with the deployed image digest,
  compose hash, and reviewed `outbox.py` build ID.
- The Agent runtime verifier proves the exact running CVM, allowlisted compose,
  pinned image, healthy API, and current worker heartbeat.
- Agent and Pocket circuits have been independently proved open, the recovery
  state directory has a restorable backup, and an operator owns the rollback.
- Every external Agent `outbox.py` change required by the recovery has landed
  and passed that repository's tests. Loose files under `candidates/` are not
  release inputs.

After those gates, staging is explicit and still does not start or enable a
service:

```bash
deploy/hindsight-recovery/install.sh --stage-controller-services
systemd-analyze --user verify \
  ~/.config/systemd/user/hindsight-recovery-{watchdog,tunnel,guard,controller}.service
```

Run the verifier, then start the fail-closed monitors manually. The watchdog is
intentionally first and tolerates this bounded unarmed startup window. A full
audit must pass with the monitors active before the controller is started:

```bash
$PY ~/.local/share/hindsight-recovery/current/runtime/verify-agent-runtime.py
systemctl --user start hindsight-recovery-watchdog.service
systemctl --user start hindsight-recovery-tunnel.service
systemctl --user start hindsight-recovery-guard.service
$PY "$RECOVERY" audit --full
systemctl --user start hindsight-recovery-controller.service
```

There is no public `release` command or manual phase editor. The controller
requires restart-persistent clean checks and full audits before its internal
release path. At exactly 5,142 it opens Agent, refreshes only exact completed
operations, proves 5,142 unique succeeded documents and stable spend, then
records the final maintenance hold. Pocket remains open throughout.

Rollback starts by failing closed, then restores the default masks:

```bash
$PY "$RECOVERY" emergency-open --reason 'manual controller rollback'
systemctl --user stop hindsight-recovery-controller.service \
  hindsight-recovery-guard.service hindsight-recovery-watchdog.service
deploy/hindsight-recovery/install.sh
systemctl --user is-enabled hindsight-recovery-controller.service  # masked
```
