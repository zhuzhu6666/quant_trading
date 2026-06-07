"""Auth dependency stub. v1: hardcoded user. v2: JWT/OAuth."""
from typing import Annotated

from fastapi import Header


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
) -> str:
    """v1: return hardcoded user. v2: parse JWT from Authorization header."""
    # v1: no auth enforced; always return "zhu" for local dev
    return "zhu"
