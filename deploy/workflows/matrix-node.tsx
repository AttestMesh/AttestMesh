/** @jsxImportSource smithers-orchestrator */
// Matrix-node bring-up on the SELF-HOSTED on-chain dstack box — durable, ordered (smithers).
//
//   deploy → cluster → patha → prime → bind → verify → agent
//
// `deploy` seals the matrix-admin-agent + its egress firewall into the CVM (the compose and
// matrix-node.sh thread the sealed agent env); the `agent` step confirms the agent is healthy AND
// its egress is LOCKED via /healthz (the CVM is a TEE, so the agent self-verifies egress). See
// docs/specs/matrix-admin-agent.md.
//
// Each step shells out to deploy/matrix-node.sh (which tees per-step output to deploy/logs/ and
// persists app_id/cluster/impl in a state file), so a failure is visible and the run RESUMES from
// the failed step without redoing on-chain work. `deploy` also waits for synapse to be live BEFORE
// the cluster/bind steps register the node — the first member of a fresh cluster is the immutable
// CSK originator and the deployed contracts have no removeMember, so a broken node must never be the
// one that registers (see deploy/matrix-node-steps-log.md).
//
// Prereqs: MCP server patched (docker-login pre_launch + APP_ID inject); contracts/lib/ populated;
// deploy/compose/matrix-node.yaml carries all fixes (no host port >20000; APP_ID env; keygen module
// form; media_store_path). SSH+sudo to the box; box deployer key at /root/.attestmesh/base-deployer.json.
//
// Run from the repo root:
//   source deploy/env.sh
//   # agent secrets/config are read from the env (inherited by the child), like TS_AUTHKEY:
//   TS_AUTHKEY=tskey-… BOT_PASSWORD=… MATRIX_ADMIN_MXIDS=@you:<server> \
//     LLM_BASE_URL=… LLM_MODEL=… LLM_API_KEY=… [MATRIX_ADMIN_SENDERS=… INITIAL_ADMIN=…] \
//     bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx \
//     --input '{"node":"matrix-node","meshCidrIp":"168951808"}'
//   # resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx --run-id <id> --resume true
//
// Day-2 (not in this workflow; run directly — REUSES the app_id, keeps membership + CSK originator):
//   TS_AUTHKEY=… deploy/matrix-node.sh <node> update    # roll new compose/env in place
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  deploy: Step,
  cluster: Step,
  patha: Step,
  prime: Step,
  bind: Step,
  verify: Step,
  agent: Step,
});

// Repo root, from this file's location (deploy/workflows/ → ../..), so it runs from any cwd.
const ROOT = new URL("../..", import.meta.url).pathname;

// Run a routine with deploy/env.sh loaded; inherit stdio so matrix-node.sh's own logging surfaces.
// TS_AUTHKEY/MESH_CIDR_IP are passed via the CHILD ENV (set below), never on the command line.
function run(step: string, routine: string): { ok: boolean; step: string } {
  execSync(`cd "${ROOT}" && source deploy/env.sh >/dev/null && ${routine}`, {
    stdio: "inherit",
    shell: "/bin/bash",
  });
  return { ok: true, step };
}

export default smithers((ctx) => {
  const input = (ctx.input || {}) as { node?: string; tsAuthKey?: string; meshCidrIp?: string };
  const node = input.node || "matrix-node";

  // TS_AUTHKEY is a secret → pass via the inherited child env, not the command line. Missing-secret
  // is enforced at RUN time by matrix-node.sh (`${TS_AUTHKEY:?…}`), not here, so `graph` can render.
  const tsKey = input.tsAuthKey || process.env.TS_AUTHKEY || "";
  if (tsKey) process.env.TS_AUTHKEY = tsKey;
  if (input.meshCidrIp) process.env.MESH_CIDR_IP = input.meshCidrIp;

  const mn = (sub: string) => `deploy/matrix-node.sh ${node} ${sub}`;

  return (
    <Workflow name="attestmesh-matrix-node">
      <Sequence>
        <Task id="deploy" output={outputs.deploy}>
          {() => run("deploy", mn("deploy"))}
        </Task>
        <Task id="cluster" output={outputs.cluster}>
          {() => run("cluster", mn("cluster"))}
        </Task>
        <Task id="patha" output={outputs.patha}>
          {() => run("patha", mn("patha"))}
        </Task>
        <Task id="prime" output={outputs.prime}>
          {() => run("prime", mn("prime"))}
        </Task>
        <Task id="bind" output={outputs.bind}>
          {() => run("bind", mn("bind"))}
        </Task>
        <Task id="verify" output={outputs.verify}>
          {() => run("verify", mn("verify"))}
        </Task>
        <Task id="agent" output={outputs.agent}>
          {() => run("agent", mn("verify-agent"))}
        </Task>
      </Sequence>
    </Workflow>
  );
});
