"""Shared ID validation, used before interpolating feature/task ids into
GitHub API paths or workflow-backend URLs.

Split out of plugins/db.py (which owned it only incidentally, as the module
that also happened to interpolate ids into SQL parameters) so it has no
dependency on how workspace/feature data is fetched.
"""

from __future__ import annotations

import re

_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def _validate_id(value: str, name: str) -> None:
    """Raise ValueError if value contains characters unsafe for URL path interpolation."""
    if not _ID_RE.match(value):
        raise ValueError(f"Invalid {name}: {value!r}")
