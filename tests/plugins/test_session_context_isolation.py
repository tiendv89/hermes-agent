"""Tests for G2 — strict session-scoped feature_id resolution (thread-context-isolation/T2).

Covers:
- Two parallel sessions with different feature_ids each resolve their own feature_id.
- Session A completes; session B starts with a different feature_id — B's feature_id
  is resolved correctly even when A's stale thread-local would give the wrong value.
- feature_id is empty after resolution → _write_artifact rejects with a clear error.
- get_context_for_session no longer falls back to thread-local for unknown sessions.
- _get_session_context uses agent_session_id + per-session dict (not raw thread-local).
- _resolve_ids uses per-session dict via get_agent_session_id() + get_context_for_session().
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------


def _clean_plugin_modules():
    for key in list(sys.modules.keys()):
        if key.startswith("plugins"):
            del sys.modules[key]


def _load_context():
    _clean_plugin_modules()
    spec = importlib.util.spec_from_file_location(
        "plugins.context",
        REPO_ROOT / "plugins" / "context.py",
        submodule_search_locations=[str(REPO_ROOT / "plugins")],
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins"
    sys.modules["plugins.context"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_modules():
    _clean_plugin_modules()
    yield
    _clean_plugin_modules()


# ---------------------------------------------------------------------------
# get_context_for_session — no thread-local fallback
# ---------------------------------------------------------------------------


class TestGetContextForSessionNoFallback:
    def test_returns_per_session_values(self):
        """Returns (workspace_id, feature_id) from the per-session store."""
        ctx = _load_context()
        ctx.set_context("sess-1", "ws-1", "feat-1")
        ws, feat = ctx.get_context_for_session("sess-1")
        assert ws == "ws-1"
        assert feat == "feat-1"

    def test_unknown_session_returns_empty_strings(self):
        """An unknown session_id returns ('', '') — no thread-local fallback."""
        ctx = _load_context()
        ctx.set_context("sess-A", "ws-A", "feat-A")
        ws, feat = ctx.get_context_for_session("unknown-session")
        assert ws == ""
        assert feat == ""

    def test_empty_session_id_returns_empty_strings(self):
        """Empty session_id returns ('', '') — not the thread-local."""
        ctx = _load_context()
        ctx.set_context("sess-B", "ws-B", "feat-B")
        ws, feat = ctx.get_context_for_session("")
        assert ws == ""
        assert feat == ""

    def test_two_parallel_sessions_isolated(self):
        """Two sessions stored in _by_session resolve independently."""
        ctx = _load_context()
        ctx.set_context("session-alpha", "ws-alpha", "feat-alpha")
        ctx.set_context("session-beta", "ws-beta", "feat-beta")

        ws_a, feat_a = ctx.get_context_for_session("session-alpha")
        ws_b, feat_b = ctx.get_context_for_session("session-beta")

        assert ws_a == "ws-alpha" and feat_a == "feat-alpha"
        assert ws_b == "ws-beta" and feat_b == "feat-beta"

    def test_stale_thread_local_does_not_leak_feature_id(self):
        """Session B does not inherit session A's feature_id via stale thread-local.

        Simulates ThreadPoolExecutor thread reuse: thread-local keeps session A's
        values after its turn completes, but session B's lookup returns B's values.
        """
        ctx = _load_context()
        ctx.set_context("sess-A", "ws-A", "feat-A")
        ctx.clear_context("sess-A")
        # Thread-local still has "feat-A" (simulating a reused thread)
        assert ctx.get_feature_id() == "feat-A"

        ctx.set_context("sess-B", "ws-B", "feat-B")
        ctx.set_agent_context("sess-B", None)

        ws_b, feat_b = ctx.get_context_for_session("sess-B")
        assert ws_b == "ws-B"
        assert feat_b == "feat-B"


# ---------------------------------------------------------------------------
# _get_session_context — uses agent_session_id + per-session dict
# ---------------------------------------------------------------------------


class TestGetSessionContextIsolation:
    def _load_plugins(self):
        _clean_plugin_modules()
        init_path = REPO_ROOT / "plugins" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            "plugins",
            init_path,
            submodule_search_locations=[str(REPO_ROOT / "plugins")],
        )
        mod = importlib.util.module_from_spec(spec)
        mod.__package__ = "plugins"
        mod.__path__ = [str(REPO_ROOT / "plugins")]
        sys.modules["plugins"] = mod
        spec.loader.exec_module(mod)
        return mod

    def test_returns_none_when_no_agent_session_id(self):
        """Without agent_session_id, _get_session_context returns None (G10 skipped)."""
        plugins_mod = self._load_plugins()
        result = plugins_mod._get_session_context()
        assert result is None

    def test_returns_context_via_agent_session_id(self):
        """Resolves workspace/feature from per-session dict using agent_session_id."""
        plugins_mod = self._load_plugins()
        import plugins.context as ctx

        ctx.set_context("turn-1", "ws-T1", "feat-T1")
        ctx.set_agent_context("turn-1", None)

        result = plugins_mod._get_session_context()
        assert result is not None
        assert result["workspace_id"] == "ws-T1"
        assert result["feature_id"] == "feat-T1"

    def test_stale_thread_local_does_not_affect_result(self):
        """Stale thread-local from session A does not appear in session B's context."""
        plugins_mod = self._load_plugins()
        import plugins.context as ctx

        # Session A populates thread-local
        ctx.set_context("sess-A", "ws-A", "feat-A")
        ctx.clear_context("sess-A")
        assert ctx.get_workspace_id() == "ws-A"  # stale thread-local still present

        ctx.set_context("sess-B", "ws-B", "feat-B")
        ctx.set_agent_context("sess-B", None)

        result = plugins_mod._get_session_context()
        assert result is not None
        assert result["workspace_id"] == "ws-B", "Must resolve B, not stale A"
        assert result["feature_id"] == "feat-B", "Must resolve B, not stale A"

    def test_two_sessions_return_own_context(self):
        """Switching agent_session_id between calls returns each session's own context."""
        plugins_mod = self._load_plugins()
        import plugins.context as ctx

        ctx.set_context("sess-X", "ws-X", "feat-X")
        ctx.set_context("sess-Y", "ws-Y", "feat-Y")

        ctx.set_agent_context("sess-X", None)
        result_x = plugins_mod._get_session_context()
        assert result_x is not None
        assert result_x["workspace_id"] == "ws-X"
        assert result_x["feature_id"] == "feat-X"

        ctx.set_agent_context("sess-Y", None)
        result_y = plugins_mod._get_session_context()
        assert result_y is not None
        assert result_y["workspace_id"] == "ws-Y"
        assert result_y["feature_id"] == "feat-Y"


