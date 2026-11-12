"""Credentials for the authenticated Blender socket (never tool arguments)."""

import os
import re

AUTH_TOKEN_ENV = "BLENDER_MCP_AUTH_TOKEN"
AUTH_PROTOCOL_VERSION = 1
AUTH_TOKEN_ERROR = (
    "Set BLENDER_MCP_AUTH_TOKEN to a random secret of 32-256 printable ASCII "
    "characters without spaces, and configure the same secret in Blender."
)


class AuthenticationError(ConnectionError):
    """The connection must not be used, even if an old add-on would accept it."""


def load_auth_token() -> str:
    token = os.environ.get(AUTH_TOKEN_ENV, "")
    if not re.fullmatch(r"[!-~]{32,256}", token):
        raise AuthenticationError(AUTH_TOKEN_ERROR)
    return token
