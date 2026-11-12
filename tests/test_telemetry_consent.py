"""Exercise every telemetry egress path with a configured, enabled backend.

No live Blender, filesystem identity, worker threads or HTTP requests are used.
"""

import ast
import threading
import types
import sys
from collections import deque
from contextlib import contextmanager, suppress

import pytest

from conftest import ROOT_ADDON
from blender_mcp import consent_prompt, server, telemetry, trajectory
from blender_mcp.telemetry import EventType, TelemetryCollector


DISABLE_VARS = ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY")


@pytest.fixture
def harness(monkeypatch):
    for variable in DISABLE_VARS:
        monkeypatch.delenv(variable, raising=False)
    backend = types.SimpleNamespace(
        enabled=True, max_prompt_length=1000, supabase_url="https://telemetry.invalid",
        supabase_anon_key="test-only", supabase_bucket="test-images", timeout=1.0,
    )
    config = types.ModuleType("blender_mcp.config")
    config.telemetry_config = backend
    monkeypatch.setitem(sys.modules, "blender_mcp.config", config)
    monkeypatch.setattr(TelemetryCollector, "_get_or_create_uuid", lambda self: "test-only-id")
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)

    state = types.SimpleNamespace(reply={"consent": False}, commands=[], posts=[])

    def command(name, params=None):
        state.commands.append((name, params))
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply

    monkeypatch.setattr(server, "get_blender_connection", lambda: types.SimpleNamespace(send_command=command))

    def post(url, **kwargs):
        state.posts.append((url, kwargs))
        return types.SimpleNamespace(raise_for_status=lambda: None)

    # Both modules use this httpx object; capture all event, storage and row POSTs.
    monkeypatch.setattr(telemetry.httpx, "post", post)
    state.collector = TelemetryCollector()
    monkeypatch.setattr(telemetry, "_telemetry_collector", state.collector)
    state.recorder = trajectory.TrajectoryRecorder()
    return state


@pytest.mark.parametrize("reply", [
    {"consent": False}, {}, {"consent": None}, {"consent": "false"},
    {"consent": "true"}, {"consent": 1}, {"consent": True, "error": "unavailable"},
    None, [], RuntimeError("Blender unreachable"),
])
def test_off_or_unknown_blocks_every_collection_and_upload(harness, reply):
    h = harness
    h.reply = reply
    for event_type in EventType:
        h.collector.record_event(event_type, prompt_text="private", error_message="private")
    assert h.collector._queue.empty()
    assert h.collector.upload_screenshot(b"private image", "test") == ""
    assert not h.recorder.record_feedback(feedback="reject", correction_text="private")
    assert not h.recorder.record_observe_step(tool_name="look", modality="image", summary="private")
    assert not h.recorder._enqueue_row("trajectory_steps", {"private": "data"})
    assert not h.recorder._post_row("trajectory_steps", {"private": "data"})
    assert not h.recorder._post_row("trajectory_feedback", {"private": "data"})
    assert not telemetry.is_telemetry_enabled()
    assert h.posts == []


def test_enabled_backend_and_explicit_consent_allow_each_upload(harness):
    h = harness
    h.reply = {"consent": True}
    for event_type in EventType:
        h.collector.record_event(event_type, prompt_text="opted in")
        event = h.collector._queue.get_nowait()
        h.collector._send_event(event)
        h.collector._queue.task_done()
    assert h.collector.upload_screenshot(b"opted-in image", "test").startswith("storage:test-images/")
    assert h.recorder._post_row("trajectory_steps", {"tool_name": "test"})
    assert h.recorder._post_row("trajectory_feedback", {"feedback": "accept"})
    assert len(h.posts) == len(EventType) + 3
    assert [args["json"]["event_type"] for _, args in h.posts[:len(EventType)]] == list(EventType)
    assert telemetry.is_telemetry_enabled()


class _WorkerFinished(Exception):
    pass


def drain_worker_once(monkeypatch, writer):
    """Use the real worker loop and task accounting without an infinite thread."""
    original_get = writer._queue.get
    remaining = writer._queue.qsize()

    def get():
        nonlocal remaining
        if remaining == 0:
            raise _WorkerFinished
        remaining -= 1
        return original_get(block=False)

    with monkeypatch.context() as patch:
        patch.setattr(writer._queue, "get", get)
        with pytest.raises(_WorkerFinished):
            writer._worker_loop()
    assert writer._queue.unfinished_tasks == 0
    assert writer._queue.empty()


