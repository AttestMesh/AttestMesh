/** @jsxImportSource smithers-orchestrator */
// R2-host AttestMesh node bring-up — durable, ordered (smithers).
//
//   deploy -> prime -> bind -> verify -> health -> s3 -> r2 -> isolation
//
// This workflow deploys the encrypting S3 gateway node (docs/specs/r2-host-node.md)
// into the existing Matrix cluster (C3). Matrix must already be deployed and
// registered: r2-host-node.sh reads deploy/logs/matrix-node-matrix-node.state to
// inherit CLUSTER and MEMBER_IMPL. Pre-reqs on the operator host:
//   - ~/.attestmesh/r2-host-r2.toml (R2 endpoint/bucket/region/access_key_id/secret_access_key)
//   - ~/.teesql/ghcr-pull.toml (registry pull creds)
//   - ~/.attestmesh/r2-host.env is generated on first run (mesh-client S3 creds)
// Secrets flow through inherited env / stdin pipes and are sealed by dstack; they
// are never put on the Smithers command line.
//
// Run from the repo root:
//   source deploy/env.sh
//   bunx smithers-orchestrator up deploy/workflows/r2-host-node.tsx \
//     --input '{"node":"r2-host-node"}'
//
// Resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/r2-host-node.tsx --run-id <id> --resume true
//
// Day-2 roll, preserving app_id/membership (disk is cache-only; BOX_FRESH_DISK=1
// is always safe and doubles as the key-recovery drill):
//   deploy/r2-host-node.sh r2-host-node update
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  deploy: Step,
  prime: Step,
  bind: Step,
  verify: Step,
  health: Step,
  s3: Step,
  r2: Step,
  isolation: Step,
});

const ROOT = new URL("../..", import.meta.url).pathname;

function run(step: string, routine: string): { ok: boolean; step: string } {
  execSync(`cd "${ROOT}" && source deploy/env.sh >/dev/null && ${routine}`, {
    stdio: "inherit",
    shell: "/bin/bash",
  });
  return { ok: true, step };
}

export default smithers((ctx) => {
  const input = (ctx.input || {}) as { node?: string };
  const node = input.node || "r2-host-node";

  const rn = (sub: string) => `deploy/r2-host-node.sh ${node} ${sub}`;

  return (
    <Workflow name="attestmesh-r2-host-node">
      <Sequence>
        <Task id="deploy" output={outputs.deploy}>
          {() => run("deploy", rn("deploy"))}
        </Task>
        <Task id="prime" output={outputs.prime}>
          {() => run("prime", rn("prime"))}
        </Task>
        <Task id="bind" output={outputs.bind}>
          {() => run("bind", rn("bind"))}
        </Task>
        <Task id="verify" output={outputs.verify}>
          {() => run("verify", rn("verify"))}
        </Task>
        <Task id="health" output={outputs.health}>
          {() => run("health", rn("verify-health"))}
        </Task>
        <Task id="s3" output={outputs.s3}>
          {() => run("s3", rn("verify-s3"))}
        </Task>
        <Task id="r2" output={outputs.r2}>
          {() => run("r2", rn("verify-r2"))}
        </Task>
        <Task id="isolation" output={outputs.isolation}>
          {() => run("isolation", rn("verify-isolation"))}
        </Task>
      </Sequence>
    </Workflow>
  );
});
