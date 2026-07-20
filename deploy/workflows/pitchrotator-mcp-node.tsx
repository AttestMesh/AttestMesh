/** @jsxImportSource smithers-orchestrator */
// PitchRotator MCP dedicated-network bring-up — durable and ordered (Smithers).
//
//   deploy -> cluster -> Path A -> prime -> bind -> start -> verify -> MCP smoke
//
// The shell driver owns every deployment and verification primitive. Smithers
// only persists their ordering and exit status so a failed run can be resumed
// without repeating successful on-chain/CVM work. Runtime secrets are inherited
// from the operator environment and sealed by the driver; neither workflow input
// nor the generated command line accepts secret values.
//
// Run from the repository root:
//   source deploy/env.sh
//   bunx smithers-orchestrator up deploy/workflows/pitchrotator-mcp-node.tsx \
//     --input '{"node":"pitchrotator-mcp-node"}'
//
// Resume after correcting a failed step:
//   bunx smithers-orchestrator up deploy/workflows/pitchrotator-mcp-node.tsx \
//     --run-id <id> --resume true
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execFileSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });
const Input = z
  .object({
    node: z
      .string()
      .regex(/^[a-z0-9][a-z0-9-]{0,62}$/, "node must be a DNS-style label")
      .optional(),
  })
  .strict();

const { Workflow, smithers, outputs } = createSmithers({
  deploy: Step,
  cluster: Step,
  patha: Step,
  prime: Step,
  bind: Step,
  start: Step,
  verify: Step,
  mcp: Step,
});

const ROOT = new URL("../..", import.meta.url).pathname;

function run(step: string, node: string, action: string): { ok: boolean; step: string } {
  execFileSync("/bin/bash", ["-c", 'source deploy/env.sh >/dev/null && exec "$@"', "smithers", `deploy/pitchrotator-mcp-node.sh`, node, action], {
    cwd: ROOT,
    stdio: "inherit",
    env: process.env,
  });
  return { ok: true, step };
}

export default smithers((ctx) => {
  const input = Input.parse(ctx.input || {});
  const node = input.node || "pitchrotator-mcp-node";

  return (
    <Workflow name="attestmesh-pitchrotator-mcp-node">
      <Sequence>
        <Task id="deploy" output={outputs.deploy}>
          {() => run("deploy", node, "deploy")}
        </Task>
        <Task id="cluster" output={outputs.cluster}>
          {() => run("cluster", node, "cluster")}
        </Task>
        <Task id="patha" output={outputs.patha}>
          {() => run("patha", node, "patha")}
        </Task>
        <Task id="prime" output={outputs.prime}>
          {() => run("prime", node, "prime")}
        </Task>
        <Task id="bind" output={outputs.bind}>
          {() => run("bind", node, "bind")}
        </Task>
        <Task id="start" output={outputs.start}>
          {() => run("start", node, "start")}
        </Task>
        <Task id="verify" output={outputs.verify}>
          {() => run("verify", node, "verify")}
        </Task>
        <Task id="mcp" output={outputs.mcp}>
          {() => run("mcp", node, "verify-mcp")}
        </Task>
      </Sequence>
    </Workflow>
  );
});
