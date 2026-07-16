# sandboxd production node

This release has one exact machine profile: **8 vCPU, 16,384 MiB RAM, and 300 GiB disk**.
The measured tenant budget remains independently fixed at 5,000 CPU millis, 10,240 MiB memory,
4,096 exact guest tasks, 237,568 MiB of quota-backed disk, and 32 concurrent sandboxes, all at 1.0
overcommit. The separate runsc host-task ceilings are fixed at `18*N+512` per tenant; measured
pre-launch proves their full aggregate plus a reserved OS margin against the kernel and parent
cgroup and refuses to start when that profile does not fit.
Hardware discovery never changes those admission limits.

The measured create policy keeps the 100-mCPU runsc floor only for isolated workloads. A runsc
sandbox declaring an ingress port must explicitly request at least 1,000 mCPU; smaller requests are
rejected before reservation and are never silently upsized. This matches Fleet's smallest published
plan and keeps gVisor cold start inside the bounded ingress-readiness transaction.

Treat any future increase of that networked floor as a drain-or-upsize migration. While the old
compose is still running, explicitly PATCH every retained networked sandbox to the new minimum (or
destroy/recreate it) and verify the ledger before deploying the new compose. Recovery rejects a
retained serving row below the configured floor instead of rewriting its measured size. If one was
missed, roll back to the previous measured compose, remediate it explicitly, and retry; rows already
being destroyed or deleted are exempt only so cleanup can complete.

An explicit future machine upsize requires one reviewed release changing the VMM profile,
`EXPECTED_HOST_VCPUS`, the explicit Docker CPU-affinity list, and any separately approved tenant
budget. An out-of-band resize fails the exact profile check. Tenant resources likewise change only
through the authenticated monotonic-upsize API; pressure, restart, and reconciliation never resize
them.

## Deployment

Use the durable state journal and its deployment lock; do not call the VMM update RPC directly:

```sh
source deploy/env.sh
deploy/sandboxd-node.sh sandboxd update
deploy/sandboxd-node.sh sandboxd verify-health
deploy/sandboxd-node.sh sandboxd smoke
```

A successful whole-VM replacement deliberately retains its stopped predecessor in
`PREVIOUS_VM_ID` for an operator rollback. The in-place updater will not run while that dormant
same-app VM exists: it still owns storage and could be started later. Once the rollback window is
explicitly over, inspect that exact stopped VM and retire it with its full journaled id:

```sh
SANDBOXD_RETIRE_PREVIOUS_VM_ID=<exact-PREVIOUS_VM_ID> \
  deploy/sandboxd-node.sh sandboxd retire-previous
```

This destructive action accepts only the journaled predecessor, never the current VM. Before
`RemoveVm`, it durably records both VM identities, both measured hashes, and both resource profiles,
then independently proves an inventory containing exactly the steady current VM and that stopped
predecessor. It clears `PREVIOUS_VM_ID` only after the predecessor is absent and the exact current
VM is the sole same-app entry. A crash or lost RPC response leaves a resumable retirement journal;
all other deployment actions remain blocked until the same exact-id command reconciles it.

An in-place update writes its VM id, previous compose hash, target compose hash, and phase to the
state journal before stopping the VM. If the command is interrupted, leave the compose and journal
unchanged and rerun `update`; recovery accepts only the recorded previous or target hash, resumes the
same VM, and commits `H` only after exact target health plus one-active-VM inventory. A third hash
fails closed. Never delete or hand-edit the journal to get past that check.

For a legacy interrupted update that predates this journal, first inspect the exact VMM hash,
resources, app identity, and same-app inventory. Only then may one invocation set
`SANDBOXD_UPDATE_RECOVERY_FROM_HASH` to that full 64-hex inspected hash. This is an exact incident
reconciliation value, not a boolean bypass; the script persists it as the previous hash before the
next mutation.

If a journaled update reached `UPDATE_PHASE=upgraded` but failed the target health gate, keep the
journal intact. Prepare the reviewed hotfix compose, inspect the state journal and VMM, and confirm
that the same recorded VM still has the exact journaled `UPDATE_H`, 8-vCPU/16,384-MiB/300-GiB
profile, app id, and same-app inventory. Then authorize that one transition with the full failed
hash (not the new hash):

```sh
SANDBOXD_UPDATE_FAILED_TARGET_REBASE_FROM_HASH=<64-hex-failed-UPDATE_H> \
  deploy/sandboxd-node.sh sandboxd update
```

This is accepted only from `upgraded`, only when the newly measured compose differs, and only after
the failed target cannot prove healthy on three fresh checks. The updater then re-reads the exact VM,
resources, app, failed hash, status, and inventory before atomically changing the journal to
`prepared` with the failed hash as `UPDATE_PREVIOUS_H` and the hotfix as `UPDATE_H`. The
last-known-good `H` is deliberately preserved until the hotfix proves exact health and unique
inventory. A missing/wrong opt-in, healthy old target, different VM, resource drift, app drift,
third compose hash, or ambiguous inventory fails before allowlisting or VMM mutation. Do not use
the legacy `SANDBOXD_UPDATE_RECOVERY_FROM_HASH` path for a journaled failed target.

