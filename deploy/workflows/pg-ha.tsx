/** @jsxImportSource smithers-orchestrator */
// Postgres HA cluster bring-up (Patroni + etcd + HAProxy over the AttestMesh wg mesh) —
// durable, ordered (smithers). See docs/specs/pg-ha.md.
//
//   ownership -> deployAll -> primeAll -> bindAll -> verifyAll -> runtime -> backup -> isolation
//
// deployAll internally runs register-all (N DstackApp contracts) -> compute-peers
// (off-chain mesh-IP precompute + collision check) -> create-all (N CVMs with the
// sealed PGHA_PEERS static-bootstrap list). The node COUNT is dynamic but the graph
// is static: all per-node looping lives in the pg-ha-node.sh `-all` actions, which
// are re-entrant against per-node state files — so a failed step resumes without
// redoing on-chain work.
//
// This workflow targets a new Safe-owned, chain-only mesh. It does not depend on
// Matrix, Tailscale, or an SSH member. Client R2 credentials and the path to the
// direct upstream bucket-scoped TOML flow from ~/.attestmesh/pg-ha.env and are
// sealed by dstack; they are never put on the Smithers command line.
//
// Run from the repo root:
//   source deploy/env.sh
//   bunx smithers-orchestrator up deploy/workflows/pg-ha.tsx \
//     --input '{"name":"pg-ha","count":3}'
//
// Resume after fixing a failed step:
//   bunx smithers-orchestrator up deploy/workflows/pg-ha.tsx --run-id <id> --resume true
//
// Day-2 serialized roll (disk-preserving, verify-ha gated between nodes):
//   deploy/pg-ha-node.sh pg-ha update-all
// Failover drill (StopVm the leader, prove promote + write recovery + rejoin):
//   deploy/pg-ha-node.sh pg-ha verify-failover
import { createSmithers, Sequence, Task } from "smithers-orchestrator";
import { z } from "zod";
import { execSync } from "node:child_process";

const Step = z.object({ ok: z.boolean(), step: z.string() });

const { Workflow, smithers, outputs } = createSmithers({
  ownership: Step,
  deployAll: Step,
  primeAll: Step,
  bindAll: Step,
  verifyAll: Step,
  ha: Step,
  backup: Step,
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
    name?: string;
    count?: number;
  };
  const name = input.name || "pg-ha";

  if (input.count) process.env.PGHA_COUNT = String(input.count);
  const pgha = (sub: string) => `deploy/pg-ha-node.sh ${name} ${sub}`;

  return (
    <Workflow name="attestmesh-pg-ha">
      <Sequence>
        <Task id="ownership" output={outputs.ownership}>
          {() => run("ownership", pgha("verify-ownership"))}
        </Task>
        <Task id="deployAll" output={outputs.deployAll}>
          {() => run("deployAll", pgha("deploy-all"))}
        </Task>
        <Task id="primeAll" output={outputs.primeAll}>
          {() => run("primeAll", pgha("prime-all"))}
        </Task>
        <Task id="bindAll" output={outputs.bindAll}>
          {() => run("bindAll", pgha("bind-all"))}
        </Task>
        <Task id="verifyAll" output={outputs.verifyAll}>
          {() => run("verifyAll", pgha("verify-all"))}
        </Task>
        <Task id="ha" output={outputs.ha}>
          {() => run("runtime", pgha("verify-runtime"))}
        </Task>
        <Task id="backup" output={outputs.backup}>
          {() => run("backup", pgha("verify-backup"))}
        </Task>
        <Task id="isolation" output={outputs.isolation}>
          {() => run("isolation", pgha("verify-isolation-all"))}
        </Task>
      </Sequence>
    </Workflow>
  );
});
