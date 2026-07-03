# Subscription-Pooled Inference for Multi-Agent Workloads on Sakana Fugu

**A reference architecture for resilient, cost-efficient use of subscription allowances across a Hermes agent fleet, with compliance analysis and cost observability**

Version 0.2 — July 3, 2026
Status: Draft for internal review

---

## Abstract

Sakana AI's Fugu, released June 22, 2026, exposes a learned multi-agent orchestration system behind a single OpenAI-compatible API, with two purchase paths: monthly subscriptions ($20/$100/$200) with opaque usage allowances, and pay-as-you-go (PAYG) billing at $5/$30 per million input/output tokens for Fugu Ultra. Subscription allowances appear to carry substantially more effective token value than their face price, at the cost of lower routing priority than PAYG traffic. This paper describes an architecture for running many concurrent Hermes agents against a small pool of Fugu subscriptions so that no agent ever stalls on a single exhausted account, with PAYG absorbing overflow, while preserving prompt-cache locality and maintaining per-account cost observability. It also presents a clause-level analysis of Sakana's Terms of Service as they bear on multi-account pooling, and identifies two contractual risks — a competitor-use clause and default training on user content — that are likely more consequential than the account-pooling question itself.

## 1. Problem Statement

Agent harnesses such as Hermes generate inference traffic with three properties that interact badly with subscription-billed model access:

First, traffic is bursty and parallel. A coordinator agent spawning workers can multiply request volume by an order of magnitude within seconds, exhausting a single account's rate or usage ceiling mid-run. A run that dies halfway because one account hit its ceiling wastes everything spent on the run so far; resilience against mid-run exhaustion is the primary design driver.

Second, context is large and highly repetitive. Each agent turn re-sends system prompts, tool definitions, and accumulated conversation state. Provider-side prompt caching discounts this repeated context by roughly 90% (Fugu Ultra cached input is $0.50/M versus $5/M fresh), but cache entries are scoped per account. Any routing scheme that moves a session between accounts mid-burst silently forfeits the discount and accelerates allowance burn by up to 10x on the input side.

Third, Fugu's internal orchestration amplifies billed usage invisibly. A request returning a 500-token answer may consume 5,000–15,000 tokens once delegation, verification, and synthesis rounds are counted; these orchestration tokens bill at standard rates and draw down subscription allowances the same way. Visible output volume is therefore a poor predictor of allowance consumption, and utilization targets must be set empirically.

The operator's goals, in priority order: (a) never stall an agent run on an exhausted account — degrade to PAYG rather than fail; (b) preserve prompt-cache locality, since it dominates input-side economics; (c) retain the subscription tiers because their effective token value per dollar appears to exceed PAYG rates, and avoid stranding purchased allowance at period end; (d) stay inside Sakana's Terms of Service. Where these conflict, resilience and compliance win over allowance-utilization efficiency.

## 2. Fugu Pricing and Priority Model

### 2.1 Rate card

| Dimension | Fugu Ultra (fugu-ultra-20260615) | Notes |
|---|---|---|
| Input | $5 / M tokens | $10 above 272K context |
| Output | $30 / M tokens | $45 above 272K context |
| Cached input | $0.50 / M tokens | $1.00 above 272K context |
| Standard Fugu | Passthrough at underlying model's rate | No fee stacking across agents |

Subscription tiers are defined only as relative multiples — Pro is 10x Standard; Max is reported as either 20x or 30x Standard depending on source — with no published token or dollar allowance. Sakana attributes this to variable orchestration cost per query. The only public calibration point is anecdotal: heavy users report the $200 Max tier covering roughly three hours per week of intensive Ultra usage. At typical frontier agent-loop burn rates of $200–400/hour in API-equivalent terms, that is 3 h/week × ~4.3 weeks × $200–400/h ≈ $2,600–5,200/month of effective allowance — an inference from anecdote, not a published figure, but the premise for preferring subscriptions at all.

### 2.2 Priority inversion

