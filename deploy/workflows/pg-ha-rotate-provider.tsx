/** @jsxImportSource smithers-orchestrator */
// Fail-closed cutover of an already-provisioned PG-HA replica to Phala.
//
// Smithers owns ordering and durable resume. The shell driver owns live invariants and refuses
// every mutation unless the candidate is exact-sized/locked-down, both provider-specific compose
// hashes are Safe-admitted, backups are fresh, and the candidate is streaming. The retired VM is
// stopped only after a survivor proves the final peer map and the etcd voter removal.
//
// The candidate deployment itself and Safe signatures remain explicit approval boundaries. Supply
// their resulting CVM ID and compose hashes as inputs; this routine verifies them on-chain before
// cutover. Secrets stay in the normal sealed env/config files and never enter Smithers input.
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { execFileSync } from "node:child_process";
import { z } from "zod";

const NodeName = z.string().regex(/^pg[1-9][0-9]*$/);
const Hash = z.string().regex(/^0x[a-fA-F0-9]{64}$/);
const Input = z.object({
  name: z.string().regex(/^[a-z0-9-]+$/),
  candidate: NodeName,
  retired: NodeName,
  survivorBoxNodes: z.array(NodeName).min(1).max(2),
  evidenceNode: NodeName,
  finalPeers: z.string().regex(/^pg[1-9][0-9]*=10\.[0-9]+\.[0-9]+\.[0-9]+(,pg[1-9][0-9]*=10\.[0-9]+\.[0-9]+\.[0-9]+)*$/),
  phalaCvmId: z.string().uuid(),
  boxComposeHash: Hash,
  phalaComposeHash: Hash,
  confirmation: z.string(),
  temporaryEnvFile: z.string().nullish(),
}).passthrough();

const Step = z.object({ ok: z.literal("true"), step: z.string(), at: z.string() });
const { Workflow, smithers, outputs } = createSmithers({
  input: Input,
  preflight: Step,
  candidate: Step,
  backup: Step,
  converge: Step,
  retire: Step,
  final: Step,
});

const ROOT = new URL("../..", import.meta.url).pathname;

function environment(input: z.infer<typeof Input>): NodeJS.ProcessEnv {
  return {
    ...process.env,
    PGHA_ROTATION_CANDIDATE: input.candidate,
    PGHA_ROTATION_RETIRED: input.retired,
    PGHA_ROTATION_FINAL_PEERS: input.finalPeers,
    PGHA_ROTATION_PHALA_CVM_ID: input.phalaCvmId,
    PGHA_ROTATION_EVIDENCE_NODE: input.evidenceNode,
    PGHA_ROTATION_BOX_COMPOSE_HASH: input.boxComposeHash,
    PGHA_ROTATION_PHALA_COMPOSE_HASH: input.phalaComposeHash,
    PGHA_ROTATION_ENV_FILE: input.temporaryEnvFile || "",
    PGHA_PEERS_OVERRIDE: input.finalPeers,
    PGHA_ALLOW_UNVERIFIED_ROLL: "1",
  };
}

function pgha(input: z.infer<typeof Input>, action: string, node?: string): void {
  execFileSync(
    "/bin/bash",
    ["-lc", 'source deploy/env.sh >/dev/null && exec deploy/pg-ha-node.sh "$1" "$2" "$3"', "smithers", input.name, action, node || ""],
    { cwd: ROOT, env: environment(input), stdio: "inherit" },
  );
}

function done(step: string) {
  return { ok: "true" as const, step, at: new Date().toISOString() };
}

export default smithers((ctx) => {
  const input = Input.parse(ctx.input || {});
  if (input.candidate === input.retired) throw new Error("candidate and retired node must differ");
  if (!input.survivorBoxNodes.includes(input.evidenceNode)) {
    throw new Error("evidenceNode must be one of survivorBoxNodes");
  }
  if (input.confirmation !== `retire:${input.retired}:for:${input.candidate}`) {
    throw new Error(`confirmation must equal retire:${input.retired}:for:${input.candidate}`);
  }

  return (
    <Workflow name="attestmesh-pg-ha-provider-rotation">
      <Sequence>
        <Task id="rotationPreflight" output={outputs.preflight}>
          {() => { pgha(input, "rotation-preflight"); return done("preflight"); }}
        </Task>
        <Task id="freshBackups" output={outputs.backup}>
          {() => { pgha(input, "rotation-backup-gate"); return done("fresh-backups"); }}
        </Task>
        <Task id="convergeSurvivors" output={outputs.converge}>
          {() => {
            for (const node of input.survivorBoxNodes) {
              pgha(input, "update-only", node);
              pgha(input, "rotation-survivor-gate", node);
            }
            return done("survivors-converged");
          }}
        </Task>
        <Task id="candidateStreaming" output={outputs.candidate}>
          {() => { pgha(input, "rotation-candidate-gate"); return done("candidate-streaming"); }}
        </Task>
        <Task id="retireOldSecondary" output={outputs.retire}>
          {() => { pgha(input, "rotation-retire"); return done("retired-secondary-stopped"); }}
        </Task>
        <Task id="finalEvidence" output={outputs.final}>
          {() => { pgha(input, "rotation-final"); return done("final-evidence"); }}
        </Task>
      </Sequence>
    </Workflow>
  );
});
