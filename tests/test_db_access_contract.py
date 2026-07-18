import ast
from pathlib import Path
import sqlite3

from execution.event_sizing import EventSizing


_PRODUCTION_FOLDERS = (
    "backend",
    "execution",
    "risk",
    "alpha",
    "monitor",
    "config",
    "data",
    "research",
)


def _python_files(repo: Path, folders: tuple[str, ...] = _PRODUCTION_FOLDERS):
    for folder in folders:
        base = repo / folder
        if base.exists():
            yield from base.rglob("*.py")


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8", errors="replace"), filename=str(path))


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _direct_connection_calls(tree: ast.Module) -> list[tuple[ast.Call, str]]:
    """Return direct sqlite/duckdb/psycopg connect calls, including aliases."""
    module_aliases: dict[str, str] = {}
    function_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in {"sqlite3", "duckdb", "psycopg"}:
                    module_aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in {"sqlite3", "duckdb", "psycopg"}:
            for alias in node.names:
                if alias.name == "connect":
                    function_aliases[alias.asname or alias.name] = node.module

    calls: list[tuple[ast.Call, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name) and node.func.id in function_aliases:
            calls.append((node, function_aliases[node.func.id]))
            continue
        if not isinstance(node.func, ast.Attribute) or node.func.attr != "connect":
            continue
        owner = _dotted_name(node.func.value).split(".", 1)[0]
        if owner in module_aliases:
            calls.append((node, module_aliases[owner]))
    return calls


class _ScopedCallVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.scope: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        self.calls.append((self.scope[-1] if self.scope else "<module>", node))
        self.generic_visit(node)


def test_business_code_uses_db_helpers_for_direct_connections():
    repo = Path(__file__).resolve().parents[1]
    allowed_prefixes = {
        "backend/core/db.py",
        "backend/core/state_store.py",
        "execution/event_sizing.py",
        "research/",
        "scripts/",
        "tests/",
    }
    offenders: list[str] = []
    for folder in ("backend", "execution", "risk", "alpha", "monitor"):
        for path in (repo / folder).rglob("*.py"):
            rel = path.relative_to(repo).as_posix()
            if any(rel == prefix.rstrip("/") or rel.startswith(prefix) for prefix in allowed_prefixes):
                continue
            for call, module in _direct_connection_calls(_parse(path)):
                offenders.append(f"{rel}:{call.lineno}: {module}.connect")
    assert offenders == []


def test_event_sizing_allows_legacy_sqlite_event_files(tmp_path):
    db = tmp_path / "events.sqlite"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE events(date TEXT, type TEXT, description TEXT, importance INTEGER)")
    conn.execute("INSERT INTO events VALUES ('2026-07-03', 'NFP', 'jobs', 3)")
    conn.commit()
    conn.close()

    sizing = EventSizing(db_path=str(db), enabled=True)

    assert sizing.enabled is True
    assert sizing._events


def test_runtime_state_code_does_not_open_state_sqlite_directly():
    repo = Path(__file__).resolve().parents[1]
    allowed = {
        # Central helper contains sqlite3 calls but rejects STATE_DB before
        # reaching them; it is the enforcement point, not a state writer.
        "backend/core/db.py",
        "scripts/migrate_state_sqlite_to_pg.py",
        "scripts/verify_state_pg_parity.py",
    }
    offenders: list[str] = []
    paths = [*_python_files(repo), *(repo / "scripts").rglob("*.py")]
    for path in paths:
        rel = path.relative_to(repo).as_posix()
        tree = _parse(path)
        sqlite_calls = [call for call, module in _direct_connection_calls(tree) if module == "sqlite3"]
        if not sqlite_calls:
            continue
        touches_default_state = any(
            isinstance(node, ast.Name) and node.id == "STATE_DB"
            or isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and ("data/state.db" in node.value or node.value.endswith("state.db"))
            for node in ast.walk(tree)
        )
        if touches_default_state and rel not in allowed:
            offenders.extend(f"{rel}:{call.lineno}" for call in sqlite_calls)
    assert offenders == []


def test_runtime_config_overlay_writes_stay_behind_mutation_service():
    """Overlay persistence is private to RuntimeConfigMutationService.

    Startup/restore/status code may construct RuntimeConfigOverlayService, but
    production callers must not invoke its write method directly.  The AST
    check deliberately follows the service expression instead of matching a
    comment or docstring containing ``apply_patch``.
    """
    repo = Path(__file__).resolve().parents[1]
    boundary = "backend/services/runtime_config_mutation.py"
    offenders: list[str] = []
    for path in _python_files(repo):
        rel = path.relative_to(repo).as_posix()
        if rel == boundary:
            continue
        tree = _parse(path)
        overlay_constructors = {"RuntimeConfigOverlayService"}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "backend.services.runtime_config_overlay":
                overlay_constructors.update(
                    alias.asname or alias.name
                    for alias in node.names
                    if alias.name == "RuntimeConfigOverlayService"
                )
        overlay_variables: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if not isinstance(value, ast.Call) or _dotted_name(value.func) not in overlay_constructors:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            overlay_variables.update(target.id for target in targets if isinstance(target, ast.Name))
        visitor = _ScopedCallVisitor()
        visitor.visit(tree)
        for scope, call in visitor.calls:
            if not isinstance(call.func, ast.Attribute) or call.func.attr != "apply_patch":
                continue
            receiver = call.func.value
            direct_overlay = (
                isinstance(receiver, ast.Call)
                and _dotted_name(receiver.func) in overlay_constructors
            )
            named_overlay = isinstance(receiver, ast.Attribute) and receiver.attr == "overlay"
            assigned_overlay = isinstance(receiver, ast.Name) and receiver.id in overlay_variables
            if direct_overlay or named_overlay or assigned_overlay:
                offenders.append(f"{rel}:{call.lineno} ({scope})")
    assert offenders == []


def test_factor_weight_mutations_are_decided_by_decision_policy():
    """A function that persists factor weights must also use DecisionPolicy.

    Snapshot rollback is assembled in a pure helper but must execute through
    FactorWeightChangeService.  Producers either invoke DecisionPolicy directly
    or delegate the complete decision/admission/risk/application/mutation flow
    to that single governed use-case boundary.
    """
    repo = Path(__file__).resolve().parents[1]
    mutation_names = {"apply_patch", "_apply_runtime_patch", "patch"}
    decision_names = {"decide", "fast_decide"}
    offenders: list[str] = []
    for path in _python_files(repo):
        rel = path.relative_to(repo).as_posix()
        tree = _parse(path)
        for function in (
            node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ):
            constants = {
                node.value
                for node in ast.walk(function)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)
            }
            if "factor_portfolio_weights" not in constants:
                continue
            calls = [node for node in ast.walk(function) if isinstance(node, ast.Call)]
            writes_runtime = any(
                isinstance(call.func, ast.Attribute) and call.func.attr in mutation_names for call in calls
            )
            if not writes_runtime:
                continue
            uses_policy = any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr in decision_names
                and any(isinstance(node, ast.Name) and node.id == "DecisionPolicy" for node in ast.walk(function))
                for call in calls
            )
            uses_governed_weight_service = any(
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "execute"
                and any(
                    isinstance(node, ast.Name) and node.id == "FactorWeightChangeService"
                    for node in ast.walk(function)
                )
                for call in calls
            )
            is_weight_service_boundary = (
                rel == "backend/services/factor_weight_change.py"
                and function.name == "execute"
                and any(
                    isinstance(call.func, ast.Attribute) and call.func.attr == "plan"
                    for call in calls
                )
            )
            if not (uses_policy or uses_governed_weight_service or is_weight_service_boundary):
                offenders.append(f"{rel}:{function.lineno} ({function.name})")
    assert offenders == []


