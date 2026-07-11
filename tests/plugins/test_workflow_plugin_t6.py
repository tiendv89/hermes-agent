"""Tests for the T6 skills subsystem (local-bundle loader).

Skills are bundled with the gateway under ``plugins/skills/claude/`` and the
index is built by walking that tree — no network or GitHub token involved.

Covers:
  - SkillEntry dataclass
  - _parse_description: extracts description from YAML frontmatter
  - _skills_for_repos: maps repo IDs to skill names
  - build_index / get_index: loads the real bundle; caches after first call
  - _index_skill / _build_index_from_bundle: indexing a directory tree
    (descriptions required, references collected, excluded buckets)
  - load_skill (handle): happy path, unknown skill, empty index
  - load_skill check_available: gated on a non-empty index
  - _TOOLS registration: load_skill present in register()
  - inject_context: skill descriptions injected; load_skill advertised when
    the index is populated; no skills block when the index is empty
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_modules():
    """Remove plugins modules between tests to avoid cross-test pollution
    (the skill index caches at module scope)."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env_vars(monkeypatch):
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_BACKEND_SERVICE_TOKEN", raising=False)
    yield


def _write_skill(skill_dir: Path, description: str | None) -> None:
    """Write a minimal SKILL.md into *skill_dir* (description optional)."""
    skill_dir.mkdir(parents=True, exist_ok=True)
    if description is None:
        body = f"---\nname: {skill_dir.name}\n---\n# {skill_dir.name}"
    else:
        body = f"---\nname: {skill_dir.name}\ndescription: {description}\n---\n# {skill_dir.name}"
    (skill_dir / "SKILL.md").write_text(body, encoding="utf-8")


# ---------------------------------------------------------------------------
# SkillEntry dataclass
# ---------------------------------------------------------------------------


class TestSkillEntry:
    def test_fields_accessible(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="foo", description="A foo skill", path="claude/technical_skills/foo")
        assert e.name == "foo"
        assert e.description == "A foo skill"
        assert e.path == "claude/technical_skills/foo"
        assert e.is_authoring is False
        assert e.body == ""
        assert e.references == {}

    def test_body_setter(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="foo", description="desc", path="some/path")
        e.body = "# Foo\nContent here."
        assert e.body == "# Foo\nContent here."

    def test_authoring_flag(self):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name="tech-lead", description="Tech lead authoring skill",
                       path="claude/workflow_skills/tech-lead", is_authoring=True)
        assert e.is_authoring is True


# ---------------------------------------------------------------------------
# _parse_description
# ---------------------------------------------------------------------------


