/** @jsxImportSource smithers-orchestrator */
// Recover a provisioned r2-host after its real dstack KMS signer was omitted
// from the cluster allowlist:
// proof -> allow root -> simulate -> register -> clean roll -> verify -> health
// -> optional mesh data-plane check -> isolation.
//
// The proof step independently recovers the KMS signer and refuses to continue
// unless it equals EXPECTED_KMS_ROOT. Registration is always eth_call-simulated
// before the direct transaction. Every step is idempotent and resumable.
//
// Run:
//   source deploy/env.sh
//   REGISTRATION_PAYLOAD_FILE=/tmp/r2-register.json \
//     bunx smithers-orchestrator up deploy/workflows/r2-host-recovery.tsx \
//     --input '{"node":"csk-object-store-solo","verifyStorage":false}'
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execFileSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });
const { Workflow, smithers, outputs } = createSmithers({
  capture: Step,
  proof: Step,
  allowRoot: Step,
  simulate: Step,
  register: Step,
  cleanRoll: Step,
  verify: Step,
  health: Step,
  storage: Step,
  isolation: Step,
});

const ROOT = new URL("../..", import.meta.url).pathname;

function run(step: string, node: string, action: string): { ok: boolean; step: string } {
  execFileSync("bash", ["-lc", `source deploy/env.sh >/dev/null && exec deploy/r2-host-recovery.sh "$1" "$2"`, "smithers", node, action], {
    cwd: ROOT,
    stdio: "inherit",
    env: process.env,
  });
  return { ok: true, step };
}

export default smithers((ctx) => {
  const input = (ctx.input || {}) as {
    node?: string;
    verifyStorage?: boolean;
    payloadFile?: string;
    expectedKmsRoot?: string;
  };
  const node = input.node || "csk-object-store-solo";
  const verifyStorage = input.verifyStorage ?? false;
  if (input.payloadFile) process.env.REGISTRATION_PAYLOAD_FILE = input.payloadFile;
  if (input.expectedKmsRoot) process.env.EXPECTED_KMS_ROOT = input.expectedKmsRoot;

  return (
    <Workflow name="attestmesh-r2-host-recovery">
      <Sequence>
        <Task id="capture" output={outputs.capture}>{() => run("capture", node, "capture-proof")}</Task>
        <Task id="proof" output={outputs.proof}>{() => run("proof", node, "verify-proof")}</Task>
        <Task id="allow-root" output={outputs.allowRoot}>{() => run("allow-root", node, "allow-root")}</Task>
        <Task id="simulate" output={outputs.simulate}>{() => run("simulate", node, "simulate-register")}</Task>
        <Task id="register" output={outputs.register}>{() => run("register", node, "submit-register")}</Task>
        <Task id="clean-roll" output={outputs.cleanRoll}>{() => run("clean-roll", node, "clean-roll")}</Task>
        <Task id="verify" output={outputs.verify}>{() => run("verify", node, "verify")}</Task>
        <Task id="health" output={outputs.health}>{() => run("health", node, "health")}</Task>
        {verifyStorage && (
          <Task id="storage" output={outputs.storage}>{() => run("storage", node, "storage")}</Task>
        )}
        <Task id="isolation" output={outputs.isolation}>{() => run("isolation", node, "isolation")}</Task>
      </Sequence>
    </Workflow>
  );
});
