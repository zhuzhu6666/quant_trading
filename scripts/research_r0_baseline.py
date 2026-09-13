#!/usr/bin/env python3
"""R0 基线测量：因子 × 持仓监督 × 复盘归因 的学习信号质量只读审计。

只读 canonical_v2 / runtime 事实，输出 JSON 摘要；不写任何状态、不新增表。
依据 docs/planning/systematic-research-roadmap.md §R0。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict

sys.path.insert(0, "/home/ubuntu/quant_trading")

from backend.core.db import get_state_pg_conn  # noqa: E402
from backend.services.canonical_v2_reader import read_payload  # noqa: E402


def _rows(conn, sql, params=()):
    out = []
    for r in conn.execute(sql, params).fetchall():
        out.append(r if isinstance(r, dict) else dict(zip([c[0] for c in r], r)))
    return out


def _payloads(conn, event_type, limit=None):
    sql = "SELECT payload_hash, observed_at FROM canonical_v2.event WHERE event_type=%s ORDER BY observed_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    for r in _rows(conn, sql, (event_type,)):
        p = read_payload(conn, r["payload_hash"])
        if isinstance(p, dict):
            p["_observed_at"] = r["observed_at"]
            yield p


def _dist(counter, total=None):
    total = total or sum(counter.values())
    return {str(k): {"n": v, "pct": round(100.0 * v / total, 1) if total else 0.0} for k, v in counter.most_common()}


def section_overview(conn):
    types = _rows(conn, """
        SELECT event_type, count(*) AS n,
               to_char(min(observed_at) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS first_cst,
               to_char(max(observed_at) AT TIME ZONE 'Asia/Shanghai','YYYY-MM-DD') AS last_cst
        FROM canonical_v2.event GROUP BY event_type ORDER BY count(*) DESC""")
    return {"event_types": types}


def section_attribution(conn):
    reviews = list(_payloads(conn, "trade_review"))
    outcome = Counter()
    contam = Counter()
    responsibility = Counter()
    causal_level = Counter()
    contribution_factor = Counter()
    contribution_missing = 0
    fill_delay = []
    mfe_capture = []
    for p in reviews:
        outcome[p.get("outcome_label")] += 1
        rv = p.get("review") if isinstance(p.get("review"), dict) else {}
        si = rv.get("system_issue_context") or {}
        contam[bool(si.get("contaminates_learning"))] += 1
        if si.get("contaminates_learning"):
            responsibility[si.get("primary_responsibility")] += 1
        fa = rv.get("factor_attribution") or {}
        causal_level[fa.get("causal_level")] += 1
        if fa.get("largest_contribution_factor"):
            contribution_factor[fa["largest_contribution_factor"]] += 1
        else:
            contribution_missing += 1
        et = rv.get("entry_timing_context") or {}
        d = et.get("decision_to_fill_delay_seconds")
        if isinstance(d, (int, float)) and d >= 0:
            fill_delay.append(d)
        pnl, mfe = p.get("pnl"), p.get("mfe")
        if isinstance(pnl, (int, float)) and isinstance(mfe, (int, float)) and mfe and mfe > 0:
            mfe_capture.append(min(max(pnl / mfe, 0.0), 2.0))
    samples = _rows(conn, """
        SELECT sample_type, label_status, integrity, system_contaminated, governance_eligible,
               governance_ineligible_reason, count(*) AS n
        FROM canonical_v2.training_sample_row GROUP BY 1,2,3,4,5,6 ORDER BY n DESC""")
    sample_total = sum(r["n"] for r in samples)
    eligible_n = sum(r["n"] for r in samples if r["governance_eligible"])
    contaminated_n = sum(r["n"] for r in samples if r["system_contaminated"])
    ineligible_reasons = Counter()
    for r in samples:
        if not r["governance_eligible"]:
            ineligible_reasons[r["governance_ineligible_reason"] or "<null>"] += r["n"]
    return {
        "trade_review_total": len(reviews),
        "outcome_label": _dist(outcome),
        "system_issue": {
            "contaminates_learning": _dist(contam),
            "primary_responsibility_when_contaminated": _dist(responsibility),
        },
        "factor_attribution": {
            "causal_level": _dist(causal_level),
            "largest_contribution_factor_top": _dist(contribution_factor),
            "largest_contribution_missing": contribution_missing,
        },
        "entry_timing": {
            "decision_to_fill_delay_seconds": {
                "n": len(fill_delay),
                "p50": round(statistics.median(fill_delay), 2) if fill_delay else None,
                "p95": round(sorted(fill_delay)[int(0.95 * len(fill_delay))], 2) if fill_delay else None,
                "max": round(max(fill_delay), 2) if fill_delay else None,
            },
        },
        "mfe_capture_ratio_pnl_over_mfe": {
            "n": len(mfe_capture),
            "p25": round(sorted(mfe_capture)[int(0.25 * len(mfe_capture))], 3) if mfe_capture else None,
            "median": round(statistics.median(mfe_capture), 3) if mfe_capture else None,
            "p75": round(sorted(mfe_capture)[int(0.75 * len(mfe_capture))], 3) if mfe_capture else None,
        },
        "training_sample_row": {
            "total": sample_total,
            "governance_eligible_n": eligible_n,
            "governance_eligible_pct": round(100.0 * eligible_n / sample_total, 1) if sample_total else 0.0,
            "system_contaminated_n": contaminated_n,
            "ineligible_reasons": _dist(ineligible_reasons),
            "by_sample_type": _dist(Counter(
                {k: sum(r["n"] for r in samples if (r["sample_type"], r["label_status"]) == k)
                 for k in {(r["sample_type"], r["label_status"]) for r in samples}})),
        },
    }


def section_supervision(conn):
    cf_labels = Counter()
    cf_mature = Counter()
    cf_gov_eligible = Counter()
    cf_binding = Counter()
    for p in _payloads(conn, "counterfactual_review"):
        cf_labels[p.get("label")] += 1
        cf_mature[p.get("maturity_status")] += 1
        cf_gov_eligible[bool(p.get("governance_eligible"))] += 1
        cf_binding[p.get("position_supervisor_binding_status") or p.get("position_supervisor_binding_reason")] += 1
    exec_status = Counter()
    stage = Counter()
    actions = Counter()
    risk_allowed = Counter()
    for p in _payloads(conn, "supervisor_trace"):
        stage[p.get("stage")] += 1
        exec_status[p.get("execution_status")] += 1
        actions[(p.get("requested_action"), p.get("effective_action"))] += 1
        risk_allowed[bool(p.get("risk_allowed"))] += 1
    eval_reason = Counter()
    eval_tags = Counter()
    eval_template = Counter()
    for p in _payloads(conn, "supervisor_evaluation"):
        eval_reason[p.get("summary_reason")] += 1
        for t in p.get("trigger_tags") or []:
            eval_tags[t] += 1
        eval_template[p.get("supervisor_template_id")] += 1
    return {
        "counterfactual_review": {
            "label": _dist(cf_labels),
            "maturity_status": _dist(cf_mature),
            "governance_eligible": _dist(cf_gov_eligible),
            "binding_status": _dist(cf_binding),
        },
        "supervisor_trace": {
            "stage": _dist(stage),
            "execution_status": _dist(exec_status),
            "requested_to_effective": _dist(actions),
            "risk_allowed": _dist(risk_allowed),
        },
        "supervisor_evaluation": {
            "summary_reason": _dist(eval_reason),
            "trigger_tags_top": _dist(eval_tags),
            "template_id": _dist(eval_template),
        },
    }


def section_factors(conn):
    contrib = _rows(conn, """
        SELECT factor, count(*) AS n, avg(net_contribution) AS avg_net,
               avg(entry_contribution) AS avg_entry, avg(hold_contribution) AS avg_hold,
               avg(exit_contribution) AS avg_exit,
               count(*) FILTER (WHERE net_contribution < 0) AS neg_n
        FROM runtime.factor_contribution_review GROUP BY factor ORDER BY avg_net DESC""")
    # 亏损交易 vs 盈利交易的因子净贡献
    trade_pnl = {}
    for p in _payloads(conn, "trade_review"):
        tid, pnl = p.get("trade_id"), p.get("pnl")
        if tid and isinstance(pnl, (int, float)):
            trade_pnl[tid] = pnl
    by_factor_loss = defaultdict(list)
    by_factor_win = defaultdict(list)
    for r in _rows(conn, "SELECT trade_id, factor, net_contribution FROM runtime.factor_contribution_review"):
        pnl = trade_pnl.get(r["trade_id"])
        if pnl is None:
            continue
        (by_factor_win if pnl > 0 else by_factor_loss)[r["factor"]].append(r["net_contribution"])
    def agg(vals):
        return {"n": len(vals), "avg": round(statistics.mean(vals), 4)} if vals else {"n": 0, "avg": None}
    regime = Counter()
    regime_conf = []
    factor_presence = Counter()
    for p in _payloads(conn, "risk_decision", limit=2000):
        regime[p.get("regime_id")] += 1
        c = p.get("regime_confidence")
        if isinstance(c, (int, float)):
            regime_conf.append(c)
        for fs in p.get("factor_snapshots") or []:
            if isinstance(fs, dict) and fs.get("factor"):
                factor_presence[fs["factor"]] += 1
    return {
        "contribution_review": {
            "total_rows": sum(r["n"] for r in contrib),
            "per_factor": [
                {**r, "neg_pct": round(100.0 * r["neg_n"] / r["n"], 1)} for r in contrib
            ],
        },
        "contribution_by_trade_outcome": {
            "losing_trades": {f: agg(v) for f, v in sorted(by_factor_loss.items(), key=lambda kv: statistics.mean(kv[1]))},
            "winning_trades": {f: agg(v) for f, v in sorted(by_factor_win.items(), key=lambda kv: -statistics.mean(kv[1]))},
        },
        "risk_decision_sample_2000": {
            "regime_id": _dist(regime),
            "regime_confidence_median": round(statistics.median(regime_conf), 3) if regime_conf else None,
            "factor_presence_top": _dist(factor_presence),
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", help="同时把完整结果写到该 JSON 文件")
    args = ap.parse_args()
    conn = get_state_pg_conn(read_only=True)
    report = {
        "schema": "research_r0_baseline.v1",
        "note": "只读基线测量，依据 systematic-research-roadmap.md R0；provenance v36 之前为 unknown 属预期，未过滤",
        "overview": section_overview(conn),
        "attribution": section_attribution(conn),
        "supervision": section_supervision(conn),
        "factors": section_factors(conn),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2, default=str)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            f.write(text)
        print(f"written: {args.json}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
