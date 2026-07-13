"""Tests for plugins/tools/file_ops.py (T1: write_file + edit_file, go-owner gated).

Covers:
  - _validate_path: rejects leading '/', '..' segments, empty path; accepts valid relative paths.
  - handle_write_file: go-owned → calls write_document_content with expected args.
  - handle_write_file: ts-owned → returns unsupported_owner error, no storage-service call.
  - handle_write_file: missing workspace/feature_id → error.
  - handle_write_file: storage-service error → ok=False.
  - handle_edit_file: go-owned → read-before-write, applies edits, writes back.
  - handle_edit_file: ts-owned → returns unsupported_owner error, no storage-service call.
  - handle_edit_file: warns on missing old_string, still writes.
  - handle_edit_file: storage-service error → ok=False.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

_WORKSPACE_ID = "ws-test"
_FEATURE_ID = "my-feature"
_VERSION_ID = "v1-uuid-abc"
_NOTES_PATH = "notes.md"
_NOTES_CONTENT = "# Notes\n\nSome notes.\n"
_NESTED_PATH = "handoffs/handoff.md"


def _make_feature_detail(owner: str = "go"):
    detail = {
        "feature_name": _FEATURE_ID,
        "title": "My Feature",
        "stage": "product_spec",
        "status": "in_design",
        "owner": owner,
        "init_pr_url": None,
        "stages": {},
    }
    if owner == "absent":
        del detail["owner"]
    return detail


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


def _make_fake_ssc(*, read_return=None, write_return=None, error=None):
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
        fake.write_document_content = MagicMock(
            return_value=write_return or {"ok": True, "version_id": _VERSION_ID}
        )

    sys.modules["plugins.storage_service_client"] = fake
    return fake


def _patch_wbc(mod: types.ModuleType, feature_detail: dict) -> None:
    wbc_name = "src.services.workflow_backend_client"
    if wbc_name not in sys.modules:
        for parent in ("src", "src.services"):
            if parent not in sys.modules:
                sys.modules[parent] = types.ModuleType(parent)
        wbc_mod = types.ModuleType(wbc_name)
        sys.modules[wbc_name] = wbc_mod
    else:
        wbc_mod = sys.modules[wbc_name]

    wbc_mod.get_feature_detail = AsyncMock(return_value=feature_detail)
    wbc_mod.run_async = _sync_run_async


def _inject_ssc(mod: types.ModuleType, fake_ssc: types.ModuleType) -> None:
    mod.StorageServiceError = fake_ssc.StorageServiceError
    mod.read_document_content = fake_ssc.read_document_content
    mod.write_document_content = fake_ssc.write_document_content


def _enter_patches(*patches_list) -> ExitStack:
    stack = ExitStack()
    for p in patches_list:
        stack.enter_context(p)
    return stack


def _setup_fake_edit_module() -> types.ModuleType:
    """Inject a minimal fake plugins.tools.edit into sys.modules.

    file_ops.py does a lazy ``from .edit import _apply_edits`` inside
    handle_edit_file. The real edit.py has a heavy dependency chain (document_repo,
    workflow_backend_client, aiohttp). This fake provides the same pure-logic
    _apply_edits without triggering those imports, so unit tests remain isolated.
    """
    _ensure_plugins_pkg()
    # Ensure plugins.tools package stub exists.
    if "plugins.tools" not in sys.modules:
        tools_pkg = types.ModuleType("plugins.tools")
        tools_pkg.__path__ = [str(REPO_ROOT / "plugins" / "tools")]
        tools_pkg.__package__ = "plugins.tools"
        sys.modules["plugins.tools"] = tools_pkg

    fake_edit = types.ModuleType("plugins.tools.edit")
    fake_edit.__package__ = "plugins.tools"

    def _apply_edits(content: str, edits: list) -> tuple:
        warnings = []
        for edit in edits:
            old = edit.get("old_string", "")
            new = edit.get("new_string", "")
            if old not in content:
                warnings.append(f"old_string not found: {old[:60]!r}")
                continue
            content = content.replace(old, new, 1)
        return content, warnings

    fake_edit._apply_edits = _apply_edits
    sys.modules["plugins.tools.edit"] = fake_edit
    return fake_edit


def _load_file_ops(
    feature_detail: dict, *, read_return=None, write_return=None, ssc_error=None
):
    fake_ssc = _make_fake_ssc(
        read_return=read_return, write_return=write_return, error=ssc_error
    )
    _setup_fake_edit_module()
    mod = _load_module_file(
        "plugins.tools.file_ops", REPO_ROOT / "plugins" / "tools" / "file_ops.py"
    )
    _patch_wbc(mod, feature_detail)
    _inject_ssc(mod, fake_ssc)
    return mod, fake_ssc


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
    monkeypatch.delenv("STORAGE_SERVICE_URL", raising=False)
    monkeypatch.delenv("STORAGE_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)


# ---------------------------------------------------------------------------
# _validate_path
# ---------------------------------------------------------------------------


class TestValidatePath:
    def _load(self):
        _ensure_plugins_pkg()
        _make_fake_ssc()
        _setup_fake_edit_module()
        fake_ssc = sys.modules["plugins.storage_service_client"]
        mod = _load_module_file(
            "plugins.tools.file_ops", REPO_ROOT / "plugins" / "tools" / "file_ops.py"
        )
        _inject_ssc(mod, fake_ssc)
        return mod

    def test_valid_simple_path(self):
        mod = self._load()
        assert mod._validate_path("notes.md") is None

    def test_valid_nested_path(self):
        mod = self._load()
        assert mod._validate_path("handoffs/handoff.md") is None

    def test_rejects_empty(self):
        mod = self._load()
        assert mod._validate_path("") is not None

    def test_rejects_absolute_path(self):
        mod = self._load()
        err = mod._validate_path("/etc/passwd")
        assert err is not None
        assert "leading" in err or "/" in err

    def test_rejects_dotdot_segment(self):
        mod = self._load()
        err = mod._validate_path("../other-feature/secret.md")
        assert err is not None
        assert ".." in err

    def test_rejects_dotdot_in_middle(self):
        mod = self._load()
        err = mod._validate_path("sub/../../../etc/passwd")
        assert err is not None

    def test_valid_subdirectory(self):
        mod = self._load()
        assert mod._validate_path("docs/research-notes.md") is None


# ---------------------------------------------------------------------------
# handle_write_file — go-owned
# ---------------------------------------------------------------------------


class TestWriteFileGoOwner:
    def test_go_owned_calls_write_document_content(self):
        """go-owned feature: write_file writes to storage-service with correct args."""
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["path"] == _NOTES_PATH
        assert result["version_id"] == _VERSION_ID
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID,
            _FEATURE_ID,
            _NOTES_PATH,
            _NOTES_CONTENT,
            user_id="user-1",
            org_id="org-1",
            feature_slug=_FEATURE_ID,
        )

    def test_go_owned_nested_path(self):
        """go-owned feature: write_file works for nested paths."""
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NESTED_PATH,
                content="# Handoff\n",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["path"] == _NESTED_PATH
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID,
            _FEATURE_ID,
            _NESTED_PATH,
            "# Handoff\n",
            user_id="user-1",
            org_id="org-1",
            feature_slug=_FEATURE_ID,
        )

    def test_go_owned_no_git_write_attempted(self):
        """go-owned feature: write_file must not invoke any git-backed writes."""
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        # Only write_document_content should have been called (no read for write_file)
        fake_ssc.read_document_content.assert_not_called()

    def test_storage_service_error_returns_ok_false(self):
        """go-owned feature: storage-service error → ok=False."""
        # Use the fake SSC's error class to avoid importing the real package.
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))
        error = mod.StorageServiceError(
            "connection refused", reason_code="request_error"
        )
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"), ssc_error=error
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert "error" in result

    def test_unsafe_path_rejected(self):
        """go-owned feature: write_file rejects path-traversal attempts."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path="../other/secret.md",
                content="evil",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        fake_ssc.write_document_content.assert_not_called()

    def test_absolute_path_rejected(self):
        """go-owned feature: write_file rejects absolute paths."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path="/etc/passwd",
                content="evil",
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        fake_ssc.write_document_content.assert_not_called()

    def test_missing_context_returns_error(self):
        """write_file without workspace/feature context → ok=False."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=""),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value=""),
            patch("plugins.context.get_org_id", return_value=""),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
            )

        assert result["ok"] is False
        assert "required" in result["error"]
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# handle_write_file — no feature_id (workspace-root write)
# ---------------------------------------------------------------------------


