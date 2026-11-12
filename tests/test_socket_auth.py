"""Fail-closed transport authentication, without running Blender or live code."""

import ast
import json
import os
import re
import socket
import threading
import time
import types

import pytest

from conftest import ROOT_ADDON, TEST_AUTH_TOKEN, socket_command
from blender_mcp.auth import AuthenticationError, load_auth_token
from blender_mcp.server import BlenderConnection
from test_server_threading import _load_server_class, _make_server
from test_socket_unicode import _ScriptedSocket


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    monkeypatch.delenv("BLENDER_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("BLENDER_MCP_DISABLE_TELEMETRY", "true")


def addon_token_reader(prefs=None):
    tree = ast.parse(ROOT_ADDON.read_text(encoding="utf-8"))
    body = [node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "_blendermcp_auth_token"]
    namespace = {"os": os, "re": re,
                 "get_blendermcp_addon_preferences": lambda context=None: prefs}
    exec(compile(ast.Module(body=body, type_ignores=[]), "<addon-auth>", "exec"), namespace)
    return namespace["_blendermcp_auth_token"]


@pytest.mark.parametrize("token", [None, "", "short", "a" * 257, "a" * 31,
                                        "secret with spaces" * 3, "é" * 40, "a" * 32 + "\n"])
def test_missing_or_invalid_configuration_fails_before_client_opens_socket(monkeypatch, token):
    if token is not None:
        monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", token)
    opened = []
    monkeypatch.setattr("blender_mcp.server.socket.socket", lambda *a: opened.append(a))
    with pytest.raises(AuthenticationError):
        BlenderConnection("localhost", 9876).connect()
    assert opened == []
    with pytest.raises(ValueError):
        addon_token_reader()()


def test_preference_secret_is_used_only_when_environment_is_unset(monkeypatch):
    read_token = addon_token_reader(types.SimpleNamespace(auth_token=TEST_AUTH_TOKEN))
    assert read_token() == TEST_AUTH_TOKEN
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", "different-test-secret-0123456789-abcdefgh")
    assert read_token() == load_auth_token()
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", "")
    with pytest.raises(ValueError):
        read_token()  # Explicit empty env must not silently fall back.


@pytest.mark.parametrize("token", ["a" * 32, "b" * 256, TEST_AUTH_TOKEN])
def test_valid_token_policy_agrees_on_both_sides(monkeypatch, token):
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", token)
    assert addon_token_reader()() == load_auth_token() == token


def test_server_cannot_bind_without_a_secret(monkeypatch, capsys):
    cls, timers = _load_server_class()
    monkeypatch.setitem(cls.start.__globals__, "_blendermcp_auth_token", addon_token_reader())
    server = cls(port=0)
    server.start()
    assert not server.running
    assert server.socket is None
    assert timers == {}
    assert "Authentication is required" in capsys.readouterr().out


def run_handler(commands):
    cls, _ = _load_server_class()
    server = cls(port=0)
    server.running = True
    server._auth_token = TEST_AUTH_TOKEN
    executed = []
    server.execute_command = lambda command: executed.append(command) or {"status": "success"}
    client = _ScriptedSocket([json.dumps(command).encode() for command in commands])
    server._handle_client(client)
    return server, client, executed


@pytest.mark.parametrize("command_type", ["execute_code", "get_scene_info", "get_addon_info",
                                               "ping", "get_telemetry_consent", "authenticate"])
@pytest.mark.parametrize("token", [None, "", "wrong-token", 42, True, [], {}, "é" * 40])
def test_unauthorized_requests_never_reach_blender(command_type, token, capsys):
    command = {"type": command_type, "params": {"code": "must never run"}}
    if token is not None:
        command["auth_token"] = token
    server, client, executed = run_handler([command])
    assert server.command_queue.empty()
    server._drain_command_queue()
    assert executed == []
    assert json.loads(client.sent[0])["code"] == "AUTHENTICATION_FAILED"
    assert TEST_AUTH_TOKEN not in capsys.readouterr().out


@pytest.mark.parametrize("command", [None, [], "execute_code", 1, True])
def test_non_object_requests_are_rejected(command):
    server, client, _ = run_handler([command])
    assert server.command_queue.empty()
    assert json.loads(client.sent[0])["code"] == "AUTHENTICATION_FAILED"


def test_valid_authentication_never_grants_anonymous_followup_access(capsys):
    server, client, executed = run_handler([
        socket_command("authenticate"), {"type": "execute_code", "params": {"code": "no"}},
        socket_command("execute_code", {"code": "also must not be queued after rejection"}),
    ])
    assert json.loads(client.sent[0])["result"] == {"authenticated": True, "auth_protocol": 1}
    assert json.loads(client.sent[1])["code"] == "AUTHENTICATION_FAILED"
    assert server.command_queue.empty()
    assert executed == []
    assert TEST_AUTH_TOKEN not in capsys.readouterr().out


def test_authorized_commands_are_stripped_before_queue_and_execution(capsys):
    server, client, executed = run_handler([
        socket_command("authenticate"), socket_command("execute_code", {"code": "test fixture"}),
        socket_command("get_scene_info"),
    ])
    server._drain_command_queue()
    assert [command["type"] for command in executed] == ["execute_code", "get_scene_info"]
    assert all("auth_token" not in command for command in executed)
    assert TEST_AUTH_TOKEN not in json.dumps([json.loads(data) for data in client.sent])
    assert TEST_AUTH_TOKEN not in capsys.readouterr().out


def test_unauthenticated_input_is_bounded():
    cls, _ = _load_server_class()
    server = cls(port=0)
    server.running = True
    server._auth_token = TEST_AUTH_TOKEN
    client = _ScriptedSocket([b" " * 8193])
    server._handle_client(client)
    assert server.command_queue.empty()
    assert json.loads(client.sent[0])["code"] == "AUTHENTICATION_FAILED"


def test_large_authorized_script_is_allowed_after_authentication():
    code = "test fixture " * 2000
    server, _client, executed = run_handler([
        socket_command("authenticate"), socket_command("execute_code", {"code": code}),
    ])
    server._drain_command_queue()
    assert executed == [{"type": "execute_code", "params": {"code": code}}]


def test_late_queued_command_from_a_revoked_listener_is_not_executed():
    server, _client, executed = run_handler([socket_command("execute_code", {"code": "never run"})])
    # Models a handler enqueuing just after stop() emptied the queue and a new
    # listener started. A credential check before enqueue alone cannot stop it.
    server._auth_generation = object()
    server._drain_command_queue()
    assert executed == []


def test_idle_unauthenticated_connection_expires(monkeypatch):
    cls, _ = _load_server_class()
    server = cls(port=0)
    server.running = True
    server._auth_token = TEST_AUTH_TOKEN
    # Avoid a five-second sleep: the second clock read has passed the deadline.
    clock_values = iter([0.0, 6.0])
    monkeypatch.setattr(time, "monotonic", lambda: next(clock_values))
    client = _ScriptedSocket([])
    server._handle_client(client)
    assert server.command_queue.empty()
    assert json.loads(client.sent[0])["code"] == "AUTHENTICATION_FAILED"


class PeerSocket(_ScriptedSocket):
    def __init__(self, response):
        super().__init__([json.dumps(response).encode()])
        self.closed = False

    def connect(self, address):
        pass

    def close(self):
        self.closed = True


@pytest.mark.parametrize("response", [
    {"status": "error", "message": "Unknown command type: authenticate"},
    {"status": "success", "result": {}},
    {"status": "success", "result": {"authenticated": True, "auth_protocol": 2}},
    {"status": "success", "result": {"authenticated": True, "auth_protocol": True}},
    {"status": "success", "result": {"authenticated": False, "auth_protocol": 1}},
    {"status": "error", "message": TEST_AUTH_TOKEN},
    [],
])
def test_bridge_rejects_legacy_and_invalid_authentication_without_sending_code(
        monkeypatch, response, caplog):
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", TEST_AUTH_TOKEN)
    peer = PeerSocket(response)
    monkeypatch.setattr("blender_mcp.server.socket.socket", lambda *args: peer)
    connection = BlenderConnection("localhost", 9876)
    with pytest.raises(AuthenticationError):
        connection.send_command("execute_code", {"code": "must not be sent"})
    assert [json.loads(data)["type"] for data in peer.sent] == ["authenticate"]
    assert peer.closed
    assert connection.sock is None
    assert connection._auth_token == ""
    assert TEST_AUTH_TOKEN not in repr(connection)
    assert TEST_AUTH_TOKEN not in caplog.text


def test_bridge_and_addon_interoperate_and_reauthenticate_on_reconnect(monkeypatch, caplog, capsys):
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", TEST_AUTH_TOKEN)
    server = _make_server()
    server.start()
    connection = BlenderConnection("localhost", server.port)
    done = threading.Event()

    def pump():
        while not done.wait(0.005):
            server._drain_command_queue()

    thread = threading.Thread(target=pump, daemon=True)
    thread.start()
    try:
        assert connection.connect()
        assert connection.send_command("execute_code", {"code": "test fixture"}) == {"echo": "execute_code"}
        connection.disconnect()
        assert connection.send_command("get_scene_info") == {"echo": "get_scene_info"}
        connection.disconnect()
        monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", "different-test-secret-0123456789-abcdefgh")
        with pytest.raises(AuthenticationError):
            connection.send_command("execute_code")
        assert connection.sock is None
    finally:
        done.set()
        thread.join(timeout=2)
        connection.disconnect()
        server.stop()
    assert TEST_AUTH_TOKEN not in caplog.text
    assert TEST_AUTH_TOKEN not in capsys.readouterr().out


def test_raw_legacy_socket_cannot_queue_code():
    server = _make_server()
    server.start()
    try:
        with socket.create_connection(("localhost", server.port), timeout=2) as client:
            client.sendall(json.dumps({"type": "execute_code", "params": {"code": "never run"}}).encode())
            reply = json.loads(client.recv(8192))
            assert reply["code"] == "AUTHENTICATION_FAILED"
            assert client.recv(1) == b""
        assert server.command_queue.empty()
    finally:
        server.stop()


def test_stopping_and_rotating_the_secret_revokes_old_clients(monkeypatch):
    cls, _ = _load_server_class()
    server = cls(port=0)
    server.start()
    port = server.socket.getsockname()[1]
    connection = BlenderConnection("localhost", port)
    monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", TEST_AUTH_TOKEN)
    try:
        assert connection.connect()
        server.stop()
        assert server._auth_token == ""
        connection.disconnect()
        new_token = "rotated-test-secret-0123456789-abcdefghijk"
        monkeypatch.setitem(cls.start.__globals__, "_blendermcp_auth_token", lambda: new_token)
        server.port = port
        server.start()
        with pytest.raises(AuthenticationError):
            connection.connect()
        monkeypatch.setenv("BLENDER_MCP_AUTH_TOKEN", new_token)
        assert connection.connect()
    finally:
        connection.disconnect()
        server.stop()
