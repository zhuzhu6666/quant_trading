"""Causal parity replay contract and fail-closed runner.

The runner deliberately separates two questions:

* can the historical path be replayed with closed-bar causality and executable
  bid/ask prices; and
* is that replay identical enough to live to become governance evidence?

The current implementation can answer the first question.  It reuses the live
factor, selector, normalizer, compositor, RiskPolicy, position-path metrics,
safety arbitration, supervisor, trailing and protection-plan primitives.  The
historical broker receipt/reconcile stream, tick path, safety cadence, account
context and deal costs are not present in monthly bars, however.  Therefore it
reports ``diagnostic_only`` until those inputs are independently available and
verified rather than optimistically treating shared code as live parity.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping
import uuid

import pandas as pd

from backend.core.db import (
    DUCKDB_BARS_MONTHLY_DIR,
    STATE_DB,
    duckdb_readonly_connection,
)
from backend.services.research_evidence import (
    PARITY_REPLAY_ENGINE,
    PARITY_REPLAY_EVIDENCE_CLASS,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_PARITY_ARTIFACT_DIR = PROJECT_ROOT / "data" / "replay_reports"
PARITY_REPLAY_SCHEMA_VERSION = "parity_replay_report.v1"
PARITY_REPLAY_CONTRACT_VERSION = "parity_replay_contract.v1"

_BID_ASK_COLUMNS = (
    "bid_open",
    "bid_high",
    "bid_low",
    "bid_close",
    "ask_open",
    "ask_high",
    "ask_low",
    "ask_close",
)
_BASE_COLUMNS = ("time", "open", "high", "low", "close")
_CODE_BINDING_PATHS = (
    "backend/services/parity_replay.py",
    "backend/services/live_factor_wiring.py",
    "backend/services/live_decision_pipeline.py",
    "backend/services/live_loop_shell.py",
    "backend/services/live_position_lifecycle.py",
    "backend/services/live_safety_planner.py",
    "backend/services/position_metrics.py",
    "backend/services/position_supervisor.py",
    "backend/services/position_supervisor_templates.py",
    "backend/risk/metrics_snapshot.py",
    "backend/risk/var.py",
    "data/factor_frame.py",
    "alpha/runtime_factor_selection.py",
    "alpha/streaming_factor_engine.py",
    "alpha/signal_normalizer.py",
    "alpha/portfolio_compositor.py",
    "alpha/execution_gate.py",
    "risk/policy_service.py",
)
_BASE_BINDING_NAMES = ("config_hash", "data_hash", "code_hash", "artifact_hash")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_DISCOVERED_FACTOR_SOURCES = frozenset({"discovered", "generated", "shadow", "dsl", "gp"})


DecisionProvider = Callable[[pd.DataFrame, Mapping[str, Any], int], Mapping[str, Any] | None]
RiskEvaluator = Callable[[Mapping[str, Any]], Mapping[str, Any] | Any]
SupervisorEvaluator = Callable[[Mapping[str, Any]], Mapping[str, Any] | Any]


def _json_default(value: Any) -> Any:
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=_json_default,
    )


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _runtime_config_hash(value: Any) -> str:
    """Match ``evolution_ledger._stable_hash`` byte-for-byte.

    Runtime snapshots predate the replay contract and use json.dumps' default
    separators.  Binding the same payload with the replay canonicalizer would
    create a permanent false mismatch despite identical configuration.
    """

    raw = json.dumps(
        value if value is not None else {},
        ensure_ascii=False,
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _sha256_files(paths: tuple[str, ...]) -> str:
    digest = hashlib.sha256()
    for relative in paths:
        path = PROJECT_ROOT / relative
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        if path.exists():
            digest.update(path.read_bytes())
        else:
            digest.update(b"<missing>")
        digest.update(b"\0")
    return digest.hexdigest()


def _code_artifact_manifest(paths: tuple[str, ...]) -> dict[str, str]:
    """Bind each shared implementation independently as well as in aggregate."""

    manifest: dict[str, str] = {}
    for relative in paths:
        path = PROJECT_ROOT / relative
        manifest[relative] = (
            hashlib.sha256(path.read_bytes()).hexdigest()
            if path.is_file()
            else ""
        )
    return manifest


def _missing_code_binding_paths(paths: tuple[str, ...]) -> list[str]:
    """Return checked-in live primitive paths absent from the replay binding."""

    return [relative for relative in paths if not (PROJECT_ROOT / relative).is_file()]


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _to_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if is_dataclass(value) and not isinstance(value, type):
        return dict(asdict(value))
    if hasattr(value, "to_dict"):
        try:
            return dict(value.to_dict() or {})
        except Exception:
            return {}
    return {}


def _aggregate_independent_trades(trades: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse partial closes into one causal trade outcome."""

    grouped: dict[int, dict[str, Any]] = {}
    for raw in trades:
        item = dict(raw)
        decision_index = int(item.get("decision_index") or 0)
        current = grouped.get(decision_index)
        if current is None:
            current = dict(item)
            grouped[decision_index] = current
            continue
        for name in (
            "gross_pnl",
            "commission_cost",
            "spread_cost",
            "slippage_cost",
            "net_pnl",
            "closed_fraction",
            "volume_lots",
        ):
            current[name] = _safe_float(current.get(name)) + _safe_float(item.get(name))
        current["exit_index"] = item.get("exit_index")
        current["exit_ts"] = item.get("exit_ts")
        current["exit_price"] = item.get("exit_price")
        current["raw_exit_price"] = item.get("raw_exit_price")
        current["reason"] = item.get("reason")
        current["same_bar_sl_tp_path_ambiguous"] = bool(
            current.get("same_bar_sl_tp_path_ambiguous")
            or item.get("same_bar_sl_tp_path_ambiguous")
        )
    return [grouped[key] for key in sorted(grouped)]


def _sample_id(binding_hash: str, decision_index: int, factor_id: str = "") -> str:
    return hashlib.sha256(
        f"{binding_hash}:{int(decision_index)}:{factor_id}".encode("utf-8")
    ).hexdigest()


def _factor_contribution(value: Any) -> tuple[float, float]:
    item = _to_dict(value)
    if item:
        contribution = _safe_float(
            item.get("contribution"),
            _safe_float(item.get("normalized"), _safe_float(item.get("value"))),
        )
        confidence = _safe_float(item.get("confidence"), 1.0)
        return contribution, confidence
    return _safe_float(value), 1.0


def _build_learning_bundle(report: Mapping[str, Any]) -> dict[str, Any]:
    """Build deterministic, filesystem-only training rows from replay outcomes."""

    bindings = dict(report.get("bindings") or {})
    binding_hash = str(bindings.get("binding_hash") or "")
    causality = dict(report.get("causality") or {})
    data_source = dict(report.get("data_source") or {})
    manifest = dict(report.get("artifact_manifest") or {})
    selected_factor_ids = [
        str(item) for item in list(manifest.get("selected_factor_ids") or []) if str(item)
    ]
    blockers: list[str] = []
    if not binding_hash:
        blockers.append("binding_missing")
    if not bool(data_source.get("point_in_time")):
        blockers.append("point_in_time_data_unverified")
    if not bool(causality.get("closed_bar_only")):
        blockers.append("closed_bar_contract_failed")
    if not bool(causality.get("next_bar_execution")):
        blockers.append("next_bar_execution_contract_failed")
    if not bool(causality.get("executable_bid_ask", causality.get("native_bid_ask"))):
        blockers.append("executable_bid_ask_missing")
    if not selected_factor_ids or len(selected_factor_ids) > 64:
        blockers.append("current_bounded_factor_generation_unverified")
    fatal_prefixes = (
        "binding_mismatch:",
        "monthly_pit_",
        "runtime_config_snapshot_",
        "committed_runtime_config_",
        "selected_factor_identity_",
        "selected_factor_definition_",
        "selected_factor_not_active:",
        "selected_factor_explicit_",
        "selected_factor_artifact_",
        "bar_time_order_",
        "closed_bar_window_",
        "future_data_",
        "unclosed_bar_",
    )
    for reason in list(report.get("diagnostic_reasons") or []):
        text = str(reason)
        if text.startswith(fatal_prefixes):
            blockers.append(text)
        if text == "live_safety_planner_execution_error":
            blockers.append(text)

    independent = _aggregate_independent_trades(
        [dict(item) for item in list(report.get("trades") or []) if isinstance(item, Mapping)]
    )
    eligible_independent = [
        trade
        for trade in independent
        if not bool(trade.get("same_bar_sl_tp_path_ambiguous"))
    ]
    excluded_trade_count = len(independent) - len(eligible_independent)
    open_samples: list[dict[str, Any]] = []
    factor_rows: dict[str, list[dict[str, Any]]] = {}
    try:
        from research.open_quality_lightgbm import (
            FEATURE_NAMES as OPEN_FEATURE_NAMES,
            _features_from_sample,
            _rule_baseline_label,
        )

        for trade in eligible_independent:
            candidate = dict(trade.get("decision_candidate") or {})
            bar = dict(trade.get("decision_bar") or {})
            decision_index = int(trade.get("decision_index") or 0)
            pnl = _safe_float(trade.get("net_pnl"))
            signals = dict(candidate.get("signals") or {})
            factor_values = dict(candidate.get("factor_values") or {})
            action = {
                **candidate,
                "factor_roles": {factor_id: "alpha" for factor_id in selected_factor_ids},
                "composer_version": "live_parity_replay_v1",
                "n_active_factors": len(selected_factor_ids),
                "n_active_alpha_factors": len(selected_factor_ids),
                "n_abstain_factors": max(0, len(selected_factor_ids) - len(signals)),
            }
            decision_quality = {
                "schema_version": "decision_quality_context.v1",
                "n_active_factors": len(selected_factor_ids),
                "n_active_alpha_factors": len(selected_factor_ids),
                "factor_roles": action["factor_roles"],
            }
            learning_context = dict(trade.get("open_learning_context") or {})
            row = {
                "features_json": {
                    **learning_context,
                    "action_score": _safe_float(candidate.get("score")),
                    "action": action,
                    "decision_quality_context": (
                        learning_context.get("decision_quality_context") or decision_quality
                    ),
                    "bar_context": learning_context.get("bar_context") or {
                        "schema_version": "closed_bar.v1",
                        "complete": True,
                        "body_ratio": (
                            abs(_safe_float(bar.get("close")) - _safe_float(bar.get("open")))
                            / max(abs(_safe_float(bar.get("high")) - _safe_float(bar.get("low"))), 1e-12)
                        ),
                        "close_location": (
                            (_safe_float(bar.get("close")) - _safe_float(bar.get("low")))
                            / max(_safe_float(bar.get("high")) - _safe_float(bar.get("low")), 1e-12)
                        ),
                        "range_points": abs(_safe_float(bar.get("high")) - _safe_float(bar.get("low"))),
                    },
                    "market_micro_context": learning_context.get("market_micro_context") or {
                        "spread": max(
                            0.0,
                            _safe_float(bar.get("ask_close")) - _safe_float(bar.get("bid_close")),
                        ),
                        "quote_fresh": True,
                        "quote_age_seconds": 0.0,
                    },
                }
            }
            features = _features_from_sample(row)
            trade_id = f"{binding_hash}:{decision_index}"
            financial_label = "profit" if pnl > 0.0 else "loss" if pnl < 0.0 else "flat"
            replay_execution = {
                "schema_version": "execution_quality_evidence.v2",
                "evidence_state": "replay_verified",
                "source": "parity_replay",
                "broker_price_trusted": False,
                "requested_price": _safe_float(trade.get("raw_entry_price")),
                "fill_price": _safe_float(trade.get("entry_price")),
                "spread_points": _safe_float(
                    (learning_context.get("market_micro_context") or {}).get("spread")
                ),
                "slippage_points": _safe_float(trade.get("entry_price"))
                - _safe_float(trade.get("raw_entry_price")),
                "modelled_execution": True,
            }
            open_samples.append({
                "sample_id": _sample_id(binding_hash, decision_index),
                "decision_id": trade_id,
                "trade_id": trade_id,
                "position_id": trade_id,
                "created_at": _safe_float(trade.get("exit_ts")),
                "outcome_label": "good_win" if pnl > 0.0 else "bad_loss" if pnl < 0.0 else "flat",
                "pnl": pnl,
                "label": 1 if pnl > 0.0 else 0,
                "open_target_v2": {
                    "schema_version": "open_target.v2",
                    "objective": "profitable_open_outcome",
                    "financial_label": financial_label,
                    "legacy_outcome_label": "good_win" if pnl > 0.0 else "bad_loss" if pnl < 0.0 else "flat",
                    "execution_evidence_state": "replay_verified",
                    "contaminated": False,
                    "trainable": financial_label in {"profit", "loss"},
                },
                "execution_quality_evidence": replay_execution,
                "rule_label": _rule_baseline_label(features),
                "features": {name: _safe_float(features.get(name)) for name in OPEN_FEATURE_NAMES},
                "source": "historical_replay",
                "binding_hash": binding_hash,
                "feature_schema_version": "pit.v2.open_lineage",
            })
            for factor_id in selected_factor_ids:
                contribution, confidence = _factor_contribution(
                    signals.get(factor_id, factor_values.get(factor_id))
                )
                factor_rows.setdefault(factor_id, []).append({
                    "review_id": trade_id,
                    "trade_id": trade_id,
                    "position_id": trade_id,
                    "factor": factor_id,
                    "entry_contribution": contribution,
                    "hold_contribution": 0.0,
                    "exit_contribution": 0.0,
                    "net_contribution": contribution * (1.0 if pnl >= 0.0 else -1.0),
                    "confidence": confidence,
                    "entry_quality": 1.0 if pnl > 0.0 else 0.0,
                    "hold_quality": 1.0 if pnl > 0.0 else 0.0,
                    "exit_quality": 1.0 if pnl > 0.0 else 0.0,
                    "pnl": pnl,
                    "mae": 0.0,
                    "mfe": 0.0,
                    "outcome_label": "good_win" if pnl > 0.0 else "bad_loss",
                    "created_at": _safe_float(trade.get("exit_ts")),
                    "decision_index": decision_index,
                })
    except Exception as exc:
        blockers.append(f"learning_schema_build_error:{type(exc).__name__}")
        open_samples = []
        factor_rows = {}

    factor_candidates: list[dict[str, Any]] = []
    if factor_rows:
        from research.factor_governance_lightgbm import _current_row_label, _sample_from_row

        for factor_id, rows in sorted(factor_rows.items()):
            for index, row in enumerate(rows[:-1]):
                if index < 2:
                    continue
                sample = _sample_from_row(
                    row,
                    label=_current_row_label(rows[index + 1]),
                    label_source="next_same_factor_outcome_from_replay_history",
                    rolling_history=rows[max(0, index - 4):index + 1],
                )
                sample["sample_id"] = _sample_id(
                    binding_hash,
                    int(row.get("decision_index") or 0),
                    factor_id,
                )
                sample["source"] = "historical_replay"
                sample["binding_hash"] = binding_hash
                sample["feature_schema_version"] = "pit.v4.factor_regime_decision_lineage"
                factor_candidates.append(sample)

    trainable = not blockers and bool(eligible_independent)
    if not independent:
        blockers.append("no_closed_independent_trades")
        trainable = False
    elif not eligible_independent:
        blockers.append("all_trades_have_ambiguous_same_bar_exit")
        trainable = False
    usable_open_samples = open_samples if trainable else []
    usable_factor_samples = factor_candidates if trainable else []
    return {
        "schema_version": "parity_learning_bundle.v1",
        "source": "historical_replay",
        "causal_level": "simulated",
        "governance_eligible": False,
        "trainable": trainable,
        "blockers": list(dict.fromkeys(blockers)),
        "bindings": bindings,
        "feature_schemas": {
            "open": "pit.v2.open_lineage",
            "factor": "pit.v4.factor_regime_decision_lineage",
        },
        "independent_trade_count": len(independent),
        "excluded_trade_count": excluded_trade_count,
        "candidate_open_sample_count": len(open_samples),
        "candidate_factor_sample_count": len(factor_candidates),
        "open_sample_count": len(usable_open_samples),
        "factor_sample_count": len(usable_factor_samples),
        "open_samples": usable_open_samples,
        "factor_samples": usable_factor_samples,
    }