def test_live_broker_mutations_are_confined_to_reviewed_execution_adapters():
    """Do not let new production code acquire a side door to cTrader writes.

    These adapters either evaluate RiskPolicyService themselves or receive its
    verdict from the risk-gated live pipeline.  Emergency close is deliberately
    isolated from alpha/PG while remaining close-only.  The allowlist is kept at
    a module boundary so internal function extraction does not churn this test.
    """
    repo = Path(__file__).resolve().parents[1]
    broker_methods = {"market_buy", "market_sell", "close_position", "amend_position_sltp"}
    approved_modules = {
        "backend/services/live_emergency.py": {"close_position"},
        "backend/services/live_service.py": broker_methods,
        "backend/services/live_supervision_actions.py": {
            "close_position",
            "amend_position_sltp",
        },
    }
    offenders: list[str] = []
    for path in _python_files(repo):
        rel = path.relative_to(repo).as_posix()
        if rel == "execution/ctrader_bridge.py":
            continue
        visitor = _ScopedCallVisitor()
        visitor.visit(_parse(path))
        for scope, call in visitor.calls:
            if not isinstance(call.func, ast.Attribute) or call.func.attr not in broker_methods:
                continue
            if call.func.attr not in approved_modules.get(rel, set()):
                offenders.append(f"{rel}:{call.lineno} ({scope} -> {call.func.attr})")
    assert offenders == []


def test_state_query_helper_is_postgres_only():
    repo = Path(__file__).resolve().parents[1]
    text = (repo / "scripts/state_query.py").read_text(encoding="utf-8")

    assert "get_state_pg_conn" in text
    assert "sqlite3" not in text
    assert "data/state.db" in text
