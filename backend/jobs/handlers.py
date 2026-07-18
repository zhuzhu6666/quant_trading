"""Versioned persistent-job handler registry.

Imports stay inside handlers so the worker only loads the research stack for
the claimed job kind.  None of these handlers owns broker execution authority.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

from backend.jobs.progress import ProgressCB


JobHandler = Callable[[Mapping[str, Any], ProgressCB], Any]


def run_backtest_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.backtest_service import run_backtest

    return run_backtest(dict(params), progress)


def run_discover_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.discover_service import run_discovery

    return run_discovery(dict(params), progress)


def run_tuning_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.tuning_service import run_tuning

    return run_tuning(dict(params), progress)


def run_ab_test_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.ab_service import run_ab

    return run_ab(dict(params), progress)


def run_external_refresh_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.external_data_refresh import run_external_data_refresh

    return run_external_data_refresh(params, progress)


def run_sync_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.sync_service import run_sync_once

    return run_sync_once(dict(params), progress)


def run_factor_health_job(params: Mapping[str, Any], progress: ProgressCB) -> Any:
    from backend.services.factor_health_service import run_factor_health

    return run_factor_health(dict(params), progress)


def run_parameter_template_validation_job(
    params: Mapping[str, Any],
    progress: ProgressCB,
) -> Any:
    from backend.services.parameter_template_validation import (
        run_parameter_template_offline_validation,
    )

    return run_parameter_template_offline_validation(dict(params), progress)


PERSISTENT_JOB_HANDLERS: dict[str, JobHandler] = {
    "backtest": run_backtest_job,
    "discover": run_discover_job,
    "tuning": run_tuning_job,
    "ab_test": run_ab_test_job,
    "external_refresh": run_external_refresh_job,
    "sync": run_sync_job,
    "factor_health": run_factor_health_job,
    "parameter_template_validation": run_parameter_template_validation_job,
}


def persistent_job_handlers() -> dict[str, JobHandler]:
    return dict(PERSISTENT_JOB_HANDLERS)
