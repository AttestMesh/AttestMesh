#!/usr/bin/env python3
"""Box-side deploy helper for confidential-sandboxes sandboxd."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, "/opt/dstack-mcp")
import mcp_dstack as m  # noqa: E402

NAME = os.environ.get("BOX_NAME", "sandboxd-node")
COMPOSE_PATH = os.environ.get("BOX_COMPOSE", "/tmp/sandboxd-node.yaml")
VCPU = int(os.environ.get("BOX_VCPU", "8"))
MEM = int(os.environ.get("BOX_MEM", "16384"))
DISK = int(os.environ.get("BOX_DISK", "300"))
PORTS = json.loads(os.environ.get("BOX_PORTS", "[]"))
NET_MODE = os.environ.get("BOX_NET_MODE", "bridge").strip().lower() or "bridge"
GATEWAY_ENABLED = os.environ.get("BOX_GATEWAY_ENABLED", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}

ENV_KEYS = [
    "CHAIN_ID",
    "RPC_URL",
    "BUNDLER_URL",
    "GAS_POLICY_ID",
    "INDEXER_REGISTRY_ADDR",
    "GATEWAY_DOMAIN",
    "CLUSTER",
    "PEER_ENVELOPE_FALLBACK",
    "SANDBOX_DAEMON_TOKEN",
    "PUBLIC_BASE_URL",
    "ONCHAIN_CLUSTER_DIAMOND",
    "APP_DOMAIN",
    "CLOUDFLARE_API_TOKEN",
    "CLOUDFLARE_SYNCLAVE_API_TOKEN",
    "DSTACK_DOCKER_USERNAME",
    "DSTACK_DOCKER_PASSWORD",
    "DSTACK_DOCKER_REGISTRY",
]


def gateway_url(app_id: str) -> str:
    return f"https://{app_id[2:].lower()}-8080.gateway.attestmesh.xyz"


def build_env(app_id: str = "", compose_hash: str = "") -> dict[str, str]:
    env = {key: os.environ.get("E_" + key, "") for key in ENV_KEYS}
    env["DSTACK_DOCKER_REGISTRY"] = env.get("DSTACK_DOCKER_REGISTRY") or "ghcr.io"
    if app_id and not env.get("PUBLIC_BASE_URL"):
        env["PUBLIC_BASE_URL"] = gateway_url(app_id)
    return env


def app_compose_and_hash(env_keys: list[str]) -> tuple[str, str]:
    # Snapshot the operator input exactly once. The rendered string returned here is both hashed
    # and handed to the VMM; callers must never reopen COMPOSE_PATH between authorization and
    # mutation.
    docker_compose_file = Path(COMPOSE_PATH).read_text()
    app_compose = {
        "manifest_version": 2,
        "name": NAME,
        "runner": "docker-compose",
        "docker_compose_file": docker_compose_file,
        "kms_enabled": True,
        "gateway_enabled": GATEWAY_ENABLED,
        "local_key_provider_enabled": False,
        "key_provider_id": "",
        # The guest-agent dashboard otherwise exposes service/container inventory and, when logs are
        # enabled, request metadata to unauthenticated port 8090 clients.
        "public_logs": False,
        "public_sysinfo": False,
        # The gateway must never expose the guest-agent dashboard (8090), sidecar health (9090), or
        # one-shot registration helper (9092). Keep only audited product ingress and mesh transport.
        "port_policy": {
            "restrict_mode": True,
            "ports": [
                {"port": 443, "pp": False},
                {"port": 8080, "pp": False},
                {"port": 51900, "pp": False},
            ],
        },
        "allowed_envs": sorted(set(env_keys) | {"APP_ID"}),
        "no_instance_id": False,
        "secure_time": False,
    }
    app_compose["pre_launch_script"] = r"""set -euo pipefail
RUNSC_URL="https://storage.googleapis.com/gvisor/releases/release/20260420.0/x86_64/runsc"
RUNSC_SHA512="9efeefada7b9a7bcc21dc3a1ad3531d11dfac267808cced45d047aab742f15ecb91b2bb635dea78a4ae36817f76e5ed223b7ad80f3165f15dd24de9b0c95726f"
INSTALL_DIR="/dstack/persistent/bin"
RUNSC_BIN="$INSTALL_DIR/runsc"

DOCKER_DATASET="dstack/sandboxd-docker"
DOCKER_DATA_ROOT="/var/lib/sandboxd-docker"
DOCKER_DATA_LIMIT="28G"
DOCKER_DATA_LIMIT_BYTES=$((28 * 1024 * 1024 * 1024))

STATE_DATASET="dstack/sandboxd-state"
STATE_ROOT="/var/lib/sandboxd-state"
STATE_LIMIT="2G"
STATE_LIMIT_BYTES=$((2 * 1024 * 1024 * 1024))
STATE_RESERVE_MIB=2048

QUOTA_ZVOL="dstack/sandboxd-data"
QUOTA_DEVICE="/dev/zvol/dstack/sandboxd-data"
QUOTA_MOUNT="/var/lib/sandboxd-data"
QUOTA_ZVOL_SIZE="251G"
QUOTA_ZVOL_BYTES=$((251 * 1024 * 1024 * 1024))
QUOTA_SOLD_MIB=237568
QUOTA_FS_HEADROOM_MIB=18432
QUOTA_POOL_HEADROOM_MIB=28672
QUOTA_TOOLS_IMAGE="ghcr.io/dmvt/confidential-sandboxes@sha256:7ce355e2ea70b1e6a88770bd88839e3e38585817b4198223baf433857caf40c8"
EXPECTED_HOST_VCPUS=8
# The VMM resource readback must still be exactly 16,384 MiB. Inside this TDX image that allocation
# exposes about 15,034 MiB after confidential-guest firmware/kernel reservations, so retain a
# conservative 14.5 GiB guest-visible floor while still rejecting every old 8 GiB node.
MIN_HOST_MEMORY_MIB=14848

# Registry credentials are sealed into the CVM but need not persist on its host filesystem.
DOCKER_CONFIG="/run/sandboxd-prelaunch-docker-auth"
DOCKER_AFFINITY_DIR="/etc/systemd/system/docker.service.d"
DOCKER_AFFINITY_TMP="$DOCKER_AFFINITY_DIR/.sandboxd-affinity.$$"
export DOCKER_CONFIG
mkdir -p "$DOCKER_CONFIG"
chmod 0700 "$DOCKER_CONFIG"
trap 'rm -rf "$DOCKER_CONFIG"; rm -f "$DOCKER_AFFINITY_TMP"' EXIT

for tool in awk curl df docker dockerd find findmnt grep head jq mount rm sed sysctl systemctl zfs zpool; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "missing required sandboxd host tool: $tool" >&2
    exit 1
  }
done

# UpgradeApp does not resize an existing VM. Validate the resources visible inside the guest so an
# update of an old 4-vCPU/8-GiB node cannot install admission budgets intended for the new profile.
# dstack's minimal host image does not ship getconf. Count the online processors from procfs using
# awk, which is already a required pre-launch tool, and still validate the result as an integer.
actual_vcpus=$(awk -F: '/^processor[[:space:]]*:/ {count++} END {print count + 0}' /proc/cpuinfo)
actual_memory_kib=$(awk '/^MemTotal:/ {print $2}' /proc/meminfo)
case "$actual_vcpus" in
  ''|*[!0-9]*) echo "invalid guest vCPU readback: $actual_vcpus" >&2; exit 1 ;;
