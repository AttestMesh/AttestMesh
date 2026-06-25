/** @jsxImportSource smithers-orchestrator */
// Private Matrix node bring-up on the SELF-HOSTED on-chain dstack box — durable, ordered (smithers).
//
//   deploy → cluster → patha → prime → bind → verify → agent → client → isolation
//
// Deploys a Matrix homeserver as a full AttestMesh node "this way": PRIVATE (tailnet-only, gateway-off),
// confidential (HTTPS terminated INSIDE the CVM via tailscale serve), fast (bridge networking → a DIRECT
// Tailscale path), and HOST-ISOLATED (the host opens nothing toward the CVM). The method, every invariant,
// and the connect steps are documented in deploy/matrix-node-deploy.md; the full debugging history is in
// deploy/matrix-node-access-journal.md.
//
// Each step shells out to deploy/matrix-node.sh (which tees per-step output to deploy/logs/ and persists
// app_id/cluster/impl/VM_ID/IAPW in a state file), so a failure is visible and the run RESUMES from the
// failed step without redoing on-chain work. The defaults that make it "this way" live in matrix-node.sh +
// the compose: BOX_NET_MODE=bridge, BOX_GATEWAY_ENABLED=false, no host ports, public_baseurl sentinel +
// nginx $host rewrite of the login well_known.
//
//   • deploy  seals the matrix-admin-agent + its egress firewall into the CVM, then WAITS for synapse to be
//             live BEFORE cluster/bind register the node — the first member of a fresh cluster is the
//             immutable CSK originator and the contracts have no removeMember, so a broken node must never
//             be the one that registers.
//   • agent   confirms the agent is healthy AND its egress is LOCKED (the CVM is a TEE → it self-verifies).
//   • client  runs the EXACT Element path — login → follow the login well_known → initial sync 200 —
//             catching a bad public_baseurl that would hang clients on "Syncing" even on a healthy server.
//   • isolation asserts, FROM THE BOX, that the CVM's private ports refuse (the platform invariant).
//
// Prereqs: bridge networking enabled in the box vmm.toml (one-time; journal W1–W4); MCP server patched
// (docker-login pre_launch + APP_ID inject); contracts/lib/ populated; SSH+sudo to the box; box deployer
// key at /root/.attestmesh/base-deployer.json.
//
// Run from the repo root:
//   source deploy/env.sh
//   # agent secrets/config are read from the env (inherited by the child), like TS_AUTHKEY. INITIAL_ADMIN
//   # is needed for the `client` step (its localpart + the IAPW from state drive the login check).
//   TS_AUTHKEY=tskey-… BOT_PASSWORD=… MATRIX_ADMIN_MXIDS=@you:<server> INITIAL_ADMIN=@you:<server> \
//     LLM_BASE_URL=… LLM_MODEL=z-ai/glm-5.2 LLM_API_KEY=… [MATRIX_ADMIN_SENDERS=…] \
//     bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx \
//     --input '{"node":"matrix-node","meshCidrIp":"168951808"}'
//   # resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/matrix-node.tsx --run-id <id> --resume true
//
// Day-2 (not in this workflow; run directly — REUSES the app_id, keeps membership + CSK originator, and
// self-verifies agent+client+isolation after the roll):
//   TS_AUTHKEY=… INITIAL_ADMIN=@you:<server> deploy/matrix-node.sh <node> update
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
  client: Step,
  isolation: Step,
  backup: Step,
});

// Repo root, from this file's location (deploy/workflows/ → ../..), so it runs from any cwd.
const ROOT = new URL("../..", import.meta.url).pathname;

// Run a routine with deploy/env.sh loaded; inherit stdio so matrix-node.sh's own logging surfaces.
// Secrets (TS_AUTHKEY/BOT_PASSWORD/LLM_*/INITIAL_ADMIN/…) flow via the inherited CHILD ENV, never the CLI.
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

  // TS_AUTHKEY is a secret → pass via the inherited child env, not the command line. Missing-secret is
  // enforced at RUN time by matrix-node.sh (`${TS_AUTHKEY:?…}`), not here, so `graph` can render.
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
        <Task id="client" output={outputs.client}>
          {() => run("client", mn("verify-client"))}
        </Task>
        <Task id="isolation" output={outputs.isolation}>
          {() => run("isolation", mn("verify-isolation"))}
        </Task>
        {process.env.BACKUP_ENABLED === "true" && (
          <Task id="backup" output={outputs.backup}>
            {() => run("backup", mn("backup-status"))}
          </Task>
        )}
      </Sequence>
    </Workflow>
  );
});
