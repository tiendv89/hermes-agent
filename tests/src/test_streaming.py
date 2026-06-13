"""Tests for src.streaming — OpenAI chat-completions SSE translation.

The translator emits hermes's native /v1/chat/completions streaming format:
    - a leading role chunk (delta.role = "assistant")
    - chat.completion.chunk frames with delta.content for text
    - `event: hermes.tool.progress` frames for tool start/complete
    - `event: hermes.artifact.saved` (workflow-gateway extension) for write tools
    - a finish chunk (finish_reason + usage)
    - the `data: [DONE]` sentinel

Tool callbacks are exercised with hermes's REAL positional signatures
(tool_executor.py): tool_start_callback(call_id, name, args) and
tool_complete_callback(call_id, name, args, result).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STREAMING_DIR = REPO_ROOT / "src" / "streaming"


def _load_streaming():
    """Import src.streaming in isolation."""
    spec = importlib.util.spec_from_file_location(
        "src.streaming",
        STREAMING_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "src.streaming"
    sys.modules["src.streaming"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("src")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("src")]
    for k in keys:
        del sys.modules[k]


def _parse_frame(chunk: str) -> dict:
    """Parse a single SSE frame (data-only or event+data) into a structured dict."""
    event = None
    data = None
    for line in chunk.splitlines():
        if line.startswith("event: "):
            event = line[len("event: "):]
        elif line.startswith("data: "):
            data = line[len("data: "):]
    assert data is not None, f"No data line in frame: {chunk!r}"
    if data == "[DONE]":
        return {"_event": event, "_done": True}
    parsed = json.loads(data)
    parsed["_event"] = event
    return parsed


class TestHermesSSETranslator:
    def _collect(self, streaming, make_events, model="test-model"):
        """Create translator in async context, run make_events, drain stream.

        Returns the list of parsed frames, excluding the terminal [DONE].
        """
        frames = []

        async def _run():
            t = streaming.HermesSSETranslator(model=model)
            make_events(t)
            async for chunk in t.stream():
                frame = _parse_frame(chunk)
                if frame.get("_done"):
                    break
                frames.append(frame)

        asyncio.run(_run())
        return frames

    def test_leads_with_role_chunk(self):
        streaming = _load_streaming()

        def do(t):
            t.done()

        frames = self._collect(streaming, do)
        assert frames[0]["object"] == "chat.completion.chunk"
        assert frames[0]["choices"][0]["delta"] == {"role": "assistant"}
        assert frames[0]["model"] == "test-model"

    def test_on_delta_emits_content_chunk(self):
        streaming = _load_streaming()

        def do(t):
            t.on_delta("hello")
            t.done()

        frames = self._collect(streaming, do)
        content_frames = [f for f in frames if f.get("choices", [{}])[0].get("delta", {}).get("content")]
        assert len(content_frames) == 1
        assert content_frames[0]["choices"][0]["delta"]["content"] == "hello"
        assert content_frames[0]["choices"][0]["finish_reason"] is None

    def test_on_delta_ignores_none_sentinel(self):
        streaming = _load_streaming()

        # hermes fires stream_delta_callback(None) before tool execution as a
        # display-flush sentinel — it must not produce a content chunk.
        def do(t):
            t.on_delta("real text")
            t.on_delta(None)
            t.done()

        frames = self._collect(streaming, do)
        content_frames = [
            f for f in frames if f.get("choices", [{}])[0].get("delta", {}).get("content") is not None
        ]
        assert len(content_frames) == 1
        assert content_frames[0]["choices"][0]["delta"]["content"] == "real text"

    def test_on_tool_start(self):
        streaming = _load_streaming()

        # Real hermes signature: tool_start_callback(call_id, name, args)
        def do(t):
            t.on_tool_start("cid-1", "my_tool", {"feature_id": "x"})
            t.done()

        frames = self._collect(streaming, do)
        tool_frames = [f for f in frames if f.get("_event") == "hermes.tool.progress"]
        assert len(tool_frames) == 1
        assert tool_frames[0]["tool"] == "my_tool"
        assert tool_frames[0]["toolCallId"] == "cid-1"
        assert tool_frames[0]["status"] == "running"

    def test_on_tool_complete(self):
        streaming = _load_streaming()

        # Real hermes signature: tool_complete_callback(call_id, name, args, result)
        def do(t):
            t.on_tool_complete("cid-2", "some_tool", {"q": "x"}, {"result": "ok"})
            t.done()

        frames = self._collect(streaming, do)
        tool_frames = [f for f in frames if f.get("_event") == "hermes.tool.progress"]
        assert len(tool_frames) == 1
        assert tool_frames[0]["tool"] == "some_tool"
        assert tool_frames[0]["toolCallId"] == "cid-2"
        assert tool_frames[0]["status"] == "completed"

    def test_write_tool_ok_emits_artifact_saved(self):
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-3", "workflow_write_product_spec", {"content": "..."}, {"ok": True}
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.tool.progress" in events
        assert "hermes.artifact.saved" in events
        artifact = next(f for f in frames if f.get("_event") == "hermes.artifact.saved")
        assert artifact["artifact"] == "product_spec"

    def test_write_tool_not_ok_no_artifact_saved(self):
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-4",
                "workflow_write_product_spec",
                {"content": "..."},
                {"ok": False, "error": "nope"},
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" not in events

    def test_finish_chunk_carries_usage(self):
        streaming = _load_streaming()

        def do(t):
            t.on_delta("hi")
            t.on_usage(input_tokens=100, output_tokens=50, cached_tokens=10)
            t.done()

        frames = self._collect(streaming, do)
        finish = next(f for f in frames if f.get("choices", [{}])[0].get("finish_reason") == "stop")
        assert finish["usage"]["prompt_tokens"] == 100
        assert finish["usage"]["completion_tokens"] == 50
        assert finish["usage"]["total_tokens"] == 150

    def test_on_error_emits_error_finish(self):
        streaming = _load_streaming()

        def do(t):
            t.on_error("boom")
            t.done()  # idempotent — must not double-finish

        frames = self._collect(streaming, do)
        finishes = [f for f in frames if f.get("choices", [{}])[0].get("finish_reason")]
        assert len(finishes) == 1
        assert finishes[0]["choices"][0]["finish_reason"] == "error"
        assert finishes[0]["hermes"]["error"] == "boom"

    def test_done_is_idempotent(self):
        streaming = _load_streaming()

        def do(t):
            t.done()
            t.done()

        frames = self._collect(streaming, do)
        finishes = [f for f in frames if f.get("choices", [{}])[0].get("finish_reason")]
        assert len(finishes) == 1

    def test_emits_keepalive_while_idle(self):
        streaming = _load_streaming()
        # _KEEPALIVE_SECONDS lives in the sse submodule; stream() reads it as a
        # module global at call time, so patch it there to speed up the test.
        sse_mod = sys.modules["src.streaming.sse"]
        sse_mod._KEEPALIVE_SECONDS = 0.02

        raw_chunks = []

        async def _run():
            t = streaming.HermesSSETranslator(model="m")

            async def _delayed_answer():
                # Simulate a slow tool: no events for a few keepalive intervals.
                await asyncio.sleep(0.1)
                t.on_delta("the answer")
                t.done()

            task = asyncio.ensure_future(_delayed_answer())
            async for chunk in t.stream():
                raw_chunks.append(chunk)
                if chunk.strip() == "data: [DONE]":
                    break
            await task

        asyncio.run(_run())
        # At least one keepalive comment was sent before the answer arrived,
        # and the real answer still came through afterward.
        assert any(c.startswith(": keepalive") for c in raw_chunks)
        assert any('"content": "the answer"' in c for c in raw_chunks)