class TestParseDescription:
    def test_extracts_description_from_frontmatter(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\ndescription: A helpful skill for doing things.\n---\n# Foo"
        assert _parse_description(skill_md) == "A helpful skill for doing things."

    def test_returns_empty_when_no_frontmatter(self):
        from plugins.skills.index import _parse_description

        assert _parse_description("# No frontmatter here") == ""

    def test_returns_empty_when_no_description_field(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\n---\n# Content"
        assert _parse_description(skill_md) == ""

    def test_handles_multiline_frontmatter(self):
        from plugins.skills.index import _parse_description

        skill_md = "---\nname: foo\nother: value\ndescription: This is the description.\n---\n"
        assert _parse_description(skill_md) == "This is the description."


# ---------------------------------------------------------------------------
# build_index / get_index — loads the real bundle, then caches
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_loads_bundled_skills(self):
        from plugins.skills.index import build_index

        index = build_index()
        # Known knowledge + authoring skills ship in the bundle.
        assert "python-best-practices" in index
        assert "typescript-best-practices" in index
        assert "tech-lead" in index
        assert index["tech-lead"].is_authoring is True
        assert index["python-best-practices"].is_authoring is False

    def test_includes_all_workflow_skills(self):
        from plugins.skills.index import build_index

        index = build_index()
        # Every workflow_skills/* entry is loadable (is_authoring=True),
        # including mutation/execution skills — load_skill returns guidance only.
        for name in ("approve-feature", "reject-feature", "set-feature-stage",
                     "start-implementation", "list-features", "tech-lead"):
            assert name in index, name
            assert index[name].is_authoring is True

    def test_empty_when_bundle_missing(self, monkeypatch, tmp_path):
        import plugins.skills.index as index_mod

        monkeypatch.setattr(index_mod, "_BUNDLE_ROOT", tmp_path / "does-not-exist")
        assert index_mod.build_index() == {}


class TestGetIndex:
    def test_result_is_cached_and_built_once(self):
        with patch(
            "plugins.skills.index._build_index_from_bundle",
            return_value={"python-best-practices": MagicMock()},
        ) as mock_build:
            from plugins.skills import get_index

            r1 = get_index()
            r2 = get_index()

        assert r1 is r2
        mock_build.assert_called_once()

    def test_get_skill_returns_none_for_unknown(self):
        from plugins.skills import get_skill

        assert get_skill("no-such-skill-xyz") is None

    def test_get_skill_returns_entry_when_known(self):
        from plugins.skills import get_skill

        entry = get_skill("python-best-practices")
        assert entry is not None
        assert entry.name == "python-best-practices"
        assert entry.description


# ---------------------------------------------------------------------------
# get_shared_rules — CLAUDE.shared.md (injected into the system prompt)
# ---------------------------------------------------------------------------


class TestSharedRules:
    def test_returns_bundled_shared_rules(self):
        from plugins.skills import get_shared_rules

        rules = get_shared_rules()
        assert rules  # non-empty
        assert "Feature lifecycle" in rules

    def test_cached_after_first_read(self):
        import plugins.skills.index as index_mod

        with patch.object(index_mod, "_read", wraps=index_mod._read) as spy:
            index_mod.get_shared_rules()
            index_mod.get_shared_rules()
        assert spy.call_count == 1

    def test_empty_when_file_missing(self, monkeypatch, tmp_path):
        import plugins.skills.index as index_mod

        monkeypatch.setattr(index_mod, "_BUNDLE_ROOT", tmp_path / "missing")
        assert index_mod.get_shared_rules() == ""


# ---------------------------------------------------------------------------
# Bucket assignment — technical = knowledge, workflow = authoring
# ---------------------------------------------------------------------------


class TestSkillBuckets:
    def test_technical_skills_are_knowledge(self):
        from plugins.skills.index import build_index

        index = build_index()
        assert index["python-best-practices"].is_authoring is False
        assert index["typescript-best-practices"].is_authoring is False

    def test_workflow_skills_are_authoring(self):
        from plugins.skills.index import build_index

        index = build_index()
        for name in ("tech-lead", "init-feature", "approve-feature", "set-feature-stage"):
            assert index[name].is_authoring is True

    def test_entry_path_reflects_bucket(self):
        from plugins.skills import get_skill

        assert get_skill("python-best-practices").path.startswith("technical_skills/")
        assert get_skill("tech-lead").path.startswith("workflow_skills/")


# ---------------------------------------------------------------------------
# _build_index_from_bundle / _index_skill — directory walking
# ---------------------------------------------------------------------------


class TestBuildIndexFromBundle:
    def test_indexes_technical_and_authoring_skills(self, tmp_path):
        _write_skill(tmp_path / "technical_skills" / "python-best-practices", "Python best practices.")
        _write_skill(tmp_path / "workflow_skills" / "tech-lead", "Tech lead authoring skill.")

        from plugins.skills.index import _build_index_from_bundle

        index = _build_index_from_bundle(tmp_path)

        assert index["python-best-practices"].description == "Python best practices."
        assert index["python-best-practices"].is_authoring is False
        assert index["tech-lead"].is_authoring is True

    def test_skips_skill_without_description(self, tmp_path):
        _write_skill(tmp_path / "technical_skills" / "no-desc", None)

        from plugins.skills.index import _build_index_from_bundle

        assert "no-desc" not in _build_index_from_bundle(tmp_path)

    def test_includes_all_workflow_skills(self, tmp_path):
        _write_skill(tmp_path / "workflow_skills" / "approve-feature", "Approve a feature.")
        _write_skill(tmp_path / "workflow_skills" / "start-implementation", "Start implementation.")

        from plugins.skills.index import _build_index_from_bundle

        index = _build_index_from_bundle(tmp_path)
        assert index["approve-feature"].is_authoring is True
        assert index["start-implementation"].is_authoring is True

    def test_collects_reference_files(self, tmp_path):
        ts_dir = tmp_path / "technical_skills" / "ts"
        _write_skill(ts_dir, "TypeScript skill.")
        (ts_dir / "advanced-types.md").write_text("# Advanced Types", encoding="utf-8")
        (ts_dir / "references").mkdir()
        (ts_dir / "references" / "patterns.md").write_text("# Patterns", encoding="utf-8")

        from plugins.skills.index import _build_index_from_bundle

        entry = _build_index_from_bundle(tmp_path)["ts"]
        assert entry.references["advanced-types.md"] == "# Advanced Types"
        assert entry.references["references/patterns.md"] == "# Patterns"


# ---------------------------------------------------------------------------
# load_skill tool handler
# ---------------------------------------------------------------------------


class TestLoadSkillHandler:
    def _make_entry(self, name: str = "python-best-practices") -> object:
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name=name, description=f"{name} description", path=f"p/{name}")
        e.body = f"# {name}\nContent."
        e.references = {"ref.md": "# Reference"}
        return e

    def test_happy_path_returns_skill(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry):
            from plugins.tools.skills import handle

            result = handle(name="python-best-practices")

        assert result["ok"] is True
        skill = result["skill"]
        assert skill["name"] == "python-best-practices"
        assert skill["description"] == "python-best-practices description"
        assert "# python-best-practices" in skill["body"]
        assert "ref.md" in skill["references"]

    def test_unknown_skill_returns_error(self):
        with patch("plugins.skills.get_skill", return_value=None):
            with patch("plugins.skills.get_index", return_value={"python-best-practices": MagicMock()}):
                from plugins.tools.skills import handle

                result = handle(name="no-such-skill")

        assert result["ok"] is False
        assert "no-such-skill" in result["error"]
        assert "python-best-practices" in result["error"]

    def test_empty_index_returns_descriptive_error(self):
        with (
            patch("plugins.skills.get_skill", return_value=None),
            patch("plugins.skills.get_index", return_value={}),
        ):
            from plugins.tools.skills import handle

            result = handle(name="something")

        assert result["ok"] is False
        assert "plugins/skills" in result["error"]

    def test_empty_name_returns_error(self):
        from plugins.tools.skills import handle

        result = handle(name="")
        assert result["ok"] is False
        assert "non-empty" in result["error"]

    def test_dict_name_is_coerced(self):
        """Over the MCP path the model may pass {"name": "..."} instead of a str.
        It must not raise 'dict' object has no attribute 'strip'."""
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry) as mock_get:
            from plugins.tools.skills import handle

            result = handle(name={"name": "python-best-practices"})
        assert result["ok"] is True
        mock_get.assert_called_once_with("python-best-practices")

    def test_non_string_name_does_not_raise(self):
        from plugins.tools.skills import handle

        # A bare None / number must degrade to the empty-name error, not crash.
        assert handle(name=None)["ok"] is False
        assert handle(name=123)["ok"] is False

    def test_whitespace_stripped(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry) as mock_get:
            from plugins.tools.skills import handle

            handle(name="  python-best-practices  ")
        mock_get.assert_called_once_with("python-best-practices")

    def test_references_returned_as_copy(self):
        entry = self._make_entry()
        with patch("plugins.skills.get_skill", return_value=entry):
            from plugins.tools.skills import handle

            r1 = handle(name="python-best-practices")
            r2 = handle(name="python-best-practices")

        r1["skill"]["references"]["new_key"] = "injected"
        assert "new_key" not in r2["skill"]["references"]


