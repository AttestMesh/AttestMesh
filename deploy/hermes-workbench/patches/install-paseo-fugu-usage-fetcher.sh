#!/bin/sh
set -eu

PASEO_ROOT="${PASEO_ROOT:-$(npm root -g)/@getpaseo/cli}"
QF_DIR="$PASEO_ROOT/node_modules/@getpaseo/server/dist/server/services/quota-fetcher"
MANIFEST="$QF_DIR/manifest.js"

test -d "$QF_DIR"
mkdir -p "$QF_DIR/providers"

cat > "$QF_DIR/providers/hermes-fugu.js" <<'EOF'
import { existsSync, promises as fs } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";
import YAML from "yaml";
import { balanceToneFromRemaining, fetchProviderApi, unavailableUsage } from "../usage.js";

function asNumber(value) {
    if (value === null || value === undefined || value === "")
        return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

function fmtNumber(value, digits = 0) {
    const n = asNumber(value);
    if (n === null)
        return "n/a";
    return n.toLocaleString("en-US", { maximumFractionDigits: digits });
}

function fmtUsd(value) {
    const n = asNumber(value);
    if (n === null)
        return "n/a";
    return `$${n.toFixed(4)}`;
}

function routerRoot(baseUrl) {
    const trimmed = String(baseUrl || "").replace(/\/+$/, "");
    return trimmed.endsWith("/v1") ? trimmed.slice(0, -3) : trimmed;
}

function toneFromUsedPct(usedPct) {
    if (usedPct === null)
        return "default";
    if (usedPct >= 95)
        return "danger";
    if (usedPct >= 80)
        return "warning";
    return "ok";
}

async function readYaml(path) {
    return YAML.parse(await fs.readFile(path, "utf8"));
}

export class HermesFuguQuotaProvider {
    constructor(options) {
        this.providerId = "hermes";
        this.displayName = "Hermes / Fugu Router";
        this.fetchApi = options.fetch ?? fetch;
        this.logger = options.logger;
    }

    async fetchUsage() {
        const config = await this.readConfig();
        if (!config)
            return unavailableUsage(this);
        const root = routerRoot(config.baseUrl);
        if (!root)
            return unavailableUsage(this);

        const summary = await this.fetchJson(`${root}/fugu/api/summary?window=month&model=all`, config.apiKey);
        if (!summary)
            return unavailableUsage(this);

        const globalSpend = await this.fetchJson(`${root}/global/spend`, config.apiKey).catch(() => null);
        return this.toUsage(summary, globalSpend);
    }

    async readConfig() {
        const candidates = [
            process.env["HERMES_CONFIG"],
            join(homedir(), ".hermes", "config.yaml"),
            join(homedir(), ".hermes", "config.yml"),
        ].filter(Boolean);
        for (const path of candidates) {
            if (!existsSync(path))
                continue;
            try {
                const config = await readYaml(path);
                const model = config?.model ?? {};
                const baseUrl = model.base_url ?? model.baseUrl;
                const apiKey = model.api_key ?? model.apiKey;
                if (baseUrl && apiKey) {
                    return { baseUrl: String(baseUrl), apiKey: String(apiKey) };
                }
            }
            catch (error) {
                this.logger.debug({ err: error, path }, "Failed to read Hermes fugu-router config");
            }
        }
        return null;
    }

    async fetchJson(url, apiKey) {
        const response = await fetchProviderApi(this.fetchApi, url, {
            headers: {
                Authorization: `Bearer ${apiKey}`,
                Accept: "application/json",
            },
        });
        if (!response.ok) {
            this.logger.debug({ status: response.status, url }, "Fugu router usage fetch failed");
            return null;
        }
        return response.json();
    }

    toUsage(summary, globalSpend) {
        const totals = summary?.totals ?? {};
        let allowanceUnits = 0;
        for (const account of Array.isArray(summary?.accounts) ? summary.accounts : []) {
            const allowance = asNumber(account?.window?.allowance_usage_units);
            if (allowance !== null && allowance > 0)
                allowanceUnits += allowance;
        }

        const balances = [];
        const usageUnits = asNumber(totals.usage_units);
        if (usageUnits !== null) {
            const limit = allowanceUnits > 0 ? allowanceUnits : null;
            const remaining = limit !== null ? Math.max(0, limit - usageUnits) : null;
            balances.push({
                id: "fugu_credits",
                label: "Fugu credits",
                used: usageUnits,
                remaining,
                limit,
                unit: "credits",
                tone: balanceToneFromRemaining(remaining),
            });
        }
        const estimatedCost = asNumber(totals.estimated_cost_usd);
        if (estimatedCost !== null) {
            balances.push({
                id: "estimated_cost",
                label: "Estimated cost",
                used: estimatedCost,
                unit: "usd",
                tone: "default",
            });
        }
        const litellmSpend = asNumber(totals.litellm_spend);
        if (litellmSpend !== null) {
            balances.push({
                id: "litellm_spend",
                label: "LiteLLM spend",
                used: litellmSpend,
                unit: "usd",
                tone: "default",
            });
        }
        const maxBudget = asNumber(globalSpend?.max_budget);
        const globalSpendValue = asNumber(globalSpend?.spend);
        if (maxBudget !== null && maxBudget > 0 && globalSpendValue !== null) {
            const remaining = Math.max(0, maxBudget - globalSpendValue);
            balances.push({
                id: "litellm_budget",
                label: "LiteLLM budget",
                used: globalSpendValue,
                remaining,
                limit: maxBudget,
                unit: "usd",
                tone: balanceToneFromRemaining(remaining),
            });
        }

        const windows = [];
        for (const account of Array.isArray(summary?.accounts) ? summary.accounts : []) {
            const window = account?.window;
            const used = asNumber(window?.used_units);
            const allowance = asNumber(window?.allowance_usage_units);
            if (used === null || allowance === null || allowance <= 0)
                continue;
            const usedPct = Math.max(0, Math.min(100, (used / allowance) * 100));
            windows.push({
                id: String(account.account_id ?? windows.length),
                label: String(account.display_name ?? account.account_id ?? "Route"),
                usedPct,
                remainingPct: Math.max(0, 100 - usedPct),
                resetsAt: null,
                tone: toneFromUsedPct(usedPct),
            });
        }

        const details = [
            { id: "usage_units", label: "Usage units", value: fmtNumber(totals.usage_units, 1) },
            { id: "total_tokens", label: "Total tokens", value: fmtNumber(totals.total_tokens, 0) },
            { id: "input_tokens", label: "Input tokens", value: fmtNumber(totals.input_tokens, 0) },
            { id: "output_tokens", label: "Output tokens", value: fmtNumber(totals.output_tokens, 0) },
            { id: "cached_input", label: "Cached input", value: fmtNumber(totals.cached_input_tokens, 0) },
            { id: "estimated_cost_detail", label: "Estimated cost", value: fmtUsd(totals.estimated_cost_usd) },
            { id: "litellm_spend_detail", label: "LiteLLM spend", value: fmtUsd(totals.litellm_spend) },
        ];
        if (!windows.length && !(maxBudget !== null && maxBudget > 0)) {
            details.push({
                id: "remaining",
                label: "Remaining",
                value: "n/a (router has no upstream balance)",
            });
        }

        return {
            providerId: this.providerId,
            displayName: this.displayName,
            status: "available",
            planLabel: "Fugu Router",
            sourceLabel: "fugu-router",
            fetchedAt: new Date().toISOString(),
            windows,
            balances,
            details,
            error: null,
        };
    }
}
EOF

export MANIFEST
node --input-type=module <<'EOF'
import { readFile, writeFile } from "node:fs/promises";

const path = process.env.MANIFEST;
let text = await readFile(path, "utf8");

if (!text.includes('providers/hermes-fugu.js')) {
    text = text.replace(
        'import { KimiQuotaProvider } from "./providers/kimi.js";',
        'import { HermesFuguQuotaProvider } from "./providers/hermes-fugu.js";\nimport { KimiQuotaProvider } from "./providers/kimi.js";',
    );
}

if (!text.includes('providerId: "hermes"')) {
    text = text.replace(
        '    {\n        providerId: "kimi",',
        '    {\n        providerId: "hermes",\n        create: (options) => new HermesFuguQuotaProvider({ logger: options.logger, fetch: options.fetch }),\n    },\n    {\n        providerId: "kimi",',
    );
}

await writeFile(path, text);
EOF