# ---------------------------------------------------------------------------
# _resolve_ids — uses per-session dict, not raw thread-local
# ---------------------------------------------------------------------------


class TestResolveIdsIsolation:
    def _load_artifacts(self):
        """Load only context and artifacts (no full plugins/__init__ needed)."""
        _clean_plugin_modules()
        # Load context first
        ctx_spec = importlib.util.spec_from_file_location(
            "plugins.context",
            REPO_ROOT / "plugins" / "context.py",
            submodule_search_locations=[str(REPO_ROOT / "plugins")],
        )
        ctx_mod = importlib.util.module_from_spec(ctx_spec)
        ctx_mod.__package__ = "plugins"
        sys.modules["plugins.context"] = ctx_mod
        ctx_spec.loader.exec_module(ctx_mod)

        # Provide stub for write_document_content (storage-service not available in tests)
        import types
        storage_stub = types.ModuleType("plugins.clients.storage_service_client")
        storage_stub.StorageServiceError = Exception
        storage_stub.write_document_content = lambda *a, **kw: {"version_id": "v1"}
        sys.modules["plugins.clients"] = types.ModuleType("plugins.clients")
        sys.modules["plugins.clients.storage_service_client"] = storage_stub

        # Stub plugins.validation
        val_stub = types.ModuleType("plugins.validation")
        val_stub._validate_id = lambda value, name: None
        sys.modules["plugins.validation"] = val_stub

        # Load artifacts
        art_spec = importlib.util.spec_from_file_location(
            "plugins.tools.artifacts",
            REPO_ROOT / "plugins" / "tools" / "artifacts.py",
        )
        art_mod = importlib.util.module_from_spec(art_spec)
        art_mod.__package__ = "plugins.tools"
        sys.modules["plugins.tools.artifacts"] = art_mod
        art_spec.loader.exec_module(art_mod)
        return ctx_mod, art_mod

    def test_explicit_ids_returned_as_is(self):
        """Explicit workspace_id and feature_id are passed through unchanged."""
        ctx, art = self._load_artifacts()
        ctx.set_context("sess-1", "ws-ctx", "feat-ctx")
        ctx.set_agent_context("sess-1", None)
        wid, fid = art._resolve_ids("ws-explicit", "feat-explicit")
        assert wid == "ws-explicit"
        assert fid == "feat-explicit"

    def test_resolves_from_per_session_dict(self):
        """Omitting workspace/feature resolves them from the per-session store."""
        ctx, art = self._load_artifacts()
        ctx.set_context("sess-2", "ws-2", "feat-2")
        ctx.set_agent_context("sess-2", None)
        wid, fid = art._resolve_ids("", "")
        assert wid == "ws-2"
        assert fid == "feat-2"

    def test_stale_thread_local_not_used(self):
        """Stale thread-local from session A does not bleed into session B."""
        ctx, art = self._load_artifacts()
        ctx.set_context("sess-A", "ws-A", "feat-A")
        ctx.clear_context("sess-A")  # A's turn is done; thread-local stays stale

        ctx.set_context("sess-B", "ws-B", "feat-B")
        ctx.set_agent_context("sess-B", None)

        wid, fid = art._resolve_ids("", "")
        assert wid == "ws-B", "Must resolve B, not stale A"
        assert fid == "feat-B", "Must resolve B, not stale A"

    def test_empty_when_no_session_registered(self):
        """When no session is registered and none is active, returns ('', '')."""
        _ctx, art = self._load_artifacts()
        # No set_context or set_agent_context
        wid, fid = art._resolve_ids("", "")
        assert wid == ""
        assert fid == ""


