import configparser
from importlib.metadata import PackageNotFoundError, requires, version
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYTEST_INI = PROJECT_ROOT / "pytest.ini"


def _pytest_filterwarnings() -> list[str]:
    parser = configparser.ConfigParser()
    parser.read(PYTEST_INI)
    raw = parser.get("pytest", "filterwarnings", fallback="")
    return [line.strip() for line in raw.splitlines() if line.strip()]


def test_protobuf_python312_warning_filter_is_precise():
    filters = _pytest_filterwarnings()

    protobuf_filters = [
        item for item in filters
        if "google\\.protobuf\\.internal\\.well_known_types" in item
    ]

    assert protobuf_filters == [
        "ignore:datetime\\.datetime\\.utcfromtimestamp\\(\\) is deprecated.*:"
        "DeprecationWarning:google\\.protobuf\\.internal\\.well_known_types"
    ]


def test_pytest_does_not_hide_all_deprecation_warnings():
    filters = _pytest_filterwarnings()

    broad_filters = {
        "ignore::DeprecationWarning",
        "ignore:::DeprecationWarning",
        "ignore:.*:DeprecationWarning",
    }
    assert not broad_filters.intersection(filters)


def test_ctrader_open_api_protobuf_dependency_contract_when_installed():
    try:
        reqs = requires("ctrader-open-api") or []
    except PackageNotFoundError:
        pytest.skip("ctrader-open-api is optional in some test environments")

    normalized = {req.replace(" ", "").lower() for req in reqs}

    assert version("ctrader-open-api") == "0.9.2"
    assert "protobuf(==3.20.1)" in normalized
    assert version("protobuf") == "3.20.1"