@pytest.mark.parametrize("revocation", [False, None, RuntimeError("disconnected")])
def test_queues_discard_previously_consented_data_at_send_time(harness, monkeypatch, revocation):
    h = harness
    h.reply = {"consent": True}
    h.collector.record_event(EventType.STARTUP)
    h.collector.record_event(EventType.TOOL_EXECUTION, prompt_text="queued private data")
    assert h.recorder._enqueue_row("trajectory_steps", {"private": "queued scene data"})
    assert h.recorder.record_feedback(feedback="reject", correction_text="queued feedback")
    assert h.collector.check_user_consent() is True

    # No time elapses: the previous True must not authorize any of these sends.
    h.reply = revocation if isinstance(revocation, Exception) else {"consent": revocation}
    drain_worker_once(monkeypatch, h.collector)
    drain_worker_once(monkeypatch, h.recorder)
    assert h.collector.upload_screenshot(b"queued image", "test") == ""
    assert h.posts == []

    h.reply = {"consent": True}
    assert h.collector._queue.empty() and h.recorder._queue.empty()
    h.collector.record_event(EventType.TOOL_EXECUTION, prompt_text="new opt-in")
    drain_worker_once(monkeypatch, h.collector)
    assert len(h.posts) == 1
    assert h.posts[0][1]["json"]["prompt_text"] == "new opt-in"


@pytest.mark.parametrize("variable", DISABLE_VARS)
def test_environment_override_dominates_live_ui_and_existing_queues(harness, monkeypatch, variable):
    h = harness
    h.reply = {"consent": True}
    h.collector.record_event(EventType.TOOL_EXECUTION)
    assert h.recorder._enqueue_row("trajectory_steps", {"private": "queued"})
    monkeypatch.setenv(variable, "true")
    before = len(h.commands)
    drain_worker_once(monkeypatch, h.collector)
    drain_worker_once(monkeypatch, h.recorder)
    h.collector.record_event(EventType.STARTUP)
    assert h.collector.upload_screenshot(b"image", "test") == ""
    assert not h.recorder.record_feedback(feedback="reject")
    assert not telemetry.is_telemetry_enabled()
    assert len(h.commands) == before  # Hard-disabled paths need no Blender query.
    assert h.posts == []


def test_backend_disable_is_also_rechecked_after_enqueue(harness, monkeypatch):
    h = harness
    h.reply = {"consent": True}
    h.collector.record_event(EventType.TOOL_EXECUTION)
    assert h.recorder._enqueue_row("trajectory_feedback", {"feedback": "accept"})
    h.collector.config.enabled = False
    drain_worker_once(monkeypatch, h.collector)
    drain_worker_once(monkeypatch, h.recorder)
    assert h.collector.upload_screenshot(b"image", "test") == ""
    assert h.posts == []


def test_disable_tool_writes_the_preference_and_reports_all_telemetry_off(harness, monkeypatch):
    h = harness
    h.reply = {"consent": False}
    result = server.disable_telemetry(None)
    assert ("set_telemetry_consent", {"consent": False}) in h.commands
    assert "All telemetry is now OFF" in result
    assert "still apply" not in result
    assert h.posts == []


def test_consent_prompt_applies_explicit_choice_without_a_cached_singleton(harness):
    assert consent_prompt._apply_consent(True)
    assert harness.commands == [("set_telemetry_consent", {"consent": True})]


def test_addon_opt_out_discards_manual_edits_and_skips_operators_performed_while_off():
    tree = ast.parse(ROOT_ADDON.read_text(encoding="utf-8"))
    names = {"UserEditRecorder", "_telemetry_consent_enabled", "_register_edit_capture_handlers",
             "_unregister_edit_capture_handlers", "sync_edit_capture_handlers"}
    body = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name in names]
    prefs = types.SimpleNamespace(telemetry_consent=True)
    operators = ["before opt-in"]
    handlers = types.SimpleNamespace(undo_post=[], redo_post=[], depsgraph_update_post=[])
    bpy = types.SimpleNamespace(
        context=types.SimpleNamespace(
            preferences=types.SimpleNamespace(addons={"test_addon": types.SimpleNamespace(preferences=prefs)}),
            window_manager=types.SimpleNamespace(operators=operators),
        ),
        app=types.SimpleNamespace(handlers=handlers),
        types=types.SimpleNamespace(blendermcp_server=types.SimpleNamespace(running=True)),
    )
    namespace = {"__name__": "test_addon", "bpy": bpy, "deque": deque,
                 "contextmanager": contextmanager, "suppress": suppress,
                 "MAX_EDIT_EVENTS": 100, "EDIT_POLL_MIN_INTERVAL": 0.1,
                 "_blendermcp_undo_post": lambda *a: None,
                 "_blendermcp_redo_post": lambda *a: None,
                 "_blendermcp_depsgraph_post": lambda *a: None}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<addon-consent>", "exec"), namespace)
    recorder = namespace["UserEditRecorder"]()
    namespace["_edit_recorder"] = recorder
    sync = namespace["sync_edit_capture_handlers"]
    assert sync()
    recorder._events.append({"private": "old edit"})
    recorder._resync_operator_baseline()
    prefs.telemetry_consent = False
    assert not sync()
    assert recorder.drain() == []
    assert not handlers.undo_post and not handlers.redo_post and not handlers.depsgraph_update_post
    operators.append("edit performed while off")
    prefs.telemetry_consent = True
    assert sync()
    captured = []
    recorder._record_operator = captured.append
    recorder.poll_operators(now=1.0)
    assert captured == []
    operators.append("new opted-in edit")
    recorder.poll_operators(now=2.0)
    assert captured == ["new opted-in edit"]
