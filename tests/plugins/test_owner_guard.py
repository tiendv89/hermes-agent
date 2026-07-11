"""Tests for the owner guard on the four document tools (T15).

Covers:
  - read_document: go-owned → proxies to storage-service (mocked)
  - read_document: ts-owned / absent owner → unchanged git-backed read (storage-service NOT called)
  - read_document: go-owned status.yaml → falls back to git (status lives in git)
  - write_product_spec: go-owned → proxies to storage-service; no git commit
  - write_product_spec: ts-owned / absent owner → storage-service NOT called
  - write_technical_design: go-owned → proxies to storage-service; no git commit
  - write_technical_design: ts-owned / absent owner → storage-service NOT called
  - edit_document: go-owned → read+apply+write via storage-service; no git commit
  - edit_document: ts-owned / absent owner → storage-service NOT called
  - storage_service_client unit tests (HTTP-level)
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_TOKEN = "ghp_testtoken"
_STORAGE_URL = "http://storage-service:8090"
_STORAGE_TOKEN = "storage-token-abc"
_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_OWNER = "testorg"
_REPO = "testws"
_COMMIT_SHA = "deadbeef123"
_VERSION_ID = "v1-uuid-abc"
_PRODUCT_SPEC_CONTENT = "# Product Spec\n\nContent here.\n"
_TD_CONTENT = "# Technical Design\n\nDesign here.\n"


def _make_workspace_context():
    return {
        "management_repo": _REPO,
        "repos": [{"id": _REPO, "github": f"https://github.com/{_OWNER}/{_REPO}"}],
    }


def _make_feature_detail(owner: str = "ts", stages: dict | None = None):
    return {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "product_spec",
        "status": "in_design",
        "owner": owner,
        "init_pr_url": None,
        "stages": stages if stages is not None else {},
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins/src modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins") or k.startswith("src")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("STORAGE_SERVICE_URL", raising=False)
    monkeypatch.delenv("STORAGE_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_plugins_pkg():
    if "plugins" not in sys.modules:
        pkg = types.ModuleType("plugins")
        pkg.__path__ = [str(REPO_ROOT / "plugins")]
        pkg.__package__ = "plugins"
        sys.modules["plugins"] = pkg


def _sync_run_async(coro):
    """Synchronously resolve an async coroutine."""
    if asyncio.iscoroutine(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()
    return coro


def _load_module_file(name: str, file_path: Path) -> types.ModuleType:
    """Load a module directly from its file, bypassing package __init__."""
    _ensure_plugins_pkg()
    spec = importlib.util.spec_from_file_location(name, file_path)
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = ".".join(name.split(".")[:-1])
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _patch_module_wbc(mod: types.ModuleType, feature_detail: dict, workspace_ctx: dict | None = None) -> None:
    """Patch workflow_backend_client calls on a loaded module object.

    For modules that import via ``from src.services.workflow_backend_client import ...``
    at module level, patching the module attribute works.  For modules that do
    *local* imports inside function bodies (like _write_artifact in artifacts.py),
    we must patch the source module directly so the local ``from ... import``
    picks up the mock.
    """
    if workspace_ctx is None:
        workspace_ctx = _make_workspace_context()

    # Patch the source module so local-import callsites pick up the mocks.
    import sys
    wbc_name = "src.services.workflow_backend_client"
    if wbc_name not in sys.modules:
        wbc_mod = types.ModuleType(wbc_name)
        # Ensure parent packages exist
        for parent in ("src", "src.services"):
            if parent not in sys.modules:
                sys.modules[parent] = types.ModuleType(parent)
        sys.modules[wbc_name] = wbc_mod
    else:
        wbc_mod = sys.modules[wbc_name]

    wbc_mod.get_feature_detail = AsyncMock(return_value=feature_detail)
    wbc_mod.get_workspace_context = AsyncMock(return_value=workspace_ctx)
    wbc_mod.run_async = _sync_run_async

    # Also patch module-level attributes for modules that import at the top.
    mod.get_feature_detail = wbc_mod.get_feature_detail
    mod.get_workspace_context = wbc_mod.get_workspace_context
    mod.run_async = _sync_run_async


def _make_fake_ssc(*, read_return=None, write_return=None, error=None):
    """Build a fake storage_service_client module."""
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
        fake.read_document_content = MagicMock(return_value=read_return or {"content": "", "version_id": None})
        fake.write_document_content = MagicMock(return_value=write_return or {"ok": True, "version_id": _VERSION_ID})

    sys.modules["plugins.storage_service_client"] = fake
    return fake


def _inject_ssc_into_mod(mod: types.ModuleType, fake_ssc: types.ModuleType) -> None:
    """Replace storage_service_client references on the loaded module."""
    mod.StorageServiceError = fake_ssc.StorageServiceError
    mod.read_document_content = fake_ssc.read_document_content
    if hasattr(fake_ssc, "write_document_content"):
        mod.write_document_content = fake_ssc.write_document_content


def _enter_patches(*patches_list) -> ExitStack:
    """Enter all given patch context managers and return the ExitStack."""
    stack = ExitStack()
    for p in patches_list:
        stack.enter_context(p)
    return stack


# ---------------------------------------------------------------------------
# read_document — go-owned path
# ---------------------------------------------------------------------------


class TestReadDocumentGoOwner:
    def _load(self, feature_detail: dict, ssc_read_return=None, ssc_error=None):
        fake_ssc = _make_fake_ssc(read_return=ssc_read_return, error=ssc_error)
        mod = _load_module_file("plugins.tools.read", REPO_ROOT / "plugins" / "tools" / "read.py")
        _patch_module_wbc(mod, feature_detail)
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_go_owned_product_spec_proxies_to_storage_service(self):
        """go-owned feature: read_document proxies to storage-service, not git."""
        read_return = {"content": _PRODUCT_SPEC_CONTENT, "version_id": _VERSION_ID}
        mod, fake_ssc = self._load(_make_feature_detail(owner="go"), ssc_read_return=read_return)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["content"] == _PRODUCT_SPEC_CONTENT
        assert result["exists"] is True
        assert result["sha"] == _VERSION_ID
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "product_spec.md",
            user_id="user-1", org_id="org-1",
        )

    def test_go_owned_technical_design_proxies_to_storage_service(self):
        """go-owned feature: read_document for technical_design proxies to storage-service."""
        read_return = {"content": _TD_CONTENT, "version_id": _VERSION_ID}
        mod, fake_ssc = self._load(_make_feature_detail(owner="go"), ssc_read_return=read_return)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="technical_design",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["content"] == _TD_CONTENT
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "tech_design.md",
            user_id="user-1", org_id="org-1",
        )

    def test_go_owned_not_found_returns_empty_content(self):
        """go-owned feature: 404 from storage-service → exists=False, content empty."""
        read_return = {"content": "", "version_id": None}
        mod, fake_ssc = self._load(_make_feature_detail(owner="go"), ssc_read_return=read_return)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["exists"] is False
        assert result["content"] == ""

    def test_go_owned_status_reads_from_db_not_git_or_storage(self, monkeypatch):
        """go-owned feature: status is synthesized from get_feature_detail's DB
        fields — no git, no storage-service. workflow-backend no longer creates
        a git branch/PR for go features, so status.yaml doesn't exist for them."""
        stages = {"product_spec": {"review_status": "draft"}}
        mod, fake_ssc = self._load(_make_feature_detail(owner="go", stages=stages))

        read_doc_mock = MagicMock(
            side_effect=AssertionError("git must not be touched for go features")
        )
        mod.read_document = read_doc_mock

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="status",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True, result.get("error")
        assert result["exists"] is True
        assert result["branch"] is None
        # storage-service must NOT have been called (status isn't stored there either)
        fake_ssc.read_document_content.assert_not_called()
        # git path must NOT have been used
        read_doc_mock.assert_not_called()
        # content reflects the DB-sourced fields
        assert "in_design" in result["content"]
        assert "product_spec" in result["content"]

    def test_go_owned_storage_service_error_returns_ok_false(self):
        """go-owned feature: storage-service error → ok=False."""
        from plugins.storage_service_client import StorageServiceError
        error = StorageServiceError("missing_config", reason_code="missing_config")
        mod, fake_ssc = self._load(_make_feature_detail(owner="go"), ssc_error=error)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert "error" in result

    def test_go_owned_arbitrary_filename_proxies_to_storage_service(self):
        """go-owned feature: an arbitrary filename (not one of the 3 canonical
        documents, e.g. README.md) is treated as a literal storage-service path
        instead of being rejected — regression for the old strict enum that
        made read_document unable to read anything but product_spec/
        technical_design/status."""
        read_return = {"content": "# README\n", "version_id": _VERSION_ID}
        mod, fake_ssc = self._load(_make_feature_detail(owner="go"), ssc_read_return=read_return)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="README.md",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["content"] == "# README\n"
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "README.md",
            user_id="user-1", org_id="org-1",
        )


