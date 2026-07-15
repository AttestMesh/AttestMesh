# sandboxd production node

This release has one exact machine profile: **8 vCPU, 16,384 MiB RAM, and 300 GiB disk**.
The measured tenant budget remains independently fixed at 5,000 CPU millis, 10,240 MiB memory,
16,384 PIDs, 237,568 MiB of quota-backed disk, and 50 concurrent sandboxes, all at 1.0 overcommit.
Hardware discovery never changes those admission limits.

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

Pre-launch stops managed workloads before restarting Docker, restores the host firewall before the
daemon resumes durable rows, and fails closed unless all of these are true:

- the guest exposes exactly eight processors;
- the measured `/etc/systemd/system/docker.service.d/override.conf` shadows DStack 0.5.11's
  one-CPU vendor affinity;
- dockerd's effective `Cpus_allowed_list` is `0-7` and `docker info .NCPU` is exactly `8`;
- Docker uses the managed 28 GiB ZFS data root and the pinned runsc runtime;
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
- memory, PID, XFS block, and XFS inode limits fail at their declared boundaries;
- explicit upsize succeeds, every downsize is rejected, and restart preserves the exact size;
- two untrusted tenants have distinct private `/29` networks, cannot reach each other, the host,
  Docker, the control network, or link-local metadata directly, while public egress and canonical
  public ingress still work;
- aggregate commitments can reach exactly the fixed host ceilings and the next minimum request is
  rejected without leakage;
- quote verification binds the sandbox identity and manifest to the allowlisted app and compose;
- cleanup returns sandbox count plus all committed and pending resources to zero.

Do not consume 50 permanent allocation-ledger entries merely to retest the concurrent-count branch;
that path is covered by unit/concurrency recovery tests. Use two live probe identities for the
aggregate saturation test.

## Incident checks

If a request above one CPU fails with a Docker “only 1 CPUs available” error, stop launch. Inspect
the serial pre-launch log and the effective Docker service affinity; do not bypass the check with
raw CPU-period/quota flags. If the firewall watchdog latches a failure, sandboxd deliberately
quiesces tenants and remains fail-closed until a reviewed redeploy restores the exact rule set.
