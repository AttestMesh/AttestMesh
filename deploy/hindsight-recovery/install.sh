#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/hindsight-recovery
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/hindsight-recovery
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user

install -d -m 0700 "$CONFIG_DIR" "$STATE_DIR"
install -d -m 0755 "$UNIT_DIR"
if [ ! -e "$CONFIG_DIR/recovery.env" ]; then
  install -m 0600 "$HERE/recovery.env.example" "$CONFIG_DIR/recovery.env"
fi
install -m 0644 "$HERE/systemd/hindsight-recovery-tunnel.service" "$UNIT_DIR/"
install -m 0644 "$HERE/systemd/hindsight-recovery-guard.service" "$UNIT_DIR/"
install -m 0644 "$HERE/systemd/hindsight-recovery-watchdog.service" "$UNIT_DIR/"

systemctl --user daemon-reload
echo "installed recovery services; start watchdog first, then tunnel and guard"
