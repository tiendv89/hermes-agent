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
                "cid-3", "write_product_spec", {"content": "..."}, {"ok": True}
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
                "write_product_spec",
                {"content": "..."},
                {"ok": False, "error": "nope"},
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" not in events

    # -- write_file / edit_file artifact-saved tests (T4: m3-agent-edit-file) -----

    def test_write_file_ok_emits_artifact_saved(self):
        """write_file with ok=True emits hermes.artifact.saved with artifact=generic_file."""
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-wf1", "write_file", {"path": "notes.md", "content": "..."}, {"ok": True, "path": "notes.md", "version_id": "v1"}
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" in events
        artifact = next(f for f in frames if f.get("_event") == "hermes.artifact.saved")
        assert artifact["artifact"] == "generic_file"

    def test_write_file_ok_json_string_output_emits_artifact_saved(self):
        """write_file with JSON-string result (as emitted by plugin wrapper) emits artifact saved."""
        import json
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-wf2", "write_file", {"path": "notes.md"},
                json.dumps({"ok": True, "path": "notes.md", "version_id": "v1"}),
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" in events
        artifact = next(f for f in frames if f.get("_event") == "hermes.artifact.saved")
        assert artifact["artifact"] == "generic_file"

    def test_write_file_not_ok_no_artifact_saved(self):
        """write_file with ok=False must NOT emit hermes.artifact.saved."""
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-wf3", "write_file", {"path": "notes.md"},
                {"ok": False, "error": "unsupported_owner"},
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" not in events

    def test_edit_file_ok_emits_artifact_saved(self):
        """edit_file with ok=True emits hermes.artifact.saved with artifact=generic_file."""
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-ef1", "edit_file",
                {"path": "notes.md", "edits": [{"old_string": "a", "new_string": "b"}]},
                {"ok": True, "path": "notes.md", "version_id": "v2"},
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" in events
        artifact = next(f for f in frames if f.get("_event") == "hermes.artifact.saved")
        assert artifact["artifact"] == "generic_file"

    def test_edit_file_ok_json_string_output_emits_artifact_saved(self):
        """edit_file with JSON-string result (as emitted by plugin wrapper) emits artifact saved."""
        import json
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-ef2", "edit_file", {"path": "notes.md"},
                json.dumps({"ok": True, "path": "notes.md", "version_id": "v2"}),
            )
            t.done()

        frames = self._collect(streaming, do)
        events = [f.get("_event") for f in frames]
        assert "hermes.artifact.saved" in events
        artifact = next(f for f in frames if f.get("_event") == "hermes.artifact.saved")
        assert artifact["artifact"] == "generic_file"

    def test_edit_file_not_ok_no_artifact_saved(self):
        """edit_file with ok=False must NOT emit hermes.artifact.saved."""
        streaming = _load_streaming()

        def do(t):
            t.on_tool_complete(
                "cid-ef3", "edit_file", {"path": "notes.md"},
                {"ok": False, "error": "unsupported_owner"},
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

    # -- agent.reasoning tests (T1: m3-agent-chat-thinking) -----------------

    def test_on_reasoning_emits_reasoning_delta_frame(self):
        """on_reasoning emits a well-formed agent.reasoning / reasoning.delta frame."""
        streaming = _load_streaming()

        def do(t):
            t.on_reasoning("I should think about this carefully.")
            t.done()

        frames = self._collect(streaming, do)
        reasoning_frames = [f for f in frames if f.get("_event") == "agent.reasoning"]
        assert len(reasoning_frames) >= 1
        delta_frames = [f for f in reasoning_frames if f.get("object") == "reasoning.delta"]
        assert len(delta_frames) == 1
        assert delta_frames[0]["content"] == "I should think about this carefully."

    def test_on_reasoning_ignores_falsy_delta(self):
        """Falsy reasoning deltas (None, empty string) produce no frames."""
        streaming = _load_streaming()

        def do(t):
            t.on_reasoning(None)
            t.on_reasoning("")
            t.on_delta("answer")
            t.done()

        frames = self._collect(streaming, do)
        reasoning_frames = [f for f in frames if f.get("_event") == "agent.reasoning"]
        assert reasoning_frames == []

    def test_reasoning_accumulated_separately_from_content(self):
        """Reasoning deltas must not appear in assistant content chunks."""
        streaming = _load_streaming()

        def do(t):
            t.on_reasoning("thinking...")
            t.on_delta("the answer")
            t.done()

        frames = self._collect(streaming, do)
        # Content chunks must contain only the delta text, not any reasoning text.
        content_frames = [
            f for f in frames
            if f.get("object") == "chat.completion.chunk"
            and f.get("choices", [{}])[0].get("delta", {}).get("content")
        ]
        assert len(content_frames) == 1
        assert content_frames[0]["choices"][0]["delta"]["content"] == "the answer"
        # Reasoning frames are emitted with the distinct event type.
        reasoning_frames = [f for f in frames if f.get("_event") == "agent.reasoning"]
        assert len(reasoning_frames) >= 1

    def test_no_reasoning_turn_emits_no_reasoning_frames(self):
        """A turn with no reasoning callback fires produces zero agent.reasoning frames."""
        streaming = _load_streaming()

        def do(t):
            t.on_delta("plain answer")
            t.done()

        frames = self._collect(streaming, do)
        reasoning_frames = [f for f in frames if f.get("_event") == "agent.reasoning"]
        assert reasoning_frames == []

    def test_reasoning_done_frame_emitted_on_turn_end(self):
        """When reasoning was streamed, a reasoning.done frame is emitted at turn end."""
        streaming = _load_streaming()

        def do(t):
            t.on_reasoning("step one")
            t.on_reasoning("step two")
            t.done()

        frames = self._collect(streaming, do)
        done_frames = [
            f for f in frames
            if f.get("_event") == "agent.reasoning" and f.get("object") == "reasoning.done"
        ]
        assert len(done_frames) == 1

    def test_no_reasoning_done_frame_when_no_reasoning(self):
        """No reasoning.done frame when no reasoning was emitted."""
        streaming = _load_streaming()

        def do(t):
            t.on_delta("answer only")
            t.done()

        frames = self._collect(streaming, do)
        done_frames = [
            f for f in frames
            if f.get("_event") == "agent.reasoning" and f.get("object") == "reasoning.done"
        ]
        assert done_frames == []

    def test_reasoning_interleaved_with_content_stays_separate(self):
        """Multi-burst reasoning interleaved with content is accumulated separately."""
        streaming = _load_streaming()

        def do(t):
            t.on_reasoning("burst one")
            t.on_delta("answer part 1 ")
            t.on_reasoning("burst two")
            t.on_delta("answer part 2")
            t.done()

        frames = self._collect(streaming, do)
        reasoning_frames = [f for f in frames if f.get("_event") == "agent.reasoning"
                            and f.get("object") == "reasoning.delta"]
        content_frames = [
            f for f in frames
            if f.get("object") == "chat.completion.chunk"
            and f.get("choices", [{}])[0].get("delta", {}).get("content")
        ]
        assert len(reasoning_frames) == 2
        assert {rf["content"] for rf in reasoning_frames} == {"burst one", "burst two"}
        assert len(content_frames) == 2

    def test_on_reasoning_does_not_call_append_message(self):
        """on_reasoning must not invoke append_message — reasoning is ephemeral."""
        import types
        from unittest.mock import MagicMock

        streaming = _load_streaming()

        # Inject a stub for src.db.store so that if on_reasoning ever tries to
        # persist reasoning deltas, the mock records the call and fails the assertion.
        mock_append = MagicMock()
        db_stub = types.ModuleType("src.db.store")
        db_stub.append_message = mock_append  # type: ignore[attr-defined]
        sys.modules["src.db.store"] = db_stub

        def do(t):
            t.on_reasoning("should not persist this")
            t.done()

        self._collect(streaming, do)
        mock_append.assert_not_called()

    def test_content_stream_parity_with_reasoning(self):
        """Assistant content frames are byte-identical whether or not reasoning is emitted."""
        streaming = _load_streaming()

        def _content_frames(frames):
            return [
                f for f in frames
                if f.get("object") == "chat.completion.chunk"
            ]

        def do_with_reasoning(t):
            t.on_reasoning("some thinking")
            t.on_delta("Hello")
            t.on_delta(", world")
            t.done()

        def do_without_reasoning(t):
            t.on_delta("Hello")
            t.on_delta(", world")
            t.done()

        frames_with = self._collect(streaming, do_with_reasoning)
        # Re-load to get a fresh module state (autouse fixture handles module cleanup
        # between tests, but we need two runs within a single test here).
        for k in list(sys.modules.keys()):
            if k.startswith("src"):
                del sys.modules[k]
        streaming2 = _load_streaming()
        frames_without = self._collect(streaming2, do_without_reasoning)

        content_with = _content_frames(frames_with)
        content_without = _content_frames(frames_without)

        # Strip the finish frame (it carries usage which is the same anyway)
        # and compare content deltas.
        def _deltas(fs):
            return [f["choices"][0]["delta"].get("content") for f in fs
                    if f["choices"][0]["delta"].get("content")]

        assert _deltas(content_with) == _deltas(content_without)


def _load_bus_translator():
    """Import src.streaming.bus_translator with minimal stubs for the bus dependency."""
    import types

    # Stub the bus module so BusPublishingSSETranslator can be imported
    # without a running event loop or Postgres.
    bus_stub = types.ModuleType("src.realtime.bus")
    realtime_stub = types.ModuleType("src.realtime")
    realtime_stub.bus = bus_stub  # type: ignore[attr-defined]
    sys.modules.setdefault("src.realtime", realtime_stub)
    sys.modules["src.realtime.bus"] = bus_stub

    class _FakeBus:
        def __init__(self):
            self.events = []

        def publish(self, session_id, event):
            self.events.append((session_id, event))

    fake_bus = _FakeBus()
    bus_stub.get_bus = lambda: fake_bus  # type: ignore[attr-defined]

    # Load the SSE base first (already done by _load_streaming in the fixture).
    _load_streaming()

    bus_dir = REPO_ROOT / "src" / "streaming"
    spec = importlib.util.spec_from_file_location(
        "src.streaming.bus_translator",
        bus_dir / "bus_translator.py",
    )
    mod = importlib.util.module_from_spec(spec)
    mod.__package__ = "src.streaming"
    sys.modules["src.streaming.bus_translator"] = mod
    spec.loader.exec_module(mod)
    return mod, fake_bus


class TestBusPublishingSSETranslatorReasoning:
    """Tests for BusPublishingSSETranslator.on_reasoning bus fan-out."""

    def test_on_reasoning_publishes_to_bus(self):
        """on_reasoning fan-out publishes agent.reasoning to the bus."""
        mod, fake_bus = _load_bus_translator()

        async def _run():
            t = mod.BusPublishingSSETranslator(session_id="sess-1")
            t.on_reasoning("thinking out loud")
            t.done()
            async for _ in t.stream():
                pass

        asyncio.run(_run())

        bus_reasoning = [
            e for (_, e) in fake_bus.events
            if e.get("event") == "agent.reasoning"
        ]
        assert len(bus_reasoning) == 1
        assert bus_reasoning[0]["data"] == {"content": "thinking out loud"}

    def test_on_reasoning_also_emits_sse_frame(self):
        """on_reasoning emits both the SSE frame AND the bus publish."""
        mod, fake_bus = _load_bus_translator()
        sse_frames = []

        async def _run():
            t = mod.BusPublishingSSETranslator(session_id="sess-2")
            t.on_reasoning("step A")
            t.done()
            async for chunk in t.stream():
                if chunk.strip().startswith("data: [DONE]"):
                    break
                if "agent.reasoning" in chunk:
                    sse_frames.append(chunk)

        asyncio.run(_run())

        # SSE frame emitted by the base class method.
        assert len(sse_frames) >= 1
        # Bus publish emitted by the override.
        bus_events = [e for (_, e) in fake_bus.events if e.get("event") == "agent.reasoning"]
        assert len(bus_events) == 1

    def test_on_reasoning_falsy_delta_no_bus_publish(self):
        """Falsy reasoning delta is not published to the bus."""
        mod, fake_bus = _load_bus_translator()

        async def _run():
            t = mod.BusPublishingSSETranslator(session_id="sess-3")
            t.on_reasoning(None)
            t.on_reasoning("")
            t.done()
            async for _ in t.stream():
                pass

        asyncio.run(_run())

        bus_reasoning = [
            e for (_, e) in fake_bus.events
            if e.get("event") == "agent.reasoning"
        ]
        assert bus_reasoning == []
