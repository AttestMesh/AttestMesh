/** @jsxImportSource smithers-orchestrator */
// AttestMesh Path A deployment — durable, ordered routine (smithers).
//
// Standardizes the order-sensitive Path A sequence (dstack base KMS) with crash-recovery +
// resume:
//   preflight → infra → cluster → pathaUpgrade → webhook
//     → node { env-file → deploy → prime → upgrade → verify }
//
// Each step is a deterministic compute task that shells out to the logged bash routines in
// deploy/{onchain,webhook,node-pathA}.sh (which tee per-step output to deploy/logs/), so a
// failure is visible and the run resumes from the failed step. The node sub-steps are
// individually durable: a `verify` timeout (registration is a slow, sponsored on-chain UserOp)
// resumes from `verify` WITHOUT re-deploying the CVM — node-pathA.sh persists CVM_ID/app_id in
// a state file, so prime/upgrade are also re-entrant.
//
// Run from the repo root:
//   source deploy/env.sh                       # loads ~/.teesql creds into the environment
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx \
//     --input '{"node":"attestmesh-node-1"}'
//   # resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx --run-id <id> --resume true
//
// Idempotency: onchain.sh infra is a no-op if already deployed (FORCE=1 to redeploy);
// patha-upgrade re-cuts + redeploys the impl (cheap, harmless); webhook ensures its custom-domain
// route; the node step is gated on a `phala login` session and fails loudly with the unblock.
// cluster/memberImpl default to the live Base-mainnet Path A deployment — pass new values via
// --input for a fresh cluster (capture them from the cluster/pathaUpgrade step logs).
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  preflight: Step,
  infra: Step,
  cluster: Step,
  pathaUpgrade: Step,
  webhook: Step,
  nodeEnv: Step,
  nodeDeploy: Step,
  nodePrime: Step,
  nodeUpgrade: Step,
  nodeVerify: Step,
});

// Repo root, computed from this file's location (deploy/workflows/ → ../..), so the
// workflow runs regardless of the cwd smithers is invoked from.
const ROOT = new URL("../..", import.meta.url).pathname;

// Live Base-mainnet Path A deployment (override via --input for a fresh cluster).
const DEFAULT_CLUSTER = "0xA46273adC86c772C7D8daE896a5fbfdDA2B6ccFA";
const DEFAULT_MEMBER_IMPL = "0xBe579F0B8A971d0F8b083Eb3E8bB241985A3C5C4";

// Run a deploy routine with the env loaded; inherit stdio so the bash routines' own logging
// surfaces. execSync throws on non-zero exit → the step fails (and is resumable).
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
    cluster?: string;
    memberImpl?: string;
  };
  const node = input.node || "attestmesh-node-1";
  const cluster = input.cluster || DEFAULT_CLUSTER;
  const memberImpl = input.memberImpl || DEFAULT_MEMBER_IMPL;

  // Env the Path A node routine needs: CLUSTER (the diamond) + MEMBER_IMPL (its upgrade-target
  // impl) + COMPOSE (the node compose). ENV_FILE auto-defaults + is auto-built inside the script.
  const np = (sub: string) =>
    `CLUSTER=${cluster} MEMBER_IMPL=${memberImpl} COMPOSE=deploy/compose/node-1.yaml ` +
    `deploy/node-pathA.sh ${node} ${sub}`;

  return (
    <Workflow name="attestmesh-deploy-pathA">
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
        <Task id="pathaUpgrade" output={outputs.pathaUpgrade}>
          {() => run("pathaUpgrade", `deploy/onchain.sh patha-upgrade ${cluster}`)}
        </Task>
        <Task id="webhook" output={outputs.webhook}>
          {() => run("webhook", "deploy/webhook.sh deploy")}
        </Task>
        <Task id="nodeEnv" output={outputs.nodeEnv}>
          {() => run("nodeEnv", np("env-file"))}
        </Task>
        <Task id="nodeDeploy" output={outputs.nodeDeploy}>
          {() => run("nodeDeploy", np("deploy"))}
        </Task>
        <Task id="nodePrime" output={outputs.nodePrime}>
          {() => run("nodePrime", np("prime"))}
        </Task>
        <Task id="nodeUpgrade" output={outputs.nodeUpgrade}>
          {() => run("nodeUpgrade", np("upgrade"))}
        </Task>
        <Task id="nodeVerify" output={outputs.nodeVerify}>
          {() => run("nodeVerify", np("verify"))}
        </Task>
      </Sequence>
    </Workflow>
  );
});
