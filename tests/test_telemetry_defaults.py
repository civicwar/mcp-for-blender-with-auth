"""A clean checkout needs no deployment-only telemetry configuration."""

import sys
import threading
import types

import pytest

from blender_mcp.telemetry import EventType, TelemetryCollector


def test_missing_backend_configuration_disables_collection(monkeypatch):
    monkeypatch.setitem(sys.modules, "blender_mcp.config", None)
    for variable in ("DISABLE_TELEMETRY", "BLENDER_MCP_DISABLE_TELEMETRY", "MCP_DISABLE_TELEMETRY"):
        monkeypatch.delenv(variable, raising=False)
    monkeypatch.setattr(TelemetryCollector, "_get_or_create_uuid", lambda self: "test-only-id")
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    collector = TelemetryCollector()
    assert collector.config.enabled is False
    collector.record_event(EventType.TOOL_EXECUTION, tool_name="test", prompt_text="private")
    assert collector._queue.empty()
    assert collector.upload_screenshot(b"test", "test") == ""


def test_existing_backend_configuration_is_preserved(monkeypatch):
    config = types.ModuleType("blender_mcp.config")
    config.telemetry_config = types.SimpleNamespace(enabled=False)
    monkeypatch.setitem(sys.modules, "blender_mcp.config", config)
    monkeypatch.setattr(TelemetryCollector, "_get_or_create_uuid", lambda self: "test-only-id")
    monkeypatch.setattr(threading.Thread, "start", lambda self: None)
    assert TelemetryCollector().config is config.telemetry_config


def test_missing_dependencies_inside_config_are_not_hidden(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def broken_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "config" and level == 1:
            raise ModuleNotFoundError("missing dependency", name="missing_config_dependency")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(ModuleNotFoundError, match="missing dependency"):
        TelemetryCollector()
