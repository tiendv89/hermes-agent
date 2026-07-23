"""Unit tests for the per-turn doc-vs-coding classifier in coding_triage.py.

Coverage:
  - is_coding_request(): fails open (False) on empty message, disabled flag,
    or classifier error
  - is_coding_request(): returns True only on a confident CODING verdict
  - is_coding_request(): mid-thread doc -> "now implement it" flips to True
    given the prior turn as history context
  - _classifier_model(): Anthropic drops to Haiku; override env var wins
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))


def _inject_mock_agent():
    """Inject a mock `agent.auxiliary_client` into sys.modules so that
    `from agent.auxiliary_client import call_llm` succeeds in tests."""
    agent_pkg = types.ModuleType("agent")
    aux_mod = types.ModuleType("agent.auxiliary_client")
    aux_mod.call_llm = MagicMock()
    sys.modules["agent"] = agent_pkg
    sys.modules["agent.auxiliary_client"] = aux_mod
    return aux_mod


def _remove_mock_agent():
    sys.modules.pop("agent.auxiliary_client", None)
    sys.modules.pop("agent", None)


def _load_coding_triage():
    """Import coding_triage fresh (HERMES_CODING_TRIAGE taken from env at call time)."""
    mod_name = "src.api.triage"
    sys.modules.pop(mod_name, None)
    spec = importlib.util.spec_from_file_location(
        mod_name,
        REPO_ROOT / "src" / "api" / "triage.py",
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def ct():
    """coding_triage module with HERMES_CODING_TRIAGE=1 (enabled), no LLM mocked."""
    old = os.environ.get("HERMES_CODING_TRIAGE")
    os.environ["HERMES_CODING_TRIAGE"] = "1"
    mod = _load_coding_triage()
    yield mod
    if old is None:
        os.environ.pop("HERMES_CODING_TRIAGE", None)
    else:
        os.environ["HERMES_CODING_TRIAGE"] = old
    sys.modules.pop("src.api.triage", None)


@pytest.fixture
def ct_disabled():
    old = os.environ.get("HERMES_CODING_TRIAGE")
    os.environ["HERMES_CODING_TRIAGE"] = "0"
    mod = _load_coding_triage()
    yield mod
    if old is None:
        os.environ.pop("HERMES_CODING_TRIAGE", None)
    else:
        os.environ["HERMES_CODING_TRIAGE"] = old
    sys.modules.pop("src.api.triage", None)


@pytest.fixture
def ct_with_llm():
    old = os.environ.get("HERMES_CODING_TRIAGE")
    os.environ["HERMES_CODING_TRIAGE"] = "1"
    aux_mod = _inject_mock_agent()
    mod = _load_coding_triage()
    yield mod, aux_mod
    if old is None:
        os.environ.pop("HERMES_CODING_TRIAGE", None)
    else:
        os.environ["HERMES_CODING_TRIAGE"] = old
    _remove_mock_agent()
    sys.modules.pop("src.api.triage", None)


def _resp(text: str) -> MagicMock:
    resp = MagicMock()
    resp.choices[0].message.content = text
    return resp


# ---------------------------------------------------------------------------
# Fail-open behavior
# ---------------------------------------------------------------------------


class TestFailOpen:
    def test_empty_message_not_coding(self, ct):
        assert ct.is_coding_request("") is False

    def test_whitespace_only_not_coding(self, ct):
        assert ct.is_coding_request("   ") is False

    def test_disabled_flag_always_false(self, ct_disabled):
        assert ct_disabled.is_coding_request("implement the login endpoint") is False

    def test_classifier_error_fails_open(self, ct):
        # No agent.auxiliary_client injected -> import fails -> fails open.
        result = ct.is_coding_request("implement the login endpoint")
        assert result is False

    def test_classifier_exception_fails_open(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.side_effect = RuntimeError("provider unreachable")
        assert mod.is_coding_request("implement the login endpoint") is False

    def test_garbled_verdict_fails_open(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("uhh not sure")
        assert mod.is_coding_request("implement the login endpoint") is False


# ---------------------------------------------------------------------------
# Classifier verdicts
# ---------------------------------------------------------------------------


class TestClassifierVerdicts:
    def test_coding_verdict_returns_true(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("CODING")
        assert mod.is_coding_request("implement task 3, the login endpoint") is True

    def test_doc_verdict_returns_false(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("DOC")
        assert mod.is_coding_request("write the technical design for auth") is False

    def test_call_llm_uses_cheap_classifier_params(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("DOC")
        mod.is_coding_request("write the product spec")
        _, kwargs = aux_mod.call_llm.call_args
        assert kwargs["temperature"] == 0
        assert kwargs["max_tokens"] == 8
        assert kwargs["timeout"] == 10


# ---------------------------------------------------------------------------
# Mid-thread intent flip: doc turn -> "now implement it"
# ---------------------------------------------------------------------------


class TestMidThreadFlip:
    def test_history_is_included_in_classifier_prompt(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("CODING")

        history = [
            {"role": "user", "content": "Write the technical design for the login flow"},
            {
                "role": "assistant",
                "content": "I've drafted the technical design for the login flow.",
            },
        ]
        result = mod.is_coding_request(
            "Looks good, now implement it", history=history
        )
        assert result is True

        _, kwargs = aux_mod.call_llm.call_args
        user_content = kwargs["messages"][1]["content"]
        assert "technical design for the login flow" in user_content
        assert "now implement it" in user_content.lower()

    def test_doc_turn_alone_stays_doc(self, ct_with_llm):
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("DOC")

        result = mod.is_coding_request(
            "Write the technical design for the login flow", history=[]
        )
        assert result is False

    def test_short_followup_without_history_still_calls_llm(self, ct_with_llm):
        """Unlike scope_guard's trivial-greeting short-circuit, short
        follow-ups here must still reach the classifier — context (not
        message length) is what disambiguates coding intent."""
        mod, aux_mod = ct_with_llm
        aux_mod.call_llm.return_value = _resp("CODING")

        mod.is_coding_request("go ahead", history=[
            {"role": "assistant", "content": "Want me to implement task 3 now?"},
        ])
        aux_mod.call_llm.assert_called_once()


# ---------------------------------------------------------------------------
# _classifier_model
# ---------------------------------------------------------------------------


class TestClassifierModel:
    def test_anthropic_provider_drops_to_haiku(self, ct):
        assert ct._classifier_model("anthropic", "claude-sonnet-4-6") == "claude-haiku-4-5"

    def test_claude_in_model_name_drops_to_haiku(self, ct):
        assert ct._classifier_model(None, "claude-sonnet-4-6") == "claude-haiku-4-5"

    def test_non_anthropic_keeps_own_model(self, ct):
        assert ct._classifier_model("deepseek", "deepseek-chat") == "deepseek-chat"

    def test_override_env_var_wins(self, ct, monkeypatch):
        monkeypatch.setenv("HERMES_CODING_TRIAGE_MODEL", "custom-model")
        assert ct._classifier_model("anthropic", "claude-sonnet-4-6") == "custom-model"
