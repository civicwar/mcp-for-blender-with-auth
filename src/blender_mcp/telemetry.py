"""
Opt-in telemetry for Blender MCP.
All collection and uploads require current, explicit Blender UI consent.
"""

import contextlib
import logging
import os
import platform
import queue
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

logger = logging.getLogger("blender-mcp-telemetry")


def get_package_version() -> str:
    """Get the installed package version"""
    try:
        return version("mcp-for-blender")
    except PackageNotFoundError:
        # Pre-rename distribution name.
        try:
            return version("blender-mcp")
        except PackageNotFoundError:
            return "unknown"


MCP_VERSION = get_package_version()

class EventType(str, Enum):
    """Types of telemetry events"""
    STARTUP = "startup"
    TOOL_EXECUTION = "tool_execution"
    PROMPT_SENT = "prompt_sent"
    CONNECTION = "connection"
    ERROR = "error"


@dataclass
class TelemetryEvent:
    """Structure for telemetry events"""
    event_type: EventType
    customer_uuid: str
    session_id: str
    timestamp: float
    version: str
    platform: str

    # Optional fields
    tool_name: str | None = None
    prompt_text: str | None = None
    success: bool = True
    duration_ms: float | None = None
    error_message: str | None = None
    blender_version: str | None = None
    addon_version: str | None = None
    addon_protocol: int | None = None
    metadata: dict[str, Any] | None = None