esac
case "$actual_memory_kib" in
  ''|*[!0-9]*) echo "invalid guest memory readback: $actual_memory_kib" >&2; exit 1 ;;
esac
[ "$actual_vcpus" -eq "$EXPECTED_HOST_VCPUS" ] || {
  echo "sandboxd guest has $actual_vcpus vCPUs; require exactly $EXPECTED_HOST_VCPUS" >&2
  exit 1
}
[ "$actual_memory_kib" -ge $((MIN_HOST_MEMORY_MIB * 1024)) ] || {
  echo "sandboxd guest has $((actual_memory_kib / 1024)) MiB RAM; need at least $MIN_HOST_MEMORY_MIB MiB visible" >&2
  exit 1
}

sha512_file() {
  if command -v sha512sum >/dev/null 2>&1; then
    sha512sum "$1" | awk '{print $1}'
  elif command -v openssl >/dev/null 2>&1; then
    openssl dgst -sha512 "$1" | awk '{print $NF}'
  elif command -v python3 >/dev/null 2>&1; then
    python3 -c 'import hashlib,sys;print(hashlib.sha512(open(sys.argv[1],"rb").read()).hexdigest())' "$1"
  else
    echo "no sha512 tool found" >&2
    return 1
  fi
}

mkdir -p "$INSTALL_DIR"
if [ ! -x "$RUNSC_BIN" ] || [ "$(sha512_file "$RUNSC_BIN" 2>/dev/null || true)" != "$RUNSC_SHA512" ]; then
  echo "[prelaunch] installing pinned runsc"
  curl -fsSL -o "$RUNSC_BIN.tmp" "$RUNSC_URL"
  actual="$(sha512_file "$RUNSC_BIN.tmp")"
  if [ "$actual" != "$RUNSC_SHA512" ]; then
    echo "runsc sha512 mismatch: got $actual want $RUNSC_SHA512" >&2
    exit 1
  fi
  mv "$RUNSC_BIN.tmp" "$RUNSC_BIN"
  chmod +x "$RUNSC_BIN"
fi

# Keep the durable allocation ledger and sandbox metadata outside Docker's deliberately small data
# root. A full image/cache dataset must not make SQLite unwritable. The equal refreservation gives
# this control-plane filesystem real backing space rather than only a logical quota.
legacy_state_volumes=$(docker volume ls -q --filter label=com.docker.compose.volume=sandboxd-state)
[ -z "$legacy_state_volumes" ] || {
  echo "legacy Docker sandboxd-state volume(s) require a reviewed migration before this release: $legacy_state_volumes" >&2
  exit 1
}
if ! zfs list -H -o name "$STATE_DATASET" >/dev/null 2>&1; then
  if [ -d "$STATE_ROOT" ] && [ -n "$(find "$STATE_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "refusing to mount state dataset over nonempty $STATE_ROOT" >&2
    exit 1
  fi
  zfs create \
    -o mountpoint="$STATE_ROOT" \
    -o quota="$STATE_LIMIT" \
    -o refquota="$STATE_LIMIT" \
    -o refreservation="$STATE_LIMIT" \
    -o atime=off \
    -o devices=off \
    -o exec=off \
    -o setuid=off \
    -o sandboxd:managed=1 \
    "$STATE_DATASET"
fi
[ "$(zfs get -H -o value type "$STATE_DATASET")" = "filesystem" ] || {
  echo "sandboxd state dataset is not a filesystem: $STATE_DATASET" >&2
  exit 1
}
[ "$(zfs get -H -o value sandboxd:managed "$STATE_DATASET")" = "1" ] || {
  echo "refusing unmanaged sandboxd state dataset $STATE_DATASET" >&2
  exit 1
}
[ "$(zfs get -H -o value mountpoint "$STATE_DATASET")" = "$STATE_ROOT" ] || {
  echo "unexpected sandboxd state dataset mountpoint" >&2
  exit 1
}
[ "$(zfs get -H -o value mounted "$STATE_DATASET")" = "yes" ] || {
  echo "sandboxd state dataset is not mounted: $STATE_DATASET" >&2
  exit 1
}
zfs set \
  quota="$STATE_LIMIT" \
  refquota="$STATE_LIMIT" \
  refreservation="$STATE_LIMIT" \
  atime=off devices=off exec=off setuid=off \
  "$STATE_DATASET"
for property in quota refquota refreservation; do
  [ "$(zfs get -Hp -o value "$property" "$STATE_DATASET")" -eq "$STATE_LIMIT_BYTES" ] || {
    echo "sandboxd state dataset $property is not exactly $STATE_LIMIT" >&2
    exit 1
  }
done
for property in atime devices exec setuid; do
  [ "$(zfs get -H -o value "$property" "$STATE_DATASET")" = "off" ] || {
    echo "sandboxd state dataset $property is not off" >&2
    exit 1
  }
done
chmod 0700 "$STATE_ROOT"

# Docker image layers, writable container metadata, named volumes, and container logs are outside a
# tenant's /data quota. Put the entire Docker root on its own managed ZFS dataset with a real 28 GiB
# ceiling before any app or quota-tools image is pulled. Never hide or migrate an existing root.
current_docker_root=$(docker info --format '{{.DockerRootDir}}')
if [ "$current_docker_root" != "$DOCKER_DATA_ROOT" ]; then
  existing_payload="$({ docker image ls -aq; docker container ls -aq; docker volume ls -q; } | sed '/^$/d' | head -n 1)"
  [ -z "$existing_payload" ] || {
    echo "refusing to replace nonempty Docker data-root $current_docker_root" >&2
    exit 1
  }
fi
if ! zfs list -H -o name "$DOCKER_DATASET" >/dev/null 2>&1; then
  if [ -d "$DOCKER_DATA_ROOT" ] && [ -n "$(find "$DOCKER_DATA_ROOT" -mindepth 1 -maxdepth 1 -print -quit)" ]; then
    echo "refusing to mount Docker dataset over nonempty $DOCKER_DATA_ROOT" >&2
    exit 1
  fi
  zfs create \
    -o mountpoint="$DOCKER_DATA_ROOT" \
    -o quota="$DOCKER_DATA_LIMIT" \
    -o refquota="$DOCKER_DATA_LIMIT" \
    -o sandboxd:managed=1 \
    "$DOCKER_DATASET"
fi
[ "$(zfs get -H -o value type "$DOCKER_DATASET")" = "filesystem" ] || {
  echo "Docker data-root dataset is not a filesystem: $DOCKER_DATASET" >&2
  exit 1
}
[ "$(zfs get -H -o value sandboxd:managed "$DOCKER_DATASET")" = "1" ] || {
  echo "refusing unmanaged Docker data-root dataset $DOCKER_DATASET" >&2
  exit 1
}
[ "$(zfs get -H -o value mountpoint "$DOCKER_DATASET")" = "$DOCKER_DATA_ROOT" ] || {
  echo "unexpected Docker dataset mountpoint" >&2
  exit 1
}
[ "$(zfs get -H -o value mounted "$DOCKER_DATASET")" = "yes" ] || {
  echo "Docker data-root dataset is not mounted: $DOCKER_DATASET" >&2
  exit 1
}
zfs set quota="$DOCKER_DATA_LIMIT" refquota="$DOCKER_DATA_LIMIT" "$DOCKER_DATASET"
[ "$(zfs get -Hp -o value quota "$DOCKER_DATASET")" -eq "$DOCKER_DATA_LIMIT_BYTES" ] || {
  echo "Docker dataset quota is not $DOCKER_DATA_LIMIT" >&2
  exit 1
}
[ "$(zfs get -Hp -o value refquota "$DOCKER_DATASET")" -eq "$DOCKER_DATA_LIMIT_BYTES" ] || {
  echo "Docker dataset refquota is not $DOCKER_DATA_LIMIT" >&2
  exit 1
}

# Older sandboxd releases set `unless-stopped` on tenant and daemon containers. Neutralize and stop
# both before restarting dockerd; otherwise sandboxd can bind the underlying quota-mount directory
# before XFS is mounted, or untrusted code can start before the host firewall is restored. The new
# compose also pins sandboxd to `restart: no`, so the dstack runner starts it only after pre-launch
# has mounted XFS. The manager then resumes only durable rows recorded as running. Any failure aborts.
managed_ids=$(docker container ls -aq --filter label=cs.managed=1)
daemon_ids=$(docker container ls -aq --filter label=com.docker.compose.service=sandboxd)
legacy_backup_ids=$(docker container ls -aq --filter label=com.docker.compose.service=sandboxd-backup)
quiesce_ids="$managed_ids $daemon_ids $legacy_backup_ids"
for container_id in $quiesce_ids; do
  docker update --restart=no "$container_id" >/dev/null
done
for container_id in $quiesce_ids; do
  if [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "true" ]; then
    docker stop --time 30 "$container_id" >/dev/null
  fi
done
for container_id in $quiesce_ids; do
  [ "$(docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' "$container_id")" = "no" ] || {
    echo "failed to clear pre-firewall restart policy: $container_id" >&2
    exit 1
  }
  [ "$(docker inspect --format '{{.State.Running}}' "$container_id")" = "false" ] || {
    echo "failed to stop container before dockerd restart: $container_id" >&2
    exit 1
  }
done

mkdir -p /etc/docker
DOCKER_DAEMON_CONFIG=/etc/docker/daemon.json
DOCKER_DAEMON_CONFIG_NEW=/etc/docker/daemon.json.new
if [ -f /etc/docker/daemon.json ]; then
  jq --arg p "$RUNSC_BIN" --arg root "$DOCKER_DATA_ROOT" \
    '.runtimes.runsc = {"path": $p}
     | ."data-root" = $root
     | ."storage-driver" = "zfs"
     | ."log-driver" = "local"
     | ."log-opts" = {"max-size": "20m", "max-file": "3"}
     | ."exec-opts" = (((."exec-opts" // [])
         | map(select((startswith("native.cgroupdriver=")) | not)))
         + ["native.cgroupdriver=systemd"])
     | ."cgroup-parent" = "system.slice"' \
    "$DOCKER_DAEMON_CONFIG" > "$DOCKER_DAEMON_CONFIG_NEW"
else
  cat > "$DOCKER_DAEMON_CONFIG_NEW" <<JSON
{
  "runtimes": {
    "runsc": {
      "path": "$RUNSC_BIN"
    }
  },
  "data-root": "$DOCKER_DATA_ROOT",
  "storage-driver": "zfs",
  "exec-opts": ["native.cgroupdriver=systemd"],
  "cgroup-parent": "system.slice",
  "log-driver": "local",
  "log-opts": {
    "max-size": "20m",
    "max-file": "3"
  }
}
JSON
fi
dockerd --validate --config-file "$DOCKER_DAEMON_CONFIG_NEW"
chmod 0644 "$DOCKER_DAEMON_CONFIG_NEW"
mv -f "$DOCKER_DAEMON_CONFIG_NEW" "$DOCKER_DAEMON_CONFIG"

# dstack 0.5.11 ships a vendor docker.service drop-in named override.conf that pins dockerd to CPU
# 0. Moby validates NanoCPUs (`docker --cpus`) against dockerd's own process affinity, so leaving
# that vendor default in place makes an eight-vCPU guest reject every sandbox over one vCPU. Shadow
# the exact vendor filename from /etc with this measured, fixed eight-vCPU profile. A future machine
# upsize must change the VMM profile, EXPECTED_HOST_VCPUS, this explicit list, and (if desired) the
# separately fixed tenant admission budget in one reviewed deployment; it never expands implicitly.
mkdir -p "$DOCKER_AFFINITY_DIR"
cat > "$DOCKER_AFFINITY_TMP" <<'SYSTEMD'
[Service]
CPUAffinity=0 1 2 3 4 5 6 7
SYSTEMD
chmod 0644 "$DOCKER_AFFINITY_TMP"
mv -f "$DOCKER_AFFINITY_TMP" "$DOCKER_AFFINITY_DIR/override.conf"
systemctl daemon-reload
systemctl restart docker

# Fail before the firewall and app start if the service manager, process affinity, or Docker API
# disagrees with the measured machine profile. This catches a vendor drop-in regression instead of
# advertising capacity that Docker cannot apply.
docker_main_pid=$(systemctl show docker.service --property MainPID --value)
case "$docker_main_pid" in
  ''|*[!0-9]*|0) echo "invalid docker.service MainPID: $docker_main_pid" >&2; exit 1 ;;
esac
docker_cpu_affinity=$(awk '/^Cpus_allowed_list:/ {print $2}' "/proc/$docker_main_pid/status")
[ "$docker_cpu_affinity" = "0-7" ] || {
  echo "dockerd CPU affinity is $docker_cpu_affinity; require exactly 0-7" >&2
  exit 1
}
docker_ncpu=$(docker info --format '{{.NCPU}}')
case "$docker_ncpu" in
  ''|*[!0-9]*) echo "invalid Docker CPU readback: $docker_ncpu" >&2; exit 1 ;;
