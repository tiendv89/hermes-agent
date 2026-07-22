"""GatewaySessionDB — wraps hermes SessionDB and mirrors writes to Postgres.

The hermes agent calls session_db.append_message() and
session_db.update_token_counts() internally after every turn. By subclassing
SessionDB we intercept those calls and fire async saves to the gateway's
Postgres store without touching run_agent.py.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


def _to_json(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value)
    except Exception:
        return str(value)


def make_gateway_session_db(
    loop: asyncio.AbstractEventLoop,
    db_factory: Callable,
    gateway_session_id: str,
    author_id: str | None = None,
    skip_user_persist: bool = False,
    is_cancelled: Callable[[], bool] | None = None,
    reply_to_message_id: int | None = None,
    thread_root_id: int | None = None,
):
    """Return a SessionDB subclass that mirrors writes for gateway_session_id to Postgres.

    author_id: if set, attached to user-role messages mirrored to Postgres.
    skip_user_persist: if True, skip mirroring user-role messages (used when the
        send service has already persisted the human message with author_id).
    is_cancelled: optional predicate; once it returns True the turn has been
        cancelled, so mirror writes are suppressed. The agent loop may not unwind
        instantly after interrupt(), and we must not persist messages produced
        after the user pressed Stop.
    reply_to_message_id / thread_root_id: when the triggering user message was a
        thread reply, every row mirrored for this turn (assistant text, tool
        calls, etc.) is tagged with the same thread context so the agent's
        response lands inside the thread instead of the main channel.
    """
    from hermes_state import SessionDB

    from src.db.store import (
        append_message as pg_append,
    )
    from src.db.store import (
        end_session as pg_end_session,
    )
    from src.db.store import (
        set_session_archived as pg_set_archived,
    )
    from src.db.store import (
        set_session_title as pg_set_title,
    )
    from src.db.store import (
        update_session_cwd as pg_update_cwd,
    )
    from src.db.store import (
        update_session_meta as pg_update_meta,
    )
    from src.db.store import (
        update_session_model as pg_update_model,
    )
    from src.db.store import (
        update_system_prompt as pg_update_system_prompt,
    )
    from src.db.store import (
        update_token_counts as pg_update_tokens,
    )

    class GatewaySessionDB(SessionDB):
        def append_message(
            self,
            session_id: str,
            role: str,
            content: str | None = None,
            tool_name: str | None = None,
            tool_calls: Any = None,
            tool_call_id: str | None = None,
            token_count: int | None = None,
            finish_reason: str | None = None,
            reasoning: str | None = None,
            reasoning_content: str | None = None,
            reasoning_details: Any = None,
            codex_reasoning_items: Any = None,
            codex_message_items: Any = None,
            platform_message_id: str | None = None,
            observed: bool = False,
            **kwargs: Any,
        ) -> int:
            result = super().append_message(
                session_id,
                role,
                content=content,
                tool_name=tool_name,
                tool_calls=tool_calls,
                tool_call_id=tool_call_id,
                token_count=token_count,
                finish_reason=finish_reason,
                reasoning=reasoning,
                reasoning_content=reasoning_content,
                reasoning_details=reasoning_details,
                codex_reasoning_items=codex_reasoning_items,
                codex_message_items=codex_message_items,
                platform_message_id=platform_message_id,
                observed=observed,
                **kwargs,
            )

            if session_id != gateway_session_id:
                return result

            # Turn cancelled — do not persist anything the agent produces after
            # the user pressed Stop. The partial reply (if any) is saved once by
            # the cancel handler with finish_reason='stopped'.
            if is_cancelled is not None and is_cancelled():
                return result

            # Skip mirroring user messages when the send service pre-persisted them.
            if role == "user" and skip_user_persist:
                return result

            _author = author_id if role == "user" else None

            async def _save() -> None:
                async with db_factory() as db:
                    await pg_append(
                        db,
                        gateway_session_id,
                        role=role,
                        content=content,
                        tool_name=tool_name,
                        tool_calls=_to_json(tool_calls),
                        tool_call_id=tool_call_id,
                        token_count=token_count,
                        finish_reason=finish_reason,
                        reasoning=reasoning,
                        reasoning_content=reasoning_content,
                        reasoning_details=_to_json(reasoning_details),
                        codex_reasoning_items=_to_json(codex_reasoning_items),
                        codex_message_items=_to_json(codex_message_items),
                        platform_message_id=platform_message_id,
                        observed=observed,
                        author_id=_author,
                        reply_to_message_id=reply_to_message_id,
                        thread_root_id=thread_root_id,
                        image_ids=kwargs.get("image_ids"),
                        file_ids=kwargs.get("file_ids"),
                    )

            # Message persistence must be reliable — fire-and-forget would
            # silently drop the row on a transient error or a busy loop (the
            # "agent message sometimes not stored" symptom). append_message is
            # called on the agent's worker thread, so we block until the mirror
            # completes (bounded), surfacing failures instead of losing them.
            future = asyncio.run_coroutine_threadsafe(_save(), loop)
            try:
                future.result(timeout=15)
            except Exception:
                logger.exception(
                    "GatewaySessionDB: failed to mirror append_message to Postgres"
                )
            return result

        def update_token_counts(
            self,
            session_id: str,
            input_tokens: int = 0,
            output_tokens: int = 0,
            model: str | None = None,
            cache_read_tokens: int = 0,
            cache_write_tokens: int = 0,
            reasoning_tokens: int = 0,
            estimated_cost_usd: float | None = None,
            actual_cost_usd: float | None = None,
            cost_status: str | None = None,
            cost_source: str | None = None,
            pricing_version: str | None = None,
            billing_provider: str | None = None,
            billing_base_url: str | None = None,
            billing_mode: str | None = None,
            api_call_count: int = 0,
            absolute: bool = False,
            **kwargs: Any,
        ) -> None:
            super().update_token_counts(
                session_id,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                model=model,
                cache_read_tokens=cache_read_tokens,
                cache_write_tokens=cache_write_tokens,
                reasoning_tokens=reasoning_tokens,
                estimated_cost_usd=estimated_cost_usd,
                actual_cost_usd=actual_cost_usd,
                cost_status=cost_status,
                cost_source=cost_source,
                pricing_version=pricing_version,
                billing_provider=billing_provider,
                billing_base_url=billing_base_url,
                billing_mode=billing_mode,
                api_call_count=api_call_count,
                absolute=absolute,
                **kwargs,
            )

            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_update_tokens(
                            db,
                            gateway_session_id,
                            input_tokens=input_tokens,
                            output_tokens=output_tokens,
                            cache_read_tokens=cache_read_tokens,
                            cache_write_tokens=cache_write_tokens,
                            reasoning_tokens=reasoning_tokens,
                            api_call_count=api_call_count,
                            estimated_cost_usd=estimated_cost_usd,
                            actual_cost_usd=actual_cost_usd,
                            cost_status=cost_status,
                            cost_source=cost_source,
                            pricing_version=pricing_version,
                            billing_provider=billing_provider,
                            billing_base_url=billing_base_url,
                            billing_mode=billing_mode,
                            model=model,
                        )
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror update_token_counts to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def end_session(self, session_id: str, end_reason: str) -> None:
            super().end_session(session_id, end_reason)
            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_end_session(db, gateway_session_id, end_reason)
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror end_session to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def update_session_cwd(self, session_id: str, cwd: str) -> None:
            super().update_session_cwd(session_id, cwd)
            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_update_cwd(db, gateway_session_id, cwd)
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror update_session_cwd to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def update_session_meta(
            self,
            session_id: str,
            model_config_json: str,
            model: str | None = None,
        ) -> None:
            super().update_session_meta(session_id, model_config_json, model=model)
            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_update_meta(
                            db, gateway_session_id, model_config_json, model=model
                        )
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror update_session_meta to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def update_system_prompt(self, session_id: str, system_prompt: str) -> None:
            super().update_system_prompt(session_id, system_prompt)
            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_update_system_prompt(
                            db, gateway_session_id, system_prompt
                        )
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror update_system_prompt to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def update_session_model(self, session_id: str, model: str) -> None:
            super().update_session_model(session_id, model)
            if session_id != gateway_session_id:
                return

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_update_model(db, gateway_session_id, model)
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror update_session_model to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)

        def set_session_title(self, session_id: str, title: str) -> bool:
            result = super().set_session_title(session_id, title)
            if session_id != gateway_session_id:
                return result

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_set_title(db, gateway_session_id, title)
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror set_session_title to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)
            return result

        def set_session_archived(self, session_id: str, archived: bool) -> bool:
            result = super().set_session_archived(session_id, archived)
            if session_id != gateway_session_id:
                return result

            async def _save() -> None:
                try:
                    async with db_factory() as db:
                        await pg_set_archived(db, gateway_session_id, archived)
                except Exception:
                    logger.exception(
                        "GatewaySessionDB: failed to mirror set_session_archived to Postgres"
                    )

            asyncio.run_coroutine_threadsafe(_save(), loop)
            return result

    return GatewaySessionDB()
