"""Tests for plugins/tools/suggest_next_actions.py (m3-agent-cta T4).

Covers:
  - _validate_suggestions: happy path, too many suggestions (> 3), invalid category,
    missing required fields, title/description/button_label length limits
  - handle: valid payload → persists + publishes + returns {"status": "ok"}
  - handle: invalid payload → returns {"status": "error"} without DB/bus calls
  - handle: no session context → returns {"status": "ok"} with warning, no crash
  - SCHEMA shape: required fields, maxItems, enum values present
  - Registration: suggest_next_actions present in plugins._TOOLS
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stub_module(name: str, attrs: dict | None = None) -> ModuleType:
    m = ModuleType(name)
    for k, v in (attrs or {}).items():
        setattr(m, k, v)
    return m


def _load_suggest_next_actions_isolated():
    """Import plugins/tools/suggest_next_actions.py without importing the full
    plugins package (which would cascade to psycopg → libpq not found).
    """
    # Stub every upstream module that suggest_next_actions imports lazily.
    fake_context = _make_stub_module(
        "plugins.context",
        {
            "get_agent_session_id": lambda: "",
            "get_agent_loop": lambda: None,
            "get_agent_db_factory": lambda: None,
        },
    )
    fake_store = _make_stub_module(
        "src.db.store",
        {
            "get_latest_assistant_message_id": AsyncMock(return_value=None),
            "update_message_cta_suggestions": AsyncMock(),
        },
    )
    fake_bus_module = _make_stub_module("src.realtime.bus")
    fake_bus = MagicMock()
    fake_bus_module.get_bus = lambda: fake_bus

    sys.modules.setdefault("plugins.context", fake_context)
    sys.modules.setdefault("src.db.store", fake_store)
    sys.modules.setdefault("src.realtime.bus", fake_bus_module)

    mod_path = REPO_ROOT / "plugins" / "tools" / "suggest_next_actions.py"
    spec = importlib.util.spec_from_file_location(
        "plugins.tools.suggest_next_actions",
        mod_path,
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "plugins.tools"
    sys.modules["plugins.tools.suggest_next_actions"] = mod
    spec.loader.exec_module(mod)
    return mod, fake_context, fake_store, fake_bus_module, fake_bus


def _valid_suggestion(**overrides):
    base = {
        "id": "sug-1",
        "title": "Approve spec",
        "category": "Lifecycle",
        "description": "Approve the product spec to advance the feature.",
        "action_text": "/approve-product-spec",
        "button_label": "Approve spec",
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    # Also clean src stubs
    src_keys = [
        k
        for k in sys.modules
        if k.startswith(("src.db.store", "src.realtime"))
    ]
    for k in src_keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    src_keys = [
        k
        for k in sys.modules
        if k.startswith(("src.db.store", "src.realtime"))
    ]
    for k in src_keys:
        del sys.modules[k]


# ---------------------------------------------------------------------------
# _validate_suggestions — unit tests
# ---------------------------------------------------------------------------


class TestValidateSuggestions:
    def setup_method(self):
        self.mod, *_ = _load_suggest_next_actions_isolated()
        self.validate = self.mod._validate_suggestions

    def test_happy_path_single_suggestion(self):
        result = self.validate([_valid_suggestion()])
        assert len(result) == 1
        assert result[0]["category"] == "Lifecycle"

    def test_happy_path_three_suggestions(self):
        suggestions = [
            _valid_suggestion(id="s1", category="Lifecycle"),
            _valid_suggestion(id="s2", category="Clarify"),
            _valid_suggestion(id="s3", category="Review"),
        ]
        result = self.validate(suggestions)
        assert len(result) == 3

    def test_rejects_more_than_three(self):
        suggestions = [_valid_suggestion(id=f"s{i}") for i in range(4)]
        with pytest.raises(ValueError, match="at most 3"):
            self.validate(suggestions)

    def test_rejects_empty_list(self):
        with pytest.raises(ValueError, match="at least 1"):
            self.validate([])

    def test_rejects_non_list(self):
        with pytest.raises(ValueError, match="must be an array"):
            self.validate("not a list")

    def test_rejects_invalid_category(self):
        with pytest.raises(ValueError, match="category"):
            self.validate([_valid_suggestion(category="InvalidCat")])

    @pytest.mark.parametrize(
        "category",
        ["Lifecycle", "Clarify", "Review", "Edit", "Action", "GitNexus", "RAG"],
    )
    def test_accepts_all_valid_categories(self, category):
        result = self.validate([_valid_suggestion(category=category)])
        assert result[0]["category"] == category

    def test_rejects_title_too_long(self):
        with pytest.raises(ValueError, match="title.*40"):
            self.validate([_valid_suggestion(title="x" * 41)])

    def test_accepts_title_at_limit(self):
        result = self.validate([_valid_suggestion(title="x" * 40)])
        assert len(result) == 1

    def test_rejects_description_too_long(self):
        with pytest.raises(ValueError, match="description.*120"):
            self.validate([_valid_suggestion(description="x" * 121)])

    def test_rejects_button_label_too_long(self):
        with pytest.raises(ValueError, match="button_label.*20"):
            self.validate([_valid_suggestion(button_label="x" * 21)])

    def test_rejects_missing_required_field(self):
        bad = _valid_suggestion()
        del bad["action_text"]
        with pytest.raises(ValueError, match="missing required"):
            self.validate([bad])

    def test_rejects_non_dict_item(self):
        with pytest.raises(ValueError, match="must be an object"):
            self.validate(["not a dict"])


# ---------------------------------------------------------------------------
# handle — happy path
# ---------------------------------------------------------------------------


class TestHandleHappyPath:
    def setup_method(self):
        (
            self.mod,
            self.fake_context,
            self.fake_store,
            self.fake_bus_module,
            self.fake_bus,
        ) = _load_suggest_next_actions_isolated()

    def test_returns_status_ok(self):
        """Handle dispatches to the async path and returns ok.

        We mock asyncio.run_coroutine_threadsafe to avoid needing a live event
        loop running in a background thread.
        """
        suggestions = [_valid_suggestion()]

        mock_db_factory = MagicMock()
        self.fake_context.get_agent_session_id = lambda: "sess-abc"
        self.fake_context.get_agent_loop = lambda: MagicMock()  # non-None loop
        self.fake_context.get_agent_db_factory = lambda: mock_db_factory

        # Stub out the async dispatch so we don't need a real running loop.
        fake_future = MagicMock()
        fake_future.result.return_value = None

        with patch("asyncio.run_coroutine_threadsafe", return_value=fake_future):
            result = self.mod.handle(suggestions=suggestions)

        assert result["status"] == "ok"

    def test_invalid_validation_skips_persist(self):
        """When validation fails, no DB or bus call is made."""
        suggestions = [_valid_suggestion(category="Bad")]

        self.fake_context.get_agent_session_id = lambda: "sess-abc"
        self.fake_context.get_agent_loop = lambda: None
        self.fake_context.get_agent_db_factory = lambda: None

        result = self.mod.handle(suggestions=suggestions)
        assert result["status"] == "error"
        # Bus publish should not be called for invalid payload.
        self.fake_bus.publish.assert_not_called()

    def test_publishes_bus_event_on_success(self):
        suggestions = [_valid_suggestion()]
        loop = asyncio.new_event_loop()

        mock_db_factory = MagicMock()
        mock_db = AsyncMock()
        mock_db_factory.return_value.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db_factory.return_value.__aexit__ = AsyncMock(return_value=False)

        published_events = []

        def fake_publish(session_id, event):
            published_events.append((session_id, event))

        self.fake_bus.publish.side_effect = fake_publish
        self.fake_context.get_agent_session_id = lambda: "sess-abc"
        self.fake_context.get_agent_loop = lambda: loop
        self.fake_context.get_agent_db_factory = lambda: mock_db_factory
        self.fake_store.get_latest_assistant_message_id = AsyncMock(return_value=99)
        self.fake_store.update_message_cta_suggestions = AsyncMock()

        self.mod.handle(suggestions=suggestions)
        # Drain the event loop so the run_coroutine_threadsafe task runs to completion.
        loop.run_until_complete(asyncio.sleep(0))
        loop.close()

        assert any(
            evt[1].get("event") == "turn.cta_suggestions" for evt in published_events
        ), f"turn.cta_suggestions not published: {published_events}"


# ---------------------------------------------------------------------------
# handle — no session context
# ---------------------------------------------------------------------------


class TestHandleNoSessionContext:
    def setup_method(self):
        self.mod, self.fake_context, *_ = _load_suggest_next_actions_isolated()

    def test_no_session_id_returns_ok_with_warning(self):
        suggestions = [_valid_suggestion()]
        self.fake_context.get_agent_session_id = lambda: ""
        self.fake_context.get_agent_loop = lambda: None
        self.fake_context.get_agent_db_factory = lambda: None

        result = self.mod.handle(suggestions=suggestions)

        assert result["status"] == "ok"
        assert "warning" in result


# ---------------------------------------------------------------------------
# SCHEMA shape
# ---------------------------------------------------------------------------


class TestSchemaShape:
    def setup_method(self):
        self.mod, *_ = _load_suggest_next_actions_isolated()

    def test_schema_has_description(self):
        assert "description" in self.mod.SCHEMA
        assert self.mod.SCHEMA["description"]

    def test_schema_parameters_has_suggestions_property(self):
        params = self.mod.SCHEMA["parameters"]
        assert "suggestions" in params["properties"]

    def test_suggestions_max_items_is_3(self):
        items_schema = self.mod.SCHEMA["parameters"]["properties"]["suggestions"]
        assert items_schema["maxItems"] == 3

    def test_suggestions_min_items_is_1(self):
        items_schema = self.mod.SCHEMA["parameters"]["properties"]["suggestions"]
        assert items_schema["minItems"] == 1

    def test_category_enum_has_all_expected_values(self):
        item_schema = self.mod.SCHEMA["parameters"]["properties"]["suggestions"][
            "items"
        ]
        enum = item_schema["properties"]["category"]["enum"]
        expected = {
            "Lifecycle",
            "Clarify",
            "Review",
            "Edit",
            "Action",
            "GitNexus",
            "RAG",
        }
        assert set(enum) == expected

    def test_required_fields_present_in_item_schema(self):
        item_schema = self.mod.SCHEMA["parameters"]["properties"]["suggestions"][
            "items"
        ]
        required = set(item_schema["required"])
        assert {
            "id",
            "title",
            "category",
            "description",
            "action_text",
            "button_label",
        } <= required


# ---------------------------------------------------------------------------
# Registration: suggest_next_actions in _TOOLS
# ---------------------------------------------------------------------------


class TestToolRegistration:
    def test_suggest_next_actions_in_tools_list(self):
        """suggest_next_actions must appear in plugins._TOOLS."""
        # Stub the full plugins import chain to avoid unrelated import-time deps.
        # check_workflow_available now comes from src.services.workflow_backend_client,
        # which has no heavy dependencies, so it's fine to import for real here.
        stub_context = _make_stub_module("plugins.context", {})
        stub_hooks = _make_stub_module(
            "plugins.hooks", {"inject_context": lambda **_: None}
        )
        stub_mcp = _make_stub_module("plugins.clients.mcp_client", {})

        # Individual tool modules — return minimal stubs.
        def _stub_tool(name, schema=None, handle=None):
            return _make_stub_module(
                f"plugins.tools.{name}",
                {
                    "SCHEMA": schema or {"description": "", "parameters": {}},
                    "handle": handle or (lambda **_: {}),
                },
            )

        stub_tools_pkg = _make_stub_module("plugins.tools", {})
        sys.modules["plugins.context"] = stub_context
        sys.modules["plugins.hooks"] = stub_hooks
        sys.modules["plugins.clients.mcp_client"] = stub_mcp
        sys.modules["plugins.tools"] = stub_tools_pkg

        # Stub guardrails (added by T3 — sanitize_result passthrough so __init__ loads).
        stub_guardrails = _make_stub_module(
            "plugins.tools.guardrails",
            {"sanitize_result": lambda tool_name, result: result},
        )
        sys.modules["plugins.tools.guardrails"] = stub_guardrails
        stub_tools_pkg.guardrails = stub_guardrails

        tool_names = [
            "workspace",
            "feature",
            "artifacts",
            "edit",
            "file_ops",
            "read",
            "read_workspace_file",
            "list_documents",
            "tasks",
            "gitnexus",
            "rag",
            "skills",
            "approval",
            "approve",
            "move_feature",
            "tasks_write",
            "create_tasks",
            "parse_tasks",
            "vcs_pr_context",
            "vcs_pr_review",
            "lookup_feature",
            "init_feature",
            "create_pr",
            "ensure_branch",
            "commit_files",
            "feature_context",
        ]
        for tname in tool_names:
            stub = _stub_tool(tname)
            sys.modules[f"plugins.tools.{tname}"] = stub
            setattr(stub_tools_pkg, tname, stub)

        # Also stub artifacts and edit with their multi-schema attributes.
        sys.modules["plugins.tools.artifacts"].WRITE_SPEC_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.artifacts"].WRITE_TD_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.artifacts"].handle_write_product_spec = lambda **_: {}
        sys.modules["plugins.tools.artifacts"].handle_write_technical_design = (
            lambda **_: {}
        )
        sys.modules["plugins.tools.edit"].EDIT_DOCUMENT_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.edit"].handle_edit_document = lambda **_: {}
        sys.modules["plugins.tools.file_ops"].WRITE_FILE_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.file_ops"].handle_write_file = lambda **_: {}
        sys.modules["plugins.tools.file_ops"].EDIT_FILE_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.file_ops"].handle_edit_file = lambda **_: {}
        sys.modules["plugins.tools.read"].READ_FILE_SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.read"].handle_read_file = lambda **_: {}
        sys.modules["plugins.tools.tasks"].handle = lambda **_: {}
        sys.modules["plugins.tools.approval"].handle = lambda **_: {}
        sys.modules["plugins.tools.approve"].handle = lambda **_: {}
        sys.modules["plugins.tools.tasks_write"].handle = lambda **_: {}
        sys.modules["plugins.tools.gitnexus"].check_available = lambda **_: False
        sys.modules["plugins.tools.rag"].check_available = lambda **_: False
        sys.modules["plugins.tools.skills"].handle = lambda **_: {}
        sys.modules["plugins.tools.skills"].check_available = lambda **_: False
        sys.modules["plugins.tools.skills"].SCHEMA = {
            "description": "",
            "parameters": {},
        }
        sys.modules["plugins.tools.vcs_pr_context"].check_available = lambda **_: (
            False
        )
        sys.modules["plugins.tools.vcs_pr_review"].check_available = lambda **_: (
            False
        )
        sys.modules["plugins.tools.lookup_feature"].check_available = lambda **_: False
        sys.modules["plugins.tools.create_pr"].check_available = lambda **_: False
        sys.modules["plugins.tools.ensure_branch"].check_available = lambda **_: False
        sys.modules["plugins.tools.commit_files"].check_available = lambda **_: False

        # Load the real suggest_next_actions into sys.modules first.
        sna_mod, *_ = _load_suggest_next_actions_isolated()
        sys.modules["plugins.tools.suggest_next_actions"] = sna_mod
        stub_tools_pkg.suggest_next_actions = sna_mod

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

        # _TOOLS is now populated by the profile setup, not at module load time.
        # Check the workflow profile's tool list instead.
        from src.tool_setup import _WORKFLOW_TOOLS

        names = {t["name"] for t in _WORKFLOW_TOOLS}
        assert "suggest_next_actions" in names, (
            f"suggest_next_actions missing from _WORKFLOW_TOOLS; registered: {sorted(names)}"
        )
