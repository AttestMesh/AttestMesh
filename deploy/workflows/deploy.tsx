/** @jsxImportSource smithers-orchestrator */
// AttestMesh full-system deployment — durable, ordered routine (smithers).
//
// Standardizes the order-sensitive bring-up of EVERYTHING (dstack base KMS, Path A)
// with crash-recovery + resume:
//   preflight → infra → cluster → pathaUpgrade → webhook → indexerEnsure
//     → node-1 { env-file → deploy → prime → upgrade → verify }
//     → node-2 { env-file → deploy → prime → upgrade → verify }
//     → meshVerify (both nodes phase=healthy: wg mesh + heartbeat convergence + CSK)
//
// The indexer is SHARED infrastructure — one instance serves every cluster (and the
// networks it watches), so indexerEnsure is a no-op whenever IndexerRegistry already
// has an endpoint; it only deploys+registers on a chain with no indexer yet.
//
// Each step is a deterministic compute task that shells out to the logged bash routines in
// deploy/{onchain,webhook,indexer,node-pathA}.sh (which tee per-step output to deploy/logs/),
// so a failure is visible and the run resumes from the failed step. The node sub-steps are
// individually durable: a `verify` timeout (registration is a slow, sponsored on-chain UserOp)
// resumes from `verify` WITHOUT re-deploying the CVM — node-pathA.sh persists CVM_ID/app_id in
// a state file, so prime/upgrade are also re-entrant. Same for indexer.sh (CVM_ID/APP_ID).
//
// Run from the repo root:
//   source deploy/env.sh                       # loads ~/.teesql creds into the environment
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx
//   # resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/deploy.tsx --run-id <id> --resume true
//
// Idempotency: onchain.sh infra is a no-op if already deployed (FORCE=1 to redeploy);
// patha-upgrade re-cuts + redeploys the impl (cheap, harmless); webhook ensures its
// custom-domain route; indexerEnsure no-ops when the shared indexer is already registered;
// the node steps are gated on a `phala login` session and fail
// loudly with the unblock. cluster/memberImpl default to the live Base-mainnet Path A
// deployment — pass new values via --input for a fresh cluster (capture them from the
// cluster/pathaUpgrade step logs).
//
// Day-2 routines (not part of this workflow; run directly):
//   deploy/node-pathA.sh <node> update       # roll new compose/env (allowlists hash first)
//   deploy/node-pathA.sh <node> restart      # re-pull :latest (image-only roll)
//   deploy/node-pathA.sh <node> mesh-verify  # poll healthz for phase=healthy
//   deploy/indexer.sh <name> update          # roll the indexer (no cluster gate)
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
  indexerEnsure: Step,
  node1Env: Step,
  node1Deploy: Step,
  node1Prime: Step,
  node1Upgrade: Step,
  node1Verify: Step,
  node2Env: Step,
  node2Deploy: Step,
  node2Prime: Step,
  node2Upgrade: Step,
  node2Verify: Step,
  meshVerify: Step,
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
    node1?: string;
    node2?: string;
    indexer?: string;
    cluster?: string;
    memberImpl?: string;
  };
  const node1 = input.node1 || "attestmesh-node-1";
  const node2 = input.node2 || "attestmesh-node-2";
  const indexer = input.indexer || "attestmesh-indexer-1";
  const cluster = input.cluster || DEFAULT_CLUSTER;
  const memberImpl = input.memberImpl || DEFAULT_MEMBER_IMPL;

  // Env the Path A node routine needs: CLUSTER (the diamond) + MEMBER_IMPL (its upgrade-target
  // impl) + COMPOSE (the shared node compose). ENV_FILE auto-defaults + is auto-built inside.
  const np = (node: string, sub: string) =>
    `CLUSTER=${cluster} MEMBER_IMPL=${memberImpl} COMPOSE=deploy/compose/node-1.yaml ` +
    `deploy/node-pathA.sh ${node} ${sub}`;
  const ix = (sub: string) => `deploy/indexer.sh ${indexer} ${sub}`;

  return (
    <Workflow name="attestmesh-deploy-full">
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
        <Task id="indexerEnsure" output={outputs.indexerEnsure}>
          {() => run("indexerEnsure", ix("ensure"))}
        </Task>
        <Task id="node1Env" output={outputs.node1Env}>
          {() => run("node1Env", np(node1, "env-file"))}
        </Task>
        <Task id="node1Deploy" output={outputs.node1Deploy}>
          {() => run("node1Deploy", np(node1, "deploy"))}
        </Task>
        <Task id="node1Prime" output={outputs.node1Prime}>
          {() => run("node1Prime", np(node1, "prime"))}
        </Task>
        <Task id="node1Upgrade" output={outputs.node1Upgrade}>
          {() => run("node1Upgrade", np(node1, "upgrade"))}
        </Task>
        <Task id="node1Verify" output={outputs.node1Verify}>
          {() => run("node1Verify", np(node1, "verify"))}
        </Task>
        <Task id="node2Env" output={outputs.node2Env}>
          {() => run("node2Env", np(node2, "env-file"))}
        </Task>
        <Task id="node2Deploy" output={outputs.node2Deploy}>
          {() => run("node2Deploy", np(node2, "deploy"))}
        </Task>
        <Task id="node2Prime" output={outputs.node2Prime}>
          {() => run("node2Prime", np(node2, "prime"))}
        </Task>
        <Task id="node2Upgrade" output={outputs.node2Upgrade}>
          {() => run("node2Upgrade", np(node2, "upgrade"))}
        </Task>
        <Task id="node2Verify" output={outputs.node2Verify}>
          {() => run("node2Verify", np(node2, "verify"))}
        </Task>
        <Task id="meshVerify" output={outputs.meshVerify}>
          {() =>
            run(
              "meshVerify",
              `${np(node1, "mesh-verify")} && ${np(node2, "mesh-verify")}`,
            )
          }
        </Task>
      </Sequence>
    </Workflow>
  );
});
