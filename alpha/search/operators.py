"""alpha/search/operators.py — OperatorRegistry for GP search.

Singleton registry for DSL operators. Replaces the hardcoded ALLOWED_OPS dict
in alpha/factor_dsl.py with a registry pattern. Supports metadata lookup,
compatibility with the existing (category, min_args, max_args) tuple format,
and registration of new operators.
"""

import threading
from typing import Callable, Optional


# ── Stub helper functions ──────────────────────────────────────────────
# These exist for metadata/registration purposes only. Actual evaluation
# logic lives in DSLEvaluator (alpha/factor_dsl.py).


def _op_ts_mean(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_std(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_sum(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_min(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_max(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_corr(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_rank(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_delta(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_delay(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_ts_decay_linear(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_rank(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_normalize(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_quantile(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_sign(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_abs(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_log(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_sqrt(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_power(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_signed_log(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


def _op_zscore(*args, **kwargs):
    raise NotImplementedError("Evaluated by DSLEvaluator")


# ── OperatorRegistry ──────────────────────────────────────────────────


class OperatorRegistry:
    """Singleton registry for DSL operators.

    Each operator is stored with its callable, arity, category, and
    optional description. The get_meta() method returns a tuple in the
    (category, min_args, max_args) format used by ALLOWED_OPS in
    alpha/factor_dsl.py for drop-in compatibility.
    """

    _instance: Optional["OperatorRegistry"] = None
    _lock = threading.Lock()

    def __init__(self):
        self._ops: dict[str, dict] = {}

    def register(
        self,
        name: str,
        fn: Callable,
        arity: int,
        category: str,
        description: str = "",
    ) -> None:
        """Register an operator.

        Args:
            name: Operator name (e.g. "ts_mean").
            fn: Callable implementing the operator (stub — real logic in DSLEvaluator).
            arity: Exact number of arguments the operator expects.
            category: Operator category ("ts", "cs", "math", etc.).
            description: Human-readable description.

        Raises:
            ValueError: If an operator with the same name is already registered.
        """
        if name in self._ops:
            raise ValueError(
                f"Operator '{name}' is already registered"
            )
        self._ops[name] = {
            "fn": fn,
            "arity": arity,
            "category": category,
            "description": description,
        }

    def get(self, name: str) -> Optional[dict]:
        """Get full operator metadata dict, or None if not found."""
        return self._ops.get(name)

    def get_meta(self, name: str) -> Optional[tuple[str, int, int]]:
        """Return (category, min_args, max_args) compatible with ALLOWED_OPS format.

        Since arity is the exact argument count, min_args == max_args == arity.

        Args:
            name: Operator name.

        Returns:
            Tuple of (category, min_args, max_args) or None if not found.
        """
        op = self._ops.get(name)
        if op is None:
            return None
        return (op["category"], op["arity"], op["arity"])

    def all_names(self) -> list[str]:
        """Return sorted list of all registered operator names."""
        return sorted(self._ops.keys())

    @classmethod
    def shared(cls) -> "OperatorRegistry":
        """Return the shared singleton instance (thread-safe)."""
        if cls._instance is not None:
            return cls._instance
        with cls._lock:
            if cls._instance is None:
                cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_singleton(cls) -> None:
        """Reset the singleton instance (for testing)."""
        with cls._lock:
            cls._instance = None


# ── register_standard_ops ─────────────────────────────────────────────


def register_standard_ops() -> None:
    """Register all standard operators. Safe to call multiple times."""
    reg = OperatorRegistry.shared()
    # Skip if already registered
    if reg.all_names():
        return

    # ── 时序算子 ──
    reg.register(
        "ts_mean", _op_ts_mean, 2, "ts", "Rolling mean: ts_mean(x, n)"
    )
    reg.register(
        "ts_std", _op_ts_std, 2, "ts", "Rolling std: ts_std(x, n)"
    )
    reg.register(
        "ts_sum", _op_ts_sum, 2, "ts", "Rolling sum: ts_sum(x, n)"
    )
    reg.register(
        "ts_min", _op_ts_min, 2, "ts", "Rolling min: ts_min(x, n)"
    )
    reg.register(
        "ts_max", _op_ts_max, 2, "ts", "Rolling max: ts_max(x, n)"
    )
    reg.register(
        "ts_corr",
        _op_ts_corr,
        3,
        "ts",
        "Rolling correlation: ts_corr(x, y, n)",
    )
    reg.register(
        "ts_rank", _op_ts_rank, 2, "ts", "Rolling rank: ts_rank(x, n)"
    )
    reg.register(
        "delta", _op_delta, 2, "ts", "Difference: delta(x, n)"
    )
    reg.register(
        "delay", _op_delay, 2, "ts", "Lag: delay(x, n)"
    )
    reg.register(
        "ts_decay_linear",
        _op_ts_decay_linear,
        2,
        "ts",
        "Linear decay: ts_decay_linear(x, n)",
    )

    # ── 横截面算子 ──
    reg.register("rank", _op_rank, 1, "cs", "Cross-sectional rank")
    reg.register(
        "normalize", _op_normalize, 1, "cs", "Z-score normalize"
    )
    reg.register(
        "quantile", _op_quantile, 2, "cs", "Quantile: quantile(x, q)"
    )

    # ── 一元数学 ──
    reg.register("sign", _op_sign, 1, "math", "Sign: sign(x)")
    reg.register("abs", _op_abs, 1, "math", "Absolute value: abs(x)")
    reg.register(
        "log", _op_log, 1, "math", "Natural log: log(x)"
    )
    reg.register(
        "sqrt", _op_sqrt, 1, "math", "Square root: sqrt(x)"
    )
    reg.register(
        "power", _op_power, 2, "math", "Power: power(x, exp)"
    )

    # ── 新增算子 ──
    reg.register(
        "signed_log",
        _op_signed_log,
        1,
        "math",
        "Sign-preserving log: sign(x)*log(|x|+1)",
    )
    reg.register(
        "zscore",
        _op_zscore,
        2,
        "ts",
        "Rolling z-score: (x - mean(x,n)) / std(x,n)",
    )
