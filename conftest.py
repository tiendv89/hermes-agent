"""Pytest configuration — ensures repo root is first in sys.path.

This prevents tests/plugins/ from shadowing the actual plugins/ package
when tests do `from plugins import ...`.
"""
import sys
from pathlib import Path

_REPO_ROOT = str(Path(__file__).parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
elif sys.path[0] != _REPO_ROOT:
    sys.path.remove(_REPO_ROOT)
    sys.path.insert(0, _REPO_ROOT)

# Extra debug: print sys.path
def pytest_configure(config):
    import sys
    from pathlib import Path
    repo = str(Path(__file__).parent)
    # Ensure repo root always precedes tests/ in sys.path
    tests_dir = str(Path(__file__).parent / 'tests')
    if tests_dir in sys.path:
        sys.path.remove(tests_dir)
    if repo not in sys.path:
        sys.path.insert(0, repo)
    elif sys.path[0] != repo:
        sys.path.remove(repo)
        sys.path.insert(0, repo)