Sakana states that pay-as-you-go consumption tokens are served at higher priority than monthly-plan tokens. This design therefore consciously trades latency for allowance value on the subscription pool, and recovers priority only on the PAYG overflow path. Workloads that are latency-critical should be pinned to the PAYG deployment regardless of allowance state.

### 2.3 The 272K context cliff

Crossing 272K tokens of context doubles input, output, and cached rates wholesale. Hermes agent configurations should cap context assembly below this threshold; for long-horizon agents this argues for aggressive compaction or summarization at ~260K rather than letting context drift across the boundary.

## 3. Reference Architecture

### 3.1 Topology

A single self-hosted LiteLLM proxy fronts the entire Hermes fleet. Hermes is configured with one base URL; all account multiplexing is invisible to the harness.

```
Hermes agents (N sessions)
        │  OpenAI-compatible, one base URL
        ▼
Hermes ACP adapter ── session→deployment assignment (headroom-based, sticky)
        │
        ▼
LiteLLM proxy  ──── Redis (shared usage state, cooldowns)
        │
        ├── deployment: fugu-sub-1   (subscription key A)
        ├── deployment: fugu-sub-2   (subscription key B)
        ├── ...
        └── fallback:  fugu-payg     (pay-as-you-go key)
```

Each subscription account is registered as a separate deployment of the same logical model. The PAYG key is deliberately excluded from the primary pool and configured as a fallback, so it receives traffic only when a session's assigned subscription deployment is cooling down or exhausted.

### 3.2 Two-layer routing: placement, then identity

Balancing must occur at agent granularity, not request granularity — and these are two different mechanisms that must not be conflated.

**Placement (session-assignment time).** When a Hermes agent spawns, the ACP adapter assigns it to the subscription deployment with the most remaining headroom, reading the Redis-backed usage state that LiteLLM maintains. Usage-based signals apply *here and only here*. The assignment is recorded and the agent's requests carry a deployment-scoped virtual key.

**Identity (request time).** Once placed, every request in the session goes to the assigned deployment. No per-request rebalancing. A configuration that lets a usage-based router move live sessions between accounts silently fragments prompt cache and can 10x input-side burn; the router's job at request time is only fallback and cooldown handling, not load balancing.

