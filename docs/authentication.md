# Authenticated Blender socket

This fork requires a shared secret for **every** socket command. There is no
anonymous mode or default credential. Install the matching bridge and add-on from
this checkout together; the public `mcp-for-blender` release does not provide this
feature. Fork package `2.2.7+auth.1` and add-on 1.9 / protocol 16 use
authentication protocol 1.

The upstream telemetry backend configuration is not tracked in the repository.
When it is absent, this fork leaves telemetry disabled and still serves modeling
tools. No backend URL or deployment credential is needed for a local checkout.
Even with a backend configured, **Allow Telemetry** off prevents all telemetry
uploads, including usage counts and queued trajectory/feedback records processed
while off. See [telemetry controls](../README.md#telemetry-control) for consent,
environment overrides and in-progress upload behavior.

## Local setup and migration

An agent can prepare code, run tests and explain configuration using its existing
permissions. The developer saves their work, operates Blender's UI, installs the
add-on and edits personal agent configuration. This recipe does not authorize an
agent to restart Blender or replace a working installation automatically.

1. In this checkout, prepare the local package with `uv sync --frozen`. This creates
   a checkout-specific `.venv`; it does not replace an existing MCP installation.
   For tests, install pytest into that environment:

   ```powershell
   uv pip install --python .venv/Scripts/python.exe pytest
   $env:BLENDER_MCP_DISABLE_TELEMETRY = 'true'
   ./.venv/Scripts/python.exe -m pytest -q
   ```

   On macOS/Linux the executables are under `.venv/bin/` instead of `.venv/Scripts/`.

2. Generate a different random secret for each developer/Blender trust boundary:

   ```powershell
   ./.venv/Scripts/python.exe -c "import secrets; print(secrets.token_urlsafe(32))"
   ```

   Keep this value private and outside Git, tool arguments, prompts and screenshots.
   The accepted format is 32-256 printable ASCII characters without spaces. Length
   validation does not make a predictable password strong; use the random generator.

3. Save the current scene and back up the existing add-on and Blender preferences.
   Stop the old listener through Blender before replacing the add-on. Install the
   bundled fork add-on into the intended Blender version, using the executable from
   this checkout. For example, on Windows with Blender 5.2:

   ```powershell
   $addonsDir = Join-Path $env:APPDATA 'Blender Foundation/Blender/5.2/scripts/addons'
   ./.venv/Scripts/mcp-for-blender.exe install-addon --addons-dir $addonsDir
   ```

   Do not use a website installer, floating `uvx` command or broad `setup` command
   for this migration. Those may install upstream code or edit other client entries.

4. In Blender, reload the add-on when ready, then open **Edit > Preferences >
   Add-ons > mcp-for-blender-with-auth**. Paste the secret into **Auth Token** and save
   Preferences. Keep **Bind Address** at `127.0.0.1` and **Allow Remote Connections**
   off. Start the listener manually after setting the secret. Auto-start safely
   skips startup when no valid secret is configured.

   Alternatively, set `BLENDER_MCP_AUTH_TOKEN` in **Blender's launch environment**
   before starting Blender. An environment value overrides the preference, including
   an invalid/empty value, which prevents startup. Credentials are never read from
   the loaded scene, so opening a `.blend` cannot change the authentication secret.

5. Update only the intended personal MCP entry. Its command must point to this
   checkout's executable, and its environment must contain the same secret. A Codex
   example follows; replace the executable path, port and placeholder, preserving
   other personal settings:

   ```toml
   [mcp_servers.blender]
   command = "D:/Workspace/OS_Contributions/mcp-for-blender-with-auth/.venv/Scripts/mcp-for-blender.exe"

   [mcp_servers.blender.env]
   BLENDER_HOST = "127.0.0.1"
   BLENDER_PORT = "39876"
   BLENDER_MCP_AUTH_TOKEN = "<paste-generated-secret>"
   BLENDER_MCP_DISABLE_TELEMETRY = "true"
   BLENDER_MCP_SAFE_MODE = "1"
   ```

   Other clients use the same environment variables in their server entry. There
   is intentionally no `--auth-token` CLI argument or MCP tool parameter: those
   would expose credentials through process listings or model context.

6. Reconnect the MCP client. Ask for `get_addon_status`, then `get_scene_info` to
   verify the connection without editing the scene. The bridge performs its own
   authentication probe before any tool command. A legacy add-on or wrong/missing
   secret fails closed; it never falls back to unauthenticated execution.

The fork's add-on update check targets this fork, and rejects update source without
the matching authentication-protocol marker. That marker is a compatibility check,
not a cryptographic signature or proof that arbitrary downloaded code is safe.
Keep upgrades deliberate. To disable the independent startup update check, set
`BLENDERMCP_NO_UPDATE_CHECK=1` in **Blender's** launch environment. Telemetry settings
do not disable that check.

## Rotation and revocation

Stop the listener in Blender; this closes existing client sockets and clears its
in-memory credential. Generate a new secret, change the Auth Token preference (or
Blender's launch environment), and update every authorized client entry. Start the
listener and reconnect clients. Existing clients with the old secret are rejected.

Preferences are snapshotted when the listener starts. Editing the preference while
it is running does not rotate the active secret. If Blender inherited the secret
through its environment, changing a terminal variable or the preference cannot
change that process's environment: save work and use the new launch environment
at the next planned Blender start.

## Security boundary

- Clients that cannot obtain the secret cannot submit Blender commands. Checks run
  in the socket handler before queueing **any** command, including observations and
  code execution. The secret is removed before queueing, and authentication errors
  do not echo it. Authentication probes do not invoke a Blender operation.
- The token is a bearer credential sent over TCP without TLS. This does not
  authenticate the listener's identity or encrypt traffic. Use localhost for local
  work; use an encrypted, authenticated tunnel for remote access. A party able to
  intercept the connection or impersonate the listener could capture the token.
- Apps running as the same OS user, administrators or malware may read environment
  variables, personal client configuration, Blender preferences or process memory.
  This feature does not isolate a compromised account. OS account/process isolation
  is needed for that threat. The password UI masks the preference but does not
  encrypt the saved credential.
- The shared secret grants full socket access; it does not provide per-client
  permissions. Authorized Python still runs with Blender's OS permissions. Keep
  `BLENDER_MCP_SAFE_MODE=1` on the bridge for its additional code-validation guard;
  a different authorized socket client can bypass that bridge-only validator.
- The initial unauthenticated input is limited to 8 KiB and five seconds. The
  official bridge authenticates before sending potentially large scripts. This is
  not comprehensive protection against connection flooding or other denial of service.

## Wire protocol

The bridge's first request is the following JSON object. The placeholder here is
not a usable credential:

```json
{"type":"authenticate","auth_token":"<shared-secret>"}
```

The expected response is:

```json
{"status":"success","result":{"authenticated":true,"auth_protocol":1}}
```

Every subsequent JSON command includes the same top-level `auth_token` field,
alongside the existing `type` and `params`. The add-on compares the credential with
`hmac.compare_digest`, removes it, and queues only an authorized command. Send one
request and wait for its response before sending the next, as with the existing
JSON socket protocol. Reconnects require a new authentication probe.

Missing, malformed or wrong credentials produce this generic response and close
the connection. A successfully authenticated request does not permit anonymous
follow-up requests:

```json
{"status":"error","code":"AUTHENTICATION_FAILED","message":"Authentication failed"}
```
