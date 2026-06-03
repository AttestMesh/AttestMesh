/** @jsxImportSource smithers-orchestrator */
// AttestMesh deployment — durable, ordered routine (smithers).
//
// Standardizes the order-sensitive deploy sequence with crash-recovery + resume:
//   preflight → infra → cluster → node (predict → seed app_id → image → CVM → verify).
// Each step is a deterministic *compute* task that shells out to the logged bash
// routines in deploy/{onchain,node}.sh (which tee per-step output to deploy/logs/),
// so failures are visible and the run is resumable from the failed step.
//
// Run from the repo root:
//   source deploy/env.sh            # loads ~/.teesql creds into the environment
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx --input '{"node":"node-1"}'
//   # resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx --run-id <id> --resume true
//
// Idempotency: onchain.sh infra is a no-op if already deployed (FORCE=1 to redeploy);
// the node step is gated on PHALA_CLOUD_API_KEY and fails loudly with the unblock if absent.
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  preflight: Step,
  infra: Step,
  cluster: Step,
  node: Step,
});

// Repo root, computed from this file's location (deploy/workflows/ → ../..), so the
// workflow runs regardless of the cwd smithers is invoked from.
const ROOT = new URL("../..", import.meta.url).pathname;

// Run a deploy routine with the env loaded; inherit stdio so the bash routines' own
// logging surfaces. execSync throws on non-zero exit → the step fails (and is resumable).
function run(step: string, routine: string): { ok: boolean; step: string } {
  execSync(`cd "${ROOT}" && source deploy/env.sh >/dev/null && ${routine}`, {
    stdio: "inherit",
    shell: "/bin/bash",
  });
  return { ok: true, step };
}

export default smithers((ctx) => {
  const node = (ctx.input && ctx.input.node) || "node-1";
  return (
    <Workflow name="attestmesh-deploy">
      <Sequence>
        <Task id="preflight" output={outputs.preflight}>
          {() => run("preflight", "deploy/onchain.sh preflight")}
        </Task>
        <Task id="infra" output={outputs.infra}>
          {() => run("infra", "deploy/onchain.sh infra")}
        </Task>
        <Task id="cluster" output={outputs.cluster}>
          {() => run("cluster", "deploy/onchain.sh cluster attestmesh-1")}
        </Task>
        <Task id="node" output={outputs.node}>
          {() => run("node", `deploy/node.sh ${node} all`)}
        </Task>
      </Sequence>
    </Workflow>
  );
});
