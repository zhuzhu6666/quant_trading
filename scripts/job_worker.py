#!/usr/bin/env python3
"""Run the feature-flagged PostgreSQL research job worker."""
from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import threading
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loguru import logger


def _validate_worker_settings_yaml() -> None:
    """Fail before claiming work when the deployment YAML is unusable."""
    from backend.jobs.release_preflight import validate_persistent_job_worker_settings

    validate_persistent_job_worker_settings()


def _validate_worker_startup() -> None:
    """Run read-only config and schema gates before a queue client is built."""
    _validate_worker_settings_yaml()
    from backend.core.db import init_state_db

    # Read-only minimum-version gate.  This process never applies DDL.
    init_state_db()


def _kind_limits(values: list[str]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values:
        kind, separator, limit = str(raw).partition("=")
        if not separator or not kind.strip():
            raise ValueError(f"invalid_kind_limit:{raw}")
        result[kind.strip()] = max(0, int(limit))
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-sec", type=float, default=2.0)
    parser.add_argument("--lease-sec", type=float, default=60.0)
    parser.add_argument("--heartbeat-sec", type=float, default=10.0)
    parser.add_argument("--global-limit", type=int, default=2)
    parser.add_argument(
        "--kind-limit",
        action="append",
        default=[
            "backtest=1",
            "discover=1",
            "tuning=1",
            "ab_test=1",
            "external_refresh=1",
            "sync=1",
            "factor_health=1",
            "parameter_template_validation=1",
        ],
    )
    parser.add_argument("--worker-id", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from backend.core.logging import setup_logging
    from backend.core.state_schema_migrations import STATE_SCHEMA_MIN_VERSION
    from backend.core.static_feature_flags import shared_static_feature_flags

    setup_logging()
    flags = shared_static_feature_flags()
    if not flags.pg_job_queue_v2_enabled:
        logger.warning(
            "[job_worker] disabled; set release flag "
            "QUANT_PG_JOB_QUEUE_V2_ENABLED=1 only after state schema minimum v{} passes",
            STATE_SCHEMA_MIN_VERSION,
        )
        return 0

    from backend.jobs.capability import PersistentJobWorkerCapability
    from backend.jobs.handlers import persistent_job_handlers
    from backend.jobs.pg_queue import PgJobQueue
    from backend.jobs.worker import PersistentJobWorker

    _validate_worker_startup()
    worker_id = str(args.worker_id or "").strip() or (
        f"{socket.gethostname()}:{os.getpid()}"
    )
    handlers = persistent_job_handlers()
    capability = PersistentJobWorkerCapability(
        worker_id=worker_id,
        handler_kinds=tuple(handlers),
    )
    worker = PersistentJobWorker(
        queue=PgJobQueue(),
        worker_id=worker_id,
        handlers=handlers,
        poll_interval_sec=args.poll_sec,
        lease_sec=args.lease_sec,
        heartbeat_interval_sec=args.heartbeat_sec,
        global_limit=args.global_limit,
        kind_limits=_kind_limits(args.kind_limit),
        status_callback=capability.publish,
    )
    stop_event = threading.Event()

    def _stop(_signum, _frame) -> None:
        stop_event.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    if args.once:
        result = worker.run_once(stop_event=stop_event)
        logger.info("[job_worker] once result={}", result)
        return 0
    logger.info("[job_worker] started worker_id={} kinds={}", worker_id, sorted(worker.handlers))
    worker.run_forever(stop_event=stop_event)
    logger.info("[job_worker] stopped worker_id={}", worker_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