class TestWriteFileNoFeatureId:
    def test_writes_directly_to_workspace_root_when_no_feature_id(self):
        """No feature_id (explicit or context) → writes via the workspace-root
        endpoint (feature_id="") without any go-owner check, path untouched."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path="api.txt",
                content="hello",
                workspace_id=_WORKSPACE_ID,
            )

        assert result["ok"] is True
        assert result["path"] == "api.txt"
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID,
            "",
            "api.txt",
            "hello",
            user_id="user-1",
            org_id="org-1",
            feature_slug="",
        )

    def test_no_feature_id_skips_owner_check(self):
        """No feature_id → get_feature_detail must not be called."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))
        wbc_mod = sys.modules["src.services.workflow_backend_client"]

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path="api.txt",
                content="hello",
                workspace_id=_WORKSPACE_ID,
            )

        assert result["ok"] is True
        wbc_mod.get_feature_detail.assert_not_called()

    def test_missing_workspace_id_still_returns_error(self):
        """No workspace_id at all (explicit or context) → ok=False, even with no feature_id."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=""),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value=""),
            patch("plugins.context.get_org_id", return_value=""),
        ):
            result = mod.handle_write_file(
                path="api.txt",
                content="hello",
            )

        assert result["ok"] is False
        assert "required" in result["error"]
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# handle_write_file — ts-owned / absent-owner
# ---------------------------------------------------------------------------


class TestWriteFileTsOwner:
    def test_ts_owned_returns_unsupported_owner_error(self):
        """ts-owned feature: write_file returns unsupported_owner error."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="ts"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result.get("error") == "unsupported_owner"
        assert "go-owned" in result.get("message", "")
        fake_ssc.write_document_content.assert_not_called()

    def test_absent_owner_returns_unsupported_owner_error(self):
        """Absent owner defaults to ts: write_file must return unsupported_owner."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="absent"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result.get("error") == "unsupported_owner"
        fake_ssc.write_document_content.assert_not_called()

    def test_ts_owned_does_not_call_storage_service(self):
        """ts-owned feature: write_file must not call storage-service at all."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="ts"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            mod.handle_write_file(
                path=_NOTES_PATH,
                content=_NOTES_CONTENT,
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# handle_edit_file — go-owned
# ---------------------------------------------------------------------------


