"""Tests for T6 skills subsystem additions to plugins.

Covers:
  - SkillEntry dataclass
  - get_index: empty when env vars absent; cached after first call
  - _parse_description: extracts description from YAML frontmatter
  - _skills_for_repos: maps repo IDs to skill names
  - build_index: returns empty dict when WORKFLOW_GITHUB_REPO is not set
  - load_skill (handle): happy path, unknown skill, empty index
  - load_skill check_available: gated on WORKFLOW_GITHUB_REPO
  - _TOOLS registration: workflow_load_skill present in register()
  - inject_context: skill descriptions injected in output
  - inject_context: stack-matched skills injected for technical_design stage
  - inject_context: workflow_load_skill advertised when WORKFLOW_GITHUB_REPO set
  - inject_context: no skills block when index is empty
  - index excludes mutation and execution skills by path (unit test)
  - load_skill returns body + references for known skill
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
    """Remove plugins modules between tests to avoid cross-test pollution."""
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("plugins")]
    for k in keys:
        del sys.modules[k]


@pytest.fixture(autouse=True)
def _clear_env_vars(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("WORKFLOW_GITHUB_REPO", raising=False)
    monkeypatch.delenv("WORKFLOW_GITHUB_BRANCH", raising=False)
    monkeypatch.delenv("GITNEXUS_MCP_URL", raising=False)
    monkeypatch.delenv("RAG_MCP_URL", raising=False)
    monkeypatch.delenv("WORKFLOW_DATABASE_URL", raising=False)
    yield


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
# build_index / get_index — env var gating
# ---------------------------------------------------------------------------


class TestBuildIndex:
    def test_empty_when_no_env_vars(self):
        from plugins.skills.index import build_index

        result = build_index()
        assert result == {}

    def test_empty_when_token_missing(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")
        from plugins.skills.index import build_index

        result = build_index()
        assert result == {}

    def test_empty_when_repo_missing(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        from plugins.skills.index import build_index

        result = build_index()
        assert result == {}

    def test_empty_when_repo_format_invalid(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "not-valid-format")
        from plugins.skills.index import build_index

        result = build_index()
        assert result == {}

    def test_calls_github_api_when_configured(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")
        with patch("plugins.skills.index._build_index_from_github", return_value={}) as mock_build:
            from plugins.skills.index import build_index

            result = build_index()
        mock_build.assert_called_once_with("owner", "repo", "main", "ghp_fake")
        assert result == {}

    def test_custom_branch_passed_through(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")
        monkeypatch.setenv("WORKFLOW_GITHUB_BRANCH", "develop")
        with patch("plugins.skills.index._build_index_from_github", return_value={}) as mock_build:
            from plugins.skills.index import build_index

            build_index()
        mock_build.assert_called_once_with("owner", "repo", "develop", "ghp_fake")


# ---------------------------------------------------------------------------
# get_index — caching
# ---------------------------------------------------------------------------


class TestGetIndex:
    def test_returns_empty_without_config(self):
        from plugins.skills.index import get_index

        assert get_index() == {}

    def test_result_is_cached(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")

        fake_entry = MagicMock()
        fake_entry.name = "python-best-practices"

        call_count = {"n": 0}

        def fake_build(owner, repo, ref, token):
            call_count["n"] += 1
            return {"python-best-practices": fake_entry}

        with patch("plugins.skills.index._build_index_from_github", side_effect=fake_build):
            from plugins.skills import get_index

            r1 = get_index()
            r2 = get_index()

        assert r1 is r2
        assert call_count["n"] == 1

    def test_get_skill_returns_none_for_unknown(self, monkeypatch):
        monkeypatch.delenv("WORKFLOW_GITHUB_REPO", raising=False)
        from plugins.skills import get_skill

        assert get_skill("no-such-skill") is None

    def test_get_skill_returns_entry_when_known(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_fake")
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")

        from plugins.skills.index import SkillEntry

        entry = SkillEntry(name="python-best-practices", description="Python skill", path="p")
        with patch("plugins.skills.index._build_index_from_github", return_value={"python-best-practices": entry}):
            from plugins.skills import get_skill

            result = get_skill("python-best-practices")
        assert result is entry


# ---------------------------------------------------------------------------
# Skill path filtering — excluded skills
# ---------------------------------------------------------------------------


class TestSkillPathFiltering:
    """Verify the path-based inclusion logic without hitting GitHub."""

    def _make_blobs(self, paths: list[str]) -> list[dict]:
        return [{"path": p, "type": "blob"} for p in paths]

    def test_technical_skills_included(self):
        from plugins.skills.index import _TECHNICAL_SKILLS_PREFIX

        path = f"{_TECHNICAL_SKILLS_PREFIX}/python-best-practices/SKILL.md"
        assert path.startswith(_TECHNICAL_SKILLS_PREFIX + "/")

    def test_authoring_skills_included(self):
        from plugins.skills.index import _AUTHORING_SKILLS, _WORKFLOW_SKILLS_PREFIX

        for skill in _AUTHORING_SKILLS:
            path = f"{_WORKFLOW_SKILLS_PREFIX}/{skill}/SKILL.md"
            parts = path[len(_WORKFLOW_SKILLS_PREFIX) + 1:].split("/")
            assert parts[0] in _AUTHORING_SKILLS

    def test_mutation_skills_excluded(self):
        from plugins.skills.index import _AUTHORING_SKILLS, _MUTATION_SKILLS

        for skill in _MUTATION_SKILLS:
            assert skill not in _AUTHORING_SKILLS

    def test_execution_skills_excluded(self):
        from plugins.skills.index import _AUTHORING_SKILLS, _EXECUTION_SKILLS

        for skill in _EXECUTION_SKILLS:
            assert skill not in _AUTHORING_SKILLS


# ---------------------------------------------------------------------------
# _build_index_from_github — unit with mocked GitHub responses
# ---------------------------------------------------------------------------


class TestBuildIndexFromGithub:
    def _make_tree(self, paths: list[str]) -> dict:
        return {
            "truncated": False,
            "tree": [{"path": p, "type": "blob"} for p in paths],
        }

    def test_indexes_technical_skill(self):
        tree = self._make_tree([
            "claude/technical_skills/python-best-practices/SKILL.md",
        ])
        skill_md = "---\nname: python-best-practices\ndescription: Python best practices skill.\n---\n# Python"

        with (
            patch("plugins.skills.index.requests.get") as mock_get,
        ):
            # First call: tree; second call: SKILL.md content
            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree

            import base64
            file_resp = MagicMock()
            file_resp.raise_for_status = MagicMock()
            file_resp.status_code = 200
            file_resp.json.return_value = {
                "encoding": "base64",
                "content": base64.b64encode(skill_md.encode()).decode(),
            }

            mock_get.side_effect = [tree_resp, file_resp]

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "python-best-practices" in result
        entry = result["python-best-practices"]
        assert entry.description == "Python best practices skill."
        assert entry.body == skill_md
        assert entry.is_authoring is False

    def test_indexes_authoring_skill(self):
        tree = self._make_tree([
            "claude/workflow_skills/tech-lead/SKILL.md",
        ])
        skill_md = "---\nname: tech-lead\ndescription: Tech lead authoring skill.\n---\n# TL"

        with patch("plugins.skills.index.requests.get") as mock_get:
            import base64

            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree

            file_resp = MagicMock()
            file_resp.raise_for_status = MagicMock()
            file_resp.status_code = 200
            file_resp.json.return_value = {
                "encoding": "base64",
                "content": base64.b64encode(skill_md.encode()).decode(),
            }

            mock_get.side_effect = [tree_resp, file_resp]

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "tech-lead" in result
        assert result["tech-lead"].is_authoring is True

    def test_excludes_mutation_skills(self):
        tree = self._make_tree([
            "claude/workflow_skills/approve-feature/SKILL.md",
            "claude/workflow_skills/reject-feature/SKILL.md",
        ])

        with patch("plugins.skills.index.requests.get") as mock_get:
            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree
            mock_get.return_value = tree_resp

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "approve-feature" not in result
        assert "reject-feature" not in result

    def test_excludes_execution_skills(self):
        tree = self._make_tree([
            "claude/workflow_skills/start-implementation/SKILL.md",
            "claude/workflow_skills/review-pr/SKILL.md",
        ])

        with patch("plugins.skills.index.requests.get") as mock_get:
            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree
            mock_get.return_value = tree_resp

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "start-implementation" not in result
        assert "review-pr" not in result

    def test_skips_skill_without_description(self):
        tree = self._make_tree([
            "claude/technical_skills/no-desc/SKILL.md",
        ])
        skill_md = "---\nname: no-desc\n---\n# No description here"

        with patch("plugins.skills.index.requests.get") as mock_get:
            import base64

            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree

            file_resp = MagicMock()
            file_resp.raise_for_status = MagicMock()
            file_resp.status_code = 200
            file_resp.json.return_value = {
                "encoding": "base64",
                "content": base64.b64encode(skill_md.encode()).decode(),
            }

            mock_get.side_effect = [tree_resp, file_resp]

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "no-desc" not in result

    def test_includes_reference_files(self):
        tree = self._make_tree([
            "claude/technical_skills/ts/SKILL.md",
            "claude/technical_skills/ts/advanced-types.md",
            "claude/technical_skills/ts/references/patterns.md",
        ])
        skill_md = "---\nname: ts\ndescription: TypeScript skill.\n---\n# TS"

        with patch("plugins.skills.index.requests.get") as mock_get:
            import base64

            def make_file_resp(content: str):
                r = MagicMock()
                r.raise_for_status = MagicMock()
                r.status_code = 200
                r.json.return_value = {
                    "encoding": "base64",
                    "content": base64.b64encode(content.encode()).decode(),
                }
                return r

            tree_resp = MagicMock()
            tree_resp.raise_for_status = MagicMock()
            tree_resp.json.return_value = tree

            mock_get.side_effect = [
                tree_resp,
                make_file_resp(skill_md),           # SKILL.md
                make_file_resp("# Advanced Types"),  # advanced-types.md
                make_file_resp("# Patterns"),        # references/patterns.md
            ]

            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert "ts" in result
        entry = result["ts"]
        assert "advanced-types.md" in entry.references
        assert "references/patterns.md" in entry.references

    def test_handles_tree_fetch_failure(self):
        import requests as req

        with patch("plugins.skills.index.requests.get", side_effect=req.RequestException("network error")):
            from plugins.skills.index import _build_index_from_github

            result = _build_index_from_github("owner", "repo", "main", "token")

        assert result == {}


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
        assert "WORKFLOW_GITHUB_REPO" in result["error"]

    def test_empty_name_returns_error(self):
        from plugins.tools.skills import handle

        result = handle(name="")
        assert result["ok"] is False
        assert "non-empty" in result["error"]

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

        # Modifying one result's references doesn't affect the other
        r1["skill"]["references"]["new_key"] = "injected"
        assert "new_key" not in r2["skill"]["references"]


class TestLoadSkillCheckAvailable:
    def test_false_when_repo_not_set(self):
        from plugins.tools.skills import check_available

        assert check_available() is False

    def test_true_when_repo_set(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")
        from plugins.tools.skills import check_available

        assert check_available() is True


# ---------------------------------------------------------------------------
# _TOOLS registration
# ---------------------------------------------------------------------------


class TestToolsRegistration:
    def test_load_skill_in_tools_list(self):
        from plugins import _TOOLS

        names = [t["name"] for t in _TOOLS]
        assert "workflow_load_skill" in names

    def test_load_skill_has_required_fields(self):
        from plugins import _TOOLS

        tool = next(t for t in _TOOLS if t["name"] == "workflow_load_skill")
        assert "schema" in tool
        assert "handler" in tool
        assert "check_fn" in tool

    def test_register_includes_load_skill(self):
        ctx = MagicMock()
        from plugins import register

        register(ctx)
        registered_names = [call.kwargs.get("name") or call.args[0]
                            for call in ctx.register_tool.call_args_list]
        assert "workflow_load_skill" in registered_names


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
    def _call_inject(self, workspace_id: str = "ws-1", feature_id: str = "feat-1", monkeypatch=None):
        if monkeypatch:
            monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
        import plugins.context as ctx_mod
        ctx_mod.set_context("sess-1", workspace_id, feature_id)
        from plugins.hooks import inject_context

        result = inject_context(session_id="sess-1")
        return result["context"] if result else ""

    def test_no_skills_block_when_index_empty(self, monkeypatch):
        with patch("plugins.hooks._build_skills_block", return_value=None):
            with patch("plugins.hooks.check_workflow_available", return_value=False):
                content = self._call_inject()
        assert "Available skills" not in content

    def test_skills_block_injected_when_index_populated(self, monkeypatch):
        skills_block = "## Available skills (call workflow_load_skill to load full content)\n  python-best-practices: Python skill"
        with (
            patch("plugins.hooks._build_skills_block", return_value=skills_block),
            patch("plugins.hooks.check_workflow_available", return_value=False),
        ):
            content = self._call_inject()
        assert "Available skills" in content
        assert "python-best-practices" in content

    def test_load_skill_advertised_when_repo_set(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_GITHUB_REPO", "owner/repo")
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
        ):
            content = self._call_inject()
        assert "workflow_load_skill" in content

    def test_load_skill_not_advertised_when_repo_unset(self):
        with (
            patch("plugins.hooks._build_skills_block", return_value=None),
            patch("plugins.hooks.check_workflow_available", return_value=False),
        ):
            content = self._call_inject()
        assert "workflow_load_skill" not in content

    def test_skills_block_called_with_stage(self, monkeypatch):
        monkeypatch.setenv("WORKFLOW_DATABASE_URL", "postgresql://fake")
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
        # python-best-practices should appear in the stack-matched section (before "Other knowledge skills")
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
