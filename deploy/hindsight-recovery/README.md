# Restart-safe Hindsight recovery

This package replaces ephemeral `/tmp` scripts and terminal sessions. It keeps
all credentials in their existing sealed/local files and stores only phase,
heartbeat, and audit events under `~/.local/state/hindsight-recovery`.

The watchdog has an independent one-shot SSH/PG path and persists the Agent
circuit open whenever the shared tunnel or guard disappears. The guard performs
2-second bounded checks, a full provenance/Fugu/Patroni audit every 60 seconds,
opens Agent at the 3,823 attempted cap, and may refresh only already-completed
submitted operations. Neither service ever closes Agent. `release` is the sole
circuit-close interface and requires ten consecutive clean checks. Hindsight
LLM concurrency is three and the guard opens Agent above six provider requests
in flight; Agent itself runs at max-two.

Install and verify:

```bash
deploy/hindsight-recovery/install.sh
systemd-analyze --user verify \
  ~/.config/systemd/user/hindsight-recovery-{tunnel,guard,watchdog}.service
systemctl --user start hindsight-recovery-watchdog.service
systemctl --user start hindsight-recovery-tunnel.service
systemctl --user start hindsight-recovery-guard.service
```

Operator commands use the Agent server's pinned Python environment:

```bash
PY=~/agent-session-mcp/server/.venv/bin/python
$PY deploy/hindsight-recovery/recovery.py audit --full
$PY deploy/hindsight-recovery/recovery.py refresh-retry
$PY deploy/hindsight-recovery/recovery.py prepare-resume
$PY deploy/hindsight-recovery/recovery.py release
```

Do not edit `phase.json` manually. The retry controller is pinned to the one
approved failed document and preserves its original operation in retry history.
After a pre-cap fail-closed event, `prepare-resume` may reconcile one or two
completed submitted operations only while Agent remains open, provider/Hindsight
activity is zero, every document is present, and all provenance gates pass.
