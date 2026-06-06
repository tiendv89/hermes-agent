"""Tests for workflow_gateway.streaming — SSE event translation.

Covers:
    - _sse(): correct SSE wire format
    - on_delta: emits message_output_partial
    - on_tool_start: emits tool_call_item with status=running
    - on_tool_complete: emits function_call_output; no artifact_saved for non-write tool
    - on_tool_complete write tool: emits artifact_saved when ok=True
    - on_tool_complete write tool ok=False: no artifact_saved
    - on_usage: emits usage event
    - on_error: emits error event
    - done(): emits [DONE] sentinel and terminates the async generator
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
STREAMING_DIR = REPO_ROOT / "workflow_gateway" / "streaming"


def _load_streaming():
    """Import workflow_gateway.streaming in isolation."""
    spec = importlib.util.spec_from_file_location(
        "workflow_gateway.streaming",
        STREAMING_DIR / "__init__.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "workflow_gateway.streaming"
    sys.modules["workflow_gateway.streaming"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(autouse=True)
def _clean_modules():
    keys = [k for k in sys.modules if k.startswith("workflow_gateway")]
    for k in keys:
        del sys.modules[k]
    yield
    keys = [k for k in sys.modules if k.startswith("workflow_gateway")]
    for k in keys:
        del sys.modules[k]


def _parse_sse(chunk: str) -> dict:
    """Parse a single SSE data line into a dict."""
    assert chunk.startswith("data: "), f"Not an SSE line: {chunk!r}"
    payload = chunk[len("data: "):].strip()
    if payload == "[DONE]":
        return {"type": "[DONE]"}
    return json.loads(payload)


class TestSseHelper:
    def test_format(self):
        streaming = _load_streaming()
        result = streaming._sse("foo", {"bar": 1})
        assert result.startswith("data: ")
        assert result.endswith("\n\n")
        data = json.loads(result[len("data: "):].strip())
        assert data == {"type": "foo", "bar": 1}


class TestHermesSSETranslator:
    def _collect_sync(self, translator, action_fn):
        """Run action_fn then drain the translator queue synchronously."""
        chunks = []

        async def _run():
            action_fn()
            async for chunk in translator.stream():
                if chunk.startswith("data: [DONE]"):
                    break
                chunks.append(chunk)

        asyncio.run(_run())
        return chunks

    def test_on_delta(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_delta("hello")
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 1
        data = _parse_sse(chunks[0])
        assert data["type"] == "message_output_partial"
        assert data["content"] == "hello"

    def test_on_tool_start(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_tool_start("my_tool", call_id="cid-1")
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 1
        data = _parse_sse(chunks[0])
        assert data["type"] == "tool_call_item"
        assert data["name"] == "my_tool"
        assert data["status"] == "running"
        assert data["call_id"] == "cid-1"

    def test_on_tool_complete_no_artifact(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_tool_complete("some_tool", call_id="cid-2", output={"result": "ok"})
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 1
        data = _parse_sse(chunks[0])
        assert data["type"] == "function_call_output"
        assert data["name"] == "some_tool"

    def test_on_tool_complete_write_tool_ok_emits_artifact_saved(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_tool_complete(
                "workflow_write_product_spec",
                call_id="cid-3",
                output={"ok": True},
            )
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 2
        types_seen = [_parse_sse(c)["type"] for c in chunks]
        assert "function_call_output" in types_seen
        assert "artifact_saved" in types_seen
        artifact_chunk = next(c for c in chunks if _parse_sse(c)["type"] == "artifact_saved")
        assert _parse_sse(artifact_chunk)["artifact"] == "product_spec"

    def test_on_tool_complete_write_tool_not_ok_no_artifact_saved(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_tool_complete(
                "workflow_write_product_spec",
                call_id="cid-4",
                output={"ok": False, "error": "not implemented"},
            )
            t.done()

        chunks = self._collect_sync(t, do)
        types_seen = [_parse_sse(c)["type"] for c in chunks]
        assert "artifact_saved" not in types_seen

    def test_on_usage(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_usage(input_tokens=100, output_tokens=50, cached_tokens=10)
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 1
        data = _parse_sse(chunks[0])
        assert data["type"] == "usage"
        assert data["input"] == 100
        assert data["output"] == 50
        assert data["cached"] == 10

    def test_on_error(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()

        def do():
            t.on_error("something went wrong")
            t.done()

        chunks = self._collect_sync(t, do)
        assert len(chunks) == 1
        data = _parse_sse(chunks[0])
        assert data["type"] == "error"
        assert "something went wrong" in data["message"]

    def test_done_terminates_stream(self):
        streaming = _load_streaming()
        t = streaming.HermesSSETranslator()
        collected = []

        async def _run():
            t.on_delta("first")
            t.done()
            async for chunk in t.stream():
                if "DONE" in chunk:
                    break
                collected.append(chunk)

        asyncio.run(_run())
        assert len(collected) == 1
