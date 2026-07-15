#!/usr/bin/env bash
set -euo pipefail

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd "$HERE/../.." && pwd)
CONFIG_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/hindsight-recovery
STATE_DIR=${XDG_STATE_HOME:-$HOME/.local/state}/hindsight-recovery
DATA_DIR=${XDG_DATA_HOME:-$HOME/.local/share}/hindsight-recovery
UNIT_DIR=${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
SYSTEMCTL_BIN=${SYSTEMCTL_BIN:-systemctl}
PYTHON_BIN=${RECOVERY_PYTHON:-python3}
AGENT_OUTBOX_SOURCE=${AGENT_OUTBOX_SOURCE:-$HOME/agent-session-mcp/server/outbox.py}
quarantined_units=(
  hindsight-recovery-controller.service
  hindsight-recovery-guard.service
  hindsight-recovery-watchdog.service
)

mode=containment
case ${1:-} in
  "") ;;
  --migrate-config)
    mode=migrate
    shift
    ;;
  --stage-controller-services)
    mode=stage
    shift
    ;;
  --help|-h)
    cat <<'EOF'
usage: install.sh [--migrate-config MIGRATION_ARGS... | --stage-controller-services]

Default: install the pinned runtime and tunnel, then mask the controller,
guard, and watchdog as the production incident-containment posture.

--migrate-config validates and atomically upgrades an existing recovery.env.
--stage-controller-services validates recovery.env and installs, but never
  enables or starts, the quarantined controller/guard/watchdog units.
EOF
    exit 0
    ;;
  *)
    echo "unexpected argument: $1" >&2
    exit 2
    ;;