esac
[ "$docker_ncpu" -eq "$EXPECTED_HOST_VCPUS" ] || {
  echo "Docker reports $docker_ncpu CPUs; require exactly $EXPECTED_HOST_VCPUS" >&2
  exit 1
}
[ "$(docker info --format '{{.CgroupVersion}}/{{.CgroupDriver}}')" = "2/systemd" ] || {
  echo "dockerd must use the systemd driver on unified cgroup v2" >&2
  exit 1
}
[ "$(findmnt -n -o FSTYPE /sys/fs/cgroup)" = "cgroup2" ] \
  && [ -r /sys/fs/cgroup/cgroup.controllers ] || {
  echo "host cgroup-v2 hierarchy is unavailable" >&2
  exit 1
}
[ "$(docker info --format '{{.DockerRootDir}}')" = "$DOCKER_DATA_ROOT" ] || {
  echo "dockerd did not switch to managed data-root" >&2
  exit 1
}
[ "$(docker info --format '{{.Driver}}')" = "zfs" ] || {
  echo "dockerd did not activate the ZFS storage driver" >&2
  exit 1
}
docker info --format '{{json .Runtimes}}' | grep -q 'runsc'
"$RUNSC_BIN" --version

if [ -n "${DSTACK_DOCKER_PASSWORD:-}" ] || [ -n "${DSTACK_DOCKER_USERNAME:-}" ]; then
  [ -n "${DSTACK_DOCKER_PASSWORD:-}" ] && [ -n "${DSTACK_DOCKER_USERNAME:-}" ] || {
    echo "incomplete Docker registry credentials" >&2
    exit 1
  }
  printf '%s\n' "$DSTACK_DOCKER_PASSWORD" \
    | docker login "${DSTACK_DOCKER_REGISTRY:-ghcr.io}" -u "$DSTACK_DOCKER_USERNAME" --password-stdin
fi