class TestEditFileGoOwner:
    def test_go_owned_read_before_write_applies_edits(self):
        """go-owned feature: edit_file reads current content, applies edits, writes back."""
        original = "# Notes\n\nOld section.\n"
        expected = "# Notes\n\nNew section.\n"
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            read_return={"content": original, "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "Old section.", "new_string": "New section."}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert result["path"] == _NOTES_PATH
        assert result["version_id"] == _VERSION_ID
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID,
            _FEATURE_ID,
            _NOTES_PATH,
            user_id="user-1",
            org_id="org-1",
        )
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID,
            _FEATURE_ID,
            _NOTES_PATH,
            expected,
            user_id="user-1",
            org_id="org-1",
            feature_slug=_FEATURE_ID,
        )

    def test_go_owned_reads_fresh_content_before_applying_edits(self):
        """edit_file must read current content (not stale) before applying edits."""
        fresh_content = "# Notes\n\nFresh content to edit.\n"
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            read_return={"content": fresh_content, "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[
                    {"old_string": "Fresh content to edit.", "new_string": "Updated."}
                ],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        written_content = fake_ssc.write_document_content.call_args.args[3]
        assert "Updated." in written_content
        assert "Fresh content to edit." not in written_content

    def test_go_owned_warns_on_missing_old_string(self):
        """go-owned edit_file: missing old_string → warning, write still proceeds."""
        original = "# Notes\n\nContent.\n"
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            read_return={"content": original, "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "NOT PRESENT", "new_string": "replacement"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True
        assert "warnings" in result
        assert any("NOT PRESENT" in w for w in result["warnings"])
        fake_ssc.write_document_content.assert_called_once()

    def test_go_owned_no_git_write_attempted(self):
        """go-owned edit_file must not invoke any git-backed writes."""
        original = "# Notes\n\nContent.\n"
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            read_return={"content": original, "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "Content.", "new_string": "Updated."}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is True

    def test_storage_service_error_returns_ok_false(self):
        """go-owned edit_file: storage-service error → ok=False."""
        # Use the fake SSC's error class to avoid importing the real package.
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))
        error = mod.StorageServiceError(
            "connection refused", reason_code="request_error"
        )
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"), ssc_error=error
        )

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert "error" in result

    def test_unsafe_path_rejected_for_edit(self):
        """go-owned feature: edit_file rejects path-traversal attempts."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path="../other/secret.md",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()

    def test_absolute_path_rejected_for_edit(self):
        """go-owned feature: edit_file rejects absolute paths."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path="/etc/passwd",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# handle_edit_file — no feature_id (workspace-root write)