# ---------------------------------------------------------------------------
# read_document — ts-owned / absent-owner (regression: storage-service NOT called)
# ---------------------------------------------------------------------------


class TestReadDocumentTsOwner:
    def _load_ts(self, owner: str = "ts"):
        detail = _make_feature_detail(owner=owner)
        if owner == "absent":
            del detail["owner"]
        fake_ssc = _make_fake_ssc()
        mod = _load_module_file("plugins.tools.read", REPO_ROOT / "plugins" / "tools" / "read.py")
        _patch_module_wbc(mod, detail)
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_ts_owned_does_not_call_storage_service(self):
        """ts-owned feature: read_document must not call storage-service (guard invariant)."""
        mod, fake_ssc = self._load_ts(owner="ts")
        # Git path will fail (no GITHUB_TOKEN) but we only check the guard.
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()

    def test_absent_owner_does_not_call_storage_service(self):
        """Absent owner defaults to ts → storage-service must not be called."""
        mod, fake_ssc = self._load_ts(owner="absent")
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()

    def test_ts_owned_reads_from_git_on_full_mock(self, monkeypatch):
        """ts-owned feature: full mock of git path verifies git read is invoked."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod, fake_ssc = self._load_ts(owner="ts")

        read_doc_mock = MagicMock(return_value={"content": _PRODUCT_SPEC_CONTENT, "sha": "gitsha"})
        mod.read_document = read_doc_mock
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._resolve_document_branch = MagicMock(return_value=(f"feature/{_FEATURE_ID}", None))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="product_spec",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        fake_ssc.read_document_content.assert_not_called()
        read_doc_mock.assert_called_once()

    def test_ts_owned_arbitrary_filename_reads_from_git(self, monkeypatch):
        """ts-owned feature: an arbitrary filename (e.g. README.md) is used
        literally as the git path's filename instead of being rejected."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod, fake_ssc = self._load_ts(owner="ts")

        read_doc_mock = MagicMock(return_value={"content": "# README\n", "sha": "gitsha"})
        mod.read_document = read_doc_mock
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._resolve_document_branch = MagicMock(return_value=(f"feature/{_FEATURE_ID}", None))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.mark_context_gathered"),
        ):
            result = mod.handle_read_document(
                document="README.md",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        fake_ssc.read_document_content.assert_not_called()
        read_doc_mock.assert_called_once()
        called_path = read_doc_mock.call_args.args[3]
        assert called_path == f"docs/features/{_FEATURE_ID}/README.md"


# ---------------------------------------------------------------------------
# write_product_spec — go-owned path
# ---------------------------------------------------------------------------


class TestWriteProductSpecGoOwner:
    def _load_go(self):
        fake_ssc = _make_fake_ssc(write_return={"ok": True, "version_id": _VERSION_ID})
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_go_owned_proxies_to_storage_service(self):
        """go-owned feature: write_product_spec proxies to storage-service, no git commit."""
        mod, fake_ssc = self._load_go()

        commit_called = []
        mod.commit_to_branch = MagicMock(side_effect=lambda *a, **kw: commit_called.append(1) or _COMMIT_SHA)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            result = mod.handle_write_product_spec(
                content=_PRODUCT_SPEC_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result.get("version_id") == _VERSION_ID
        assert commit_called == [], "no git commit must happen for go-owned write_product_spec"
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "product_spec.md", _PRODUCT_SPEC_CONTENT,
            user_id="user-1", org_id="org-1",
        )

    def test_go_owned_missing_config_returns_error(self):
        """go-owned feature without storage-service config → ok=False."""
        from plugins.storage_service_client import StorageServiceError
        error = StorageServiceError(
            "STORAGE_SERVICE_URL and STORAGE_SERVICE_TOKEN must both be set",
            reason_code="missing_config",
        )
        fake_ssc = _make_fake_ssc(error=error)
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            result = mod.handle_write_product_spec(
                content=_PRODUCT_SPEC_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert "STORAGE_SERVICE" in result["error"]


# ---------------------------------------------------------------------------
# write_product_spec — ts-owned / absent-owner (regression: storage-service NOT called)
# ---------------------------------------------------------------------------


class TestWriteProductSpecTsOwner:
    def _load_ts(self, owner: str = "ts"):
        detail = _make_feature_detail(owner=owner)
        if owner == "absent":
            del detail["owner"]
        fake_ssc = _make_fake_ssc()
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, detail)
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_ts_owned_does_not_call_storage_service(self):
        """ts-owned: write_product_spec must not call storage-service (guard invariant)."""
        mod, fake_ssc = self._load_ts(owner="ts")
        # Git path will fail (no GITHUB_TOKEN) but we only check the guard.
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            mod.handle_write_product_spec(
                content=_PRODUCT_SPEC_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.write_document_content.assert_not_called()

    def test_absent_owner_does_not_call_storage_service(self):
        """Absent owner defaults to ts: write_product_spec must not call storage-service."""
        mod, fake_ssc = self._load_ts(owner="absent")
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            mod.handle_write_product_spec(
                content=_PRODUCT_SPEC_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.write_document_content.assert_not_called()

    def test_ts_owned_commits_to_git_on_full_mock(self, monkeypatch):
        """ts-owned feature: full mock of git path verifies git commit is invoked."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod, fake_ssc = self._load_ts(owner="ts")

        git_committed = []

        def fake_commit(gh_owner, gh_repo, branch, path, content, sha, msg, token):
            git_committed.append(path)
            return _COMMIT_SHA

        mod.commit_to_branch = MagicMock(side_effect=fake_commit)
        mod.read_document = MagicMock(return_value={"content": "", "sha": None})
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._resolve_document_branch = MagicMock(return_value=(f"feature/{_FEATURE_ID}-init", None))
        mod.ensure_pr_for_head = MagicMock(return_value={"url": f"https://github.com/{_OWNER}/{_REPO}/pull/1"})
        mod._ensure_feature_scaffold = MagicMock()

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            result = mod.handle_write_product_spec(
                content=_PRODUCT_SPEC_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert len(git_committed) >= 1, "ts-owned write_product_spec must commit to git"
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# write_technical_design — go-owned path
# ---------------------------------------------------------------------------


class TestWriteTechnicalDesignGoOwner:
    def test_go_owned_proxies_to_storage_service(self):
        """go-owned feature: write_technical_design proxies to storage-service, no git commit."""
        fake_ssc = _make_fake_ssc(write_return={"ok": True, "version_id": _VERSION_ID})
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)

        commit_called = []
        mod.commit_to_branch = MagicMock(side_effect=lambda *a, **kw: commit_called.append(1) or _COMMIT_SHA)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            result = mod.handle_write_technical_design(
                content=_TD_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result.get("version_id") == _VERSION_ID
        assert commit_called == [], "no git commit for go-owned write_technical_design"
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "tech_design.md", _TD_CONTENT,
            user_id="user-1", org_id="org-1",
        )

    def test_go_owned_write_td_does_not_create_git_file(self):
        """go-owned feature: write_technical_design must not create a stray git file."""
        fake_ssc = _make_fake_ssc(write_return={"ok": True, "version_id": _VERSION_ID})
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)

        mod.commit_to_branch = MagicMock(side_effect=AssertionError("git commit_to_branch called for go feature"))
        mod.write_document = MagicMock(side_effect=AssertionError("write_document called for go feature"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            result = mod.handle_write_technical_design(
                content=_TD_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True

    def test_ts_owned_write_td_does_not_call_storage_service(self):
        """ts-owned: write_technical_design must not call storage-service."""
        fake_ssc = _make_fake_ssc()
        mod = _load_module_file("plugins.tools.artifacts", REPO_ROOT / "plugins" / "tools" / "artifacts.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="ts"))
        _inject_ssc_into_mod(mod, fake_ssc)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
            patch("plugins.context.was_context_gathered", return_value=True),
        ):
            mod.handle_write_technical_design(
                content=_TD_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# edit_document — go-owned path
# ---------------------------------------------------------------------------


class TestEditDocumentGoOwner:
    def _load_go(self, original_content: str = "# Spec\n\nOld content.\n"):
        fake_ssc = _make_fake_ssc(
            read_return={"content": original_content, "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )
        mod = _load_module_file("plugins.tools.edit", REPO_ROOT / "plugins" / "tools" / "edit.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_go_owned_proxies_read_apply_write_to_storage_service(self):
        """go-owned feature: edit_document reads from, applies, and writes to storage-service."""
        original = "# Spec\n\nOld content.\n"
        edited = "# Spec\n\nNew content.\n"
        mod, fake_ssc = self._load_go(original_content=original)

        commit_called = []
        mod.commit_to_branch = MagicMock(side_effect=lambda *a, **kw: commit_called.append(1) or _COMMIT_SHA)
        mod.write_document = MagicMock(side_effect=AssertionError("write_document called for go feature"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "Old content.", "new_string": "New content."}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result.get("version_id") == _VERSION_ID
        assert commit_called == [], "no git commit for go-owned edit_document"
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID, _FEATURE_ID, "product_spec.md", edited,
            user_id="user-1", org_id="org-1",
        )

    def test_go_owned_edit_warns_on_missing_old_string(self):
        """go-owned edit_document: missing old_string → warning, write proceeds."""
        mod, fake_ssc = self._load_go()

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "NOT FOUND", "new_string": "replacement"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert "warnings" in result
        assert any("NOT FOUND" in w for w in result["warnings"])

    def test_go_owned_missing_config_returns_error(self):
        """go-owned edit_document without storage-service config → ok=False."""
        from plugins.storage_service_client import StorageServiceError
        error = StorageServiceError(
            "STORAGE_SERVICE_URL and STORAGE_SERVICE_TOKEN must both be set",
            reason_code="missing_config",
        )
        fake_ssc = _make_fake_ssc(error=error)
        mod = _load_module_file("plugins.tools.edit", REPO_ROOT / "plugins" / "tools" / "edit.py")
        _patch_module_wbc(mod, _make_feature_detail(owner="go"))
        _inject_ssc_into_mod(mod, fake_ssc)

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert "STORAGE_SERVICE" in result["error"]

    def test_go_owned_no_stray_git_file_created(self):
        """go-owned edit: no git file is written; storage-service is the only write target."""
        original = "# Spec\n\nContent.\n"
        mod, fake_ssc = self._load_go(original_content=original)

        mod.commit_to_branch = MagicMock(side_effect=AssertionError("git commit_to_branch called for go feature"))
        mod.write_document = MagicMock(side_effect=AssertionError("write_document called for go feature"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "Content.", "new_string": "Updated content."}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        fake_ssc.write_document_content.assert_called_once()


# ---------------------------------------------------------------------------
# edit_document — ts-owned path (regression: storage-service NOT called)
# ---------------------------------------------------------------------------


class TestEditDocumentTsOwner:
    def _load_ts(self, owner: str = "ts"):
        detail = _make_feature_detail(owner=owner)
        if owner == "absent":
            del detail["owner"]
        fake_ssc = _make_fake_ssc()
        mod = _load_module_file("plugins.tools.edit", REPO_ROOT / "plugins" / "tools" / "edit.py")
        _patch_module_wbc(mod, detail)
        _inject_ssc_into_mod(mod, fake_ssc)
        return mod, fake_ssc

    def test_ts_owned_does_not_call_storage_service(self):
        """ts-owned feature: edit_document must not call storage-service (guard invariant)."""
        mod, fake_ssc = self._load_ts(owner="ts")
        # Git path will fail (no GITHUB_TOKEN) but we only check the guard.
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()

    def test_absent_owner_does_not_call_storage_service(self):
        """Absent owner defaults to ts: edit_document must not call storage-service."""
        mod, fake_ssc = self._load_ts(owner="absent")
        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()

    def test_ts_owned_commits_to_git_on_full_mock(self, monkeypatch):
        """ts-owned feature: full mock of git path verifies git commit is invoked."""
        monkeypatch.setenv("GITHUB_TOKEN", _GITHUB_TOKEN)
        mod, fake_ssc = self._load_ts(owner="ts")

        git_committed = []

        def fake_commit(gh_owner, gh_repo, branch, path, content, sha, msg, token):
            git_committed.append(path)
            return _COMMIT_SHA

        original = "# Spec\n\nOld content.\n"
        mod.read_document = MagicMock(return_value={"content": original, "sha": "oldsha"})
        mod.commit_to_branch = MagicMock(side_effect=fake_commit)
        mod._resolve_management_repo = MagicMock(return_value=(_OWNER, _REPO))
        mod._resolve_document_branch = MagicMock(return_value=(f"feature/{_FEATURE_ID}-init", None))
        mod.ensure_pr_for_head = MagicMock(return_value={"url": f"https://github.com/{_OWNER}/{_REPO}/pull/1"})

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_document(
                document="product_spec",
                edits=[{"old_string": "Old content.", "new_string": "New content."}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert len(git_committed) >= 1, "ts-owned edit_document must commit to git"
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# storage_service_client unit tests (HTTP-level)
# ---------------------------------------------------------------------------


class TestStorageServiceClient:
    def test_read_document_content_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.get(
            f"{_STORAGE_URL}/api/workspaces/ws1/features/feat1/documents/content?path=product_spec.md",
            json={"content": "# Hello\n", "version_id": "v1"},
        )

        from plugins.storage_service_client import read_document_content
        result = read_document_content("ws1", "feat1", "product_spec.md")
        assert result["content"] == "# Hello\n"
        assert result["version_id"] == "v1"

    def test_read_document_content_not_found(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.get(
            f"{_STORAGE_URL}/api/workspaces/ws1/features/feat1/documents/content?path=product_spec.md",
            status_code=404,
        )

        from plugins.storage_service_client import read_document_content
        result = read_document_content("ws1", "feat1", "product_spec.md")
        assert result["content"] == ""
        assert result["version_id"] is None

    def test_write_document_content_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.put(
            f"{_STORAGE_URL}/api/workspaces/ws1/features/feat1/documents/content?path=product_spec.md",
            json={"ok": True, "version_id": "v2"},
        )

        from plugins.storage_service_client import write_document_content
        result = write_document_content("ws1", "feat1", "product_spec.md", "# New content\n")
        assert result["ok"] is True
        assert result["version_id"] == "v2"

    def test_missing_config_raises_error(self, monkeypatch):
        monkeypatch.delenv("STORAGE_SERVICE_URL", raising=False)
        monkeypatch.delenv("STORAGE_SERVICE_TOKEN", raising=False)

        from plugins.storage_service_client import StorageServiceError, read_document_content
        with pytest.raises(StorageServiceError) as exc_info:
            read_document_content("ws1", "feat1", "product_spec.md")
        assert exc_info.value.reason_code == "missing_config"

    def test_server_error_raises_storage_service_error(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.get(
            f"{_STORAGE_URL}/api/workspaces/ws1/features/feat1/documents/content?path=product_spec.md",
            status_code=500,
            json={"error": "internal_server_error"},
        )

        from plugins.storage_service_client import StorageServiceError, read_document_content
        with pytest.raises(StorageServiceError) as exc_info:
            read_document_content("ws1", "feat1", "product_spec.md")
        assert exc_info.value.status == 500

    def test_write_sends_content_in_body(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.put(
            f"{_STORAGE_URL}/api/workspaces/ws1/features/feat1/documents/content?path=tech_design.md",
            json={"ok": True, "version_id": "v3"},
        )

        from plugins.storage_service_client import write_document_content
        result = write_document_content("ws1", "feat1", "tech_design.md", "# Tech Design\n")
        assert result["ok"] is True

        import json
        put_body = json.loads(requests_mock.last_request.text)
        assert put_body["content"] == "# Tech Design\n"

    def test_download_image_happy_path(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.get(
            f"{_STORAGE_URL}/api/workspaces/ws1/images/img-1",
            content=b"fake-png-bytes",
            headers={"Content-Type": "image/png"},
        )

        from plugins.storage_service_client import download_image
        result = download_image("ws1", "img-1", user_id="user-1", org_id="org-1")
        assert result["data"] == b"fake-png-bytes"
        assert result["content_type"] == "image/png"

        req = requests_mock.last_request
        assert req.headers["Authorization"] == f"Bearer {_STORAGE_TOKEN}"
        assert req.headers["X-User-Id"] == "user-1"
        assert req.headers["X-Org-Id"] == "org-1"

    def test_download_image_not_found(self, monkeypatch, requests_mock):
        monkeypatch.setenv("STORAGE_SERVICE_URL", _STORAGE_URL)
        monkeypatch.setenv("STORAGE_SERVICE_TOKEN", _STORAGE_TOKEN)

        requests_mock.get(f"{_STORAGE_URL}/api/workspaces/ws1/images/missing", status_code=404)

        from plugins.storage_service_client import StorageServiceError, download_image
        with pytest.raises(StorageServiceError) as exc_info:
            download_image("ws1", "missing")
        assert exc_info.value.status == 404
        assert exc_info.value.reason_code == "not_found"

    def test_download_image_missing_config_raises_error(self, monkeypatch):
        monkeypatch.delenv("STORAGE_SERVICE_URL", raising=False)
        monkeypatch.delenv("STORAGE_SERVICE_TOKEN", raising=False)

        from plugins.storage_service_client import StorageServiceError, download_image
        with pytest.raises(StorageServiceError) as exc_info:
            download_image("ws1", "img-1")
        assert exc_info.value.reason_code == "missing_config"
