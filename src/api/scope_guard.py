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

Disable entirely with ``HERMES_SCOPE_GUARD=0``.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

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
    "'go ahead'), clarifications, and anything ambiguous.\n"
    "  OUT — it is clearly unrelated to the workspace: general knowledge, "
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
    try:
        from agent.auxiliary_client import call_llm

        resp = call_llm(
            provider=provider,
            model=model,
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