class TelemetryCollector:
    """Main telemetry collection class"""

    def __init__(self):
        """Initialize telemetry collector"""
        # Import config here to avoid circular imports
        try:
            from .config import telemetry_config
        except ModuleNotFoundError as exc:
            if exc.name != f"{__package__}.config":
                raise
            # The upstream telemetry backend config is gitignored. A clean
            # checkout must still work without copying deployment credentials.
            # No backend fields are used while collection is disabled.
            telemetry_config = SimpleNamespace(enabled=False)
            logger.info("Telemetry disabled: no backend configuration installed")
        self.config = telemetry_config

        # Check if disabled via environment variables
        if self._is_disabled():
            self.config.enabled = False
            logger.warning("Telemetry disabled via environment variable")

        # Generate or load customer UUID
        self._customer_uuid: str = self._get_or_create_uuid()
        self._session_id: str = str(uuid.uuid4())

        # Rate limiting tracking
        self._event_timestamps: list[float] = []
        self._rate_limit_lock = threading.Lock()

        # Reported locally by the addon at handshake; uploaded only with consent.
        self.addon_version: str | None = None
        self.addon_protocol: int | None = None

        # Background queue and worker
        self._queue: "queue.Queue[TelemetryEvent]" = queue.Queue(maxsize=1000)
        self._worker: threading.Thread = threading.Thread(
            target=self._worker_loop, daemon=True
        )
        self._worker.start()

        logger.debug(f"Telemetry initialized (enabled={self.config.enabled})")

    def _is_disabled(self) -> bool:
        """Check if telemetry is disabled via environment variables"""
        disable_vars = [
            "DISABLE_TELEMETRY",
            "BLENDER_MCP_DISABLE_TELEMETRY",
            "MCP_DISABLE_TELEMETRY"
        ]

        for var in disable_vars:
            if os.environ.get(var, "").lower() in ("true", "1", "yes", "on"):
                return True
        return False

    def _get_data_directory(self) -> Path:
        """Get directory for storing telemetry data"""
        if sys.platform == "win32":
            base_dir = Path(os.environ.get('APPDATA', Path.home() / 'AppData' / 'Roaming'))
        elif sys.platform == "darwin":
            base_dir = Path.home() / 'Library' / 'Application Support'
        else:  # Linux
            base_dir = Path(os.environ.get('XDG_DATA_HOME', Path.home() / '.local' / 'share'))

        data_dir = base_dir / 'BlenderMCP'
        data_dir.mkdir(parents=True, exist_ok=True)
        return data_dir

    def _get_or_create_uuid(self) -> str:
        """Get or create anonymous customer UUID"""
        try:
            data_dir = self._get_data_directory()
            uuid_file = data_dir / "customer_uuid.txt"

            if uuid_file.exists():
                customer_uuid = uuid_file.read_text(encoding="utf-8").strip()
                if customer_uuid:
                    return customer_uuid

            # Create new UUID
            customer_uuid = str(uuid.uuid4())
            uuid_file.write_text(customer_uuid, encoding="utf-8")

            # Set restrictive permissions on Unix
            if sys.platform != "win32":
                os.chmod(uuid_file, 0o600)

            return customer_uuid
        except Exception as e:
            logger.debug(f"Failed to persist UUID: {e}")
            return str(uuid.uuid4())

    def _check_user_consent(self) -> bool:
        """Whether collection is permitted. Unreachable Blender means no."""
        return self.check_user_consent() is True

    def is_enabled(self) -> bool:
        """Require both backend permission and live UI consent, for every path."""
        return bool(
            self.config.enabled
            and not self._is_disabled()
            and self._check_user_consent()
        )

    def check_user_consent(self) -> bool | None:
        """Consent from Blender: True, False, or None if unreachable.

        Never cache an affirmative answer: the checkbox can change between
        recording an event and the background worker sending it.
        """
        try:
            from .server import get_blender_connection
            blender = get_blender_connection()
            result = blender.send_command("get_telemetry_consent")
            # Strings such as "false", missing fields and error replies are
            # not consent. Only Blender's literal boolean True grants it.
            return (
                isinstance(result, dict)
                and "error" not in result
                and result.get("consent") is True
            )
        except Exception as e:
            logger.debug(f"Could not check telemetry consent: {e}")
            return None

    def set_addon_info(self, addon_version: list[int] | None, protocol: int | None) -> None:
        """Tag later events with the connected addon's version and protocol."""
        self.addon_version = (
            ".".join(str(part) for part in addon_version) if addon_version else None
        )
        self.addon_protocol = protocol

    def record_event(
        self,
        event_type: EventType,
        tool_name: str | None = None,
        prompt_text: str | None = None,
        success: bool = True,
        duration_ms: float | None = None,
        error_message: str | None = None,
        blender_version: str | None = None,
        metadata: dict[str, Any] | None = None
    ):
        """Queue an event only with explicit consent; upload is checked again."""
        if not self.is_enabled():
            return

        # Truncate prompt if needed.
        if prompt_text and len(prompt_text) > self.config.max_prompt_length:
            prompt_text = prompt_text[:self.config.max_prompt_length] + "..."

        if error_message and len(error_message) > 200:
            error_message = error_message[:200] + "..."

        event = TelemetryEvent(
            event_type=event_type,
            customer_uuid=self._customer_uuid,
            session_id=self._session_id,
            timestamp=time.time(),
            version=MCP_VERSION,
            platform=platform.system().lower(),
            tool_name=tool_name,
            prompt_text=prompt_text,
            success=success,
            duration_ms=duration_ms,
            error_message=error_message,
            blender_version=blender_version,
            addon_version=self.addon_version,
            addon_protocol=self.addon_protocol,
            metadata=metadata
        )

        # Enqueue for background worker
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            logger.debug("Telemetry queue full, dropping event")

    def _worker_loop(self):
        """Background worker that sends telemetry"""
        while True:
            event = self._queue.get()
            try:
                self._send_event(event)
            except Exception as e:
                logger.debug(f"Telemetry send failed: {e}")
            finally:
                with contextlib.suppress(Exception):
                    self._queue.task_done()

    def _auth_headers(self) -> dict[str, str]:
        """Headers for Supabase REST/Storage API requests"""
        return {
            "apikey": self.config.supabase_anon_key,
            "Authorization": f"Bearer {self.config.supabase_anon_key}",
        }

    def _send_event(self, event: TelemetryEvent):
        """Send event to Supabase via the PostgREST API"""
        try:
            data = {
                "customer_uuid": event.customer_uuid,
                "session_id": event.session_id,
                "event_type": event.event_type.value,
                "tool_name": event.tool_name,
                "prompt_text": event.prompt_text,
                "success": event.success,
                "duration_ms": event.duration_ms,
                "error_message": event.error_message,
                "version": event.version,
                "platform": event.platform,
                "blender_version": event.blender_version,
                "addon_version": event.addon_version,
                "addon_protocol": event.addon_protocol,
                "metadata": event.metadata or {},
                "event_timestamp": int(event.timestamp),
            }

            # An event may have been queued before the user opted out. Drop
            # it instead of sending it or retaining it for a later opt-in.
            if not self.is_enabled():
                return
            response = httpx.post(
                f"{self.config.supabase_url}/rest/v1/telemetry_events",
                json=data,
                headers={**self._auth_headers(), "Prefer": "return=minimal"},
                timeout=self.config.timeout,
            )
            response.raise_for_status()
            logger.debug(f"Telemetry sent: {event.event_type}")

        except Exception as e:
            logger.debug(f"Failed to send telemetry: {e}")

    def upload_screenshot(self, image_bytes: bytes, prefix: str) -> str:
        """Upload screenshot to Supabase Storage.
        
        Args:
            image_bytes: PNG image data
            prefix: Filename prefix (e.g., 'screenshot')
            
        Returns:
            Storage path reference (storage:bucket/filename) or empty string on failure
        """
        if not self.is_enabled():
            return ""

        try:
            # Generate unique filename
            timestamp = int(time.time() * 1000)
            filename = f"{prefix}_{self._session_id}_{timestamp}.png"

            bucket = self.config.supabase_bucket
            response = httpx.post(
                f"{self.config.supabase_url}/storage/v1/object/{bucket}/{filename}",
                content=image_bytes,
                headers={**self._auth_headers(), "Content-Type": "image/png"},
                # Image uploads need more headroom than the event timeout
                timeout=10.0,
            )
            response.raise_for_status()

            return f"storage:{bucket}/{filename}"
        except Exception:
            return ""

# Global telemetry instance
_telemetry_collector: TelemetryCollector | None = None


def get_telemetry() -> TelemetryCollector:
    """Get the global telemetry collector instance"""
    global _telemetry_collector
    if _telemetry_collector is None:
        _telemetry_collector = TelemetryCollector()
    return _telemetry_collector


def record_tool_usage(
    tool_name: str,
    success: bool,
    duration_ms: float,
    error: str | None = None
):
    """Convenience function to record tool usage"""
    get_telemetry().record_event(
        event_type=EventType.TOOL_EXECUTION,
        tool_name=tool_name,
        success=success,
        duration_ms=duration_ms,
        error_message=error
    )


def record_startup(blender_version: str | None = None):
    """Record server startup event"""
    get_telemetry().record_event(
        event_type=EventType.STARTUP,
        blender_version=blender_version
    )


def is_telemetry_enabled() -> bool:
    """Check effective permission, including live consent and environment."""
    try:
        return get_telemetry().is_enabled()
    except Exception:
        return False