# Tenant bridges use stable `csb_*` host interfaces. Install the boundary in the host network
# namespace after dockerd has created DOCKER-USER. INPUT blocks tenants from reaching host and
# published ports directly. The custom forwarding chain permits true layer-2 traffic on the same
# private bridge (tenant <-> sandboxd proxy), rejects private/special destinations, and returns only
# public internet traffic to Docker's normal forwarding/NAT path. The daemon installs exact
# source-and-destination /29 returns for validated labeled tenant networks before resuming them; the
# pre-launch base chain deliberately starts with no private allow and is therefore fail-closed.
sysctl -w net.ipv4.ip_forward=1 >/dev/null
sysctl -w net.bridge.bridge-nf-call-iptables=1 >/dev/null
[ "$(sysctl -n net.ipv4.ip_forward)" = "1" ] || {
  echo "IPv4 forwarding is disabled" >&2
  exit 1
}
[ "$(sysctl -n net.bridge.bridge-nf-call-iptables)" = "1" ] || {
  echo "bridge IPv4 packets do not traverse the host firewall" >&2
  exit 1
}
# A helper container shares the host network namespace but not its mount namespace. Bind the exact
# host xtables lock inode so every save/restore serializes with dockerd's own firewall updates.
touch /run/xtables.lock
chmod 0600 /run/xtables.lock
docker run --rm --privileged --network host \
  --mount type=bind,src=/run/xtables.lock,dst=/run/xtables.lock \
  --entrypoint sh "$QUOTA_TOOLS_IMAGE" -ceu '
    IPT=
    IPTR=
    for backend in iptables-legacy iptables-nft iptables; do
      restore="${backend}-restore"
      command -v "$backend" >/dev/null 2>&1 || continue
      command -v "$restore" >/dev/null 2>&1 || continue
      if "$backend" -w 5 -nL DOCKER-USER >/dev/null 2>&1; then
        IPT="$backend"
        IPTR="$restore"
        break
      fi
    done
    [ -n "$IPT" ] || { echo "no iptables backend owns DOCKER-USER" >&2; exit 1; }
    ipt() { "$IPT" -w 5 "$@"; }
    ipt -N SANDBOXD-TENANT 2>/dev/null || true
    restore_file=/tmp/sandboxd-firewall-base
    cat > "$restore_file" <<EOF
*filter
-F SANDBOXD-TENANT
EOF
    for cidr in \
      0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 \
      169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 192.0.2.0/24 \
      192.168.0.0/16 198.18.0.0/15 198.51.100.0/24 203.0.113.0/24 \
      224.0.0.0/4 240.0.0.0/4; do
      printf "%s\n" "-A SANDBOXD-TENANT -d $cidr -j REJECT" >> "$restore_file"
    done
    printf "%s\n" "-A SANDBOXD-TENANT -j RETURN" COMMIT >> "$restore_file"
    "$IPTR" -w 5 --noflush < "$restore_file"
    # A correctly populated DOCKER-USER chain is inert if the Docker FORWARD hook was removed or
    # reordered. Canonicalize the hook as the first forwarding rule and verify it explicitly.
    while ipt -C FORWARD -j DOCKER-USER 2>/dev/null; do
      ipt -D FORWARD -j DOCKER-USER
    done
    ipt -I FORWARD 1 -j DOCKER-USER
    while ipt -C DOCKER-USER -i "csb+" -j SANDBOXD-TENANT 2>/dev/null; do
      ipt -D DOCKER-USER -i "csb+" -j SANDBOXD-TENANT
    done
    ipt -I DOCKER-USER 1 -i "csb+" -j SANDBOXD-TENANT
    while ipt -C INPUT -i "csb+" -j REJECT 2>/dev/null; do
      ipt -D INPUT -i "csb+" -j REJECT
    done
    ipt -I INPUT 1 -i "csb+" -j REJECT
    first_forward="$(ipt -S FORWARD | sed -n "/^-A FORWARD /{p;q;}")"
    [ "$first_forward" = "-A FORWARD -j DOCKER-USER" ]
    ipt -C DOCKER-USER -i "csb+" -j SANDBOXD-TENANT
    ipt -C INPUT -i "csb+" -j REJECT
    ipt -C SANDBOXD-TENANT -d 10.0.0.0/8 -j REJECT
    ipt -C SANDBOXD-TENANT -d 169.254.0.0/16 -j REJECT
    expected="$(sed -n "/^-A SANDBOXD-TENANT /p" "$restore_file")"
    "$IPT" -w 5 -t filter -S > /tmp/sandboxd-firewall-rules
    actual="$(sed -n "/^-A SANDBOXD-TENANT /p" /tmp/sandboxd-firewall-rules \
      | sed "s/ --reject-with icmp-port-unreachable$//")"
    [ "$actual" = "$expected" ]
  '

# Persistent sandbox storage is a separate XFS filesystem with project-quota enforcement. A sparse
# A 251 GiB ZFS zvol lives in the encrypted CVM data pool; HOST_DISK_MB sells 232 GiB. The extra GiB
# absorbs XFS log/metadata while preserving 18 GiB of usable filesystem reserve. Never start the app
# on an unquotaed or undersized fallback directory.
if ! zfs list -H -o name "$QUOTA_ZVOL" >/dev/null 2>&1; then
  zfs create -s -V "$QUOTA_ZVOL_SIZE" -o volblocksize=16K "$QUOTA_ZVOL"
  zfs set sandboxd:managed=1 "$QUOTA_ZVOL"
fi
[ "$(zfs get -H -o value sandboxd:managed "$QUOTA_ZVOL")" = "1" ] || {
  echo "refusing unmanaged quota zvol $QUOTA_ZVOL" >&2
  exit 1
}
# Existing deployments may still have an undersized zvol. Grow it monotonically before mounting;
# never shrink a larger operator-provisioned filesystem. A compose-only update on the old 80 GiB VM
# is rejected below by the physical-pool capacity check rather than advertising fictitious space.
current_zvol_bytes=$(zfs get -Hp -o value volsize "$QUOTA_ZVOL")
case "$current_zvol_bytes" in
  ''|*[!0-9]*) echo "invalid zvol size for $QUOTA_ZVOL: $current_zvol_bytes" >&2; exit 1 ;;
esac
if [ "$current_zvol_bytes" -lt "$QUOTA_ZVOL_BYTES" ]; then
  zfs set volsize="$QUOTA_ZVOL_SIZE" "$QUOTA_ZVOL"
fi
[ "$(zfs get -Hp -o value volsize "$QUOTA_ZVOL")" -ge "$QUOTA_ZVOL_BYTES" ] || {
  echo "quota zvol remains smaller than $QUOTA_ZVOL_SIZE" >&2
  exit 1
}

