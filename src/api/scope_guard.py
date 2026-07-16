"""Input scope guard for the workflow agent.

`shared.md`'s "Scope — stay on-topic" rule is soft system-prompt guidance — the
model is steered by it but not bound. This guard *enforces* it: before the full
agent runs, it classifies the user message and short-circuits a clearly
off-topic one with the canned decline, so the agent is never invoked for
out-of-scope chatter.

Design principle: **fail open.** Any error, empty message, disabled flag, or
uncertain verdict lets the message through to the agent. A false decline of
legitimate workspace work is worse than the occasional off-topic answer the
guard exists to prevent, so the classifier is biased to IN and only blocks a
confident OUT.

G4 — system introspection — is the exception: it uses a deterministic regex
gate (``check_introspection``) that fires *before* the LLM classifier. Introspection
attempts are blocked with certainty; there is no false-positive risk because a
message asking for the agent's own prompt/architecture is never legitimate
workspace work.

Disable entirely with ``HERMES_SCOPE_GUARD=0``.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Optional

from plugins.tools.guardrails import INTROSPECTION_PATTERNS

logger = logging.getLogger(__name__)

_TRIVIAL_IN_SCOPE = re.compile(
    r"^(hi|hey|hello|yo|sup|gm|hiya|howdy|good\s*(morning|afternoon|evening)|"
    r"thanks|thank\s*you|ty|thx|ok|okay|k|kk|yes|yep|yeah|yup|sure|"
    r"go\s*ahead|please\s*do|do\s*it|continue|proceed|sounds\s*good|"
    r"got\s*it|cool|nice|great|perfect|awesome|no|nope)"
    r"[\s!.?,…]*$",
    re.IGNORECASE,
)


def _check_introspection(text: str) -> bool:
    """Return True if *text* matches any G4 system-introspection pattern."""
    for pattern in INTROSPECTION_PATTERNS:
        if pattern.search(text):
            return True
    return False


def check_introspection(message: str) -> bool:
    """G4 public gate: return True when *message* is a system introspection attempt.

    Called by ``is_out_of_scope`` before the LLM classifier.  Also exposed for
    use by other layers (e.g. guardrails pre-dispatch check) that want to reuse
    the same pattern set without a second LLM call.
    """
    text = (message or "").strip()
    if not text:
        return False
    return _check_introspection(text)


def _is_trivially_in_scope(text: str) -> bool:
    # Introspection attempts are never trivially in scope — even very short ones.
    if _check_introspection(text):
        return False
    return len(text) <= 2 or bool(_TRIVIAL_IN_SCOPE.match(text))


# The canned decline — mirrors the example in shared.md's scope section.
SCOPE_DECLINE = (
    "I can only help with this workspace — its repos, features, tasks, and "
    "related software work. What would you like to do on the feature?"
)

_CLASSIFIER_SYSTEM = (
    "You are a scope classifier for a software-delivery workflow assistant. "
    "The assistant only helps with ONE software workspace: its repositories, "
    "features, tasks, product specs, technical designs, handoffs, pull "
    "requests, code, and the feature lifecycle.\n\n"
    "Classify the user's latest message. Reply with EXACTLY one word and "
    "nothing else:\n"
    "  IN  — it is plausibly about the workspace or its software work. This "
    "includes greetings, short confirmations/follow-ups (e.g. 'yes please', "
    "'go ahead'), clarifications, anything ambiguous, and technical research "
    "requests — e.g. reading a library's repo/docs/README, comparing tools, "
    "or looking up an API — where the subject is plausibly relevant to "
    "building, evaluating, or maintaining this workspace's software (even if "
    "the message doesn't name a specific feature or task).\n"
    "  OUT — it is clearly unrelated to software work: general knowledge, "
    "trivia, current events, crypto/finance, math/homework, personal advice, "
    "or off-topic chit-chat.\n\n"
    "When in doubt, answer IN."
)


def _enabled() -> bool:
    return os.environ.get("HERMES_SCOPE_GUARD", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def _classifier_model(provider: Optional[str], model: Optional[str]) -> Optional[str]:
    """Pick the model used for the IN/OUT classification.

    This preflight runs *serialized* in front of every reply, so it must be
    fast: a 1-word classification doesn't need the turn's (possibly heavy,
    thinking-enabled) model. For Anthropic we drop to Haiku — far lower TTFT.
    Override with ``HERMES_SCOPE_GUARD_MODEL``; other providers keep their own
    model so we never send a Claude id to a non-Anthropic endpoint.
    """
    override = os.environ.get("HERMES_SCOPE_GUARD_MODEL", "").strip()
    if override:
        return override
    is_anthropic = (provider or "").strip().lower() == "anthropic" or "claude" in (
        model or ""
    ).lower()
    if is_anthropic:
        return "claude-haiku-4-5"
    return model


def is_out_of_scope(
    message: str,
    *,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> bool:
    """Return True only when *message* is confidently OUT of scope.

    Fails open (returns False) on a disabled flag, empty message, classifier
    error, or any non-OUT / uncertain verdict.
    """
    text = (message or "").strip()
    if not text or not _enabled():
        return False

    if _is_trivially_in_scope(text):
        return False

    # G4: deterministic pre-LLM gate — no classifier call needed for these.
    if check_introspection(text):
        logger.info("scope_guard: system_introspection_blocked: %r", text[:120])
        return True

    try:
        from agent.auxiliary_client import call_llm

        resp = call_llm(
            provider=provider,
            model=_classifier_model(provider, model),
            api_key=api_key,
            base_url=base_url,
            messages=[
                {"role": "system", "content": _CLASSIFIER_SYSTEM},
                {"role": "user", "content": text},
            ],
            temperature=0,
            max_tokens=8,
            timeout=10,
        )
        verdict = (resp.choices[0].message.content or "").strip().upper()
        # Only a confident OUT blocks; empty/garbled verdicts fail open to IN.
        out = verdict.startswith("OUT")
        if out:
            logger.info("scope_guard: classified OUT of scope: %r", text[:120])
        return out
    except Exception as exc:  # noqa: BLE001 — fail open on any classifier error
        logger.warning("scope_guard: classification failed, allowing through: %s", exc)
        return False