# ---------------------------------------------------------------------------


class TestEditFileNoFeatureId:
    def test_edits_directly_at_workspace_root_when_no_feature_id(self):
        """No feature_id (explicit or context) → read/write via the
        workspace-root endpoint (feature_id="") without any go-owner check."""
        mod, fake_ssc = _load_file_ops(
            _make_feature_detail(owner="go"),
            read_return={"content": "old", "version_id": "v0"},
            write_return={"ok": True, "version_id": _VERSION_ID},
        )
        wbc_mod = sys.modules["src.services.workflow_backend_client"]

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path="api.txt",
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
            )

        assert result["ok"] is True
        assert result["path"] == "api.txt"
        wbc_mod.get_feature_detail.assert_not_called()
        fake_ssc.read_document_content.assert_called_once_with(
            _WORKSPACE_ID, "", "api.txt", user_id="user-1", org_id="org-1"
        )
        fake_ssc.write_document_content.assert_called_once_with(
            _WORKSPACE_ID, "", "api.txt", "new", user_id="user-1", org_id="org-1", feature_slug=""
        )

    def test_missing_workspace_id_still_returns_error(self):
        """No workspace_id at all (explicit or context) → ok=False, even with no feature_id."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="go"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=""),
            patch("plugins.context.get_feature_id", return_value=""),
            patch("plugins.context.get_user_id", return_value=""),
            patch("plugins.context.get_org_id", return_value=""),
        ):
            result = mod.handle_edit_file(
                path="api.txt",
                edits=[{"old_string": "old", "new_string": "new"}],
            )

        assert result["ok"] is False
        assert "required" in result["error"]
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()


# ---------------------------------------------------------------------------
# handle_edit_file — ts-owned / absent-owner
# ---------------------------------------------------------------------------


class TestEditFileTsOwner:
    def test_ts_owned_returns_unsupported_owner_error(self):
        """ts-owned feature: edit_file returns unsupported_owner error."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="ts"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result.get("error") == "unsupported_owner"
        assert "go-owned" in result.get("message", "")
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()

    def test_absent_owner_returns_unsupported_owner_error(self):
        """Absent owner defaults to ts: edit_file must return unsupported_owner."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="absent"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            result = mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        assert result["ok"] is False
        assert result.get("error") == "unsupported_owner"
        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()

    def test_ts_owned_does_not_call_storage_service(self):
        """ts-owned feature: edit_file must not call storage-service at all (critical regression guard)."""
        mod, fake_ssc = _load_file_ops(_make_feature_detail(owner="ts"))

        with _enter_patches(
            patch("plugins.context.get_workspace_id", return_value=_WORKSPACE_ID),
            patch("plugins.context.get_feature_id", return_value=_FEATURE_ID),
            patch("plugins.context.get_user_id", return_value="user-1"),
            patch("plugins.context.get_org_id", return_value="org-1"),
        ):
            mod.handle_edit_file(
                path=_NOTES_PATH,
                edits=[{"old_string": "old", "new_string": "new"}],
                workspace_id=_WORKSPACE_ID,
                feature_id=_FEATURE_ID,
            )

        fake_ssc.read_document_content.assert_not_called()
        fake_ssc.write_document_content.assert_not_called()
