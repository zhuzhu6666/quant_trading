"""Progress callback contract — injected into service functions."""
from typing import Callable

# (step_name, percent_0_to_100, human_message)
ProgressCB = Callable[[str, float, str], None]


def noop_progress(_step: str, _pct: float, _msg: str) -> None:
    """Default no-op progress callback."""
    pass