Every box-helper invocation receives a fresh root-owned `0700` directory containing read-only
compose/helper snapshots and runs under one node-wide remote `flock`; fixed `/tmp` inputs are
forbidden. The update call passes both durable journal hashes and the exact 8/16,384/300 profile to
that helper. Before `StopVm`, it rejects a compose snapshot whose hash differs from `UPDATE_H`, then
proves the current VM is exactly `UPDATE_PREVIOUS_H`, has the recorded app and resources, is either
running/started or stopped, and is the sole same-app inventory entry in either state. After stopping
it re-proves the old hash, stopped state, resources, identity, and absence of any duplicate entry.
`UpgradeApp` receives the
same in-memory compose string that was hashed; before `StartVm`, the helper twice proves the exact
target hash, stopped state, identity/resources, and sole same-app inventory. A resumed stopped
target uses the same checked-start operation. Transitional/unknown status, input replacement, a
third hash, resource drift, or any active/dormant duplicate therefore fails before the next VMM
mutation.

The first release that introduces the guest `RLIMIT_NPROC` boundary requires an empty node. Legacy
retained containers have no safe guest-task ceiling and are rejected fail-closed; they are never
grandfathered. Drain/destroy them and verify zero committed and pending sandbox capacity before
running the node update. Later explicit PID upsizes use the controlled recreation path.

Pre-launch stops managed workloads before restarting Docker, restores the host firewall before the
daemon resumes durable rows, and fails closed unless all of these are true:

- the guest exposes exactly eight processors;
- the measured `/etc/systemd/system/docker.service.d/override.conf` shadows DStack 0.5.11's
  one-CPU vendor affinity;
- dockerd's effective `Cpus_allowed_list` is `0-7` and `docker info .NCPU` is exactly `8`;
- Docker uses the managed 28 GiB ZFS data root and the pinned runsc runtime;
- Docker uses unified cgroup v2 with the systemd driver, and sandboxd's read-only host hierarchy
  readback matches every running tenant's exact CPU, memory, zero-swap, and host-PID ceiling;
- `kernel.threads-max`, `kernel.pid_max`, and `system.slice/pids.max` cover the fixed worst-case
  `18*4096 + 512*32` runtime envelope plus the configured host-task reserve, while measured
  baseline task use remains inside that reserve;
- the trusted daemon's host PID namespace makes Docker's init PID directly comparable to the exact
  leaf's `cgroup.procs`; tenant containers remain in private PID and cgroup namespaces;
- XFS project block and inode quota enforcement is active;
- the tenant firewall matches the measured rule set.

The compose healthcheck continuously rechecks Docker's eight-CPU readback and XFS enforcement.

## Canonical tenant ingress

Browser traffic must use only `sbx-<52 lowercase hex>.sandbox.synclave.net`. On the Cluster-2 box:

1. Reserve the active CVM MAC at `10.0.100.59` in `/etc/dstack-dnsmasq.conf` and validate with
   `dnsmasq --test` before restarting `dstack-dnsmasq.service`.
2. Route only the canonical SNI expression to that address in HAProxy:

   ```haproxy
   use_backend cvm_sandboxd if { req.ssl_sni -m reg -i ^sbx-[0-9a-f]{52}[.]sandbox[.]synclave[.]net$ }

   backend cvm_sandboxd
       mode tcp
       option tcp-check
       server cvm 10.0.100.59:443 check
   ```

3. Publish a DNS-only (not proxied) `A` record for `*.sandbox.synclave.net` to the box public IPv4
   address. Validate the candidate HAProxy file with `haproxy -c` before reloading it.

Back up both host configuration files before replacement. A future CVM replacement receives a new
MAC, so update the DHCP reservation while the replacement is stopped and prove the new backend
before cutover. Never broaden the SNI rule to all of `*.sandbox.synclave.net` without the canonical
host regex.

## Launch acceptance gates

Run destructive acceptance only when the authenticated capacity report is empty. The release is a
no-go unless it proves:

- each resource request one unit over its host ceiling returns `503` without a pending or committed
  leak;
- an actual runsc workload launches at 4,900 CPU millis, Docker records
  `NanoCpus=4900000000`, and a multi-worker burn is bounded near 4.9 CPUs;
- memory, guest-task PID (`RLIMIT_NPROC`/`EAGAIN`), XFS block, and XFS inode limits fail at their
  declared boundaries, while the separate host runsc-process PID cgroup remains at the reviewed
  derived `18*N+512` safety ceiling without recording a `pids.events:max` hit;
- explicit upsize succeeds, every downsize is rejected, and restart preserves the exact size;
- two untrusted tenants have distinct private `/29` networks, cannot reach each other, the host,
  Docker, the control network, or link-local metadata directly, while public egress and canonical
  public ingress still work;
- aggregate commitments can reach exactly the fixed host ceilings and the next minimum request is
  rejected without leakage;
- quote verification binds the sandbox identity and manifest to the allowlisted app and compose;
- cleanup returns sandbox count plus all committed and pending resources to zero.

The full release-candidate acceptance deliberately reaches 32 concurrent identities and therefore
consumes at least 32 permanent allocation-ledger entries even after cleanup. Run that destructive
count-saturation branch once per candidate, only after its preflight proves the required lifetime
headroom; do not use it as a routine health check. Subsequent operational smoke should use two live
probe identities, while the count branch remains covered by unit/concurrency recovery tests.

## Incident checks

If a request above one CPU fails with a Docker “only 1 CPUs available” error, stop launch. Inspect
the serial pre-launch log and the effective Docker service affinity; do not bypass the check with
raw CPU-period/quota flags. If the firewall watchdog latches a failure, sandboxd deliberately
quiesces tenants and remains fail-closed until a reviewed redeploy restores the exact rule set.