class TestLoadSkillCheckAvailable:
    def test_true_when_bundle_present(self):
        from plugins.tools.skills import check_available

        # The real bundle ships with the repo, so the index is non-empty.
        assert check_available() is True

    def test_false_when_index_empty(self):
        with patch("plugins.skills.get_index", return_value={}):
            from plugins.tools.skills import check_available

            assert check_available() is False


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_load_skill_in_tools_list(self):
        from plugins import _TOOLS

        names = [t["name"] for t in _TOOLS]
        assert "load_skill" in names

    def test_load_skill_has_required_fields(self):
        from plugins import _TOOLS

        tool = next(t for t in _TOOLS if t["name"] == "load_skill")
        assert "schema" in tool
        assert "handler" in tool
        assert "check_fn" in tool

    def test_register_includes_load_skill(self):
        ctx = MagicMock()
        from plugins import register

        register(ctx)
        registered_names = [call.kwargs.get("name") or call.args[0]
                            for call in ctx.register_tool.call_args_list]
        assert "load_skill" in registered_names


# ---------------------------------------------------------------------------
# _skills_for_repos stack matching
# ---------------------------------------------------------------------------


class TestSkillsForRepos:
    def test_ui_repo_returns_frontend_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["digital-factory-ui"])
        assert "typescript-best-practices" in skills
        assert "frontend-engineer" in skills

    def test_hermes_repo_returns_python_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["hermes-agent"])
        assert "python-best-practices" in skills

    def test_workflow_repo_returns_go_skills(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["workflow-backend"])
        assert "go-best-practices" in skills

    def test_multiple_repos_deduped(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["hermes-agent", "hermes-agent"])
        assert skills.count("python-best-practices") == 1

    def test_unknown_repo_returns_empty(self):
        from plugins.hooks import _skills_for_repos

        skills = _skills_for_repos(["totally-unknown-repo-xyz"])
        assert skills == []