# ---------------------------------------------------------------------------
# _write_artifact scope guard — rejects when feature_id is empty
# ---------------------------------------------------------------------------


class TestWriteArtifactScopeGuard:
    def _load_artifacts(self):
        """Load artifacts with minimal stubs — no full stack needed."""
        _clean_plugin_modules()
        import types

        # Stub plugins.context
        ctx_spec = importlib.util.spec_from_file_location(
            "plugins.context",
            REPO_ROOT / "plugins" / "context.py",
            submodule_search_locations=[str(REPO_ROOT / "plugins")],
        )
        ctx_mod = importlib.util.module_from_spec(ctx_spec)
        ctx_mod.__package__ = "plugins"
        sys.modules["plugins.context"] = ctx_mod
        ctx_spec.loader.exec_module(ctx_mod)

        storage_stub = types.ModuleType("plugins.clients.storage_service_client")
        storage_stub.StorageServiceError = Exception
        storage_stub.write_document_content = lambda *a, **kw: {"version_id": "v1"}
        sys.modules["plugins.clients"] = types.ModuleType("plugins.clients")
        sys.modules["plugins.clients.storage_service_client"] = storage_stub

        val_stub = types.ModuleType("plugins.validation")
        val_stub._validate_id = lambda value, name: None
        sys.modules["plugins.validation"] = val_stub

        art_spec = importlib.util.spec_from_file_location(
            "plugins.tools.artifacts",
            REPO_ROOT / "plugins" / "tools" / "artifacts.py",
        )
        art_mod = importlib.util.module_from_spec(art_spec)
        art_mod.__package__ = "plugins.tools"
        sys.modules["plugins.tools.artifacts"] = art_mod
        art_spec.loader.exec_module(art_mod)
        return ctx_mod, art_mod

    def test_empty_feature_id_returns_clear_error(self):
        """_write_artifact returns ok:False with a clear error when feature_id is empty."""
        ctx, art = self._load_artifacts()
        # Make was_context_gathered return True so we reach the scope guard
        with patch.object(ctx, "was_context_gathered", return_value=True):
            result = art._write_artifact(
                workspace_id="ws-1",
                feature_id="",
                filename="product-spec.md",
                content="# Spec",
                commit_message="doc: add spec",
                stage="product_spec",
            )
        assert result["ok"] is False
        assert "feature_id" in result["error"].lower()
        assert "session" in result["error"].lower()

    def test_none_feature_id_rejected_before_storage_call(self):
        """_write_artifact with empty feature_id is rejected before storage-service."""
        ctx, art = self._load_artifacts()
        storage_stub = sys.modules["plugins.clients.storage_service_client"]
        storage_stub.write_document_content = lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("storage_service must not be called")
        )
        with patch.object(ctx, "was_context_gathered", return_value=True):
            result = art._write_artifact(
                workspace_id="ws-1",
                feature_id="",
                filename="product-spec.md",
                content="# Spec",
                commit_message="doc: add spec",
                stage="product_spec",
            )
        assert result["ok"] is False

    def test_valid_feature_id_passes_scope_guard(self):
        """_write_artifact with a valid feature_id is not rejected by the scope guard."""
        ctx, art = self._load_artifacts()
        with patch.object(ctx, "was_context_gathered", return_value=True):
            result = art._write_artifact(
                workspace_id="ws-1",
                feature_id="feat-valid",
                filename="product-spec.md",
                content="# Spec",
                commit_message="doc: add spec",
                stage="product_spec",
            )
        # Should NOT be rejected by the scope guard — storage_service_client returns ok
        # (our stub returns {"version_id": "v1"}, so ok:True path is reached)
        assert result.get("ok") is True

    def test_feature_id_none_from_resolved_context_is_rejected(self):
        """When feature_id resolves to empty string from context, _write_artifact rejects."""
        ctx, art = self._load_artifacts()
        # Register a session with no feature_id (feature_id="")
        ctx.set_context("sess-no-feat", "ws-1", "")
        ctx.set_agent_context("sess-no-feat", None)
        with patch.object(ctx, "was_context_gathered", return_value=True):
            result = art._write_artifact(
                workspace_id="ws-1",
                feature_id="",
                filename="product-spec.md",
                content="# Spec",
                commit_message="doc: add spec",
                stage="product_spec",
            )
        assert result["ok"] is False
        assert "feature_id" in result["error"].lower()
