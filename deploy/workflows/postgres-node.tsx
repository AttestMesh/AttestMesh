/** @jsxImportSource smithers-orchestrator */
// Standalone PostgreSQL AttestMesh node bring-up — durable, ordered (smithers).
//
//   deploy -> prime -> bind -> verify -> meshEndpoint -> agent -> metrics -> isolation
//
// This workflow deploys the no-Tailscale Postgres node into the existing Matrix
// cluster. Matrix must already be deployed and registered: postgres-node.sh reads
// deploy/logs/matrix-node-matrix-node.state to inherit CLUSTER, MEMBER_IMPL, and
// the Matrix mesh IP. Secrets flow through inherited env and are sealed by dstack;
// they are never put on the Smithers command line.
//
// Run from the repo root:
//   source deploy/env.sh
//   BOTPASSWORD=... bunx smithers-orchestrator up deploy/workflows/postgres-node.tsx \
//     --input '{"node":"postgres-node"}'
//
// Resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/postgres-node.tsx --run-id <id> --resume true
//
// Day-2 roll, preserving app_id/disk/membership:
//   deploy/postgres-node.sh postgres-node update
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  deploy: Step,
  prime: Step,
  bind: Step,
  verify: Step,
  meshEndpoint: Step,
  agent: Step,
  metrics: Step,
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
  const input = (ctx.input || {}) as {
    node?: string;
    matrixTailnetFqdn?: string;
    matrixVerifyUser?: string;
  };
  const node = input.node || "postgres-node";

  if (input.matrixTailnetFqdn) process.env.MATRIX_TAILNET_FQDN = input.matrixTailnetFqdn;
  if (input.matrixVerifyUser) process.env.MATRIX_VERIFY_USER = input.matrixVerifyUser;

  const pn = (sub: string) => `deploy/postgres-node.sh ${node} ${sub}`;

  return (
    <Workflow name="attestmesh-postgres-node">
      <Sequence>
        <Task id="deploy" output={outputs.deploy}>
          {() => run("deploy", pn("deploy"))}
        </Task>
        <Task id="prime" output={outputs.prime}>
          {() => run("prime", pn("prime"))}
        </Task>
        <Task id="bind" output={outputs.bind}>
          {() => run("bind", pn("bind"))}
        </Task>
        <Task id="verify" output={outputs.verify}>
          {() => run("verify", pn("verify"))}
        </Task>
        <Task id="meshEndpoint" output={outputs.meshEndpoint}>
          {() => run("meshEndpoint", pn("verify-mesh-endpoint"))}
        </Task>
        <Task id="agent" output={outputs.agent}>
          {() => run("agent", pn("verify-agent"))}
        </Task>
        <Task id="metrics" output={outputs.metrics}>
          {() => run("metrics", pn("verify-metrics"))}
        </Task>
        <Task id="isolation" output={outputs.isolation}>
          {() => run("isolation", pn("verify-isolation"))}
        </Task>
      </Sequence>
    </Workflow>
  );
});