# A sparse zvol's logical size is not proof that the backing pool can honor it. Use the parent ZFS
# dataset's `available` value (which accounts for reservations, quotas, and pool slop), not `zpool
# free` (which does not describe writable dataset capacity). The state filesystem's exact 2 GiB
# refreservation was already validated above and is therefore already excluded from parent available.
# Add back only existing zvol/Docker allocations, then require the full 232 GiB sale + 18 GiB XFS
# headroom + 28 GiB Docker ceiling. With the already-reserved 2 GiB state filesystem, the profile
# hard-reserves at most 280 GiB and leaves at least 20 GiB of a 300 GiB guest for its OS and ZFS.
quota_pool=${QUOTA_ZVOL%%/*}
[ "$(zpool list -H -o health "$quota_pool")" = "ONLINE" ] || {
  echo "backing pool is not ONLINE: $quota_pool" >&2
  exit 1
}
parent_available_bytes=$(zfs get -Hp -o value available "$quota_pool")
zvol_referenced_bytes=$(zfs get -Hp -o value referenced "$QUOTA_ZVOL")
docker_used_bytes=$(zfs get -Hp -o value used "$DOCKER_DATASET")
case "$parent_available_bytes:$zvol_referenced_bytes:$docker_used_bytes" in
  *[!0-9:]*) echo "invalid physical capacity readback for $quota_pool" >&2; exit 1 ;;
esac
required_pool_bytes=$(((QUOTA_SOLD_MIB + QUOTA_FS_HEADROOM_MIB + QUOTA_POOL_HEADROOM_MIB) * 1024 * 1024))
[ $((parent_available_bytes + zvol_referenced_bytes + docker_used_bytes)) -ge "$required_pool_bytes" ] || {
  echo "insufficient backing pool after ${STATE_RESERVE_MIB} MiB state reservation for ${QUOTA_SOLD_MIB} MiB sold + ${QUOTA_FS_HEADROOM_MIB} MiB XFS + ${QUOTA_POOL_HEADROOM_MIB} MiB Docker" >&2
  exit 1
}
for _ in 1 2 3 4 5 6 7 8 9 10; do
  [ -b "$QUOTA_DEVICE" ] && break
  sleep 1
done
[ -b "$QUOTA_DEVICE" ] || { echo "quota zvol device missing: $QUOTA_DEVICE" >&2; exit 1; }
if [ "$(zfs get -H -o value sandboxd:format "$QUOTA_ZVOL")" != "xfs-v1" ]; then
  # The minimal dstack host has no xfsprogs. Format only our marked, managed zvol with the exact
  # pinned sandboxd image; the image is needed for the app anyway and is digest-pinned.
  docker run --rm --privileged -v /dev:/dev \
    --entrypoint mkfs.xfs "$QUOTA_TOOLS_IMAGE" -f -K "$QUOTA_DEVICE"
  zfs set sandboxd:format=xfs-v1 "$QUOTA_ZVOL"
fi
mkdir -p "$QUOTA_MOUNT"
grep -qs " $QUOTA_MOUNT " /proc/mounts || \
  mount -t xfs -o prjquota,nosuid,nodev "$QUOTA_DEVICE" "$QUOTA_MOUNT"
# Grow the XFS filesystem after a monotonic zvol expansion. The host intentionally has no xfsprogs,
# so run the checksum-pinned tools image against the already-mounted filesystem.
docker run --rm --privileged \
  -v "$QUOTA_MOUNT:$QUOTA_MOUNT" \
  --entrypoint xfs_growfs "$QUOTA_TOOLS_IMAGE" "$QUOTA_MOUNT"
# Validate the host mount before compose. sandboxd independently performs xfs_quota state and exact
# limit read-back checks from inside its trusted container and refuses to start on any mismatch.
grep -Eqs " $QUOTA_MOUNT xfs .*(prjquota|pquota)" /proc/mounts
quota_fs_kib=$(df -Pk "$QUOTA_MOUNT" | awk 'NR == 2 {print $2}')
case "$quota_fs_kib" in
  ''|*[!0-9]*) echo "invalid XFS capacity readback for $QUOTA_MOUNT" >&2; exit 1 ;;
esac
quota_fs_bytes=$((quota_fs_kib * 1024))
required_fs_bytes=$(((QUOTA_SOLD_MIB + QUOTA_FS_HEADROOM_MIB) * 1024 * 1024))
[ "$quota_fs_bytes" -ge "$required_fs_bytes" ] || {
  echo "XFS capacity below ${QUOTA_SOLD_MIB} MiB sold + ${QUOTA_FS_HEADROOM_MIB} MiB reserve" >&2
  exit 1
}
chmod 0711 "$QUOTA_MOUNT"

"""
    rendered = json.dumps(app_compose, indent=4, ensure_ascii=False)
    return rendered, hashlib.sha256(rendered.encode()).hexdigest()


def kms_urls() -> list[str]:
    return ["https://10.0.2.2:9101"] if NET_MODE == "bridge" else m.KMS_URLS


def create_vm(
    app_id: str, compose_file: str, env: dict[str, str], *, stopped: bool
) -> dict:
    sealed = dict(env)
    sealed["APP_ID"] = app_id
    return m.vmm(
        "CreateVm",
        {
            "name": NAME,
            "image": "dstack-0.5.11",
            "compose_file": compose_file,
            "vcpu": VCPU,
            "memory": MEM,
            "disk_size": DISK,
            "app_id": app_id,
            "user_config": "",
            "ports": [m._parse_port(port) for port in PORTS],
            "hugepages": False,
            "pin_numa": False,
            "stopped": stopped,
            "no_tee": False,
            "kms_urls": kms_urls(),
            "networking": {"mode": NET_MODE},
            "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
            "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
        },
    )


def describe_vm(vm_id: str) -> dict[str, object]:
    """Return only the exact VMM fields needed to gate a replacement cutover."""
    response = m.vmm("GetInfo", {"id": vm_id})
    info = response.get("info") or {}
    config = info.get("configuration") or {}
    compose_file = config.get("compose_file")
    compose_hash = (
        hashlib.sha256(compose_file.encode()).hexdigest()
        if isinstance(compose_file, str) and compose_file
        else None
    )
    return {
        "vm_id": vm_id,
        "found": bool(response.get("found")),
        "status": info.get("status"),
        "boot_progress": info.get("boot_progress"),
        "boot_error": info.get("boot_error"),
        "vcpu": config.get("vcpu"),
        "memory": config.get("memory"),
        "disk_size": config.get("disk_size"),
        "app_id": config.get("app_id"),
        "compose_hash": compose_hash,
    }


def app_inventory(app_id: str) -> dict[str, object]:
    """Enumerate every VMM entry for one app identity before either side of a cutover starts."""

    wanted = app_id.lower().removeprefix("0x")
    if len(wanted) != 40 or any(char not in "0123456789abcdef" for char in wanted):
        raise SystemExit(f"invalid app id for inventory: {app_id}")
    response = m.vmm("Status")
    vms = []
    for vm in response.get("vms") or []:
        current = str(vm.get("app_id") or "").lower().removeprefix("0x")
        if current != wanted:
            continue
        vms.append(
            {
                "vm_id": vm.get("id"),
                "name": vm.get("name"),
                "status": vm.get("status"),
                "app_id": "0x" + current,
            }
        )
    return {"app_id": "0x" + wanted, "vms": vms}


def canonical_app_id(value: object) -> str:
    app_id = str(value or "").lower().removeprefix("0x")
    if len(app_id) != 40 or any(char not in "0123456789abcdef" for char in app_id):
        raise SystemExit(f"invalid app id: {value}")
    return "0x" + app_id


def canonical_compose_hash(value: object, *, label: str) -> str:
    compose_hash = str(value or "").lower().removeprefix("0x")
    if len(compose_hash) != 64 or any(
        char not in "0123456789abcdef" for char in compose_hash
    ):
        raise SystemExit(f"invalid {label} compose hash: {value}")
    return compose_hash


def exact_resource_int(value: object, *, label: str) -> int:
    # bool is a subclass of int in Python; reject it explicitly. VMM JSON may expose integer fields
    # as decimal strings, but floats and non-decimal spellings are not exact resource readbacks.
    if type(value) is int:  # noqa: E721 - exact type is the security property here
        return value
    if isinstance(value, str) and value.isdecimal():
        return int(value)
    raise SystemExit(f"invalid exact integer resource {label}: {value}")


def checked_profile(vcpu: object, memory: object, disk: object) -> tuple[int, int, int]:
    try:
        expected = (
            exact_resource_int(vcpu, label="vcpu"),
            exact_resource_int(memory, label="memory"),
            exact_resource_int(disk, label="disk"),
        )
    except SystemExit as error:
        raise SystemExit("invalid expected VM resource profile") from error
    configured = (VCPU, MEM, DISK)
    if expected != configured:
        raise SystemExit(
            "expected VM resource profile does not match the measured helper profile: "
            f"expected={expected} helper={configured}"
        )
    return expected


def is_terminal_status(value: object) -> bool:
    return str(value or "").lower() in {"stopped", "exited"}


def is_running_status(value: object) -> bool:
    status = str(value or "").lower()
    return status in {"running", "started"}


def require_exact_vm(
    vm_id: str,
    app_id: str,
    compose_hash: str,
    profile: tuple[int, int, int],
    *,
    stopped: bool | None = None,
    context: str,
) -> dict[str, object]:
    expected_app = canonical_app_id(app_id)
    expected_hash = canonical_compose_hash(compose_hash, label="expected")
    current = describe_vm(vm_id)
    actual_app = (
        canonical_app_id(current.get("app_id")) if current.get("app_id") else ""
    )
    actual_hash = (
        canonical_compose_hash(current.get("compose_hash"), label="VMM readback")
        if current.get("compose_hash")
        else ""
    )
    try:
        actual_profile = (
            exact_resource_int(current.get("vcpu"), label="VMM vcpu"),
            exact_resource_int(current.get("memory"), label="VMM memory"),
            exact_resource_int(current.get("disk_size"), label="VMM disk"),
        )
    except SystemExit:
        actual_profile = ()
    terminal = is_terminal_status(current.get("status"))
    status_matches = stopped is None or terminal is stopped
    if not (
        current.get("found") is True
        and current.get("vm_id") == vm_id
        and actual_app == expected_app
        and actual_hash == expected_hash
        and actual_profile == profile
        and status_matches
    ):
        raise SystemExit(
            f"{context} VM identity/hash/profile/status mismatch: "
            + json.dumps(current, sort_keys=True)
        )
    return current


def require_exact_inventory(
    app_id: str,
    expected_vm_id: str,
    *,
    stopped: bool,
    context: str,
) -> None:
    inventory = app_inventory(app_id)
    vms = inventory.get("vms")
    if not isinstance(vms, list):
        raise SystemExit(f"{context} same-app inventory is malformed")
    entry = vms[0] if len(vms) == 1 and isinstance(vms[0], dict) else None
    status = entry.get("status") if entry else None
    status_matches = (
        is_terminal_status(status) if stopped else is_running_status(status)
    )
    if not (
        entry is not None
        and entry.get("vm_id") == expected_vm_id
        and canonical_app_id(entry.get("app_id")) == canonical_app_id(app_id)
        and status_matches
    ):
        raise SystemExit(
            f"{context} same-app inventory mismatch: "
            + json.dumps(
                {
                    "expected_vm_id": expected_vm_id,
                    "expected_stopped": stopped,
                    "vms": vms,
                },
                sort_keys=True,
            )
        )


def require_retirement_inventory(
    app_id: str,
    current_vm_id: str,
    previous_vm_id: str,
    *,
    context: str,
) -> None:
    """Require exactly one steady current VM and one terminal previous VM."""

    inventory = app_inventory(app_id)
    vms = inventory.get("vms")
    if not isinstance(vms, list):
        raise SystemExit(f"{context} same-app inventory is malformed")
    by_id = {
        vm.get("vm_id"): vm
        for vm in vms
        if isinstance(vm, dict) and isinstance(vm.get("vm_id"), str)
    }
    current = by_id.get(current_vm_id)
    previous = by_id.get(previous_vm_id)
    if not (
        len(vms) == 2
        and len(by_id) == 2
        and current is not None
        and previous is not None
        and canonical_app_id(current.get("app_id")) == canonical_app_id(app_id)
        and canonical_app_id(previous.get("app_id")) == canonical_app_id(app_id)
        and is_running_status(current.get("status"))
        and is_terminal_status(previous.get("status"))
    ):
        raise SystemExit(
            f"{context} retirement inventory mismatch: "
            + json.dumps(
                {
                    "current_vm_id": current_vm_id,
                    "previous_vm_id": previous_vm_id,
                    "vms": vms,
                },
                sort_keys=True,
            )
        )


def exact_profile(
    vcpu: object,
    memory: object,
    disk: object,
    *,
    label: str,
) -> tuple[int, int, int]:
    profile = (
        exact_resource_int(vcpu, label=f"{label} vcpu"),
        exact_resource_int(memory, label=f"{label} memory"),
        exact_resource_int(disk, label=f"{label} disk"),
    )
    if any(value <= 0 for value in profile):
        raise SystemExit(f"invalid non-positive {label} VM resource profile: {profile}")
    return profile


def checked_retire_previous_vm(
    app_id: str,
    current_vm_id: str,
    current_hash: str,
    previous_vm_id: str,
    previous_hash: str,
    current_profile: tuple[int, int, int],
    previous_profile: tuple[int, int, int],
) -> dict[str, object]:
    """Remove one explicitly journaled stopped predecessor without touching the current VM."""

    if not current_vm_id or not previous_vm_id or current_vm_id == previous_vm_id:
        raise SystemExit("retirement requires distinct current and previous VM ids")
    app_id = canonical_app_id(app_id)
    current_hash = canonical_compose_hash(current_hash, label="current")
    previous_hash = canonical_compose_hash(previous_hash, label="previous")

    current = require_exact_vm(
        current_vm_id,
        app_id,
        current_hash,
        current_profile,
        stopped=False,
        context="pre-retirement current",
    )
    if not is_running_status(current.get("status")):
        raise SystemExit("pre-retirement current VM is not exactly running/started")

    previous = describe_vm(previous_vm_id)
    if previous.get("found") is not True:
        # Crash recovery after an applied RemoveVm is idempotent only if the exact current VM is now
        # the sole same-app entry. Never interpret an absent predecessor as success in a larger or
        # malformed inventory.
        require_exact_inventory(
            app_id,
            current_vm_id,
            stopped=False,
            context="already-retired",
        )
        return {
            "app_id": app_id,
            "current_vm_id": current_vm_id,
            "previous_vm_id": previous_vm_id,
            "already_removed": True,
        }

    require_exact_vm(
        previous_vm_id,
        app_id,
        previous_hash,
        previous_profile,
        stopped=True,
        context="pre-retirement previous",
    )
    # This is the final VMM proof before RemoveVm. The shared node lock prevents all normal
    # deployment helpers from changing either entry between this exact inventory and mutation.
    require_retirement_inventory(
        app_id,
        current_vm_id,
        previous_vm_id,
        context="pre-retirement",
    )

    try:
        remove_result: object = m.vmm("RemoveVm", {"id": previous_vm_id})
    except Exception as error:
        # A transport error can occur after the VMM has durably removed the VM. Continue only when
        # exact postconditions prove that outcome; otherwise the shell keeps its retirement journal.
        after_error = describe_vm(previous_vm_id)
        if after_error.get("found") is True:
            raise
        remove_result = {"remove_error": str(error)}

    for _ in range(60):
        if describe_vm(previous_vm_id).get("found") is not True:
            break
        time.sleep(2)
    else:
        raise SystemExit(
            f"timed out waiting for retired VM to disappear: {previous_vm_id}"
        )

    current = require_exact_vm(
        current_vm_id,
        app_id,
        current_hash,
        current_profile,
        stopped=False,
        context="post-retirement current",
    )
    if not is_running_status(current.get("status")):
        raise SystemExit("post-retirement current VM is not exactly running/started")
    require_exact_inventory(
        app_id,
        current_vm_id,
        stopped=False,
        context="post-retirement",
    )
    return {
        "app_id": app_id,
        "current_vm_id": current_vm_id,
        "previous_vm_id": previous_vm_id,
        "already_removed": False,
        "remove": remove_result,
    }


def stop_vm_for_checked_update(
    vm_id: str,
    app_id: str,
    previous_hash: str,
    profile: tuple[int, int, int],
) -> dict[str, object]:
    """Prove the exact old VM/inventory, stop it, then prove old/stopped/quiescent."""

    before = require_exact_vm(
        vm_id,
        app_id,
        previous_hash,
        profile,
        context="pre-update",
    )
    already_stopped = is_terminal_status(before.get("status"))
    if not already_stopped and not is_running_status(before.get("status")):
        raise SystemExit(
            "pre-update VM is neither exactly stopped/exited nor running/started: "
            f"{before.get('status')}"
        )
    require_exact_inventory(
        app_id,
        vm_id,
        stopped=already_stopped,
        context="pre-update",
    )

    result: object = {"already_stopped": True}
    if not already_stopped:
        try:
            result = m.vmm("StopVm", {"id": vm_id})
        except Exception as error:
            # A transport error after StopVm may still have applied. Continue only if the exact old
            # VM is now stopped; every other outcome remains ambiguous and is left to the durable
            # shell journal for operator recovery.
            require_exact_vm(
                vm_id,
                app_id,
                previous_hash,
                profile,
                stopped=True,
                context="post-stop-error",
            )
            result = {"stop_error": str(error)}

    stopped_vm: dict[str, object] | None = None
    for _ in range(60):
        current = require_exact_vm(
            vm_id,
            app_id,
            previous_hash,
            profile,
            context="post-stop",
        )
        if is_terminal_status(current.get("status")):
            stopped_vm = current
            break
        time.sleep(2)
    if stopped_vm is None:
        raise SystemExit(f"timed out waiting for exact old VM to stop: {vm_id}")
    require_exact_vm(
        vm_id,
        app_id,
        previous_hash,
        profile,
        stopped=True,
        context="post-stop",
    )
    require_exact_inventory(app_id, vm_id, stopped=True, context="post-stop")
    return {"vm_id": vm_id, "status": stopped_vm.get("status"), "result": result}


def checked_start_vm(
    vm_id: str,
    app_id: str,
    target_hash: str,
    profile: tuple[int, int, int],
) -> dict[str, object]:
    """Start only an exact stopped target while the same-app active inventory is empty."""

    before = require_exact_vm(
        vm_id,
        app_id,
        target_hash,
        profile,
        stopped=True,
        context="checked-start",
    )
    # This is intentionally the last VMM call before StartVm. The remote node-wide flock prevents a
    # second measured helper invocation from changing the same app inventory between the proof and
    # mutation.
    require_exact_inventory(app_id, vm_id, stopped=True, context="checked-start")
    result = m.vmm("StartVm", {"id": vm_id})
    return {"vm_id": vm_id, "status_before": before.get("status"), "result": result}


def checked_update_vm(
    app_id: str,
    vm_id: str,
    previous_hash: str,
    target_hash: str,
    profile: tuple[int, int, int],
) -> dict[str, object]:
    """Perform one hash-bound, resource-bound, inventory-safe in-place update."""

    previous_hash = canonical_compose_hash(previous_hash, label="previous")
    target_hash = canonical_compose_hash(target_hash, label="target")
    env = build_env(app_id)
    # app_compose_and_hash snapshots COMPOSE_PATH once. Keep this exact string through hashing,
    # sealing, UpgradeApp, and target readback; a concurrent path swap cannot change the payload.
    compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
    if compose_hash != target_hash:
        raise SystemExit(
            "measured compose input does not match journaled target before StopVm: "
            f"measured={compose_hash} target={target_hash}"
        )
    sealed = dict(env)
    sealed["APP_ID"] = canonical_app_id(app_id)
    encrypted_env = m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id))
    parsed_ports = [m._parse_port(port) for port in PORTS]
    target_kms_urls = kms_urls()
    target_gateway_urls = [m.GATEWAY_RPC] if GATEWAY_ENABLED else []

    stop = stop_vm_for_checked_update(vm_id, app_id, previous_hash, profile)
    upgrade = m.vmm(
        "UpgradeApp",
        {
            "id": vm_id,
            "compose_file": compose_file,
            "encrypted_env": encrypted_env,
            "update_ports": True,
            "ports": parsed_ports,
            "update_kms_urls": True,
            "kms_urls": target_kms_urls,
            "update_gateway_urls": True,
            "gateway_urls": target_gateway_urls,
        },
    )
    # UpgradeApp must preserve stopped state and exact immutable resources. Re-prove an empty
    # inventory, then repeat the same checks immediately at the checked StartVm boundary.
    require_exact_vm(
        vm_id,
        app_id,
        target_hash,
        profile,
        stopped=True,
        context="post-upgrade",
    )
    require_exact_inventory(app_id, vm_id, stopped=True, context="post-upgrade")
    start = checked_start_vm(vm_id, app_id, target_hash, profile)
    return {
        "app_id": canonical_app_id(app_id),
        "compose_hash": compose_hash,
        "vm_id": vm_id,
        "stop": stop,
        "upgrade": upgrade,
        "start": start,
    }


def stop_vm(vm_id: str) -> dict[str, object]:
    """Idempotently stop one exact VM and wait for a stopped readback."""
    before = describe_vm(vm_id)
    if not before["found"]:
        raise SystemExit(f"VM not found: {vm_id}")
    status = str(before.get("status") or "").lower()
    result: object = {"already_stopped": True}
    if status not in {"stopped", "exited"}:
        try:
            result = m.vmm("StopVm", {"id": vm_id})
        except Exception as error:
            raced = describe_vm(vm_id)
            raced_status = str(raced.get("status") or "").lower()
            if raced_status not in {"stopped", "exited"}:
                raise
            result = {"already_stopped": True, "stop_error": str(error)}
    for _ in range(60):
        current = describe_vm(vm_id)
        current_status = str(current.get("status") or "").lower()
        if current_status in {"stopped", "exited"}:
            return {"vm_id": vm_id, "status": current.get("status"), "result": result}
        time.sleep(2)
    raise SystemExit(f"timed out waiting for VM to stop: {vm_id}")


def start_vm(vm_id: str) -> dict[str, object]:
    """Idempotently request start for one exact VM."""
    before = describe_vm(vm_id)
    if not before["found"]:
        raise SystemExit(f"VM not found: {vm_id}")
    status = str(before.get("status") or "").lower()
    if status.startswith("run") or status.startswith("start"):
        result: object = {"already_started": True}
    else:
        try:
            result = m.vmm("StartVm", {"id": vm_id})
        except Exception as error:
            raced = describe_vm(vm_id)
            raced_status = str(raced.get("status") or "").lower()
            if not (raced_status.startswith("run") or raced_status.startswith("start")):
                raise
            result = {"already_started": True, "start_error": str(error)}
    return {"vm_id": vm_id, "status_before": before.get("status"), "result": result}


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "hash"

    # The measured compose admits up to 5 vCPU / 10 GiB / 232 GiB / 16,384 PIDs of tenant resources.
    # This release has one exact measured machine profile. An explicit future upsize must update
    # these values, the guest preflight/affinity, and any intended admission-budget change together.
    mismatched = []
    if VCPU != 8:
        mismatched.append(f"vcpu={VCPU} != 8")
    if MEM != 16384:
        mismatched.append(f"memory={MEM} != 16384 MiB")
    if DISK != 300:
        mismatched.append(f"disk={DISK} != 300 GiB")
    if mismatched:
        raise SystemExit(
            "refusing non-production sandboxd VM profile: " + ", ".join(mismatched)
        )

    if mode == "deploy":
        compose_file, compose_hash = app_compose_and_hash(
            ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"]
        )
        app_id = m._deploy_app_contract(compose_hash)
        env = build_env(app_id, compose_hash)
        result = create_vm(app_id, compose_file, env, stopped=True)
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": result.get("id"),
                    "gateway_url": gateway_url(app_id) if GATEWAY_ENABLED else None,
                }
            )
        )
        return

    if mode == "create-replacement":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not app_id:
            raise SystemExit("usage: sandboxd-node-box.py create-replacement <app_id>")
        compose_file, compose_hash = app_compose_and_hash(ENV_KEYS)
        env = build_env(app_id, compose_hash)
        result = create_vm(app_id, compose_file, env, stopped=True)
        vm_id = result.get("id")
        if not vm_id:
            raise SystemExit("CreateVm returned no replacement VM id")
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": vm_id,
                    "gateway_url": gateway_url(app_id) if GATEWAY_ENABLED else None,
                }
            )
        )
        return

    if mode == "hash":
        _, digest = app_compose_and_hash(ENV_KEYS + ["DSTACK_DOCKER_REGISTRY"])
        print(digest)
        return

    if mode == "inventory-app":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not app_id:
            raise SystemExit("usage: sandboxd-node-box.py inventory-app <app_id>")
        print(json.dumps(app_inventory(app_id)))
        return

    if mode in {"start", "stop", "describe"}:
        vm_id = sys.argv[2] if len(sys.argv) > 2 else ""
        if not vm_id:
            raise SystemExit(f"usage: sandboxd-node-box.py {mode} <vm_id>")
        if mode == "start":
            result = start_vm(vm_id)
        elif mode == "stop":
            result = stop_vm(vm_id)
        else:
            result = describe_vm(vm_id)
        print(json.dumps(result))
        return

    if mode == "checked-start":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        target_hash = sys.argv[4] if len(sys.argv) > 4 else ""
        if len(sys.argv) != 8 or not app_id or not vm_id:
            raise SystemExit(
                "usage: sandboxd-node-box.py checked-start "
                "<app_id> <vm_id> <target_hash> <vcpu> <memory> <disk>"
            )
        profile = checked_profile(sys.argv[5], sys.argv[6], sys.argv[7])
        print(json.dumps(checked_start_vm(vm_id, app_id, target_hash, profile)))
        return

    if mode == "update":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        previous_hash = sys.argv[4] if len(sys.argv) > 4 else ""
        target_hash = sys.argv[5] if len(sys.argv) > 5 else ""
        if len(sys.argv) != 9 or not app_id or not vm_id:
            raise SystemExit(
                "usage: sandboxd-node-box.py update "
                "<app_id> <vm_id> <previous_hash> <target_hash> <vcpu> <memory> <disk>"
            )
        profile = checked_profile(sys.argv[6], sys.argv[7], sys.argv[8])
        print(
            json.dumps(
                checked_update_vm(
                    app_id,
                    vm_id,
                    previous_hash,
                    target_hash,
                    profile,
                )
            )
        )
        return

    if mode == "retire-previous":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        current_vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        current_hash = sys.argv[4] if len(sys.argv) > 4 else ""
        previous_vm_id = sys.argv[5] if len(sys.argv) > 5 else ""
        previous_hash = sys.argv[6] if len(sys.argv) > 6 else ""
        if len(sys.argv) != 13 or not app_id or not current_vm_id or not previous_vm_id:
            raise SystemExit(
                "usage: sandboxd-node-box.py retire-previous "
                "<app_id> <current_vm_id> <current_hash> <previous_vm_id> <previous_hash> "
                "<current_vcpu> <current_memory> <current_disk> "
                "<previous_vcpu> <previous_memory> <previous_disk>"
            )
        current_profile = checked_profile(sys.argv[7], sys.argv[8], sys.argv[9])
        previous_profile = exact_profile(
            sys.argv[10],
            sys.argv[11],
            sys.argv[12],
            label="previous",
        )
        print(
            json.dumps(
                checked_retire_previous_vm(
                    app_id,
                    current_vm_id,
                    current_hash,
                    previous_vm_id,
                    previous_hash,
                    current_profile,
                    previous_profile,
                )
            )
        )
        return

    if mode == "upgrade-stopped":
        app_id = sys.argv[2] if len(sys.argv) > 2 else ""
        vm_id = sys.argv[3] if len(sys.argv) > 3 else ""
        if not app_id or not vm_id:
            raise SystemExit(
                "usage: sandboxd-node-box.py upgrade-stopped <app_id> <vm_id>"
            )
        env = build_env(app_id)
        compose_file, compose_hash = app_compose_and_hash(list(env.keys()))
        sealed = dict(env)
        sealed["APP_ID"] = app_id
        # A rolled-back replacement uses upgrade-stopped so its corrected measured compose can be
        # installed without ever starting alongside the active old VM.
        before = describe_vm(vm_id)
        if not before["found"] or str(before.get("status") or "").lower() not in {
            "stopped",
            "exited",
        }:
            raise SystemExit(
                "upgrade-stopped requires an exactly stopped replacement VM"
            )
        upgrade = m.vmm(
            "UpgradeApp",
            {
                "id": vm_id,
                "compose_file": compose_file,
                "encrypted_env": m._seal_env(sealed, m._app_env_encrypt_pubkey(app_id)),
                "update_ports": True,
                "ports": [m._parse_port(port) for port in PORTS],
                "update_kms_urls": True,
                "kms_urls": kms_urls(),
                "update_gateway_urls": True,
                "gateway_urls": [m.GATEWAY_RPC] if GATEWAY_ENABLED else [],
            },
        )
        after = describe_vm(vm_id)
        if (
            str(after.get("status") or "").lower() not in {"stopped", "exited"}
            or after.get("compose_hash") != compose_hash
        ):
            raise SystemExit(
                "UpgradeApp did not preserve stopped state and exact compose hash"
            )
        print(
            json.dumps(
                {
                    "app_id": app_id,
                    "compose_hash": compose_hash,
                    "vm_id": vm_id,
                    "status": after.get("status"),
                    "upgrade": upgrade,
                }
            )
        )
        return

    raise SystemExit(
        "usage: sandboxd-node-box.py "
        "[deploy|create-replacement <app_id>|hash|inventory-app <app_id>|describe <vm_id>|stop <vm_id>|"
        "start <vm_id>|checked-start <app_id> <vm_id> <target_hash> <vcpu> <memory> <disk>|"
        "update <app_id> <vm_id> <previous_hash> <target_hash> <vcpu> <memory> <disk>|"
        "retire-previous <app_id> <current_vm_id> <current_hash> <previous_vm_id> <previous_hash> "
        "<current_vcpu> <current_memory> <current_disk> <previous_vcpu> <previous_memory> <previous_disk>|"
        "upgrade-stopped <app_id> <vm_id>]"
    )


if __name__ == "__main__":
    main()
