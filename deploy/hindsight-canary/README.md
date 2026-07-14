# Isolated Hindsight canary

This directory implements the cost-gated evaluation. It never talks to the
production Hindsight database or its contaminated operation queue.

Hard prerequisites:

- a dedicated Redpill virtual key (not the normal key) with a **$2 total hard
  budget** plus conservative RPM/TPM limits;
- `CANARY_HARD_BUDGET_USD` set to a value no greater than `2`;
- explicit per-million-token prices for the local ledger;
- fresh local databases created by `compose.yaml`.

Use Python 3.11+ with the small runtime dependency set in
`requirements.txt` (`httpx` and `psycopg[binary]`).

Copy `env.example` to `~/.attestmesh/hindsight-canary.env`, set mode `0600`,
and source it before every command. The harness compares the canary key with
`~/.attestmesh/redpill-key` and aborts if they match.

The two Hindsight services use separate databases so their background workers
cannot claim one another's jobs. Both use the existing local 384-dimensional
embedder, auto-consolidation, low reasoning, no returned reasoning, non-strict
structured output, and an 8,192 completion ceiling. Their LLM calls pass through
`budget_proxy.py`, which shares the card/question ledger, enforces the local
rolling RPM/TPM limits, serializes provider calls, and fails closed after an
ambiguous proxy crash. Retains can still have eight asynchronous operations in
flight; provider inference is serialized inside the isolated canary.

Typical flow (all output paths should be outside the repository and mode 0600):

```bash
python3 canary.py preflight
python3 canary.py select --agent-dsn "$AGENT_DSN" --pocket-dsn "$POCKET_DSN" --output "$RUN/corpus.jsonl"
python3 canary.py cards --corpus "$RUN/corpus.jsonl" --output "$RUN/cards.jsonl" --ledger "$RUN/ledger.json"
python3 canary.py questions --corpus "$RUN/corpus.jsonl" --cards "$RUN/cards.jsonl" --output "$RUN/questions.jsonl" --ledger "$RUN/ledger.json"
docker compose up -d postgres budget-proxy hindsight-20b hindsight-120b
python3 canary.py retain --corpus "$RUN/corpus.jsonl" --cards "$RUN/cards.jsonl" --variant summary --url http://127.0.0.1:28888 --bank canary-summary --state "$RUN/20b-summary-state.json"
python3 canary.py retain --corpus "$RUN/corpus.jsonl" --cards "$RUN/cards.jsonl" --variant card --url http://127.0.0.1:28888 --bank canary-card --state "$RUN/20b-card-state.json"
python3 canary.py retain --corpus "$RUN/corpus.jsonl" --cards "$RUN/cards.jsonl" --variant summary --url http://127.0.0.1:38888 --bank canary-summary --state "$RUN/120b-summary-state.json"
python3 canary.py retain --corpus "$RUN/corpus.jsonl" --cards "$RUN/cards.jsonl" --variant card --url http://127.0.0.1:38888 --bank canary-card --state "$RUN/120b-card-state.json"
```

Pass `--limit 25` to each first `retain` invocation, using the same bank and
state path that the full run will use. This validates all four model/variant
paths and gives a ledger-backed projection before the remaining documents are
authorized; rerunning without `--limit` safely continues those same states.

If session-card generation fails its schema/retry gate, omit `--cards` when
building the independent evidence questions and do not create the card banks.

`cards` is the first paid command. `select` is read-only and LLM-free. Every
`retain` invocation re-runs the dedicated-key/budget preflight and refuses
ambiguous crash-window state instead of resubmitting it.

Run `evaluation.py collect --questions "$RUN/questions.jsonl" --corpus
"$RUN/corpus.jsonl" ...` for each available model/variant bank. The corpus
argument keeps Postgres and Hindsight on the exact same snapshot even if new
production documents arrive during the canary. Then describe the outputs in a
spec:

For live databases, prefer mode-0600 environment files and
`CANARY_AGENT_DSN`/`CANARY_POCKET_DSN` so credentials never appear in process
arguments. Set `default_transaction_read_only=on` in both DSNs.

```json
{
  "runs": {
    "20b": {
      "summary": {"path": "/run/20b-summary.jsonl", "schema_retry_failure_rate": 0},
      "session_card": {"path": "/run/20b-card.jsonl", "schema_retry_failure_rate": 0}
    },
    "120b": {
      "summary": {"path": "/run/120b-summary.jsonl", "schema_retry_failure_rate": 0},
      "session_card": {"path": "/run/120b-card.jsonl", "schema_retry_failure_rate": 0}
    }
  }
}
```

Then run `evaluation.py evaluate --spec ... --output metrics.json` followed by
`canary.py gate --metrics metrics.json`. The gate is executable policy, not a
manual interpretation of scores.

`retain` submits no more than eight documents before waiting, adopts existing
active operations by document ID, and stops on duplicate IDs, 401/402, more
than 1% terminal failures, or a remote queue larger than eight. State is stored
beside the output so a crash does not cause blind resubmission.

After collecting route metrics, `canary.py gate` enforces the approved model,
content, and hybrid decision rules. A failing decision leaves production in
Postgres-only mode. Destroy the temporary stack with `docker compose down -v`
only after preserving the run artifacts.