Note the affinity requirement is *turn-to-turn stickiness within an active burst*, not lifetime pinning. Provider prompt caches have finite TTLs (typically minutes; Fugu's is unpublished — see §5). After any interruption longer than the cache TTL — an idle agent, a cooldown detour to PAYG — the cache is cold anyway and re-placement is free. Concretely: if a session's deployment exhausts, it spills to PAYG for the remainder of the burst; when it next goes idle past the (measured) cache TTL, the adapter may re-place it onto whichever subscription deployment then has headroom.

**Drain-then-spill.** When a subscription key begins returning quota or rate errors, LiteLLM's cooldown removes it from rotation and the session's traffic reaches the PAYG fallback. Agents observe transient latency, never hard failure. Cooldown behavior must be conditioned on the error signature learned in calibration (§5): a rate-limit error warrants a short cooldown (~60s); a period-quota-exhausted error must remove the deployment from placement until the allowance reset, or a monthly-exhausted key will be retried every minute for weeks.

**Graceful exhaustion.** Once empirical per-account ceilings are known (§5), per-deployment TPM/RPM limits are set slightly below the observed ceiling. Keys then degrade by routing decision rather than by mid-completion error, which matters for long agent turns that are expensive to retry.

A model switch mid-session (for example, difficulty-based escalation) also fragments cache. Tier assignment is therefore static per agent role rather than dynamic per request.

### 3.3 Tier assignment by agent role

The Fugu API does not auto-route between variants; the caller selects the model per request. The fleet maps roles to tiers:

Coordinator and verifier agents, where reasoning depth dominates, call `fugu-ultra-20260615`. The dated alias is pinned deliberately: Sakana continuously retrains coordinators and rotates the underlying model pool, so the rolling `fugu-ultra` alias will drift in behavior, which is unacceptable for reproducible agent evaluations.

Worker agents performing well-scoped tasks call standard `fugu`, which bills at underlying-model passthrough rates, drains allowance more slowly, and avoids paying Ultra's orchestration overhead on tasks that do not benefit from it. This split also spreads load across what are effectively two capacity pools.

One role class is excluded from Fugu entirely: agents whose work product is model-routing or orchestration infrastructure route to direct provider APIs, never through Fugu. This is a compliance rule, not an economic one — see §6.2's competitor clause — and it is enforced here, in the tier-assignment table, where it is mechanical rather than aspirational.

### 3.4 Reference configuration

```yaml
# litellm config.yaml (illustrative)
model_list:
  # Two deployments deliberately share model_name — LiteLLM groups
  # same-named deployments into one routing pool. Not a copy-paste error.
  - model_name: fugu-ultra
    litellm_params:
      model: openai/fugu-ultra-20260615
      api_base: <base URL from Sakana console>   # not publicly published; copy from console
      api_key: os.environ/SAKANA_SUB_1_KEY
      rpm: 60          # tune to observed ceiling minus margin
      tpm: 400000
  - model_name: fugu-ultra
    litellm_params:
      model: openai/fugu-ultra-20260615
      api_base: <base URL from Sakana console>
      api_key: os.environ/SAKANA_SUB_2_KEY
      rpm: 60
      tpm: 400000
  - model_name: fugu-ultra-payg
    litellm_params:
      model: openai/fugu-ultra-20260615
      api_base: <base URL from Sakana console>
      api_key: os.environ/SAKANA_PAYG_KEY

router_settings:
  # Placement happens in the ACP adapter (§3.2); requests arrive with a
  # deployment-scoped virtual key, so the proxy's routing strategy governs
  # only new-session placement signals and fallback, never live-session moves.
  routing_strategy: usage-based-routing-v2
  redis_host: os.environ/REDIS_HOST
  fallbacks:
    - fugu-ultra: ["fugu-ultra-payg"]
  cooldown_time: 60   # rate-limit errors only; quota-exhausted keys are
                      # removed from placement until reset (custom handler)

litellm_settings:
  callbacks: ["langfuse_otel"]
```

Custom per-token pricing for Fugu must be registered with the proxy, since LiteLLM's built-in price table does not include Sakana models; without this, computed spend is silently zero or wrong.

## 4. Cost Observability

### 4.1 Stack

The proxy's own spend tracking (virtual keys, per-key budgets, locally computed cost) provides the enforcement layer. Langfuse (MIT-licensed, self-hostable) provides the analysis layer: LiteLLM ships a native Langfuse callback, so every request emits a trace carrying model, deployment, tokens, computed cost, and arbitrary metadata. Tagging traces with `{account, agent_id, agent_role, session_id}` yields the views this architecture depends on: burn per account per day, cost per agent role, and cache-hit economics per session. Langfuse self-hosting requires PostgreSQL, ClickHouse, Redis, and S3-compatible storage — a modest Docker Compose deployment on existing infrastructure.

Prometheus metrics from LiteLLM are gated behind its Enterprise tier; for a single-operator deployment, Langfuse dashboards substitute adequately. Helicone is omitted deliberately: it competes for the proxy position on the hot path and is redundant behind LiteLLM.

### 4.2 Orchestration-token telemetry

Fugu's API responses separate visible-model tokens from orchestration tokens in `token_details` fields. No off-the-shelf observability tool inspects these. A small custom LiteLLM success-callback should extract the orchestration breakdown and attach it as Langfuse metadata. This is the single most important custom component in the stack: orchestration tokens are the hidden multiplier on allowance burn, and per-role orchestration ratios (how much fan-out Ultra performs on coordinator prompts versus worker prompts) directly inform the tier-assignment policy.

### 4.3 Reconciliation discipline

All locally computed costs are estimates derived from token counts multiplied by a price table. Price tables drift, cache-read accounting is subtle, and subscription allowances are not denominated in dollars at all. The console is the source of truth: reconcile Langfuse aggregates against Sakana's console weekly, and treat the local numbers as relative signals (which account, which role, which trend) rather than absolute spend.

## 5. Calibration Procedure

Because allowances are unpublished, utilization targets must be measured, not assumed. Note that calibration traffic is production-shaped traffic: the training opt-out (§6.3) must be exercised on every account *before week one begins*, not before eventual production deployment.

Week one runs a representative agent workload against a single subscription account with full telemetry, recording:

- tokens per day, split by fresh input, cached input, output, and orchestration;
- the error signature at exhaustion (status code and body of both rate-limit and quota errors — these drive the cooldown-vs-remove distinction in §3.2);
- the reset behavior — daily, rolling-window, or monthly. Reset semantics determine strategy: a rolling window rewards steady drain across the whole period; a monthly hard allocation tolerates front-loaded burst usage;
- the prompt-cache TTL, measured by re-sending an identical prefix at increasing intervals and watching cached-token counts. This is arguably the most decision-relevant unknown after the allowance size: a short (minutes) sliding-window TTL means idle agents lose cache regardless of affinity, and re-placement after idleness is free; a long TTL makes strict affinity worth much more.

Week two runs the same workload on PAYG, producing a true dollar cost per agent-hour and a priority/latency comparison. Dividing week-one observed token consumption by week-two unit economics yields the effective dollar value of each subscription tier — the number Sakana does not publish — and with it a rational answer to how many subscriptions the workload justifies versus simply paying PAYG rates.

Per-deployment TPM limits, the drain schedule, and the subscription count are then set from measured ceilings, and re-measured after any Sakana model-pool update, since coordinator retraining (which Sakana performs on a rolling basis) can shift orchestration ratios and therefore burn rates without any change on the operator's side.

## 6. Terms of Service Analysis

This section is an engineering read of the Fugu Terms of Service (console.sakana.ai/terms-of-service, effective June 12, 2026), not legal advice.

### 6.1 What the architecture does not violate

**Credential sharing.** The ToS prohibits disclosing, lending, sharing, or transferring credentials to any third party, and deems all use via one's credentials to be use by the account holder. "You" is defined to include the individual and any entity they represent. An operator's own agents, running on the operator's own infrastructure, authenticating with the operator's own keys through the operator's own proxy, is first-party use. Nothing in this architecture hands credentials to a third party.

**Credit locality.** Credits are usable only by or in connection with the account to which they are issued and are non-transferable. The per-deployment key design respects this: each account's traffic spends only that account's allowance. Nothing is pooled at the billing layer; only routing is pooled.

**Multiple accounts per se.** The ToS contains no express one-account-per-person or one-account-per-entity clause as of this writing.

### 6.2 Where the risk actually sits

**Circumvention.** Prohibited conduct includes placing excessive load on the Service, bypassing protective measures, safety measures, or use restrictions, and attempts to circumvent restrictions. Sakana may suspend or terminate without prior notice on a reasonable determination that a user has violated — or is likely to violate — these terms. The strongest adverse reading of this architecture is that operating several subscriptions in parallel behind one proxy is, in aggregate, use exceeding what any single account permits — and stated intent matters: a design justified as "drain every allowance to the maximum" reads as circumvention on its face, where the same architecture justified as failover resilience (an agent run should degrade to PAYG rather than die mid-run when one account exhausts) is a defensible operational posture. This paper's design goals (§1) are ordered accordingly, and honestly: resilience is the primary driver; allowance efficiency is a welcome property of the same mechanism. Distinct accounts for genuinely distinct entities (an individual and their corporation) further strengthen the posture; a farm of same-entity accounts does not. The recommendation is a small pool — two to three accounts across real entities — with PAYG absorbing everything beyond, which also happens to be the better-latency path.

**The competitor clause.** The ToS prohibits using the Service to develop or provide a product or service that competes with the Service, with AI orchestration and routing across multiple models given as the example. For an operator whose broader work includes multi-model orchestration and coordinator-style agent infrastructure, this clause deserves more attention than the account question. Running Fugu as a backend for internal agent workloads is one thing; shipping a product in the orchestration/routing category with Fugu in the loop — or trained, tuned, or evaluated against it — invites the clause directly. Related prohibitions on reverse engineering the routing layer and on using outputs to train or distill competing models point the same direction. The operative rule adopted here (§3.3): agents whose work product is routing or orchestration infrastructure never call Fugu; those workloads run on direct provider APIs. This converts the clause from a standing legal worry into an enforced routing policy.

**Expiry.** Credits expire six months after issuance. Deep prepayment is capital at risk against both expiry and the suspension scenarios above.

### 6.3 Data handling

Two provisions matter independently of pooling. First, Sakana uses Input and Output for model training by default, including human review, with an opt-out; training effects already realized are not reversible retroactively. The opt-out must be exercised on every account before the first request of any kind — including the calibration runs in §5. Second, the prohibited-conduct list bars inputting personal information, confidential information, or trade secrets at all. Proprietary source code, security-sensitive infrastructure details, and client material are contractually excluded from Fugu regardless of the opt-out state. The practical policy: Fugu accounts are treated as an untrusted execution context, and Hermes agent configurations routed to Fugu carry only material the operator would be comfortable disclosing. (That policy applies reflexively to documents like this one: an internal paper describing the operator's infrastructure and legal posture belongs in the same excluded category.)

## 7. Risks and Open Questions

The design's residual risks, in rough priority order: enforcement posture is unknown (the service is two weeks old; today's tolerated pattern may be tomorrow's banned one, and allowance is prepaid); allowance reset semantics and cache TTL are unverified and materially change the drain and affinity strategies respectively; the Max-tier multiplier is inconsistently reported (20x vs 30x) and should be confirmed in-console; Sakana's rolling coordinator retraining changes burn rates under the operator's feet, requiring periodic recalibration; and the pinned dated alias (`fugu-ultra-20260615`) will eventually be deprecated on an unknown schedule.

A standing alternative should be kept warm: an open-source cheap-first router (e.g., Maestro or RouteLLM) over direct provider APIs replicates Fugu's routing philosophy with transparent economics and no competitor-clause exposure, at the cost of operating the orchestration layer oneself. The exit is cheaper than it looks: the placement/affinity machinery of §3.2, the observability stack of §4, and the calibration discipline of §5 all transfer wholesale; only the Fugu-specific deployments and the orchestration-token callback are stranded. If subscription economics degrade, enforcement tightens, or the competitor clause becomes binding on planned work, that is the exit ramp.

## 8. Conclusion

Subscription pooling for Fugu is architecturally straightforward — a LiteLLM proxy with per-account deployments, headroom-based placement with in-burst session affinity, and drain-then-spill fallback to PAYG — and the interesting problems are elsewhere: measuring an unpublished allowance and an unpublished cache TTL, keeping cache locality intact without letting a load balancer move live sessions, surfacing orchestration tokens that no standard tool reads, and staying on the right side of a ToS whose sharpest edges are the circumvention and competitor clauses rather than any rule about account count. Run small (two to three legitimate accounts, resilience-first framing), instrument everything, treat local cost numbers as relative signals and the console as truth, opt out of training before the first calibration request, keep orchestration-product work off Fugu entirely, and keep proprietary context out of the pool.

---

*Sources: Sakana Fugu product page and FAQ (sakana.ai/fugu), Fugu Terms of Service (console.sakana.ai/terms-of-service, eff. June 12, 2026), Fugu launch coverage and pricing analyses (DataCamp, WaveSpeed, apidog, TECHSY, June–July 2026), LiteLLM documentation (docs.litellm.ai), Langfuse LiteLLM integration docs (langfuse.com). Pricing and ToS terms verified as of July 3, 2026; the service is newly launched and all figures should be re-verified in-console before financial commitment.*