# ---------------------------------------------------------------------------
# inject_context — skills injection
# ---------------------------------------------------------------------------


class TestInjectContextSkills:
    def _call_inject(self, workspace_id: str = "ws-1", feature_id: str = "feat-1"):
        import plugins.context as ctx_mod
        ctx_mod.set_context("sess-1", workspace_id, feature_id)
        from plugins.hooks import inject_context

        result = inject_context(session_id="sess-1")
        return result["context"] if result else ""

    def test_no_skills_block_when_index_empty(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={}),
        ):
            content = self._call_inject()
        assert "Available skills" not in content

    def test_skills_block_injected_when_index_populated(self):
        skills_block = "## Available skills (call load_skill to load full content)\n  python-best-practices: Python skill"
        with (
            patch("plugins.hooks._build_skills_block", return_value=skills_block),
            patch("plugins.hooks.check_workflow_available", return_value=False),
        ):
            content = self._call_inject()
        assert "Available skills" in content
        assert "python-best-practices" in content

    def test_load_skill_advertised_when_index_populated(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={"python-best-practices": MagicMock()}),
        ):
            content = self._call_inject()
        assert "load_skill" in content

    def test_load_skill_not_advertised_when_index_empty(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
            patch("plugins.hooks.get_index", return_value={}),
        ):
            content = self._call_inject()
        assert "load_skill" not in content

    def test_skills_block_called_with_stage(self, monkeypatch):
        fake_feature = {
            "ok": True,
            "feature": {
                "title": "Test Feature",
                "feature_name": "test-feature",
                "stage": "technical_design",
                "status": "in_implementation",
                "next_action": None,
            },
        }
        with (
            patch("plugins.hooks.check_workflow_available", return_value=True),
            patch("plugins.tools.workspace.handle", return_value={"ok": True, "workspace": {"repos": []}}),
            patch("plugins.tools.feature.handle", return_value=fake_feature),
            patch("plugins.tools.tasks.handle", return_value={"ok": True, "tasks": []}),
            patch("plugins.hooks._build_skills_block", return_value=None) as mock_block,
        ):
            self._call_inject()
        mock_block.assert_called_once()
        _, stage, _ = mock_block.call_args[0]
        assert stage == "technical_design"


class TestBuildSkillsBlock:
    def _make_entry(self, name: str, desc: str, is_authoring: bool = False):
        from plugins.skills.index import SkillEntry

        e = SkillEntry(name=name, description=desc, path="p", is_authoring=is_authoring)
        e.body = "# Body"
        return e

    def test_returns_none_when_index_empty(self):
        with patch("plugins.hooks.get_index", return_value={}):
            from plugins.hooks import _build_skills_block

            assert _build_skills_block("feat-1", "technical_design", ["hermes-agent"]) is None

    def test_knowledge_skills_listed(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
            "tech-lead": self._make_entry("tech-lead", "Authoring skill", is_authoring=True),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", [])

        assert "python-best-practices" in block
        assert "Knowledge skills" in block

    def test_authoring_skills_listed(self):
        index = {
            "tech-lead": self._make_entry("tech-lead", "Tech lead skill", is_authoring=True),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", [])

        assert "tech-lead" in block
        assert "Authoring skills" in block

    def test_technical_design_stage_surfaces_stack_matched_first(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
            "go-best-practices": self._make_entry("go-best-practices", "Go skill"),
            "typescript-best-practices": self._make_entry("typescript-best-practices", "TS skill"),
        }
        with (
            patch("plugins.hooks.get_index", return_value=index),
            patch("plugins.hooks._skills_for_repos", return_value=["python-best-practices"]),
        ):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "technical_design", ["hermes-agent"])

        assert "Stack-matched skills" in block
        stack_pos = block.find("Stack-matched")
        other_pos = block.find("Other knowledge")
        python_pos = block.find("python-best-practices")
        assert python_pos > stack_pos
        assert python_pos < other_pos

    def test_non_technical_design_stage_no_stack_matching(self):
        index = {
            "python-best-practices": self._make_entry("python-best-practices", "Python skill"),
        }
        with patch("plugins.hooks.get_index", return_value=index):
            from plugins.hooks import _build_skills_block

            block = _build_skills_block("feat-1", "in_implementation", ["hermes-agent"])

        assert "Stack-matched" not in block
