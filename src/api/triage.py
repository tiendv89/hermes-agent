"""Per-turn intent triage for the merged workflow gateway.

Currently houses one classifier, ``is_coding_request`` (DOC vs. CODING) —
more triage functions are expected to land here over time as ``/chat``
grows beyond a two-way split, which is why this module isn't named
``coding_triage`` even though today it only does one thing.

The chat surface is a single, persistent, feature-scoped conversation where
topic legitimately drifts turn-to-turn — "write the tech design" one turn,
"now implement it" the next. ``is_coding_request`` classifies each turn so
``agent_dispatch.py`` can route doc/spec/chat turns to the existing Hermes
workflow agent and coding turns to opencode.

Modeled directly on ``src/api/scope_guard.py``'s ``is_out_of_scope``: a
synchronous, cheap classifier call (``temperature=0``, tiny ``max_tokens``,
short timeout) that fails open on any error. Unlike the scope guard — which
only runs on a session's first turn and only needs the latest message — this
classifier runs on *every* turn and is given a short trailing window of
conversation history, since a short follow-up like "go ahead and implement
it" is only decidable in context.

Fail-open direction: any error, empty message, or disabled flag returns
False (stay on the Hermes/doc path) — spinning up a coding delegation is
higher blast radius than the occasional coding request that gets handled as
a chat reply instead, so an uncertain verdict is biased toward the cheaper,
reversible branch.

Disable entirely with ``HERMES_CODING_TRIAGE=0``.
"""

from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# How many trailing history turns give the classifier enough context to
# resolve a short follow-up ("now implement it") without paying for the
# whole conversation on every turn.
_HISTORY_WINDOW = 6

_CLASSIFIER_SYSTEM = (
    "You are an intent classifier for a software-delivery workflow "
    "assistant. Every turn is routed to one of two handlers:\n\n"
    "  DOC    — writing or discussing product specs, technical designs, "
    "task breakdowns, feature status/planning, workspace/general chat, or "
    "any other non-code-editing work.\n"
    "  CODING — the user wants actual source code written, edited, or "
    "run: implementing a task, fixing a bug, writing tests, refactoring, "
    "running a build/test command, or any other hands-on-the-codebase "
    "request.\n\n"
    "Use the conversation so far to resolve short or ambiguous follow-ups "
    "(e.g. 'go ahead and implement it', 'now build that' both mean CODING "
    "if the prior turn was discussing a task to implement). Reply with "
    "EXACTLY one word and nothing else: DOC or CODING. When genuinely "
    "uncertain, answer DOC."
)


def _enabled() -> bool:
    return os.environ.get("HERMES_CODING_TRIAGE", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _classifier_model(provider: str | None, model: str | None) -> str | None:
    """Pick the model used for the DOC/CODING classification.

    Runs serialized in front of every turn, so it must be fast — a 1-word
    classification doesn't need the turn's own (possibly heavy,
    thinking-enabled) model. For Anthropic we drop to Haiku. Override with
    ``HERMES_CODING_TRIAGE_MODEL``; other providers keep their own model so
    we never send a Claude id to a non-Anthropic endpoint.
    """
    override = os.environ.get("HERMES_CODING_TRIAGE_MODEL", "").strip()
    if override:
        return override
    is_anthropic = (provider or "").strip().lower() == "anthropic" or "claude" in (
        model or ""
    ).lower()
    if is_anthropic:
        return "claude-haiku-4-5"
    return model


def _format_history(history: list[dict[str, Any]] | None) -> str:
    """Render a short trailing window of conversation as classifier context."""
    if not history:
        return "(no prior turns)"
    window = [h for h in history[-_HISTORY_WINDOW:] if h.get("role") in ("user", "assistant")]
    if not window:
        return "(no prior turns)"
    lines = []
    for turn in window:
        role = "User" if turn.get("role") == "user" else "Assistant"
        content = (turn.get("content") or "").strip()
        if not content:
            continue
        if len(content) > 500:
            content = content[:500] + "…"
        lines.append(f"{role}: {content}")
    return "\n".join(lines) if lines else "(no prior turns)"


def is_coding_request(
    message: str,
    *,
    history: list[dict[str, Any]] | None = None,
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    base_url: str | None = None,
) -> bool:
    """Return True only when *message* is confidently a coding request.

    Fails open (returns False — stay on the Hermes/doc path) on a disabled
    flag, empty message, classifier error, or any non-CODING/uncertain
    verdict.
    """
    text = (message or "").strip()
    if not text or not _enabled():
        return False

    try:
        from agent.auxiliary_client import call_llm

        context = _format_history(history)
        resp = call_llm(
            provider=provider,
            model=_classifier_model(provider, model),
            api_key=api_key,
            base_url=base_url,
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {
                    "role": "user",
                    "content": (
                        f"Conversation so far:\n{context}\n\n"
                        f"Latest message: {text}"
                    ),
                },
            ],
            temperature=0,
            max_tokens=8,
            timeout=10,
        )
        verdict = (resp.choices[0].message.content or "").strip().upper()
        coding = verdict.startswith("CODING")
        if coding:
            logger.info("triage: classified CODING: %r", text[:120])
        return coding
    except Exception as exc:
        logger.warning("triage: classification failed, staying on doc path: %s", exc)
        return False
