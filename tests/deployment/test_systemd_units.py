from __future__ import annotations

from configparser import ConfigParser
from pathlib import Path


UNIT_DIR = Path(__file__).parents[2] / "deployment"
WORKER_UNITS = ("quant-learning-worker.service", "quant-job-worker.service")
MANAGED_UNITS = (*WORKER_UNITS, "quant-backend.service")


def _unit(name: str) -> ConfigParser:
    parser = ConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    with (UNIT_DIR / name).open(encoding="utf-8") as handle:
        parser.read_file(handle)
    return parser


def test_worker_units_have_bounded_failure_recovery_and_stop_contract():
    for name in WORKER_UNITS:
        unit = _unit(name)
        service = unit["Service"]
        unit_section = unit["Unit"]
        source = (UNIT_DIR / name).read_text(encoding="utf-8")

        assert service["Restart"] == "on-failure"
        assert float(service["RestartSec"].rstrip("s")) >= 30.0
        assert int(service["TimeoutStopSec"].rstrip("s")) >= 180
        assert service["KillMode"] == "control-group"
        assert int(unit_section["StartLimitIntervalSec"].rstrip("s")) == 300
        assert int(unit_section["StartLimitBurst"]) == 5
        assert "postgresql.service" in unit_section["After"]
        assert "quant-backend.service" in unit_section["After"]
        assert "postgresql.service" in unit_section["Wants"]
        assert source.count("Restart=") == 1
        assert "Restart=always" not in source
        assert "Restart=no" not in source


def test_managed_units_share_bounded_failure_recovery_and_stop_contract():
    for name in MANAGED_UNITS:
        unit = _unit(name)
        service = unit["Service"]
        unit_section = unit["Unit"]
        source = (UNIT_DIR / name).read_text(encoding="utf-8")

        assert service["Restart"] == "on-failure"
        assert float(service["RestartSec"].rstrip("s")) >= 30.0
        assert int(service["TimeoutStopSec"].rstrip("s")) >= 180
        assert service["KillMode"] == "control-group"
        assert int(unit_section["StartLimitIntervalSec"].rstrip("s")) == 300
        assert int(unit_section["StartLimitBurst"]) == 5
        assert "network-online.target" in unit_section["After"]
        assert "postgresql.service" in unit_section["After"]
        assert "network-online.target" in unit_section["Wants"]
        assert "postgresql.service" in unit_section["Wants"]
        assert source.count("Restart=") == 1
        assert "Restart=always" not in source
        assert "Restart=no" not in source


def test_backend_unit_is_authoritative_secret_free_fail_closed_source():
    unit = _unit("quant-backend.service")
    service = unit["Service"]
    source = (UNIT_DIR / "quant-backend.service").read_text(encoding="utf-8")

    assert service["User"] == "ubuntu"
    assert service["WorkingDirectory"] == "/home/ubuntu/quant_trading"
    assert service["EnvironmentFile"] == "/home/ubuntu/quant_trading/.env"
    assert "Environment=PATH=/home/ubuntu/quant_trading/.venv/bin:/usr/bin:/bin" in source
    assert service["KillSignal"] == "SIGTERM"
    assert service["ExecStart"] == (
        "/home/ubuntu/quant_trading/.venv/bin/uvicorn backend.app:app "
        "--host 127.0.0.1 --port 8000 --log-level info"
    )
    assert "http_proxy=" not in source
    assert "https_proxy=" not in source
    assert "no_proxy=" not in source.lower()
    assert "password=" not in source.lower()
    assert "secret=" not in source.lower()


def test_workers_remain_ordered_after_backend():
    for name in WORKER_UNITS:
        unit_section = _unit(name)["Unit"]
        assert "quant-backend.service" in unit_section["After"]


def test_learning_worker_unit_preserves_fail_closed_process_boundary():
    service = _unit("quant-learning-worker.service")["Service"]

    assert service["ExecStart"].endswith("scripts/learning_worker.py")
    assert service["WorkingDirectory"] == "/home/ubuntu/quant_trading"
    assert service["User"] == "ubuntu"
    assert service["CPUAffinity"] == "2 3"


def test_job_worker_unit_keeps_queue_disabled_by_default():
    service = _unit("quant-job-worker.service")["Service"]

    assert service["ExecStart"].endswith(
        "scripts/job_worker.py --global-limit 2 --kind-limit backtest=1 "
        "--kind-limit discover=1 --kind-limit tuning=1 --kind-limit ab_test=1 "
        "--kind-limit external_refresh=1 --kind-limit sync=1 "
        "--kind-limit factor_health=1 --kind-limit parameter_template_validation=1"
    )
    assert "Environment=QUANT_PG_JOB_QUEUE_V2_ENABLED=0" in (
        UNIT_DIR / "quant-job-worker.service"
    ).read_text(encoding="utf-8")
