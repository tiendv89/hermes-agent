"""Tests for plugins.tools.model_resolution.

Covers:
  - resolve_task_models: single agent task resolves model_id
  - resolve_task_models: human/either tasks are passed through unchanged
  - resolve_task_models: all tasks valid → ok=True
  - resolve_task_models: one unresolved display name → ok=False
  - resolve_task_models: candidates endpoint unavailable → graceful skip (ok=True)
  - resolve_task_models: repo UUID not found → graceful skip (ok=True)
  - resolve_task_models: empty task list → ok=True
  - resolve_task_models: agent task with blank model → skipped (no model_id)
  - format_unresolved_error: output contains task name and valid alternatives
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith(("plugins", "src"))]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith(("plugins", "src"))]
    for k in keys:
        del sys.modules[k]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _WorkflowBackendError(Exception):
    def __init__(self, message: str, *, reason_code: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.status = status


_CANDIDATE_SONNET = {"model_id": "uuid-sonnet", "display_name": "Claude Sonnet 4.6"}
_CANDIDATE_OPUS = {"model_id": "uuid-opus", "display_name": "Claude Opus 4.8"}
_CANDIDATES = [_CANDIDATE_SONNET, _CANDIDATE_OPUS]


def _make_task(
    name: str,
    actor_type: str = "agent",
    model: str = "Claude Sonnet 4.6",
    repo: str = "hermes-agent",
) -> dict:
    return {
        "name": name,
        "title": f"Task {name}",
        "actor_type": actor_type,
        "model": model,
        "repo": repo,
        "depends_on": [],
    }


def _load_model_resolution_with_wbc_mock(
    *,
    repo_uuid: str | None = "uuid-repo",
    candidates: list | None = None,
    repo_lookup_raises=None,
    candidates_raises=None,
):
    """Load model_resolution module with a mocked workflow_backend_client."""
    if candidates is None:
        candidates = _CANDIDATES

    wbc_mock = MagicMock()
    wbc_mock.WorkflowBackendError = _WorkflowBackendError

    if repo_lookup_raises is not None:
        wbc_mock.get_workspace_repo_by_slug = AsyncMock(side_effect=repo_lookup_raises)
    else:
        wbc_mock.get_workspace_repo_by_slug = AsyncMock(return_value=repo_uuid)

    if candidates_raises is not None:
        wbc_mock.get_implementation_candidates = AsyncMock(side_effect=candidates_raises)
    else:
        wbc_mock.get_implementation_candidates = AsyncMock(
            return_value={"candidates": candidates, "suggested_model_id": None}
        )

    # run_async wraps an awaitable — just call it synchronously in tests.
    wbc_mock.run_async = lambda coro: coro.__next__() if False else _run_coro(coro)

    sys.modules["src"] = MagicMock()
    sys.modules["src.services"] = MagicMock()
    sys.modules["src.services.workflow_backend_client"] = wbc_mock

    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg
    if "plugins.tools" not in sys.modules:
        tools_pkg = types.ModuleType("plugins.tools")
        tools_pkg.__path__ = [str(REPO_ROOT / "plugins" / "tools")]
        tools_pkg.__package__ = "plugins.tools"
        sys.modules["plugins.tools"] = tools_pkg

    path = REPO_ROOT / "plugins" / "tools" / "model_resolution.py"
    spec = importlib.util.spec_from_file_location("plugins.tools.model_resolution", path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins.tools"
    sys.modules["plugins.tools.model_resolution"] = mod
    spec.loader.exec_module(mod)
    return mod


def _run_coro(coro):
    """Drive a simple coroutine to completion (no nested awaits in test mocks)."""
    import asyncio
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


# Patch run_async in the wbc mock to actually execute the coroutine.
def _make_run_async():
    def run_async(coro):
        return _run_coro(coro)
    return run_async


# ---------------------------------------------------------------------------
# Tests: resolve_task_models
# ---------------------------------------------------------------------------


class TestResolveTaskModels:
    def _load(self, **kw):
        mod = _load_model_resolution_with_wbc_mock(**kw)
        # Patch run_async to drive coroutines.
        sys.modules["src.services.workflow_backend_client"].run_async = _make_run_async()
        return mod

    def test_single_agent_task_resolves_model_id(self):
        mod = self._load()
        tasks = [_make_task("T1", model="Claude Sonnet 4.6")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert t1["model_id"] == "uuid-sonnet"

    def test_human_task_skipped_no_model_id(self):
        mod = self._load()
        tasks = [_make_task("T1", actor_type="human", model="Claude Sonnet 4.6")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert "model_id" not in t1

    def test_either_task_skipped_no_model_id(self):
        mod = self._load()
        tasks = [_make_task("T1", actor_type="either", model="Claude Sonnet 4.6")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert "model_id" not in t1

    def test_all_valid_returns_ok_true(self):
        mod = self._load()
        tasks = [
            _make_task("T1", model="Claude Sonnet 4.6"),
            _make_task("T2", model="Claude Opus 4.8"),
        ]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        assert len(result["tasks"]) == 2

    def test_one_unresolved_returns_ok_false(self):
        mod = self._load()
        tasks = [_make_task("T1", model="Nonexistent Model")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is False
        assert len(result["unresolved"]) == 1
        assert result["unresolved"][0]["task_name"] == "T1"
        assert result["unresolved"][0]["display_name"] == "Nonexistent Model"

    def test_unresolved_lists_valid_alternatives(self):
        mod = self._load()
        tasks = [_make_task("T1", model="Bad Model")]
        result = mod.resolve_task_models("ws-1", tasks)
        alts = result["unresolved"][0]["valid_alternatives"]
        assert "Claude Sonnet 4.6" in alts
        assert "Claude Opus 4.8" in alts

    def test_candidates_unavailable_graceful_skip(self):
        mod = self._load(candidates_raises=_WorkflowBackendError("unreachable"))
        tasks = [_make_task("T1", model="Claude Sonnet 4.6")]
        result = mod.resolve_task_models("ws-1", tasks)
        # Graceful degradation: endpoint unavailable → skip, ok=True, no model_id.
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert "model_id" not in t1

    def test_repo_not_found_graceful_skip(self):
        mod = self._load(repo_uuid=None)
        tasks = [_make_task("T1", model="Claude Sonnet 4.6")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert "model_id" not in t1

    def test_empty_task_list_returns_ok_true(self):
        mod = self._load()
        result = mod.resolve_task_models("ws-1", [])
        assert result["ok"] is True
        assert result["tasks"] == []

    def test_agent_task_blank_model_not_resolved(self):
        mod = self._load()
        tasks = [_make_task("T1", model="")]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        t1 = next(t for t in result["tasks"] if t["name"] == "T1")
        assert "model_id" not in t1

    def test_model_match_is_case_sensitive(self):
        mod = self._load()
        tasks = [_make_task("T1", model="claude sonnet 4.6")]  # wrong case
        result = mod.resolve_task_models("ws-1", tasks)
        # Either resolves (if backend is lenient) or fails with unresolved.
        # The spec says case-sensitive — so this should be unresolved.
        assert result["ok"] is False

    def test_tasks_list_includes_non_agent_tasks(self):
        """Non-agent tasks are included in the returned list even when resolution runs."""
        mod = self._load()
        tasks = [
            _make_task("T1", actor_type="agent", model="Claude Sonnet 4.6"),
            _make_task("T2", actor_type="human", model=""),
        ]
        result = mod.resolve_task_models("ws-1", tasks)
        assert result["ok"] is True
        assert len(result["tasks"]) == 2


# ---------------------------------------------------------------------------
# Tests: format_unresolved_error
# ---------------------------------------------------------------------------


class TestFormatUnresolvedError:
    def _load(self):
        return _load_model_resolution_with_wbc_mock()

    def test_output_contains_task_name(self):
        mod = self._load()
        unresolved = [
            {"task_name": "T3", "display_name": "Old Model", "valid_alternatives": ["Claude Sonnet 4.6"]}
        ]
        msg = mod.format_unresolved_error(unresolved)
        assert "T3" in msg

    def test_output_contains_display_name(self):
        mod = self._load()
        unresolved = [
            {"task_name": "T3", "display_name": "Old Model", "valid_alternatives": []}
        ]
        msg = mod.format_unresolved_error(unresolved)
        assert "Old Model" in msg

    def test_output_contains_valid_alternatives(self):
        mod = self._load()
        unresolved = [
            {
                "task_name": "T3",
                "display_name": "Old Model",
                "valid_alternatives": ["Claude Sonnet 4.6", "Claude Opus 4.8"],
            }
        ]
        msg = mod.format_unresolved_error(unresolved)
        assert "Claude Sonnet 4.6" in msg

    def test_empty_alternatives_says_none_available(self):
        mod = self._load()
        unresolved = [
            {"task_name": "T3", "display_name": "Old Model", "valid_alternatives": []}
        ]
        msg = mod.format_unresolved_error(unresolved)
        assert "none available" in msg.lower() or "none" in msg.lower()

    def test_returns_string(self):
        mod = self._load()
        result = mod.format_unresolved_error([])
        assert isinstance(result, str)