esac
if [[ $mode != migrate && $# -ne 0 ]]; then
  echo "unexpected arguments for $mode mode" >&2
  exit 2
fi

# Fail closed before dependency installation or configuration validation. If
# any later step fails, no old controller binary remains runnable.
install -d -m 0755 "$UNIT_DIR"
for unit in "${quarantined_units[@]}"; do
  "$SYSTEMCTL_BIN" --user disable --now "$unit" >/dev/null 2>&1 || true
  rm -f -- "$UNIT_DIR/$unit"
  ln -s /dev/null "$UNIT_DIR/$unit"
done
"$SYSTEMCTL_BIN" --user daemon-reload

for command in "$PYTHON_BIN" sha256sum; do
  command -v "$command" >/dev/null 2>&1 || {
    echo "required command is unavailable: $command" >&2
    exit 2
  }
done

runtime_files=(
  migrate-recovery-env.py
  recovery.py
  requirements.lock
  tunnel.sh
  verify-agent-runtime.py
)
release_hash=$(
  for file in "${runtime_files[@]}"; do
    printf '%s\0' "$file"
    sha256sum "$HERE/$file" | cut -d' ' -f1
  done | sha256sum | cut -d' ' -f1
)
RELEASES_DIR=$DATA_DIR/releases
RELEASE_DIR=$RELEASES_DIR/$release_hash
RUNTIME_DIR=$RELEASE_DIR/runtime
VENV_DIR=$RELEASE_DIR/venv

install -d -m 0755 "$DATA_DIR" "$RELEASES_DIR"
if [[ ! -e $RELEASE_DIR/.complete ]]; then
  # An incomplete content-addressed directory was never selected by `current`.
  # It is safe to replace before constructing the same release again.
  rm -rf -- "$RELEASE_DIR"
  install -d -m 0755 "$RUNTIME_DIR"
  install -m 0755 \
    "$HERE/recovery.py" \
    "$HERE/migrate-recovery-env.py" \
    "$HERE/verify-agent-runtime.py" \
    "$HERE/tunnel.sh" \
    "$RUNTIME_DIR/"
  install -m 0644 "$HERE/requirements.lock" "$RUNTIME_DIR/"
  "$PYTHON_BIN" -m venv "$VENV_DIR"
  "$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check \
    --no-cache-dir \
    --requirement "$RUNTIME_DIR/requirements.lock"
  "$VENV_DIR/bin/python" -c \
    'import psycopg; assert psycopg.__version__ == "3.3.4"'
  install -m 0644 /dev/null "$RELEASE_DIR/.complete"
fi
link_tmp=$DATA_DIR/.current.$$
trap 'rm -f -- "$link_tmp"' EXIT
ln -s "releases/$release_hash" "$link_tmp"
mv -Tf "$link_tmp" "$DATA_DIR/current"

install -d -m 0700 "$CONFIG_DIR" "$STATE_DIR"
install -d -m 0755 "$UNIT_DIR"
if [[ ! -e $CONFIG_DIR/recovery.env ]]; then
  config_tmp=$CONFIG_DIR/.recovery.env.$$.tmp
  trap 'rm -f -- "$link_tmp" "$config_tmp"' EXIT
  escaped_root=${PROJECT_ROOT//\\/\\\\}
  escaped_root=${escaped_root//&/\\&}
  escaped_root=${escaped_root//|/\\|}
  sed "s|^RECOVERY_ROOT=.*$|RECOVERY_ROOT=$escaped_root|" \
    "$HERE/recovery.env.example" >"$config_tmp"
  chmod 0600 "$config_tmp"
  mv -f "$config_tmp" "$CONFIG_DIR/recovery.env"
fi

if [[ $mode == migrate ]]; then
  "$VENV_DIR/bin/python" "$RUNTIME_DIR/migrate-recovery-env.py" \
    --config "$CONFIG_DIR/recovery.env" \
    --template "$HERE/recovery.env.example" \
    --outbox-source "$AGENT_OUTBOX_SOURCE" \
    --recovery-module "$RUNTIME_DIR/recovery.py" \
    --recovery-root "$PROJECT_ROOT" \
    "$@"
fi

configured_root=$(
  "$VENV_DIR/bin/python" - "$CONFIG_DIR/recovery.env" <<'PY'
import sys
from pathlib import Path

path = Path(sys.argv[1])
values = {}
for raw in path.read_text().splitlines():
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        continue
    key, value = line.split("=", 1)
    values[key.strip()] = value.strip().strip('"').strip("'")
print(values.get("RECOVERY_ROOT", ""))
PY
)
if [[ $configured_root != "$PROJECT_ROOT" ]]; then
  echo "recovery.env points at a different checkout; run --migrate-config" >&2
  exit 2
fi

render_unit() {
  local unit=$1
  local temporary=$UNIT_DIR/.$unit.$$.tmp
  local escaped_data escaped_config
  escaped_data=${DATA_DIR//\\/\\\\}
  escaped_data=${escaped_data//&/\\&}
  escaped_data=${escaped_data//|/\\|}
  escaped_config=${CONFIG_DIR//\\/\\\\}
  escaped_config=${escaped_config//&/\\&}
  escaped_config=${escaped_config//|/\\|}
  sed \
    -e "s|%h/.local/share/hindsight-recovery|$escaped_data|g" \
    -e "s|%h/.config/hindsight-recovery|$escaped_config|g" \
    "$HERE/systemd/$unit" >"$temporary"
  install -m 0644 "$temporary" "$UNIT_DIR/$unit"
  rm -f -- "$temporary"
}

render_unit hindsight-recovery-tunnel.service
if [[ $mode == stage ]]; then
  # Static configuration validation is necessary but not sufficient to start
  # these services. README.md requires separate runtime proofs and a manual,
  # ordered start. Staging never enables or starts a quarantined unit.
  "$VENV_DIR/bin/python" "$RUNTIME_DIR/migrate-recovery-env.py" \
    --config "$CONFIG_DIR/recovery.env" \
    --template "$HERE/recovery.env.example" \
    --outbox-source "$AGENT_OUTBOX_SOURCE" \
    --recovery-module "$RUNTIME_DIR/recovery.py" \
    --recovery-root "$PROJECT_ROOT" \
    --identities-from-config \
    --check-only
  for unit in "${quarantined_units[@]}"; do
    "$SYSTEMCTL_BIN" --user disable --now "$unit" >/dev/null 2>&1 || true
    rm -f -- "$UNIT_DIR/$unit"
    render_unit "$unit"
  done
  posture="staged controller services; all remain disabled and stopped"
else
  for unit in "${quarantined_units[@]}"; do
    "$SYSTEMCTL_BIN" --user disable --now "$unit" >/dev/null 2>&1 || true
    rm -f -- "$UNIT_DIR/$unit"
    ln -s /dev/null "$UNIT_DIR/$unit"
  done
  posture="installed containment runtime; controller, guard, and watchdog are masked"
fi

"$SYSTEMCTL_BIN" --user daemon-reload
"$SYSTEMCTL_BIN" --user enable hindsight-recovery-tunnel.service
echo "$posture; tunnel is enabled but was not started"