def _timeframe_seconds(timeframe: str) -> int:
    text = str(timeframe or "M15").strip().upper()
    if len(text) < 2:
        return 900
    multiplier = {"M": 60, "H": 3600, "D": 86400}.get(text[0])
    try:
        return max(1, int(text[1:])) * int(multiplier or 60)
    except (TypeError, ValueError):
        return 900


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, pd.Timestamp):
        return float(value.timestamp())
    try:
        return float(pd.Timestamp(value).timestamp())
    except Exception:
        return 0.0


def _finite_or_none(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    try:
        if pd.isna(value):
            return None
    except Exception:
        pass
    if hasattr(value, "item"):
        try:
            value = value.item()
        except Exception:
            pass
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _data_records(frame: pd.DataFrame) -> list[dict[str, Any]]:
    columns = sorted(str(column) for column in frame.columns)
    records: list[dict[str, Any]] = []
    for _, row in frame.iterrows():
        records.append({name: _finite_or_none(row[name]) for name in columns})
    return records


def _source_file_manifest(paths: list[str]) -> tuple[list[dict[str, Any]], list[str]]:
    """Record the monthly files that supplied the selected PIT row set.

    The selected rows themselves remain the authoritative data hash.  File
    metadata is supplemental provenance and deliberately avoids hashing an
    entire actively-growing current-month DuckDB file after the snapshot read.
    """

    manifest: list[dict[str, Any]] = []
    blockers: list[str] = []
    for raw in sorted({str(value) for value in paths if str(value)}):
        path = Path(raw)
        exists = path.is_file()
        valid_name = bool(re.fullmatch(r"bars_\d{4}_\d{2}\.duckdb", path.name))
        item: dict[str, Any] = {
            "path": str(path.resolve()) if exists else raw,
            "file_name": path.name,
            "exists": exists,
            "monthly_name_valid": valid_name,
            "size_bytes": 0,
            "mtime_ns": 0,
        }
        if exists:
            stat = path.stat()
            item["size_bytes"] = int(stat.st_size)
            item["mtime_ns"] = int(stat.st_mtime_ns)
        else:
            blockers.append(f"monthly_pit_source_file_missing:{raw}")
        if not valid_name:
            blockers.append(f"monthly_pit_source_file_name_invalid:{raw}")
        manifest.append(item)
    return manifest, blockers


def _normalize_bars(
    bars: pd.DataFrame,
    *,
    timeframe: str,
    as_of: float,
    max_bars: int,
) -> tuple[pd.DataFrame, list[str]]:
    if bars is None or bars.empty:
        return pd.DataFrame(), ["monthly_pit_bars_missing"]
    frame = bars.copy()
    if "time" not in frame.columns:
        if isinstance(frame.index, pd.DatetimeIndex):
            frame["time"] = frame.index
        else:
            return pd.DataFrame(), ["bar_time_missing"]
    missing_base = [name for name in _BASE_COLUMNS if name not in frame.columns]
    if missing_base:
        return pd.DataFrame(), [f"bar_columns_missing:{','.join(missing_base)}"]

    frame["time"] = frame["time"].map(_epoch)
    frame = frame[frame["time"] > 0].copy()
    frame = frame.sort_values("time").drop_duplicates(subset=["time"], keep="last")
    if "complete" in frame.columns:
        frame = frame[frame["complete"].fillna(False).astype(bool)]
    close_delay = _timeframe_seconds(timeframe)
    frame = frame[frame["time"] + close_delay <= float(as_of)]
    if max_bars > 0 and len(frame) > max_bars:
        frame = frame.tail(max_bars)
    frame = frame.reset_index(drop=True)
    blockers: list[str] = []
    if frame.empty:
        blockers.append("closed_bar_window_empty")
    if not frame["time"].is_monotonic_increasing or frame["time"].duplicated().any():
        blockers.append("bar_time_order_invalid")
    missing_quotes = [name for name in _BID_ASK_COLUMNS if name not in frame.columns]
    if missing_quotes and "spread" in frame.columns:
        spread = pd.to_numeric(frame["spread"], errors="coerce").fillna(0.0).clip(lower=0.0)
        for price_name in ("open", "high", "low", "close"):
            mid = pd.to_numeric(frame[price_name], errors="coerce")
            frame[f"bid_{price_name}"] = mid - spread / 2.0
            frame[f"ask_{price_name}"] = mid + spread / 2.0
        missing_quotes = []
        blockers.append("native_bid_ask_modeled_from_recorded_spread")
        if not bool((spread > 0.0).any()):
            blockers.append("recorded_spread_non_positive")
    if missing_quotes:
        blockers.append(f"native_bid_ask_missing:{','.join(missing_quotes)}")
    elif frame[list(_BID_ASK_COLUMNS)].isna().any().any():
        blockers.append("native_bid_ask_contains_null")
    return frame, blockers


@dataclass(frozen=True)
class ParityReplayRequest:
    symbol: str = "XAUUSD+"
    timeframe: str = "M5"
    start: str | float | None = None
    end: str | float | None = None
    as_of: float | None = None
    max_bars: int = 5000
    warmup_bars: int = 150
    initial_equity: float = 10_000.0
    volume_lots: float = 0.01
    contract_size: float = 100.0
    commission_per_lot_round_turn: float = 18.0
    slippage_price_each_fill: float = 0.035
    persist_artifact: bool = True
    expected_bindings: Mapping[str, str] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> "ParityReplayRequest":
        item = dict(value or {})
        return cls(
            symbol=str(item.get("symbol") or "XAUUSD+"),
            timeframe=str(item.get("timeframe") or "M5").upper(),
            start=item.get("start"),
            end=item.get("end"),
            as_of=_safe_float(item.get("as_of"), 0.0) or None,
            max_bars=max(2, min(int(item.get("max_bars") or 5000), 20_000)),
            warmup_bars=max(0, min(int(item.get("warmup_bars") or 150), 10_000)),
            initial_equity=max(0.01, _safe_float(item.get("initial_equity"), 10_000.0)),
            volume_lots=max(0.0001, _safe_float(item.get("volume_lots"), 0.01)),
            contract_size=max(0.0001, _safe_float(item.get("contract_size"), 100.0)),
            commission_per_lot_round_turn=max(
                0.0,
                _safe_float(item.get("commission_per_lot_round_turn"), 18.0),
            ),
            slippage_price_each_fill=max(
                0.0,
                _safe_float(item.get("slippage_price_each_fill"), 0.035),
            ),
            persist_artifact=bool(item.get("persist_artifact", True)),
            expected_bindings=dict(item.get("expected_bindings") or {}),
        )


class MonthlyPITBarLoader:
    """Read the selected immutable rows directly from monthly bar databases."""

    def __init__(self, monthly_dir: str | Path = DUCKDB_BARS_MONTHLY_DIR):
        self.monthly_dir = Path(monthly_dir)

    def load(self, request: ParityReplayRequest) -> tuple[pd.DataFrame, dict[str, Any]]:
        paths = sorted(self.monthly_dir.glob("bars_????_??.duckdb"))
        if not paths:
            return pd.DataFrame(), {
                "source": "monthly_pit_bars",
                "source_files": [],
                "error": "monthly_bar_files_missing",
            }
        start_ts = _epoch(request.start) if request.start is not None else 0.0
        end_ts = _epoch(request.end) if request.end is not None else 0.0
        query_paths = list(reversed(paths))
        used: set[str] = set()
        errors: list[str] = []

        def read_window(
            *,
            lower: float = 0.0,
            upper: float = 0.0,
            upper_exclusive: bool = False,
            limit: int,
        ) -> pd.DataFrame:
            frames: list[pd.DataFrame] = []
            remaining = max(0, int(limit))
            for path in query_paths:
                if remaining <= 0:
                    break
                try:
                    with duckdb_readonly_connection(path, snapshot_on_lock=True) as conn:
                        query = "SELECT * FROM bars WHERE symbol=? AND timeframe=?"
                        params: list[Any] = [request.symbol, request.timeframe]
                        if lower:
                            query += " AND time>=?"
                            params.append(lower)
                        if upper:
                            query += " AND time<?" if upper_exclusive else " AND time<=?"
                            params.append(upper)
                        query += " ORDER BY time DESC LIMIT ?"
                        params.append(remaining)
                        part = conn.execute(query, params).df()
                except Exception as exc:
                    errors.append(f"{path.name}:{type(exc).__name__}:{exc}")
                    continue
                if part.empty:
                    continue
                frames.append(part)
                used.add(str(path.resolve()))
                remaining -= len(part)
            if not frames:
                return pd.DataFrame()
            return (
                pd.concat(frames, ignore_index=True)
                .sort_values("time")
                .drop_duplicates(subset=["time"], keep="last")
                .tail(limit)
                .reset_index(drop=True)
            )

        target = read_window(
            lower=start_ts,
            upper=end_ts,
            limit=request.max_bars,
        )
        if target.empty:
            return pd.DataFrame(), {
                "source": "monthly_pit_bars",
                "source_files": sorted(used),
                "errors": errors,
                "error": "monthly_bar_window_empty",
            }
        target_start_ts = _safe_float(target.iloc[0].get("time"), 0.0)
        warmup = (
            read_window(
                upper=target_start_ts,
                upper_exclusive=True,
                limit=request.warmup_bars,
            )
            if request.warmup_bars > 0 and target_start_ts > 0
            else pd.DataFrame()
        )
        frame = pd.concat([warmup, target], ignore_index=True)
        return frame, {
            "source": "monthly_pit_bars",
            "source_files": sorted(used),
            "errors": errors,
            "target_start_ts": target_start_ts,
            "target_bar_count": len(target),
            "warmup_bar_count": len(warmup),
        }


def _portfolio_config(cfg: Any) -> dict[str, Any]:
    """Use the exact deterministic config projection used by live."""

    from backend.services.live_factor_wiring import merge_portfolio_configs

    return merge_portfolio_configs(
        getattr(cfg, "factor_signal_config", {}) or {},
        getattr(cfg, "factor_portfolio_weights", {}) or {},
        _safe_float(getattr(cfg, "factor_tactical_alpha", 0.7), 0.7),
        _safe_float(getattr(cfg, "factor_signal_threshold", 0.3), 0.3),
    )


def _selected_factor_artifact_manifest(
    cfg: Any,
    selected_factor_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Bind selected factor identities/artifacts, not only their display names.

    Built-in factors are bound by the checked-in code hash.  Discovered
    factors additionally carry the stable identity and artifact fields that
    the live runtime selector requires before admission.
    """

    signal_config = dict(getattr(cfg, "factor_signal_config", {}) or {})
    manifest: dict[str, dict[str, Any]] = {}
    for factor_id in sorted({str(value) for value in selected_factor_ids if str(value)}):
        raw = signal_config.get(factor_id)
        item = dict(raw) if isinstance(raw, Mapping) else {}
        manifest[factor_id] = {
            "source": str(item.get("source") or "builtin"),
            "factor_id": str(item.get("factor_id") or factor_id),
            "expression": str(item.get("expression") or ""),
            "definition_fingerprint": str(item.get("definition_fingerprint") or ""),
            "artifact_hash": str(item.get("artifact_hash") or ""),
            "lifecycle_status": str(item.get("lifecycle_status") or ""),
            "committed_mutation_id": str(item.get("committed_mutation_id") or ""),
            "enabled": item.get("enabled"),
            "weight": item.get("weight"),
            "binding_mode": (
                "declared_factor_artifact"
                if str(item.get("source") or "builtin").lower()
                in _DISCOVERED_FACTOR_SOURCES
                else "checked_in_code_bundle"
            ),
        }
    return manifest


def _runtime_selection_manifest(decision_provider: Any) -> dict[str, Any]:
    """Bind the selector output while keeping its historical authority explicit."""

    selection = getattr(decision_provider, "selection", None)
    excluded_factor_ids = list(getattr(selection, "excluded_factor_ids", []) or [])
    reason_excluded = dict(getattr(selection, "reason_excluded", {}) or {})
    reason_counts: dict[str, int] = {}
    for reason in reason_excluded.values():
        key = str(reason or "unknown")
        reason_counts[key] = reason_counts.get(key, 0) + 1
    return {
        "selected_factor_ids": list(getattr(selection, "selected_factor_ids", []) or []),
        "excluded_factor_count": len(excluded_factor_ids),
        "excluded_factor_ids_hash": _sha256_json(sorted(str(item) for item in excluded_factor_ids)),
        "reason_counts": reason_counts,
        "historical_projection_verified": False,
    }


def _factor_artifact_blockers(
    manifest: Mapping[str, Mapping[str, Any]],
) -> list[str]:
    """Fail closed when a selected generated factor lacks its stable identity."""

    blockers: list[str] = []
    for name, raw in sorted(manifest.items()):
        item = dict(raw or {})
        source = str(item.get("source") or "builtin").strip().lower()
        if source not in _DISCOVERED_FACTOR_SOURCES:
            continue
        factor_id = str(item.get("factor_id") or "").strip()
        expression = str(item.get("expression") or "").strip()
        definition = str(item.get("definition_fingerprint") or "").strip().lower()
        artifact = str(item.get("artifact_hash") or "").strip().lower()
        if not expression or not _SHA256_RE.fullmatch(definition):
            blockers.append(f"selected_factor_identity_missing:{name}")
        else:
            try:
                from alpha.factor_identity import (
                    canonical_factor_id,
                    factor_definition_fingerprint,
                )

                expected_fingerprint = factor_definition_fingerprint(expression)
                if canonical_factor_id(expression) != factor_id:
                    blockers.append(f"selected_factor_identity_mismatch:{name}")
                if expected_fingerprint != definition:
                    blockers.append(
                        f"selected_factor_definition_fingerprint_mismatch:{name}"
                    )
            except Exception:
                blockers.append(f"selected_factor_identity_invalid:{name}")
        if not _SHA256_RE.fullmatch(artifact):
            blockers.append(f"selected_factor_artifact_missing:{name}")
        if str(item.get("lifecycle_status") or "").upper() != "ACTIVE":
            blockers.append(f"selected_factor_not_active:{name}")
        if not str(item.get("committed_mutation_id") or "").strip():
            blockers.append(f"selected_factor_committed_mutation_missing:{name}")
        if item.get("enabled") is not True:
            blockers.append(f"selected_factor_explicit_enable_missing:{name}")
        try:
            positive_weight = float(item.get("weight")) > 0.0
        except (TypeError, ValueError):
            positive_weight = False
        if not positive_weight:
            blockers.append(f"selected_factor_explicit_weight_missing:{name}")
    return blockers


class LiveComponentDecisionAdapter:
    """Reuse the live factor decision components without broker side effects."""

    def __init__(self, cfg: Any, *, max_buffer: int):
        from alpha.execution_gate import ExecutionGate
        from alpha.portfolio_compositor import PortfolioCompositor
        from alpha.runtime_factor_selection import select_runtime_factors
        from alpha.signal_normalizer import SignalNormalizer
        from alpha.streaming_factor_engine import StreamingFactorEngine
        from backend.services.live_loop_shell import execution_gate_config
        from data.factor_frame import FactorFrameBuilder

        signal_config = dict(getattr(cfg, "factor_signal_config", {}) or {})
        self.selection = select_runtime_factors(signal_config)
        factor_ids = list(self.selection.selected_factor_ids) if self.selection is not None else None
        self.engine = StreamingFactorEngine(
            max_buffer=max(200, int(max_buffer)),
            factor_runtime_config=signal_config,
            factor_frame_builder=FactorFrameBuilder(cache_ttl_sec=0.0),
            factor_ids=factor_ids,
        )
        self.normalizer = SignalNormalizer(signal_config)
        self.compositor = PortfolioCompositor(_portfolio_config(cfg))
        self.gate = ExecutionGate(execution_gate_config(cfg))
        self.cfg = cfg
        self.last_factor_values: dict[str, Any] = {}
        self.last_composite: dict[str, Any] = {}
        self.last_atr_price = 0.0
        self.last_conviction = 0.0
        self._prepared_snapshots: list[dict[str, Any]] | None = None

    def prepare(self, bars: pd.DataFrame) -> None:
        records = [
            {str(key): _finite_or_none(value) for key, value in row.items()}
            for row in bars.to_dict(orient="records")
        ]
        snapshots = self.engine.warmup_bars(records)
        if snapshots:
            minimum = int(getattr(self.engine, "MIN_BARS", 50) or 50)
            self._prepared_snapshots = [
                dict(values) if index >= minimum - 1 else {}
                for index, values in enumerate(snapshots)
            ]
        else:
            self._prepared_snapshots = [{} for _ in records]

    def release(self) -> None:
        """Drop replay-only buffers as soon as the report has been assembled."""

        self._prepared_snapshots = None
        self.last_factor_values.clear()
        self.last_composite.clear()
        self.engine.reset()

    def __call__(
        self,
        history: pd.DataFrame,
        bar: Mapping[str, Any],
        index: int,
    ) -> Mapping[str, Any] | None:
        del history
        from backend.services.live_decision_pipeline import run_live_decision_pipeline

        prepared = (
            self._prepared_snapshots[index]
            if self._prepared_snapshots is not None and index < len(self._prepared_snapshots)
            else None
        )
        decision = run_live_decision_pipeline(
            engine=self.engine,
            normalizer=self.normalizer,
            compositor=self.compositor,
            gate=self.gate,
            bar=dict(bar),
            cfg=self.cfg,
            factor_values_override=prepared,
        )
        self.last_factor_values = dict(decision.factor_values or {})
        composite_payload = _to_dict(decision.composite)
        self.last_composite = composite_payload
        close = _safe_float(bar.get("close"), 0.0)
        atr_ratio = _safe_float(self.last_factor_values.get("atr_ratio"), 0.0)
        self.last_atr_price = (
            atr_ratio * close if atr_ratio > 0.0 and close > 0.0 else 0.0
        )
        self.last_conviction = abs(
            _safe_float(getattr(decision.composite, "score", 0.0), 0.0)
        )
        if not decision.ready:
            return None
        composite = decision.composite
        gate = decision.gate_result
        direction = int(getattr(composite, "direction", 0) or 0)
        if not bool(getattr(gate, "passed", False)) or direction == 0:
            return None
        factor_values = dict(self.last_factor_values)
        atr_price = atr_ratio * close if atr_ratio > 0 and close > 0 else close * 0.001
        return {
            "direction": direction,
            "score": _safe_float(getattr(composite, "score", 0.0), 0.0),
            "atr_price": atr_price,
            "sl_distance": atr_price * _safe_float(getattr(self.cfg, "risk_sl_atr", 1.5), 1.5),
            "tp_distance": atr_price * _safe_float(getattr(self.cfg, "risk_tp_atr", 2.5), 2.5),
            "gate_reason": str(getattr(gate, "reason", "")),
            "factor_values": factor_values,
            "signals": dict(decision.signals or {}),
            "context_policy": dict(decision.context_policy or {}),
            "composite": composite_payload,
        }


def _default_risk_evaluator(cfg: Any, request: ParityReplayRequest) -> RiskEvaluator:
    def evaluate(context: Mapping[str, Any]) -> Mapping[str, Any]:
        from backend.risk.metrics_snapshot import (
            attach_internal_forward_var_input,
            build_risk_metrics_snapshot,
            freeze_closed_bar_returns,
        )
        from backend.services.live_position_lifecycle import (
            build_entry_cluster_context,
            build_open_trade_risk_context_payload,
            temporal_context_for_trade,
        )
        from risk.policy_service import RiskPolicyService

        replay_context = dict(context)
        candidate = _to_dict(replay_context.get("candidate"))
        direction = int(_safe_float(replay_context.get("direction"), 0.0))
        decision_ts = _safe_float(replay_context.get("decision_ts"), 0.0)
        current_price = _safe_float(replay_context.get("current_price"), 0.0)
        atr_price = _safe_float(replay_context.get("atr_price"), 0.0)
        if atr_price <= 0.0:
            atr_price = _safe_float(candidate.get("atr_price"), 0.0)
        if atr_price <= 0.0 and current_price > 0.0:
            factor_values = _to_dict(candidate.get("factor_values"))
            atr_ratio = _safe_float(factor_values.get("atr_ratio"), 0.0)
            atr_price = atr_ratio * current_price if atr_ratio > 0.0 else current_price * 0.001

        requested_api_volume = request.volume_lots * 10_000.0
        session_state = _to_dict(replay_context.get("session_state"))
        account = _to_dict(replay_context.get("account")) or {
            "balance": request.initial_equity,
            "equity": request.initial_equity,
        }
        var_lookback = max(
            2,
            int(getattr(cfg, "var_window", 500) or 500),
        )
        frozen_var_input = freeze_closed_bar_returns(
            list(replay_context.get("closed_bar_prices") or []),
            timestamps=list(
                replay_context.get("closed_bar_timestamps") or []
            ),
            symbol=str(replay_context.get("symbol") or request.symbol),
            timeframe=str(
                replay_context.get("timeframe") or request.timeframe
            ),
            as_of=decision_ts,
            lookback=var_lookback,
        )
        risk_snapshot_payload = build_risk_metrics_snapshot(
            forward_var_input=frozen_var_input,
            clean_trade_pnls=list(
                replay_context.get("clean_trade_pnls") or []
            ),
            positions=[],
            account=account,
            account_reconcile_id="parity-replay-account",
            positions_reconcile_id="parity-replay-positions",
            as_of=decision_ts,
            kelly_min_closed_trades=int(
                getattr(cfg, "kelly_min_closed_trades", 20) or 20
            ),
            kelly_multiplier=float(
                getattr(cfg, "kelly_fraction", 0.5) or 0.5
            ),
            kelly_max_fraction=float(
                getattr(cfg, "kelly_max_pct", 0.25) or 0.25
            ),
            var_confidence=float(
                getattr(cfg, "var_alpha", 0.95) or 0.95
            ),
            var_lookback=var_lookback,
        ).to_dict()
        replay_risk_snapshot = attach_internal_forward_var_input(
            {
                **risk_snapshot_payload["components"],
                "snapshot": risk_snapshot_payload,
                "state": "reconstructed",
                "source": "parity_replay",
                "replay_read_only": True,
            },
            frozen_var_input,
        )
        decision_freshness = {
            "schema_version": "decision_bar_freshness.v1",
            "fresh": True,
            "age_seconds": 0.0,
            "bar_ts": decision_ts,
            "timeframe_seconds": _timeframe_seconds(request.timeframe),
        }
        entry_cluster = build_entry_cluster_context(
            positions_before=[],
            direction=direction,
            symbol=str(replay_context.get("symbol") or request.symbol),
            now_ts=decision_ts,
            new_position_id=0,
            new_api_volume=0.0,
        )
        payload = build_open_trade_risk_context_payload(
            cfg=cfg,
            acct=account,
            positions=[],
            requested_api_volume=requested_api_volume,
            signal_score=_safe_float(candidate.get("score"), 0.0),
            symbol=str(replay_context.get("symbol") or request.symbol),
            direction=direction,
            current_price=current_price,
            atr_price=atr_price,
            risk_snapshot=replay_risk_snapshot,
            session_state=session_state,
            total_api_volume=0.0,
            event_sizing_context={"enabled": False, "multiplier": 1.0},
            event_filter_context={},
            event_window_learning_policy={},
            entry_quality_gate={},
            entry_cluster_context=entry_cluster,
            entry_cluster_learning_policy={},
            same_direction_cooldown_seconds=max(
                60.0,
                float(int(getattr(cfg, "risk_cooldown_bars", 3) or 3))
                * float(_timeframe_seconds(request.timeframe)),
            ),
            max_abs_entry_score=0.0,
            loop_running=True,
            bridge_connected=True,
            data_lag_seconds=0.0,
            runtime_health={
                "schema_version": "runtime_health_snapshot.v1",
                "state": "reconstructed",
                "source": "parity_replay",
                "replay_read_only": True,
            },
            temporal_context=temporal_context_for_trade(
                decision_ts=decision_ts,
                timeframe=request.timeframe,
                evaluated_at_ts=decision_ts,
                session_last_trade_ts=_safe_float(
                    replay_context.get("session_last_trade_ts"),
                    0.0,
                ),
            ),
            decision_freshness=decision_freshness,
            supervisor_reentry_block={},
        )
        payload.update({
            "autonomy_mode": str(getattr(cfg, "autonomy_mode", "manual") or "manual"),
            "runtime_incident_mode": str(getattr(cfg, "runtime_incident_mode", "normal") or "normal"),
            "replay_read_only": True,
            "historical_context": "reconstructed",
            "replay_context": {
                "decision_bar_index": replay_context.get("decision_bar_index"),
                "timeframe": str(replay_context.get("timeframe") or request.timeframe),
            },
        })
        return _to_dict(RiskPolicyService.shared().evaluate("open_trade", payload))

    return evaluate


def _default_supervisor_evaluator(context: Mapping[str, Any]) -> Mapping[str, Any]:
    from backend.services.position_supervisor import evaluate_position_supervisor

    return evaluate_position_supervisor(dict(context))


def _component_contract(
    *,
    decision_provider_is_live: bool,
    risk_evaluator_is_live: bool,
    supervisor_evaluator_is_live: bool,
) -> dict[str, Any]:
    return {
        "factor_frame": {
            "implementation": "data.factor_frame.FactorFrameBuilder",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": decision_provider_is_live,
        },
        "runtime_selector": {
            "implementation": "alpha.runtime_factor_selection.select_runtime_factors",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": False,
            "reason": (
                "historical_runtime_factor_projection_ack_health_and_registry_generation_"
                "are_unavailable"
            ),
        },
        "streaming_factor_engine": {
            "implementation": "alpha.streaming_factor_engine.StreamingFactorEngine",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": decision_provider_is_live,
        },
        "normalizer": {
            "implementation": "alpha.signal_normalizer.SignalNormalizer",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": decision_provider_is_live,
        },
        "compositor": {
            "implementation": "alpha.portfolio_compositor.PortfolioCompositor",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": decision_provider_is_live,
        },
        "execution_gate": {
            "implementation": "alpha.execution_gate.ExecutionGate",
            "reuse": "exact" if decision_provider_is_live else "fixture",
            "verified": decision_provider_is_live,
        },
        "risk_policy": {
            "implementation": "risk.policy_service.RiskPolicyService.evaluate",
            "reuse": "exact" if risk_evaluator_is_live else "fixture",
            "verified": False,
            "reason": "historical_account_session_and_runtime_health_context_is_reconstructed",
        },
        "position_path_metrics": {
            "implementation": "backend.services.position_metrics.update_position_path_metrics",
            "reuse": "exact",
            "verified": False,
            "reason": "historical_broker_unrealized_pnl_observations_are_reconstructed_from_bars",
        },
        "safety_arbitration": {
            "implementation": "backend.services.live_safety_planner.plan_live_safety_candidates",
            "reuse": "exact",
            "verified": False,
            "reason": "historical_five_second_safety_cadence_and_awe_conviction_are_unavailable",
        },
        "supervisor": {
            "implementation": "backend.services.position_supervisor.evaluate_position_supervisor",
            "reuse": "exact" if supervisor_evaluator_is_live else "fixture",
            "verified": False,
            "reason": "historical_supervisor_context_and_regime_lineage_are_reconstructed",
        },
        "trailing": {
            "implementation": "backend.services.live_position_lifecycle.build_legacy_awe_trailing_update",
            "reuse": "exact",
            "verified": False,
            "reason": "historical_awe_conviction_and_tick_observation_path_are_unavailable",
        },
        "protection_planner": {
            "implementation": (
                "backend.services.live_position_lifecycle."
                "build_supervisor_tighten_execution_plan"
            ),
            "reuse": "exact",
            "verified": False,
            "reason": "broker_amend_acceptance_and_fresh_projection_ack_are_unavailable",
        },
        "cost_model": {
            "implementation": "backend.services.parity_replay.ParityReplayRunner._close_position",
            "reuse": "modeled",
            "verified": False,
            "reason": "historical_broker_deal_commission_and_swap_receipts_are_unavailable",
        },
        "lifecycle": {
            "implementation": "backend.services.parity_replay.ParityReplayRunner",
            "reuse": "modeled",
            "verified": False,
            "reason": (
                "broker_order_receipt_position_reconcile_partial_fill_and_intrabar_tick_path_"
                "are_unavailable"
            ),
        },
    }


class ParityReplayRunner:
    """Run a deterministic, one-position causal replay over explicit bars."""

    def __init__(
        self,
        *,
        request: ParityReplayRequest,
        config: Any,
        config_snapshot: Mapping[str, Any],
        decision_provider: DecisionProvider | None = None,
        risk_evaluator: RiskEvaluator | None = None,
        supervisor_evaluator: SupervisorEvaluator | None = None,
        progress_cb: Callable[[str, float, str], None] | None = None,
    ):
        self.request = request
        self.config = config
        self.config_snapshot = dict(config_snapshot or {})
        self._decision_provider_is_live = decision_provider is None
        self._risk_evaluator_is_live = risk_evaluator is None
        self._supervisor_evaluator_is_live = supervisor_evaluator is None
        self.decision_provider = decision_provider
        self.risk_evaluator = risk_evaluator or _default_risk_evaluator(config, request)
        self.supervisor_evaluator = supervisor_evaluator or _default_supervisor_evaluator
        self.progress_cb = progress_cb
        self._adapter_error = ""
        self._path_state: dict[int, dict[str, Any]] = {}
        self._supervisor_state: dict[int, dict[str, Any]] = {}
        self._trailing_state: dict[int, dict[str, Any]] = {}
        self._latest_atr_price = 0.0
        self._latest_conviction = 0.0
        self._latest_regime = ""
        self._realized_net_pnl = 0.0
        if self.decision_provider is None:
            try:
                self.decision_provider = LiveComponentDecisionAdapter(
                    config,
                    max_buffer=max(request.warmup_bars, 200),
                )
            except Exception as exc:
                self._adapter_error = f"{type(exc).__name__}:{exc}"
                self._decision_provider_is_live = False
                self.decision_provider = lambda *_args: None

    def run(
        self,
        bars: pd.DataFrame,
        *,
        data_source: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        as_of = float(self.request.as_of or time.time())
        frame, input_blockers = _normalize_bars(
            bars,
            timeframe=self.request.timeframe,
            as_of=as_of,
            max_bars=self.request.max_bars + self.request.warmup_bars,
        )
        if self.request.end is not None and _epoch(self.request.end) > as_of:
            input_blockers.append("future_data_requested")
        if bars is not None and not bars.empty:
            raw_incomplete = (
                "complete" in bars.columns
                and not bool(bars["complete"].fillna(False).astype(bool).all())
            )
            raw_open_bar = (
                "time" in bars.columns
                and any(
                    _epoch(value) + _timeframe_seconds(self.request.timeframe) > as_of
                    for value in bars["time"].tolist()
                )
            )
            if raw_incomplete or raw_open_bar:
                input_blockers.append("unclosed_bar_present_in_requested_window")
        config_payload = (
            self.config.to_dict()
            if hasattr(self.config, "to_dict")
            else dict(self.config) if isinstance(self.config, Mapping) else {}
        )
        config_hash = _runtime_config_hash(config_payload)
        data_hash = _sha256_json(_data_records(frame)) if not frame.empty else _sha256_json([])
        code_hash = _sha256_files(_CODE_BINDING_PATHS)
        missing_code_paths = _missing_code_binding_paths(_CODE_BINDING_PATHS)
        if missing_code_paths:
            input_blockers.extend(
                f"code_binding_path_missing:{path}" for path in missing_code_paths
            )
        source_metadata = dict(data_source or {})
        target_start_ts = _safe_float(source_metadata.get("target_start_ts"), 0.0)
        decision_start_index = (
            int(frame["time"].searchsorted(target_start_ts, side="left"))
            if target_start_ts > 0.0 and not frame.empty
            else 0
        )
        if isinstance(self.decision_provider, LiveComponentDecisionAdapter) and not frame.empty:
            try:
                self.decision_provider.prepare(frame)
            except Exception as exc:
                input_blockers.append(
                    f"live_component_prepare_error:{type(exc).__name__}:{exc}"
                )
        source_name = str(source_metadata.get("source") or "")
        raw_source_files = source_metadata.get("source_files") or []
        source_files = (
            list(raw_source_files)
            if isinstance(raw_source_files, (list, tuple))
            else [str(raw_source_files)]
        )
        raw_source_errors = source_metadata.get("errors") or []
        source_errors = (
            list(raw_source_errors)
            if isinstance(raw_source_errors, (list, tuple))
            else [str(raw_source_errors)]
        )
        if source_name != "monthly_pit_bars":
            input_blockers.append("monthly_pit_data_source_unverified")
        if not source_files:
            input_blockers.append("monthly_pit_source_files_missing")
        if source_metadata.get("error"):
            input_blockers.append(
                f"monthly_pit_source_error:{source_metadata.get('error')}"
            )
        if source_errors:
            input_blockers.append("monthly_pit_source_partial_read_error")
        source_file_manifest, source_file_blockers = _source_file_manifest(source_files)
        input_blockers.extend(source_file_blockers)
        selected_factor_ids = list(
            getattr(getattr(self.decision_provider, "selection", None), "selected_factor_ids", [])
            or []
        )
        selected_factor_artifacts = _selected_factor_artifact_manifest(
            self.config,
            selected_factor_ids,
        )
        runtime_selection = _runtime_selection_manifest(self.decision_provider)
        input_blockers.extend(_factor_artifact_blockers(selected_factor_artifacts))
        components = _component_contract(
            decision_provider_is_live=self._decision_provider_is_live,
            risk_evaluator_is_live=self._risk_evaluator_is_live,
            supervisor_evaluator_is_live=self._supervisor_evaluator_is_live,
        )
        component_code_artifacts = _code_artifact_manifest(_CODE_BINDING_PATHS)
        artifact_manifest = {
            "selected_factor_ids": selected_factor_ids,
            "runtime_selection": runtime_selection,
            "selected_factor_artifacts": selected_factor_artifacts,
            "component_implementations": {
                name: item.get("implementation") for name, item in components.items()
            },
            "component_code_artifacts": component_code_artifacts,
            "config_snapshot_version": int(self.config_snapshot.get("config_version") or 0),
            "code_hash": code_hash,
        }
        artifact_hash = _sha256_json(artifact_manifest)
        bindings = {
            "config_hash": config_hash,
            "data_hash": data_hash,
            "code_hash": code_hash,
            "artifact_hash": artifact_hash,
        }
        bindings["binding_hash"] = _sha256_json(bindings)

        binding_mismatches = [
            name
            for name, expected in dict(self.request.expected_bindings or {}).items()
            if name not in bindings
            or str(expected or "").lower() != str(bindings[name]).lower()
        ]
        expected_bindings = dict(self.request.expected_bindings or {})
        # The task owns the binding freeze.  Callers may optionally provide a
        # previous binding to reproduce a run, but are not required to build it.
        missing_expected_bindings: list[str] = []
        committed_hash = str(self.config_snapshot.get("config_hash") or "")
        committed_version = int(self.config_snapshot.get("config_version") or 0)
        if not committed_hash:
            input_blockers.append("committed_runtime_config_snapshot_missing")
        elif committed_hash != config_hash:
            input_blockers.append("runtime_config_snapshot_hash_mismatch")
        if committed_version <= 0:
            input_blockers.append("committed_runtime_config_version_missing")
        if self._adapter_error:
            input_blockers.append(f"live_component_adapter_error:{self._adapter_error}")
        if binding_mismatches:
            input_blockers.extend(f"binding_mismatch:{name}" for name in binding_mismatches)

        native_bid_ask = not any(
            blocker.startswith("native_bid_ask_") for blocker in input_blockers
        )
        executable_bid_ask = not any(
            blocker.startswith("native_bid_ask_missing")
            or blocker.startswith("native_bid_ask_contains_null")
            for blocker in input_blockers
        )
        if binding_mismatches or frame.empty:
            simulation = self._empty_simulation()
        else:
            simulation = self._simulate(
                frame,
                native_bid_ask=executable_bid_ask,
                decision_start_index=decision_start_index,
            )

        causality_violations = list(simulation["causality_violations"])
        closed_bar_only = not any(
            blocker in {"bar_time_order_invalid", "closed_bar_window_empty"}
            for blocker in input_blockers
        )
        causality = {
            "schema_version": "parity_replay_causality.v1",
            "closed_bar_only": closed_bar_only,
            "next_bar_execution": not any(
                reason == "entry_not_strictly_after_decision_bar"
                for reason in causality_violations
            ),
            "native_bid_ask": native_bid_ask,
            "executable_bid_ask": executable_bid_ask,
            "quote_model": (
                "native_bid_ask"
                if native_bid_ask
                else "mid_only_with_modeled_slippage"
                if "recorded_spread_non_positive" in input_blockers
                else "recorded_spread_around_ohlc_mid"
                if executable_bid_ask
                else "unavailable"
            ),
            "decision_history_boundary": "history.iloc[:bar_index+1]",
            "violations": causality_violations,
        }

        blockers = list(dict.fromkeys(input_blockers + causality_violations))
        for name, component in components.items():
            if component.get("reuse") != "exact" or not component.get("verified"):
                blockers.append(f"component_{name}_not_live_exact")
                if component.get("reason"):
                    blockers.append(str(component["reason"]))
        blockers = list(dict.fromkeys(blockers))
        live_parity = not blockers
        evidence_class = PARITY_REPLAY_EVIDENCE_CLASS if live_parity else "diagnostic_only"
        report = {
            "schema_version": PARITY_REPLAY_SCHEMA_VERSION,
            "contract": PARITY_REPLAY_CONTRACT_VERSION,
            "replay_run_id": f"parity_{uuid.uuid4().hex[:16]}",
            "engine": PARITY_REPLAY_ENGINE,
            "evidence_class": evidence_class,
            "live_parity": live_parity,
            # Replay evidence never self-authorizes governance.  A future
            # certification service may promote a fully exact report.
            "governance_eligible": False,
            "deployable_candidate": False,
            "status": (
                "failed_binding"
                if binding_mismatches
                else "parity_verified" if live_parity else "diagnostic_only"
            ),
            "bindings": bindings,
            "artifact_manifest": artifact_manifest,
            "code_binding": {
                "paths": list(_CODE_BINDING_PATHS),
                "missing_paths": missing_code_paths,
                "artifacts": component_code_artifacts,
            },
            "binding_verification": {
                "expected": expected_bindings,
                "required_expected_names": list(_BASE_BINDING_NAMES),
                "missing_expected": missing_expected_bindings,
                "mismatches": binding_mismatches,
                "verified": not binding_mismatches and not missing_expected_bindings,
            },
            "config_snapshot": {
                "config_version": int(self.config_snapshot.get("config_version") or 0),
                "committed_config_hash": committed_hash,
                "effective_config_hash": config_hash,
                "source": str(self.config_snapshot.get("source") or ""),
            },
            "data_source": {
                **source_metadata,
                "source": source_name or "unknown",
                "source_file_manifest": source_file_manifest,
                "bar_count": max(0, len(frame) - decision_start_index),
                "loaded_bar_count": len(frame),
                "warmup_bar_count": decision_start_index,
                "first_bar_ts": _safe_float(frame.iloc[0]["time"]) if not frame.empty else 0.0,
                "last_bar_ts": _safe_float(frame.iloc[-1]["time"]) if not frame.empty else 0.0,
                "point_in_time": bool(
                    source_name == "monthly_pit_bars"
                    and source_files
                    and not source_file_blockers
                    and not source_metadata.get("error")
                    and not source_errors
                ),
            },
            "components": components,
            "causality": causality,
            "execution_model": {
                "entry": "next_bar_ask_open_for_long_bid_open_for_short",
                "exit": "executable_side_bid_for_long_ask_for_short",
                "spread": (
                    "native_bid_ask_embedded"
                    if native_bid_ask
                    else "unavailable_recorded_spread_with_modeled_slippage"
                    if "recorded_spread_non_positive" in input_blockers
                    else "recorded_spread_around_ohlc_mid"
                ),
                "commission_per_lot_round_turn": self.request.commission_per_lot_round_turn,
                "slippage_price_each_fill": self.request.slippage_price_each_fill,
                "same_bar_sl_tp_policy": "pessimistic_stop_first_and_flagged",
            },
            "lifecycle_contract": {
                "sequence": [
                    "closed_bar_decision",
                    "risk_policy_read_only",
                    "next_bar_entry_fill",
                    "protective_sl_tp",
                    "live_position_path_metrics",
                    "live_safety_candidate_arbitration",
                    "supervisor_or_trailing_protection_plan",
                    "next_bar_modeled_close_or_reduce",
                ],
                "shared_primitives": [
                    "update_position_path_metrics",
                    "build_close_position_risk_context_payload",
                    "plan_live_safety_candidates",
                    "evaluate_position_supervisor",
                    "build_legacy_awe_trailing_update",
                    "build_supervisor_tighten_execution_plan",
                ],
                "end_of_window_open_positions": "left_open_no_synthetic_fill",
            },
            "metrics": simulation["metrics"],
            "trades": simulation["trades"],
            "events": simulation["events"],
            "diagnostic_reasons": blockers,
            "created_at": time.time(),
            "artifact_path": "",
            "report_artifact_hash": "",
        }
        report["learning_bundle"] = _build_learning_bundle(report)
        return report

    def _empty_simulation(self) -> dict[str, Any]:
        return {
            "metrics": {
                "bar_count": 0,
                "decision_count": 0,
                "trade_count": 0,
                "gross_pnl": 0.0,
                "net_pnl": 0.0,
                "total_cost": 0.0,
                "commission_cost": 0.0,
                "spread_cost": 0.0,
                "slippage_cost": 0.0,
                "legacy_governance_candidate_count": 0,
            },
            "trades": [],
            "events": [],
            "causality_violations": [],
        }

    def _simulate(
        self,
        frame: pd.DataFrame,
        *,
        native_bid_ask: bool,
        decision_start_index: int = 0,
    ) -> dict[str, Any]:
        self._path_state.clear()
        self._supervisor_state.clear()
        self._trailing_state.clear()
        self._latest_atr_price = 0.0
        self._latest_conviction = 0.0
        self._latest_regime = ""
        self._realized_net_pnl = 0.0
        events: list[dict[str, Any]] = []
        trades: list[dict[str, Any]] = []
        violations: list[str] = []
        pending_open: dict[str, Any] | None = None
        pending_exit: dict[str, Any] | None = None
        position: dict[str, Any] | None = None
        decision_count = 0

        for index in range(len(frame)):
            if self.progress_cb is not None and (
                index == 0 or index % max(25, len(frame) // 100) == 0
            ):
                self.progress_cb(
                    "replaying",
                    10.0 + (80.0 * index / max(len(frame), 1)),
                    f"回放 {index}/{len(frame)} 根K线",
                )
            row = {str(key): _finite_or_none(value) for key, value in frame.iloc[index].to_dict().items()}
            ts = _safe_float(row.get("time"), 0.0)

            if pending_exit is not None and position is not None:
                if native_bid_ask:
                    fraction = max(0.0, min(1.0, _safe_float(pending_exit.get("fraction"), 1.0)))
                    self._close_position(
                        position,
                        row=row,
                        index=index,
                        ts=ts,
                        reason=str(pending_exit.get("reason") or "supervisor_close"),
                        fraction=fraction,
                        price_kind="open",
                        trades=trades,
                        events=events,
                    )
                    if position.get("remaining_fraction", 0.0) <= 1e-12:
                        position = None
                else:
                    events.append({
                        "event": "supervisor_exit_blocked",
                        "bar_index": index,
                        "event_ts": ts,
                        "reason": "native_bid_ask_missing",
                    })
                pending_exit = None

            if pending_open is not None and position is None:
                if index <= int(pending_open["decision_index"]):
                    violations.append("entry_not_strictly_after_decision_bar")
                if not native_bid_ask:
                    events.append({
                        "event": "entry_blocked",
                        "bar_index": index,
                        "event_ts": ts,
                        "reason": "native_bid_ask_missing",
                        "decision_ts": pending_open["decision_ts"],
                    })
                else:
                    position = self._open_position(pending_open, row=row, index=index, ts=ts)
                    events.append({
                        "event": "opened",
                        "bar_index": index,
                        "event_ts": ts,
                        "decision_index": pending_open["decision_index"],
                        "decision_ts": pending_open["decision_ts"],
                        "direction": position["direction"],
                        "entry_price": position["entry_price"],
                        "raw_entry_price": position["raw_entry_price"],
                    })
                pending_open = None

            if position is not None and native_bid_ask:
                exit_reason, raw_exit, ambiguous = self._protective_exit(position, row)
                if ambiguous:
                    violations.append("same_bar_sl_tp_path_ambiguous")
                    position["same_bar_sl_tp_path_ambiguous"] = True
                if exit_reason:
                    self._close_position(
                        position,
                        row=row,
                        index=index,
                        ts=ts,
                        reason=exit_reason,
                        fraction=1.0,
                        price_kind="explicit",
                        explicit_raw_price=raw_exit,
                        trades=trades,
                        events=events,
                    )
                    position = None

            if position is not None:
                try:
                    safety_plan = self._plan_position_safety(
                        position,
                        row=row,
                        index=index,
                        ts=ts,
                    )
                except Exception as exc:
                    violations.append("live_safety_planner_execution_error")
                    events.append({
                        "event": "safety_planner_error",
                        "bar_index": index,
                        "event_ts": ts,
                        "error": f"{type(exc).__name__}:{exc}",
                    })
                    safety_plan = None
                if safety_plan is not None:
                    events.append({
                        "event": "live_safety_plan",
                        "bar_index": index,
                        "event_ts": ts,
                        "candidates": [
                            {
                                "action": item.action,
                                "position_id": item.position_id,
                                "reason": item.reason,
                                "controls": dict(item.controls or {}),
                                "fingerprint": item.fingerprint,
                            }
                            for item in safety_plan.candidates
                        ],
                        "arbitration": [dict(item) for item in safety_plan.arbitration],
                    })
                    for safety_candidate in safety_plan.candidates:
                        action = str(safety_candidate.action or "").lower()
                        risk = self._evaluate_reduction_risk(
                            safety_candidate,
                            position=position,
                            ts=ts,
                        )
                        events.append({
                            "event": "safety_risk_policy_evaluated",
                            "bar_index": index,
                            "event_ts": ts,
                            "action": action,
                            "allowed": bool(risk.get("allowed", False)),
                            "reason": str(risk.get("reason") or ""),
                        })
                        if not bool(risk.get("allowed", False)):
                            continue
                        if action in {"timeout", "close", "reduce"}:
                            controls = dict(safety_candidate.controls or {})
                            pending_exit = {
                                "fraction": (
                                    _safe_float(controls.get("reduce_fraction"), 0.5)
                                    if action == "reduce"
                                    else 1.0
                                ),
                                "reason": str(safety_candidate.reason or action),
                            }
                        elif action in {"tighten", "trailing", "repair_entry_protection"}:
                            self._apply_protection_candidate(
                                safety_candidate,
                                position=position,
                                row=row,
                                index=index,
                                ts=ts,
                                events=events,
                            )

            history = frame.iloc[: index + 1]
            try:
                candidate = _to_dict(self.decision_provider(history, row, index))
            except Exception as exc:
                events.append({
                    "event": "decision_error",
                    "bar_index": index,
                    "event_ts": ts,
                    "error": f"{type(exc).__name__}:{exc}",
                })
                candidate = {}
            self._remember_live_decision_state(candidate, row=row)
            if index < decision_start_index:
                continue
            direction = int(_safe_float(candidate.get("direction"), 0.0))
            if direction not in {-1, 1}:
                continue
            decision_count += 1
            events.append({
                "event": "closed_bar_decision",
                "bar_index": index,
                "event_ts": ts,
                "direction": direction,
                "history_last_ts": _safe_float(history.iloc[-1]["time"]),
            })
            if position is not None or pending_open is not None:
                events.append({
                    "event": "decision_blocked",
                    "bar_index": index,
                    "event_ts": ts,
                    "reason": "position_or_open_pending",
                })
                continue
            risk_day = datetime.fromtimestamp(ts, tz=timezone.utc).date()
            daily_trades = [
                item
                for item in trades
                if datetime.fromtimestamp(
                    _safe_float(item.get("exit_ts"), 0.0),
                    tz=timezone.utc,
                ).date()
                == risk_day
            ]
            daily_independent = _aggregate_independent_trades(daily_trades)
            session_pnl = sum(
                _safe_float(item.get("net_pnl"), 0.0) for item in daily_trades
            )
            consecutive_losses = 0
            for trade in reversed(daily_independent):
                if _safe_float(trade.get("net_pnl"), 0.0) >= 0.0:
                    break
                consecutive_losses += 1
            session_last_trade_ts = max(
                (
                    _safe_float(item.get("exit_ts"), 0.0)
                    for item in daily_trades
                ),
                default=0.0,
            )
            replay_balance = self.request.initial_equity
            replay_peak = self.request.initial_equity
            max_drawdown_pct = 0.0
            for trade in trades:
                replay_balance += _safe_float(trade.get("net_pnl"), 0.0)
                replay_peak = max(replay_peak, replay_balance)
                if replay_peak > 0.0:
                    max_drawdown_pct = max(
                        max_drawdown_pct,
                        ((replay_peak - replay_balance) / replay_peak) * 100.0,
                    )
            current_price = _safe_float(row.get("close"), 0.0)
            atr_price = _safe_float(candidate.get("atr_price"), 0.0)
            if atr_price <= 0.0:
                factor_values = _to_dict(candidate.get("factor_values"))
                atr_ratio = _safe_float(factor_values.get("atr_ratio"), 0.0)
                atr_price = (
                    atr_ratio * current_price
                    if atr_ratio > 0.0 and current_price > 0.0
                    else current_price * 0.001
                )
            risk_context = {
                "symbol": self.request.symbol,
                "timeframe": self.request.timeframe,
                "direction": direction,
                "decision_ts": ts,
                "decision_bar_index": index,
                "current_price": current_price,
                "atr_price": atr_price,
                "account": {
                    "balance": replay_balance,
                    "equity": replay_balance,
                },
                "session_state": {
                    "pnl": session_pnl,
                    "start_balance": self.request.initial_equity,
                    "trades": len(daily_independent),
                    "consecutive_losses": consecutive_losses,
                    "drawdown_pct": max_drawdown_pct,
                    "circuit_breaker": False,
                },
                "session_last_trade_ts": session_last_trade_ts,
                "closed_bar_prices": [
                    _safe_float(value)
                    for value in history["close"].iloc[
                        -(
                            max(
                                2,
                                int(
                                    getattr(
                                        self.config,
                                        "var_window",
                                        500,
                                    )
                                    or 500
                                ),
                            )
                            + 1
                        ):
                    ].tolist()
                ],
                "closed_bar_timestamps": [
                    str(value)
                    for value in history["time"].iloc[
                        -(
                            max(
                                2,
                                int(
                                    getattr(
                                        self.config,
                                        "var_window",
                                        500,
                                    )
                                    or 500
                                ),
                            )
                            + 1
                        ):
                    ].tolist()
                ],
                "clean_trade_pnls": [
                    _safe_float(item.get("net_pnl"))
                    for item in trades
                ],
                "candidate": candidate,
            }
            try:
                risk = _to_dict(self.risk_evaluator(risk_context))
            except Exception as exc:
                risk = {"allowed": False, "reason": f"risk_policy_error:{type(exc).__name__}:{exc}"}
            allowed = bool(risk.get("allowed", False))
            events.append({
                "event": "risk_policy_evaluated",
                "bar_index": index,
                "event_ts": ts,
                "allowed": allowed,
                "reason": str(risk.get("reason") or ""),
            })
            if not allowed:
                continue
            if index + 1 >= len(frame):
                events.append({
                    "event": "entry_unfilled_end_of_window",
                    "bar_index": index,
                    "event_ts": ts,
                })
                continue
            pending_open = {
                "decision_index": index,
                "decision_ts": ts,
                "direction": direction,
                "sl_distance": max(
                    0.000001,
                    _safe_float(
                        candidate.get("sl_distance"),
                        _safe_float(row.get("close")) * 0.001,
                    ),
                ),
                "tp_distance": max(
                    0.000001,
                    _safe_float(
                        candidate.get("tp_distance"),
                        _safe_float(row.get("close")) * 0.002,
                    ),
                ),
                "candidate": candidate,
                "decision_bar": row,
            }

        if pending_open is not None:
            events.append({
                "event": "entry_unfilled_end_of_window",
                "bar_index": int(pending_open["decision_index"]),
                "event_ts": _safe_float(pending_open["decision_ts"]),
            })
        if pending_exit is not None and position is not None:
            events.append({
                "event": "supervisor_exit_unfilled_end_of_window",
                "bar_index": len(frame) - 1,
                "event_ts": _safe_float(frame.iloc[-1]["time"]),
            })
        if position is not None:
            events.append({
                "event": "position_left_open_end_of_window",
                "bar_index": len(frame) - 1,
                "event_ts": _safe_float(frame.iloc[-1]["time"]),
                "entry_ts": position["entry_ts"],
            })

        gross = sum(_safe_float(item.get("gross_pnl")) for item in trades)
        commission = sum(_safe_float(item.get("commission_cost")) for item in trades)
        spread = sum(_safe_float(item.get("spread_cost")) for item in trades)
        slippage = sum(_safe_float(item.get("slippage_cost")) for item in trades)
        net = sum(_safe_float(item.get("net_pnl")) for item in trades)
        independent = _aggregate_independent_trades(trades)
        wins = sum(_safe_float(item.get("net_pnl")) > 0.0 for item in independent)
        long_count = sum(int(item.get("direction") or 0) > 0 for item in independent)
        short_count = sum(int(item.get("direction") or 0) < 0 for item in independent)
        balance = self.request.initial_equity
        peak = balance
        max_drawdown_pct = 0.0
        for trade in independent:
            balance += _safe_float(trade.get("net_pnl"))
            peak = max(peak, balance)
            if peak > 0.0:
                max_drawdown_pct = max(max_drawdown_pct, (peak - balance) / peak * 100.0)
        return {
            "metrics": {
                "bar_count": max(0, len(frame) - decision_start_index),
                "warmup_bar_count": min(len(frame), decision_start_index),
                "decision_count": decision_count,
                "trade_count": len(trades),
                "gross_pnl": round(gross, 8),
                "net_pnl": round(net, 8),
                "total_cost": round(commission + spread + slippage, 8),
                "commission_cost": round(commission, 8),
                "spread_cost": round(spread, 8),
                "slippage_cost": round(slippage, 8),
                "independent_trade_count": len(independent),
                "long_trade_count": long_count,
                "short_trade_count": short_count,
                "winning_trade_count": wins,
                "losing_trade_count": len(independent) - wins,
                "win_rate": round(wins / len(independent), 6) if independent else 0.0,
                "max_drawdown_pct": round(max_drawdown_pct, 6),
                "open_position_at_end": position is not None,
                "legacy_governance_candidate_count": 0,
            },
            "trades": trades,
            "events": events,
            "causality_violations": list(dict.fromkeys(violations)),
        }

    def _remember_live_decision_state(
        self,
        candidate: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
    ) -> None:
        """Carry the previous closed-bar alpha state into the next safety cycle."""

        adapter_atr = _safe_float(
            getattr(self.decision_provider, "last_atr_price", 0.0),
            0.0,
        )
        candidate_atr = _safe_float(candidate.get("atr_price"), 0.0)
        if adapter_atr > 0.0 or candidate_atr > 0.0:
            self._latest_atr_price = max(adapter_atr, candidate_atr)
        elif self._latest_atr_price <= 0.0:
            factor_values = _to_dict(candidate.get("factor_values"))
            atr_ratio = _safe_float(factor_values.get("atr_ratio"), 0.0)
            close = _safe_float(row.get("close"), 0.0)
            if atr_ratio > 0.0 and close > 0.0:
                self._latest_atr_price = atr_ratio * close

        adapter_conviction = _safe_float(
            getattr(self.decision_provider, "last_conviction", 0.0),
            0.0,
        )
        candidate_conviction = abs(_safe_float(candidate.get("score"), 0.0))
        if adapter_conviction > 0.0 or candidate_conviction > 0.0:
            self._latest_conviction = max(adapter_conviction, candidate_conviction)

        composite = _to_dict(getattr(self.decision_provider, "last_composite", {}))
        if composite:
            try:
                from backend.services.live_position_lifecycle import (
                    current_regime_hint_from_composite,
                )

                self._latest_regime = str(
                    current_regime_hint_from_composite(composite) or self._latest_regime
                )
            except Exception:
                pass

    def _plan_position_safety(
        self,
        position: dict[str, Any],
        *,
        row: Mapping[str, Any],
        index: int,
        ts: float,
    ):
        """Run the live read-only safety planner over reconstructed broker state."""

        from backend.services.live_position_lifecycle import (
            build_close_position_risk_context_payload,
            build_legacy_awe_trailing_update,
            temporal_context_for_trade,
        )
        from backend.services.live_safety_planner import (
            SafetyPlannerRuntime,
            plan_live_safety_candidates,
        )

        pid = int(position.get("position_id") or 0)
        direction = int(position.get("direction") or 0)
        bid = _safe_float(row.get("bid_close"), 0.0)
        ask = _safe_float(row.get("ask_close"), 0.0)
        current_price = (
            (bid + ask) / 2.0
            if bid > 0.0 and ask > 0.0
            else _safe_float(row.get("close"), 0.0)
        )
        atr_price = _safe_float(
            self._latest_atr_price or position.get("atr_price"),
            0.0,
        )
        account = {
            "balance": self.request.initial_equity + self._realized_net_pnl,
            "equity": (
                self.request.initial_equity
                + self._realized_net_pnl
                + self._position_unrealized_pnl(position, row=row)
            ),
        }

        def build_timeout_context(item, effective_cfg, now_ts):
            temporal = temporal_context_for_trade(
                decision_ts=float(now_ts),
                timeframe=self.request.timeframe,
                evaluated_at_ts=float(now_ts),
            )
            return build_close_position_risk_context_payload(
                position_id=int(item.get("position_id") or 0),
                close_reason="holding_timeout",
                mode="replay_read_only",
                broker="ctrader",
                symbol=str(item.get("symbol") or self.request.symbol),
                entry_ts=_safe_float(item.get("open_time"), 0.0),
                entry_ts_source="reconstructed_next_bar_fill",
                temporal_context=temporal,
                max_holding_bars=int(
                    getattr(effective_cfg, "risk_max_holding_bars", 0) or 0
                ),
            )

        def evaluate_supervisor(item, _all_positions, _cfg, _account, now_ts):
            context = self._supervisor_context(
                dict(item),
                row=row,
                index=index,
                ts=float(now_ts),
            )
            verdict = _to_dict(self.supervisor_evaluator(context))
            if "recommended_controls" not in verdict and isinstance(
                verdict.get("controls"), Mapping
            ):
                verdict["recommended_controls"] = dict(verdict["controls"])
            try:
                from backend.services.live_position_lifecycle import (
                    build_supervisor_recovery_meta,
                )

                self._supervisor_state[pid] = build_supervisor_recovery_meta(
                    recovery_meta=self._supervisor_state.get(pid) or {},
                    verdict=verdict,
                )
            except Exception:
                # Replay remains diagnostic-only if its in-memory audit state
                # cannot be advanced; the decision itself is still returned.
                pass
            return verdict

        def build_trailing_update(item, existing_state, price, atr, conviction):
            update = build_legacy_awe_trailing_update(
                position=dict(item),
                existing_state=dict(existing_state or {}),
                current_price=float(price or 0.0),
                atr_price=float(atr or 0.0),
                conviction=float(conviction or 0.0),
                config_version=int(self.config_snapshot.get("config_version") or 0),
                config_hash=str(self.config_snapshot.get("config_hash") or ""),
            )
            update_pid = int(update.get("position_id") or 0)
            if update_pid > 0:
                self._trailing_state[update_pid] = dict(update.get("state") or {})
            return update

        planner_position = {
            **dict(position),
            "position_id": pid,
            "symbol": self.request.symbol,
            "current_price": current_price,
            "price_current": current_price,
            "profit": self._position_unrealized_pnl(position, row=row),
            "pnl": self._position_unrealized_pnl(position, row=row),
            "volume": self.request.volume_lots
            * _safe_float(position.get("remaining_fraction"), 1.0),
            "open_time": _safe_float(position.get("entry_ts"), 0.0),
        }
        runtime = SafetyPlannerRuntime(
            build_timeout_context=build_timeout_context,
            load_entry_protection_plan=lambda _pid: dict(
                position.get("entry_protection_plan") or {}
            ),
            evaluate_supervisor=evaluate_supervisor,
            build_trailing_update=build_trailing_update,
            trailing_state=lambda candidate_pid: dict(
                self._trailing_state.get(int(candidate_pid), {})
            ),
            composite_conviction=lambda: self._latest_conviction,
            clock=lambda: float(ts),
        )
        return plan_live_safety_candidates(
            positions=[planner_position],
            cfg=self.config,
            account=account,
            current_price=current_price,
            atr_price=atr_price,
            runtime=runtime,
            planned_at=float(ts),
        )

    def _evaluate_reduction_risk(
        self,
        candidate: Any,
        *,
        position: Mapping[str, Any],
        ts: float,
    ) -> dict[str, Any]:
        from backend.services.live_position_lifecycle import (
            build_close_position_risk_context_payload,
            supervisor_risk_action_for_action,
            temporal_context_for_trade,
        )
        from risk.policy_service import RiskPolicyService

        action = str(getattr(candidate, "action", "") or "").lower()
        policy_action = supervisor_risk_action_for_action(
            "close" if action == "timeout" else action
        )
        if not policy_action:
            policy_action = "tighten_position"
        temporal = temporal_context_for_trade(
            decision_ts=float(ts),
            timeframe=self.request.timeframe,
            evaluated_at_ts=float(ts),
        )
        payload = build_close_position_risk_context_payload(
            position_id=int(position.get("position_id") or 0),
            close_reason=str(getattr(candidate, "reason", "") or action),
            mode="replay_read_only",
            broker="ctrader",
            symbol=self.request.symbol,
            entry_ts=_safe_float(position.get("entry_ts"), 0.0),
            entry_ts_source="reconstructed_next_bar_fill",
            temporal_context=temporal,
            max_holding_bars=int(
                getattr(self.config, "risk_max_holding_bars", 0) or 0
            ),
        )
        payload.update({
            "position": dict(position),
            "controls": dict(getattr(candidate, "controls", {}) or {}),
            "loop_running": True,
            "bridge_connected": True,
            "replay_read_only": True,
            "historical_context": "reconstructed",
        })
        try:
            verdict = RiskPolicyService.shared().evaluate(policy_action, payload)
            return _to_dict(verdict)
        except Exception as exc:
            return {
                "allowed": False,
                "reason": f"risk_policy_error:{type(exc).__name__}:{exc}",
            }

    def _apply_protection_candidate(
        self,
        candidate: Any,
        *,
        position: dict[str, Any],
        row: Mapping[str, Any],
        index: int,
        ts: float,
        events: list[dict[str, Any]],
    ) -> None:
        from backend.services.live_position_lifecycle import (
            build_protection_execution_plan,
            build_supervisor_tighten_execution_plan,
        )

        direction = int(position.get("direction") or 0)
        bid = _safe_float(row.get("bid_close"), 0.0)
        ask = _safe_float(row.get("ask_close"), 0.0)
        quote = {
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2.0 if bid > 0.0 and ask > 0.0 else 0.0,
            "ts": float(ts),
        }
        planner_position = {
            **dict(position),
            "current_price": bid if direction > 0 else ask,
            "price_current": bid if direction > 0 else ask,
            "digits": int(position.get("digits") or 2),
        }
        controls = dict(getattr(candidate, "controls", {}) or {})
        action = str(getattr(candidate, "action", "") or "").lower()
        if action == "tighten":
            plan = build_supervisor_tighten_execution_plan(
                position=planner_position,
                controls=controls,
                quote=quote,
                policy={
                    "quote_max_age_seconds": getattr(
                        self.config, "supervisor_quote_max_age_seconds", 10.0
                    ),
                    "min_stop_distance_points": getattr(
                        self.config, "supervisor_min_stop_distance_points", 0.20
                    ),
                    "stop_safety_buffer_ratio": getattr(
                        self.config, "supervisor_stop_safety_buffer_ratio", 0.00008
                    ),
                    "min_tighten_delta_points": getattr(
                        self.config, "supervisor_min_tighten_delta_points", 0.01
                    ),
                    "precision": int(position.get("digits") or 2),
                    "require_side_quote": True,
                },
                evaluated_at_ts=float(ts),
            )
            planned_tp = _safe_float(
                plan.get("planned_tp") or plan.get("current_tp"),
                0.0,
            )
        else:
            plan = build_protection_execution_plan(
                position=planner_position,
                controls=controls,
                source=str(getattr(candidate, "reason", "") or action),
                entry_protection_repair_source="entry_protection_repair",
                quote=quote,
                evaluated_at_ts=float(ts),
            )
            planned_tp = _safe_float(plan.get("current_tp"), 0.0)
        sl_plan = dict(plan.get("sl_plan") or {})
        if not bool(sl_plan.get("allowed")):
            events.append({
                "event": "protection_plan_skipped",
                "bar_index": index,
                "event_ts": ts,
                "action": action,
                "reason": str(sl_plan.get("reason") or ""),
                "fingerprint": str(getattr(candidate, "fingerprint", "") or ""),
            })
            return
        old_sl = _safe_float(position.get("sl"), 0.0)
        old_tp = _safe_float(position.get("tp"), 0.0)
        planned_sl = _safe_float(plan.get("planned_sl"), 0.0)
        if planned_sl > 0.0:
            position["sl"] = planned_sl
        if planned_tp > 0.0:
            position["tp"] = planned_tp
        events.append({
            "event": "protection_plan_applied_modeled",
            "bar_index": index,
            "event_ts": ts,
            "action": action,
            "old_sl": old_sl,
            "new_sl": _safe_float(position.get("sl"), 0.0),
            "old_tp": old_tp,
            "new_tp": _safe_float(position.get("tp"), 0.0),
            "fingerprint": str(getattr(candidate, "fingerprint", "") or ""),
            "broker_projection_ack": False,
        })

    def _position_unrealized_pnl(
        self,
        position: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
    ) -> float:
        direction = int(position.get("direction") or 0)
        current = _safe_float(
            row.get("bid_close") if direction > 0 else row.get("ask_close"),
            _safe_float(row.get("close"), 0.0),
        )
        entry = _safe_float(position.get("entry_price"), 0.0)
        remaining = _safe_float(position.get("remaining_fraction"), 1.0)
        return (
            (current - entry)
            * direction
            * self.request.contract_size
            * self.request.volume_lots
            * remaining
        )

    def _open_position(
        self,
        pending: Mapping[str, Any],
        *,
        row: Mapping[str, Any],
        index: int,
        ts: float,
    ) -> dict[str, Any]:
        from backend.services.live_position_lifecycle import (
            build_entry_cluster_context,
            build_entry_protection_plan_payload,
            build_market_micro_context_payload,
            build_open_learning_context_payload,
        )
        from types import SimpleNamespace

        direction = int(pending["direction"])
        raw = _safe_float(row["ask_open"] if direction > 0 else row["bid_open"])
        slip = self.request.slippage_price_each_fill
        entry = raw + slip if direction > 0 else raw - slip
        sl_distance = _safe_float(pending["sl_distance"])
        tp_distance = _safe_float(pending["tp_distance"])
        position_id = int(index + 1)
        stop_loss = entry - sl_distance if direction > 0 else entry + sl_distance
        take_profit = entry + tp_distance if direction > 0 else entry - tp_distance
        entry_plan = build_entry_protection_plan_payload(
            schema_version="entry_protection_plan.v1",
            position_id=position_id,
            direction=direction,
            entry_price=entry,
            target_stop_loss=stop_loss,
            target_take_profit=take_profit,
            requested_volume=self.request.volume_lots * 10_000.0,
            actual_api_volume=self.request.volume_lots * 10_000.0,
            tick=index,
            created_at=ts,
            config_version=int(self.config_snapshot.get("config_version") or 0),
            config_hash=str(self.config_snapshot.get("config_hash") or ""),
            status="applied",
            source="parity_replay_modeled_fill",
        )
        candidate = _to_dict(pending.get("candidate"))
        decision_bar = {
            **_to_dict(pending.get("decision_bar")),
            "complete": True,
            "timeframe": self.request.timeframe,
        }
        signal_values = {
            str(name): _factor_contribution(value)[0]
            for name, value in _to_dict(candidate.get("signals")).items()
        }
        composite_payload = _to_dict(candidate.get("composite"))
        composite_payload.setdefault("score", _safe_float(candidate.get("score")))
        composite_payload.setdefault("factor_signals", signal_values)
        composite_payload.setdefault(
            "factor_roles",
            {name: "alpha" for name in signal_values},
        )
        composite_payload.setdefault(
            "active_weights",
            {name: 1.0 for name in signal_values},
        )
        composite = SimpleNamespace(**composite_payload)
        entry_cluster = build_entry_cluster_context(
            positions_before=[],
            direction=direction,
            symbol=self.request.symbol,
            now_ts=_safe_float(pending.get("decision_ts")),
            new_position_id=position_id,
            new_api_volume=self.request.volume_lots * 10_000.0,
        )
        decision_bid = _safe_float(
            decision_bar.get("bid_close"),
            _safe_float(decision_bar.get("close")),
        )
        decision_ask = _safe_float(
            decision_bar.get("ask_close"),
            _safe_float(decision_bar.get("close")),
        )
        market_micro = build_market_micro_context_payload(
            quote={
                "bid": decision_bid,
                "ask": decision_ask,
                "mid": (decision_bid + decision_ask) / 2.0,
                "ts": _safe_float(pending.get("decision_ts")),
            },
            current_price=_safe_float(decision_bar.get("close")),
            fill_price=entry,
            direction=direction,
            quote_age_seconds=0.0,
            quote_fresh=True,
        )
        open_learning_context = build_open_learning_context_payload(
            entry_cluster=entry_cluster,
            market_micro=market_micro,
            bar=decision_bar,
            composite=composite,
            total_api_volume_before=0.0,
            actual_api_volume=self.request.volume_lots * 10_000.0,
            requested_volume=self.request.volume_lots * 10_000.0,
            base_requested_volume=self.request.volume_lots * 10_000.0,
            current_price=_safe_float(decision_bar.get("close")),
            fill_price=entry,
            sl_price=stop_loss,
            tp_price=take_profit,
            sl_dist=sl_distance,
            tp_dist=tp_distance,
            sizing_trace={
                "source": "parity_replay",
                "volume_lots": self.request.volume_lots,
            },
            event_sizing_context={"enabled": False, "multiplier": 1.0},
            runtime_health={"state": "reconstructed", "source": "parity_replay"},
            market_session={},
            decision_freshness={
                "schema_version": "decision_bar_freshness.v1",
                "fresh": True,
                "age_seconds": 0.0,
            },
            entry_timing_context={"source": "next_bar_open"},
        )
        return {
            "position_id": position_id,
            "symbol": self.request.symbol,
            "direction": direction,
            "decision_index": int(pending["decision_index"]),
            "decision_ts": _safe_float(pending["decision_ts"]),
            "entry_index": index,
            "entry_ts": ts,
            "raw_entry_price": raw,
            "entry_price": entry,
            "sl": stop_loss,
            "tp": take_profit,
            "remaining_fraction": 1.0,
            "mfe_price": entry,
            "mae_price": entry,
            "entry_mid": (_safe_float(row["bid_open"]) + _safe_float(row["ask_open"])) / 2.0,
            "atr_price": _safe_float(
                _to_dict(pending.get("candidate")).get("atr_price"),
                self._latest_atr_price,
            ),
            "entry_regime": self._latest_regime,
            "decision_candidate": _to_dict(pending.get("candidate")),
            "decision_bar": _to_dict(pending.get("decision_bar")),
            "open_learning_context": open_learning_context,
            "entry_protection_plan": entry_plan,
            "digits": 2,
        }

    def _protective_exit(
        self,
        position: dict[str, Any],
        row: Mapping[str, Any],
    ) -> tuple[str, float, bool]:
        direction = int(position["direction"])
        if direction > 0:
            open_price = _safe_float(row["bid_open"])
            low = _safe_float(row["bid_low"])
            high = _safe_float(row["bid_high"])
            sl_hit = low <= position["sl"]
            tp_hit = high >= position["tp"]
            if sl_hit:
                return "stop_loss", min(open_price, position["sl"]), tp_hit
            if tp_hit:
                return "take_profit", max(open_price, position["tp"]), False
        else:
            open_price = _safe_float(row["ask_open"])
            high = _safe_float(row["ask_high"])
            low = _safe_float(row["ask_low"])
            sl_hit = high >= position["sl"]
            tp_hit = low <= position["tp"]
            if sl_hit:
                return "stop_loss", max(open_price, position["sl"]), tp_hit
            if tp_hit:
                return "take_profit", min(open_price, position["tp"]), False
        return "", 0.0, False

    def _close_position(
        self,
        position: dict[str, Any],
        *,
        row: Mapping[str, Any],
        index: int,
        ts: float,
        reason: str,
        fraction: float,
        price_kind: str,
        trades: list[dict[str, Any]],
        events: list[dict[str, Any]],
        explicit_raw_price: float = 0.0,
    ) -> None:
        direction = int(position["direction"])
        remaining = _safe_float(position.get("remaining_fraction"), 1.0)
        closed_fraction = min(remaining, max(0.0, float(fraction)))
        if closed_fraction <= 0:
            return
        if price_kind == "explicit":
            raw_exit = _safe_float(explicit_raw_price)
        else:
            raw_exit = _safe_float(row["bid_open"] if direction > 0 else row["ask_open"])
        slip = self.request.slippage_price_each_fill
        exit_price = raw_exit - slip if direction > 0 else raw_exit + slip
        volume = self.request.volume_lots * closed_fraction
        scale = self.request.contract_size * volume
        entry_mid = _safe_float(position.get("entry_mid"))
        half_spread = max(
            0.0,
            (_safe_float(row["ask_open"]) - _safe_float(row["bid_open"])) / 2.0,
        )
        exit_mid = raw_exit + half_spread if direction > 0 else raw_exit - half_spread
        gross = (exit_mid - entry_mid) * direction * scale
        commission = self.request.commission_per_lot_round_turn * volume
        spread_cost = (
            abs(position["raw_entry_price"] - entry_mid)
            + abs(raw_exit - exit_mid)
        ) * scale
        slippage_cost = (
            abs(position["entry_price"] - position["raw_entry_price"])
            + abs(exit_price - raw_exit)
        ) * scale
        net = gross - spread_cost - slippage_cost - commission
        trade = {
            "decision_index": position["decision_index"],
            "decision_ts": position["decision_ts"],
            "entry_index": position["entry_index"],
            "entry_ts": position["entry_ts"],
            "exit_index": index,
            "exit_ts": ts,
            "direction": direction,
            "closed_fraction": closed_fraction,
            "volume_lots": volume,
            "raw_entry_price": round(position["raw_entry_price"], 10),
            "entry_price": round(position["entry_price"], 10),
            "raw_exit_price": round(raw_exit, 10),
            "exit_price": round(exit_price, 10),
            "reason": reason,
            "gross_pnl": round(gross, 8),
            "commission_cost": round(commission, 8),
            "spread_cost": round(spread_cost, 8),
            "slippage_cost": round(slippage_cost, 8),
            "net_pnl": round(net, 8),
            "decision_candidate": _to_dict(position.get("decision_candidate")),
            "decision_bar": _to_dict(position.get("decision_bar")),
            "open_learning_context": _to_dict(position.get("open_learning_context")),
            "entry_regime": str(position.get("entry_regime") or ""),
            "same_bar_sl_tp_path_ambiguous": bool(
                position.get("same_bar_sl_tp_path_ambiguous")
            ),
        }
        trades.append(trade)
        self._realized_net_pnl += _safe_float(trade.get("net_pnl"), 0.0)
        position["remaining_fraction"] = max(0.0, remaining - closed_fraction)
        if position["remaining_fraction"] <= 1e-12:
            pid = int(position.get("position_id") or 0)
            self._path_state.pop(pid, None)
            self._supervisor_state.pop(pid, None)
            self._trailing_state.pop(pid, None)
        events.append({
            "event": "closed" if position["remaining_fraction"] <= 1e-12 else "reduced",
            "bar_index": index,
            "event_ts": ts,
            "reason": reason,
            "net_pnl": trade["net_pnl"],
            "remaining_fraction": position["remaining_fraction"],
        })

    def _supervisor_context(
        self,
        position: dict[str, Any],
        *,
        row: Mapping[str, Any],
        index: int,
        ts: float,
    ) -> dict[str, Any]:
        from backend.services.live_position_lifecycle import (
            build_close_position_risk_context_payload,
            build_holding_summary_from_close_context,
            build_position_supervisor_context_inputs,
            build_position_supervisor_context_payload,
            temporal_context_for_trade,
        )
        from backend.services.position_metrics import update_position_path_metrics

        direction = int(position["direction"])
        current = _safe_float(row["bid_close"] if direction > 0 else row["ask_close"])
        pid = int(position.get("position_id") or position.get("entry_index") or 0)
        temporal = temporal_context_for_trade(
            decision_ts=float(ts),
            timeframe=self.request.timeframe,
            evaluated_at_ts=float(ts),
        )
        temporal["completed_bars_after_entry"] = max(
            0,
            index - int(position["entry_index"]),
        )
        temporal["closed_bars_since_entry"] = temporal["completed_bars_after_entry"]
        close_context = build_close_position_risk_context_payload(
            position_id=pid,
            close_reason="position_supervisor",
            mode="replay_read_only",
            broker="ctrader",
            symbol=self.request.symbol,
            entry_ts=_safe_float(position.get("entry_ts"), 0.0),
            entry_ts_source="reconstructed_next_bar_fill",
            temporal_context=temporal,
            max_holding_bars=int(
                getattr(self.config, "risk_max_holding_bars", 0) or 0
            ),
        )
        holding = build_holding_summary_from_close_context(close_context)
        holding_seconds = _safe_float(holding.get("holding_seconds"), 0.0)
        max_holding_seconds = _safe_float(holding.get("max_holding_seconds"), 0.0)
        unrealized_pnl = self._position_unrealized_pnl(position, row=row)
        next_state, position_metrics = update_position_path_metrics(
            previous_state=self._path_state.get(pid),
            current_pnl=unrealized_pnl,
            now_ts=float(ts),
            holding_seconds=holding_seconds,
            max_holding_seconds=max_holding_seconds,
            entry_regime=str(position.get("entry_regime") or ""),
            current_regime=self._latest_regime,
        )
        self._path_state[pid] = next_state
        position_metrics = {
            **position_metrics,
            "time_in_profit": _safe_float(
                position_metrics.get("time_in_profit_seconds"),
                0.0,
            ),
            "entry_regime": str(position.get("entry_regime") or ""),
            "current_regime": self._latest_regime,
        }
        planner_position = {
            "position_id": f"replay:{position['entry_index']}",
            "symbol": self.request.symbol,
            "direction": direction,
            "entry_price": position["entry_price"],
            "current_price": current,
            "sl": position["sl"],
            "tp": position["tp"],
            "profit": unrealized_pnl,
            "pnl": unrealized_pnl,
            "volume": self.request.volume_lots * position["remaining_fraction"],
            "open_time": position["entry_ts"],
            "max_holding_seconds": max_holding_seconds,
            "holding_timeout_ratio": _safe_float(
                holding.get("holding_timeout_ratio"),
                0.0,
            ),
        }

        context = build_position_supervisor_context_payload(
            **build_position_supervisor_context_inputs(
                position=planner_position,
                cfg=self.config,
                positions=[planner_position],
                account={
                    "balance": self.request.initial_equity + self._realized_net_pnl,
                    "equity": (
                        self.request.initial_equity
                        + self._realized_net_pnl
                        + unrealized_pnl
                    ),
                },
                entry_decision_id=f"replay:{position['decision_index']}",
                risk_snapshot={"replay_read_only": True},
                market_context=_to_dict(
                    getattr(self.decision_provider, "last_composite", {})
                ),
                supervisor_state=dict(self._supervisor_state.get(pid) or {}),
                total_api_volume=self.request.volume_lots * 10_000.0,
                loop_running=True,
            ),
            temporal_context=close_context,
            position_metrics=position_metrics,
        )
        context["replay_read_only"] = True
        context["historical_context"] = "reconstructed"
        context["path_metrics_implementation"] = (
            "backend.services.position_metrics.update_position_path_metrics"
        )
        return context


class ParityReplayService:
    def __init__(
        self,
        *,
        db_path: str | Path = STATE_DB,
        bar_loader: MonthlyPITBarLoader | None = None,
        artifact_dir: str | Path = DEFAULT_PARITY_ARTIFACT_DIR,
    ):
        self.db_path = db_path
        self.bar_loader = bar_loader or MonthlyPITBarLoader()
        self.artifact_dir = Path(artifact_dir)

    def run(
        self,
        params: Mapping[str, Any] | None = None,
        progress_cb: Callable[[str, float, str], None] | None = None,
    ) -> dict[str, Any]:
        request = ParityReplayRequest.from_mapping(params)
        from config.runtime_config import shared

        cfg = shared()
        try:
            from backend.services.evolution_ledger import current_runtime_config_snapshot

            snapshot = current_runtime_config_snapshot(
                db_path=self.db_path,
                create_if_missing=False,
            )
        except Exception as exc:
            snapshot = {
                "config_version": 0,
                "config_hash": "",
                "source": "",
                "error": f"{type(exc).__name__}:{exc}",
            }
        if progress_cb is not None:
            progress_cb("loading", 7, "读取冻结的月度历史K线")
        bars, data_source = self.bar_loader.load(request)
        runner = ParityReplayRunner(
            request=request,
            config=cfg,
            config_snapshot=snapshot,
            progress_cb=progress_cb,
        )
        try:
            report = runner.run(bars, data_source=data_source)
        finally:
            adapter = runner.decision_provider
            if isinstance(adapter, LiveComponentDecisionAdapter):
                adapter.release()
        report = self._verify_frozen_inputs(report)
        if request.persist_artifact:
            report = self._persist_artifact(report)
        return report

    def _verify_frozen_inputs(self, report: Mapping[str, Any]) -> dict[str, Any]:
        """Reject training when files or bound code changed during the task."""

        payload = dict(report)
        before = dict(payload.get("bindings") or {})
        from config.runtime_config import shared

        current_config = shared()
        current_config_payload = (
            current_config.to_dict()
            if hasattr(current_config, "to_dict")
            else dict(current_config) if isinstance(current_config, Mapping) else {}
        )
        after_code_hash = _sha256_files(_CODE_BINDING_PATHS)
        source_files = [
            str(item.get("path") or "")
            for item in list(
                dict(payload.get("data_source") or {}).get("source_file_manifest") or []
            )
            if isinstance(item, Mapping) and str(item.get("path") or "")
        ]
        after_manifest, after_errors = _source_file_manifest(source_files)
        before_manifest = list(
            dict(payload.get("data_source") or {}).get("source_file_manifest") or []
        )
        changed: list[str] = []
        if str(before.get("code_hash") or "") != after_code_hash:
            changed.append("code_changed_during_replay")
        if str(before.get("config_hash") or "") != _runtime_config_hash(current_config_payload):
            changed.append("config_changed_during_replay")
        artifact_manifest = dict(payload.get("artifact_manifest") or {})
        selected_ids = list(artifact_manifest.get("selected_factor_ids") or [])
        if dict(artifact_manifest.get("selected_factor_artifacts") or {}) != dict(
            _selected_factor_artifact_manifest(current_config, selected_ids)
        ):
            changed.append("factor_artifacts_changed_during_replay")
        if after_errors or after_manifest != before_manifest:
            changed.append("data_files_changed_during_replay")
        if changed:
            reasons = list(payload.get("diagnostic_reasons") or [])
            payload["diagnostic_reasons"] = list(dict.fromkeys(reasons + changed))
            bundle = dict(payload.get("learning_bundle") or {})
            bundle["trainable"] = False
            bundle["blockers"] = list(
                dict.fromkeys(list(bundle.get("blockers") or []) + changed)
            )
            bundle["open_samples"] = []
            bundle["factor_samples"] = []
            payload["learning_bundle"] = bundle
        payload["binding_postcheck"] = {
            "verified": not changed,
            "blockers": changed,
            "code_hash": after_code_hash,
            "source_file_manifest": after_manifest,
        }
        return payload

    def _persist_artifact(self, report: Mapping[str, Any]) -> dict[str, Any]:
        payload = dict(report)
        payload["artifact_path"] = ""
        payload["report_artifact_hash"] = ""
        artifact_hash = _sha256_json(payload)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        path = self.artifact_dir / f"{payload.get('replay_run_id')}.json"
        payload["artifact_path"] = str(path)
        payload["report_artifact_hash"] = artifact_hash
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, default=_json_default) + "\n",
            encoding="utf-8",
        )
        return payload


def load_parity_learning_samples(
    sample_kind: str,
    *,
    artifact_dir: str | Path = DEFAULT_PARITY_ARTIFACT_DIR,
) -> list[dict[str, Any]]:
    """Read verified replay samples without touching runtime learning tables."""

    key = "open_samples" if sample_kind == "open" else "factor_samples"
    expected_schema = (
        "pit.v2.open_lineage"
        if sample_kind == "open"
        else "pit.v4.factor_regime_decision_lineage"
    )
    samples: dict[str, dict[str, Any]] = {}
    for path in sorted(Path(artifact_dir).glob("parity_*.json")):
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            bundle = dict(report.get("learning_bundle") or {})
            if (
                report.get("schema_version") != PARITY_REPLAY_SCHEMA_VERSION
                or bundle.get("schema_version") != "parity_learning_bundle.v1"
                or not bool(bundle.get("trainable"))
                or not bool(dict(report.get("binding_postcheck") or {}).get("verified"))
                or str(dict(bundle.get("feature_schemas") or {}).get(sample_kind) or "")
                != expected_schema
            ):
                continue
            hashed = dict(report)
            expected_hash = str(hashed.get("report_artifact_hash") or "")
            hashed["artifact_path"] = ""
            hashed["report_artifact_hash"] = ""
            if not expected_hash or _sha256_json(hashed) != expected_hash:
                continue
            for raw in list(bundle.get(key) or []):
                if not isinstance(raw, Mapping):
                    continue
                item = dict(raw)
                sample_id = str(item.get("sample_id") or "")
                if sample_id:
                    samples[sample_id] = item
        except Exception:
            continue
    return sorted(
        samples.values(),
        key=lambda item: (_safe_float(item.get("created_at")), str(item.get("sample_id") or "")),
    )


__all__ = [
    "DEFAULT_PARITY_ARTIFACT_DIR",
    "MonthlyPITBarLoader",
    "PARITY_REPLAY_CONTRACT_VERSION",
    "PARITY_REPLAY_SCHEMA_VERSION",
    "ParityReplayRequest",
    "ParityReplayRunner",
    "ParityReplayService",
    "load_parity_learning_samples",
]
