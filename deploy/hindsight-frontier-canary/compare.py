#!/usr/bin/env python3
"""Compare frontier Hindsight candidates with the completed 120B control."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


def read_json(path: str | Path) -> dict[str, Any]:
    return json.loads(Path(path).read_text())


def ledger_summary(path: str | Path, *, synthetic: bool) -> dict[str, Any]:
    ledger = read_json(path)
    calls = ledger.get("calls") or []
    input_tokens = sum(int(call.get("input_tokens") or 0) for call in calls)
    output_tokens = sum(int(call.get("output_tokens") or 0) for call in calls)
    result = {
        "calls": len(calls),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "metered_units": sum(float(call.get("cost_usd") or 0) for call in calls),
    }
    result["metering"] = (
        "synthetic_$1_per_million_tokens_usage_gate"
        if synthetic
        else "official_xai_list_price_usd"
    )
    return result


def candidate(name: str, score: dict[str, Any], baseline: dict[str, Any]) -> dict[str, Any]:
    route = score["hybrid"]
    control = baseline["hybrid"]
    deltas = {
        field: float(route[field]) - float(control[field])
        for field in (
            "overall_correctness",
            "exact_correctness",
            "temporal_cross_correctness",
        )
    }
    no_regression = bool(
        deltas["overall_correctness"] >= -1e-9
        and deltas["exact_correctness"] >= -1e-9
        and int(route["unsupported_answers"]) == 0
        and float(route["provenance_completeness"]) == 1.0
    )
    meaningful_gain = bool(
        deltas["overall_correctness"] >= 0.05 - 1e-9
        or int(route["critical_misses"]) < int(control["critical_misses"])
    )
    return {
        "model": name,
        "qualified": no_regression and meaningful_gain,
        "no_regression": no_regression,
        "meaningful_gain": meaningful_gain,
        "deltas_vs_120b": deltas,
        "critical_miss_delta": int(route["critical_misses"])
        - int(control["critical_misses"]),
        "routes": score,
    }


def disqualified_candidate(name: str, details: dict[str, Any]) -> dict[str, Any]:
    """Represent a candidate stopped before quality scoring by a safety gate."""
    return {
        "model": name,
        "qualified": False,
        "no_regression": None,
        "meaningful_gain": None,
        "disqualified": True,
        "disqualification": details,
        "routes": None,
    }


def rank(value: dict[str, Any]) -> tuple[Any, ...]:
    if value.get("routes") is None:
        return (False, float("-inf"), float("-inf"), float("-inf"), float("-inf"), float("-inf"))
    route = value["routes"]["hybrid"]
    return (
        bool(value["qualified"]),
        float(route["overall_correctness"]),
        float(route["exact_correctness"]),
        float(route["temporal_cross_correctness"]),
        -int(route["critical_misses"]),
        -float(route["mean_context_chars"]),
    )


def compare(
    baseline: dict[str, Any],
    grok: dict[str, Any],
    fugu: dict[str, Any] | None,
    grok_ledger: dict[str, Any],
    fugu_ledger: dict[str, Any],
    fugu_disqualification: dict[str, Any] | None = None,
    fugu_ultra_disqualification: dict[str, Any] | None = None,
    fugu_ultra_ledger: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if (fugu is None) == (fugu_disqualification is None):
        raise ValueError("provide exactly one of fugu score or fugu disqualification")
    candidates = {
        "grok-4.5": candidate("grok-4.5", grok, baseline),
        "fugu": (
            candidate("fugu", fugu, baseline)
            if fugu is not None
            else disqualified_candidate("fugu", fugu_disqualification or {})
        ),
    }
    usage = {
        "grok-4.5": grok_ledger,
        "fugu": fugu_ledger,
    }
    if fugu_ultra_disqualification is not None:
        candidates["fugu-ultra"] = disqualified_candidate(
            "fugu-ultra", fugu_ultra_disqualification
        )
        usage["fugu-ultra"] = fugu_ultra_ledger or {}
    best = max(candidates.values(), key=rank)
    selected = best["model"] if best["qualified"] else "gpt-oss-120b"
    return {
        "selected_model": selected,
        "upgrade_from_120b": selected != "gpt-oss-120b",
        "gate": {
            "required_overall_gain": 0.05,
            "alternative": "fewer critical misses",
            "requires_no_overall_or_exact_regression": True,
            "requires_full_provenance": True,
            "requires_zero_unsupported_answers": True,
        },
        "baseline_120b": baseline,
        "candidates": candidates,
        "usage": usage,
    }


def atomic_json(path: str | Path, value: dict[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(temporary, target)
    os.chmod(target, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True)
    parser.add_argument("--grok", required=True)
    fugu_group = parser.add_mutually_exclusive_group(required=True)
    fugu_group.add_argument("--fugu")
    fugu_group.add_argument("--fugu-disqualification")
    parser.add_argument("--grok-ledger", required=True)
    parser.add_argument("--fugu-ledger", required=True)
    parser.add_argument("--fugu-ultra-disqualification")
    parser.add_argument("--fugu-ultra-ledger")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    result = compare(
        read_json(args.baseline),
        read_json(args.grok),
        read_json(args.fugu) if args.fugu else None,
        ledger_summary(args.grok_ledger, synthetic=False),
        ledger_summary(args.fugu_ledger, synthetic=True),
        read_json(args.fugu_disqualification) if args.fugu_disqualification else None,
        (
            read_json(args.fugu_ultra_disqualification)
            if args.fugu_ultra_disqualification
            else None
        ),
        (
            ledger_summary(args.fugu_ultra_ledger, synthetic=True)
            if args.fugu_ultra_ledger
            else None
        ),
    )
    atomic_json(args.output, result)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
