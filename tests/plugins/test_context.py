"""Unit tests for plugins/context.py — identity threading (T1).

Covers:
  - set_context with user_id/org_id stores values readable via get_user_id()/get_org_id()
  - Absent identity (no user_id/org_id args) resolves to empty strings (backward-compat)
  - get_context_for_session returns (workspace_id, feature_id) only (existing contract)
  - clear_context removes the session entry
  - thread isolation: identity set on one thread is not visible on a fresh thread
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins"


def _load_context():
    """Load plugins.context in isolation (no plugins.__init__ needed)."""
    spec = importlib.util.spec_from_file_location(
        "plugins.context",
        PLUGIN_DIR / "context.py",
        submodule_search_locations=[str(PLUGIN_DIR)],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins"
    sys.modules["plugins.context"] = mod
    spec.loader.exec_module(mod)
    return mod


ctx = _load_context()


@pytest.fixture(autouse=True)
def _clean_context():
    """Reset module-level state before and after each test."""
    ctx._by_session.clear()
    ctx._context_gathered.clear()
    yield
    ctx._by_session.clear()
    ctx._context_gathered.clear()


def test_set_context_stores_identity():
    """Identity set on a turn is readable via get_user_id() / get_org_id()."""
    ctx.set_context("sess1", "ws-1", "feat-1", user_id="user-abc", org_id="org-xyz")
    assert ctx.get_user_id() == "user-abc"
    assert ctx.get_org_id() == "org-xyz"


def test_set_context_stores_workspace_feature():
    """Existing workspace_id / feature_id getters still work after adding identity."""
    ctx.set_context("sess2", "ws-2", "feat-2", user_id="u", org_id="o")
    assert ctx.get_workspace_id() == "ws-2"
    assert ctx.get_feature_id() == "feat-2"


def test_absent_identity_resolves_to_empty_strings():
    """Calling set_context without user_id/org_id produces empty strings (backward-compat)."""
    ctx.set_context("sess3", "ws-3", "feat-3")
    assert ctx.get_user_id() == ""
    assert ctx.get_org_id() == ""


def test_get_context_for_session_returns_workspace_feature_only():
    """get_context_for_session returns the (workspace_id, feature_id) 2-tuple (existing contract)."""
    ctx.set_context("sess4", "ws-4", "feat-4", user_id="u", org_id="o")
    ws, feat = ctx.get_context_for_session("sess4")
    assert ws == "ws-4"
    assert feat == "feat-4"


def test_get_context_for_session_missing_falls_back_to_thread_local():
    """A session not in _by_session falls back to the thread-local."""
    ctx.set_context("sess5", "ws-5", "feat-5")
    ws, feat = ctx.get_context_for_session("unknown-session")
    assert ws == "ws-5"
    assert feat == "feat-5"


def test_clear_context_removes_session():
    """clear_context drops the session from _by_session."""
    ctx.set_context("sess6", "ws-6", "feat-6", user_id="u", org_id="o")
    assert "sess6" in ctx._by_session
    ctx.clear_context("sess6")
    assert "sess6" not in ctx._by_session


def test_identity_isolated_per_thread():
    """Identity written on the main thread is not visible on a fresh worker thread."""
    results: dict[str, str] = {}

    def _other_thread() -> None:
        results["user_id"] = ctx.get_user_id()
        results["org_id"] = ctx.get_org_id()

    ctx.set_context("sess7", "ws-7", "feat-7", user_id="main-user", org_id="main-org")

    t = threading.Thread(target=_other_thread)
    t.start()
    t.join()

    assert results["user_id"] == "", "Thread-local must be empty on a fresh thread"
    assert results["org_id"] == "", "Thread-local must be empty on a fresh thread"


def test_by_session_stores_four_tuple():
    """Internal _by_session stores (workspace_id, feature_id, user_id, org_id)."""
    ctx.set_context("sess8", "ws-8", "feat-8", user_id="u8", org_id="o8")
    stored = ctx._by_session["sess8"]
    assert stored == ("ws-8", "feat-8", "u8", "o8")
