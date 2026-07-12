"""Tests for read_document → read_file rename/alias (T2).

Covers:
  - read_file: delegates to the same logic as read_document (alias equivalence)
  - read_document: logs a deprecation warning on invocation
  - read_file: does NOT log a deprecation warning
  - Both tools return identical results for go-owned features
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import asyncio

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_OWNER = "testorg"
_REPO = "testws"
_VERSION_ID = "v1-uuid"
_PRODUCT_SPEC_CONTENT = "# Product Spec\n\nContent here.\n"


def _make_feature_detail(owner: str = "go"):
    return {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "product_spec",
        "status": "in_design",
        "owner": owner,
        "init_pr_url": None,
        "stages": {},
    }


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("STORAGE_SERVICE_URL", raising=False)
    monkeypatch.delenv("STORAGE_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)


def _sync_run_async(coro):
    if asyncio.iscoroutine(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return coro


def _ensure_plugins_pkg():
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg


def _load_module_file(name: str, file_path: Path) -> types.ModuleType:
    _ensure_plugins_pkg()
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = ".".join(name.split(".")[:-1])
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_module_wbc(mod, feature_detail, workspace_ctx=None):
    if workspace_ctx is None:
        workspace_ctx = {
            "management_repo": _REPO,
            "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
        }
    wbc_name = "src.services.workflow_backend_client"
    if wbc_name not in sys.modules:
        wbc_mod = types.ModuleType(wbc_name)
        for parent in ("src", "src.services"):
            if parent not in sys.modules:
                sys.modules[parent] = types.ModuleType(parent)
        sys.modules[wbc_name] = wbc_mod
    else:
        wbc_mod = sys.modules[wbc_name]

    wbc_mod.get_feature_detail = AsyncMock(return_value=feature_detail)
    wbc_mod.get_workspace_context = AsyncMock(return_value=workspace_ctx)
    wbc_mod.run_async = _sync_run_async
    mod.get_feature_detail = wbc_mod.get_feature_detail
    mod.get_workspace_context = wbc_mod.get_workspace_context
    mod.run_async = _sync_run_async


def _make_fake_ssc(*, read_return=None, error=None):
    class _Error(Exception):
        def __init__(self, msg="", *, reason_code="", status=0):
            super().__init__(msg)
            self.reason_code = reason_code
            self.status = status

    fake = types.ModuleType("plugins.storage_service_client")
    fake.StorageServiceError = _Error
    if error is not None:
        fake.read_document_content = MagicMock(side_effect=error)
        fake.write_document_content = MagicMock(side_effect=error)
    else:
        fake.read_document_content = MagicMock(
            return_value=read_return or {"content": "", "version_id": None}
        )
        fake.write_document_content = MagicMock(return_value={"ok": True, "version_id": _VERSION_ID})
    sys.modules["plugins.storage_service_client"] = fake
    return fake


def _inject_ssc(mod, fake_ssc):
    mod.StorageServiceError = fake_ssc.StorageServiceError
    mod.read_document_content = fake_ssc.read_document_content


def _enter_patches(*patches_list) -> ExitStack:
    stack = ExitStack()
    for p in patches_list:
        stack.enter_context(p)
    return stack


def _ctx_patches():
    return [
        patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
        patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
        patch("plugins.context.get_user_id", return_value="user-1"),
        patch("plugins.context.get_org_id", return_value="org-1"),
        patch("plugins.context.mark_context_gathered"),
    ]


class TestReadFileAliasEquivalence:
    """read_file and read_document resolve to the same logic and return identical results."""

    def _load(self):
        fake_ssc = _make_fake_ssc(
            read_return={"content": _PRODUCT_SPEC_CONTENT, "version_id": _VERSION_ID}
        )
        mod = _load_module_file("plugins.tools.read", REPO_ROOT / "plugins" / "tools" / "read.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc(mod, fake_ssc)
        return mod, fake_ssc

    def test_handle_read_file_function_exists(self):
        mod, _ = self._load()
        assert hasattr(mod, "handle_read_file"), "handle_read_file must be defined in read.py"
        assert callable(mod.handle_read_file)

    def test_handle_read_document_function_exists(self):
        mod, _ = self._load()
        assert hasattr(mod, "handle_read_document"), "handle_read_document must remain in read.py"
        assert callable(mod.handle_read_document)

    def test_handle_read_file_is_not_handle_read_document(self):
        """They should be different objects (wrapper vs implementation)."""
        mod, _ = self._load()
        assert mod.handle_read_file is not mod.handle_read_document

    def test_read_file_returns_correct_result(self):
        mod, fake_ssc = self._load()

        with _enter_patches(*_ctx_patches()):
            result = mod.handle_read_file(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["content"] == _PRODUCT_SPEC_CONTENT
        assert result["exists"] is True
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "product_spec.md",
            user_id="user-1", org_id="org-1",
        )

    def test_read_file_and_read_document_return_same_result(self):
        """Both handlers produce identical results given the same inputs."""
        fake_ssc = _make_fake_ssc(
            read_return={"content": _PRODUCT_SPEC_CONTENT, "version_id": _VERSION_ID}
        )
        mod = _load_module_file("plugins.tools.read", REPO_ROOT / "plugins" / "tools" / "read.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc(mod, fake_ssc)

        with _enter_patches(*_ctx_patches()):
            result_file = mod.handle_read_file(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        # Reset mock call count and reload to ensure independent runs
        fake_ssc.read_document_content.reset_mock()

        with _enter_patches(*_ctx_patches()):
            result_doc = mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result_file["ok"] == result_doc["ok"]
        assert result_file["content"] == result_doc["content"]
        assert result_file["exists"] == result_doc["exists"]
        assert result_file["document"] == result_doc["document"]


class TestReadDocumentDeprecationWarning:
    """read_document logs a deprecation warning; read_file does not."""

    def _load(self):
        fake_ssc = _make_fake_ssc(
            read_return={"content": _PRODUCT_SPEC_CONTENT, "version_id": _VERSION_ID}
        )
        mod = _load_module_file("plugins.tools.read", REPO_ROOT / "plugins" / "tools" / "read.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc(mod, fake_ssc)
        return mod

    def test_read_document_logs_deprecation_warning(self, caplog):
        mod = self._load()

        with caplog.at_level(logging.WARNING, logger="plugins.tools.read"):
            with _enter_patches(*_ctx_patches()):
                mod.handle_read_document(
                    document="product_spec",
                    workspace_id=_WORKSPACE_ID,
                    feature_id=_FEATURE_ID,
                )

        deprecation_logs = [
            r for r in caplog.records
            if "deprecated" in r.message.lower()
        ]
        assert len(deprecation_logs) >= 1, (
            "handle_read_document must emit a deprecation warning. "
            f"Captured: {[r.message for r in caplog.records]}"
        )
        assert "read_file" in deprecation_logs[0].message, (
            "Deprecation warning must mention read_file as the replacement"
        )

    def test_read_file_does_not_log_deprecation_warning(self, caplog):
        mod = self._load()

        with caplog.at_level(logging.WARNING, logger="plugins.tools.read"):
            with _enter_patches(*_ctx_patches()):
                mod.handle_read_file(
                    document="product_spec",
                    workspace_id=_WORKSPACE_ID,
                    feature_id=_FEATURE_ID,
                )

        deprecation_logs = [
            r for r in caplog.records
            if "deprecated" in r.message.lower()
        ]
        assert len(deprecation_logs) == 0, (
            "handle_read_file must NOT emit any deprecation warning. "
            f"Unexpected: {[r.message for r in deprecation_logs]}"
        )

    def test_read_document_deprecation_does_not_alter_return(self, caplog):
        mod = self._load()

        with caplog.at_level(logging.WARNING):
            with _enter_patches(*_ctx_patches()):
                result = mod.handle_read_document(
                    document="product_spec",
                    workspace_id=_WORKSPACE_ID,
                    feature_id=_FEATURE_ID,
                )

        assert result["ok"] is True
        assert result["content"] == _PRODUCT_SPEC_CONTENT


class TestToolRegistrationNames:
    """Verify plugins/__init__.py registers both read_document and read_file."""

    def test_both_tools_in_registry(self):
        """Both read_document and read_file must appear in the _TOOLS tuple."""
        # We test the constant _TOOLS by inspecting the file directly.
        init_path = REPO_ROOT / "plugins" / "__init__.py"
        text = init_path.read_text(encoding="utf-8")
        assert '"read_document"' in text, "read_document must remain in _TOOLS"
        assert '"read_file"' in text, "read_file must be added to _TOOLS"

    def test_read_file_entry_uses_canonical_handler(self):
        """The read_file entry must reference handle_read_file (not the deprecated wrapper)."""
        init_path = REPO_ROOT / "plugins" / "__init__.py"
        text = init_path.read_text(encoding="utf-8")
        # Find the block for read_file
        idx = text.find('"read_file"')
        assert idx != -1, "read_file must be in __init__.py"
        # The surrounding context should reference handle_read_file
        block = text[idx:idx + 400]
        assert "handle_read_file" in block, (
            "read_file entry must reference handle_read_file, not handle_read_document"
        )

    def test_read_document_entry_uses_deprecated_wrapper(self):
        """The read_document entry must reference handle_read_document (the deprecation wrapper)."""
        init_path = REPO_ROOT / "plugins" / "__init__.py"
        text = init_path.read_text(encoding="utf-8")
        idx = text.find('"read_document"')
        assert idx != -1
        block = text[idx:idx + 400]
        assert "handle_read_document" in block, (
            "read_document entry must reference handle_read_document"
        )
