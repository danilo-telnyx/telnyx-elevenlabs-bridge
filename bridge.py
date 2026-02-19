"""
Telnyx Media Streaming <-> ElevenLabs Conversational AI Bridge

Accepts incoming WebSocket connections from Telnyx Media Streaming,
connects outbound to ElevenLabs Conversational AI WebSocket, and
translates between the two incompatible protocols in real-time.

Audio flow:
  Caller -> Telnyx (PCMU/8kHz base64) -> Bridge (transcode) -> ElevenLabs (PCM16/16kHz base64)
  ElevenLabs (PCM16/16kHz base64) -> Bridge (transcode) -> Telnyx (PCMU/8kHz base64 RTP) -> Caller

Requires Python 3.9+. For Python 3.13+, install ``audioop-lts`` as ``audioop``
was removed from the standard library.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import signal
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

import aiohttp
import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

# audioop was removed in Python 3.13 – fall back to the audioop-lts backport.
try:
    import audioop  # type: ignore[import-untyped]
except ModuleNotFoundError:
    try:
        import audioop_lts as audioop  # type: ignore[no-redef]
    except ModuleNotFoundError:
        sys.exit(
            "ERROR: 'audioop' is not available. On Python ≥3.13 install the "
            "'audioop-lts' package: pip install audioop-lts"
        )

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ELEVENLABS_AGENT_ID: str = os.getenv("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_API_KEY: str = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_OUTPUT_FORMAT: str = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_16000")
HOST: str = os.getenv("HOST", "0.0.0.0")
PORT: int = int(os.getenv("PORT", "8765"))
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("bridge")

# Track active sessions for graceful shutdown
_active_sessions: set[BridgeSession] = set()  # type: ignore[name-defined]  # forward ref


# ---------------------------------------------------------------------------
# Lifespan (startup / shutdown)
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(application: FastAPI):
    """Application lifespan: validate config on startup, clean up on shutdown."""
    if not ELEVENLABS_AGENT_ID:
        logger.error("ELEVENLABS_AGENT_ID is required! Set it in .env or environment.")
        sys.exit(1)
    logger.info("Bridge starting — agent_id=%s…", ELEVENLABS_AGENT_ID[:8])
    yield
    # Shutdown: close all active sessions
    logger.info("Shutting down — closing %d active session(s)…", len(_active_sessions))
    close_tasks = [s.close() for s in list(_active_sessions)]
    if close_tasks:
        await asyncio.gather(*close_tasks, return_exceptions=True)
    logger.info("All sessions closed.")


app = FastAPI(title="Telnyx-ElevenLabs Bridge", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Audio conversion helpers
# ---------------------------------------------------------------------------
# PCMU (μ-law) uses 8 kHz / 8-bit. ElevenLabs expects PCM16 (signed 16-bit LE)
# at 16 kHz.  We decode μ-law → PCM16 @ 8 kHz, then upsample to 16 kHz.
# Reverse path: downsample 16 kHz → 8 kHz, then encode to μ-law.
# ---------------------------------------------------------------------------

def pcmu_to_pcm16_16k(pcmu_bytes: bytes) -> bytes:
    """Convert PCMU (μ-law 8 kHz) to signed 16-bit PCM at 16 kHz."""
    pcm_8k: bytes = audioop.ulaw2lin(pcmu_bytes, 2)
    pcm_16k: bytes
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def pcm16_16k_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert signed 16-bit PCM at 16 kHz to PCMU (μ-law 8 kHz)."""
    pcm_8k: bytes
    pcm_8k, _ = audioop.ratecv(pcm16_bytes, 2, 1, 16000, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def pcm16_44k_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert signed 16-bit PCM at 44.1 kHz to PCMU (μ-law 8 kHz)."""
    pcm_8k: bytes
    pcm_8k, _ = audioop.ratecv(pcm16_bytes, 2, 1, 44100, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def elevenlabs_audio_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert ElevenLabs output audio to PCMU based on configured format."""
    if ELEVENLABS_OUTPUT_FORMAT == "pcm_44100":
        return pcm16_44k_to_pcmu(pcm16_bytes)
    return pcm16_16k_to_pcmu(pcm16_bytes)


# ---------------------------------------------------------------------------
# ElevenLabs WebSocket URL helpers
# ---------------------------------------------------------------------------

def _build_elevenlabs_ws_url() -> str:
    """Build the public ElevenLabs Conversational AI WebSocket URL."""
    return f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"


async def _get_elevenlabs_signed_url() -> Optional[str]:
    """For private agents, obtain a short-lived signed URL via the REST API."""
    if not ELEVENLABS_API_KEY:
        return None
    url = (
        "https://api.elevenlabs.io/v1/convai/conversation/get-signed-url"
        f"?agent_id={ELEVENLABS_AGENT_ID}"
    )
    headers = {"xi-api-key": ELEVENLABS_API_KEY}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("signed_url")
                logger.warning("Signed-URL request failed: HTTP %d", resp.status)
    except Exception:
        logger.warning("Failed to get ElevenLabs signed URL", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Bridge session: manages one Telnyx call <-> one ElevenLabs conversation
# ---------------------------------------------------------------------------

class BridgeSession:
    """Bridges a single Telnyx media stream to an ElevenLabs conversation."""

    def __init__(self, telnyx_ws: WebSocket) -> None:
        self.telnyx_ws: WebSocket = telnyx_ws
        self.elevenlabs_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stream_id: Optional[str] = None
        self.call_control_id: Optional[str] = None
        self.conversation_id: Optional[str] = None
        self._closed: bool = False
        self._el_task: Optional[asyncio.Task[None]] = None
        self._send_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # ElevenLabs connection
    # ------------------------------------------------------------------

    async def connect_elevenlabs(self) -> None:
        """Open outbound WebSocket to ElevenLabs Conversational AI."""
        ws_url: Optional[str] = None
        if ELEVENLABS_API_KEY:
            ws_url = await _get_elevenlabs_signed_url()

        if not ws_url:
            ws_url = _build_elevenlabs_ws_url()

        # Don't log the full URL — signed URLs contain secrets
        logger.info("Connecting to ElevenLabs (agent_id=%s…)…", ELEVENLABS_AGENT_ID[:8])
        self.elevenlabs_ws = await websockets.connect(
            ws_url,
            open_timeout=15,
            close_timeout=5,
            max_size=2**20,  # 1 MiB max message
        )

        # Send conversation config with desired output audio format
        init_msg: dict[str, Any] = {
            "type": "conversation_initiation_client_data",
            "conversation_initiation_client_data": {
                "conversation_config_override": {
                    "agent": {
                        "tts": {
                            "output_format": ELEVENLABS_OUTPUT_FORMAT,
                        }
                    }
                }
            },
        }
        await self.elevenlabs_ws.send(json.dumps(init_msg))
        logger.info("Sent conversation_initiation_client_data to ElevenLabs")

    # ------------------------------------------------------------------
    # Telnyx → ElevenLabs
    # ------------------------------------------------------------------

    async def handle_telnyx_message(self, data: dict[str, Any]) -> None:
        """Process a message from Telnyx and forward audio to ElevenLabs."""
        event: Optional[str] = data.get("event")

        if event == "connected":
            logger.info("Telnyx stream connected")
            await self.connect_elevenlabs()
            self._el_task = asyncio.create_task(
                self._listen_elevenlabs(), name="el-listener"
            )

        elif event == "start":
            start_info: dict[str, Any] = data.get("start", {})
            self.stream_id = data.get("stream_id")
            self.call_control_id = start_info.get("call_control_id")
            media_format: dict[str, Any] = start_info.get("media_format", {})
            logger.info(
                "Stream started: stream_id=%s, call_control_id=%s, format=%s",
                self.stream_id,
                self.call_control_id,
                media_format,
            )

        elif event == "media":
            await self._forward_audio_to_elevenlabs(data)

        elif event == "stop":
            logger.info("Telnyx stream stopped: stream_id=%s", self.stream_id)
            await self.close()

        elif event == "dtmf":
            digit: str = data.get("dtmf", {}).get("digit", "")
            logger.info("DTMF received: %s", digit)
            await self._send_to_elevenlabs(json.dumps({
                "type": "contextual_update",
                "text": f"User pressed DTMF key: {digit}",
            }))

        elif event == "mark":
            mark_name: str = data.get("mark", {}).get("name", "")
            logger.debug("Mark received: %s", mark_name)

        else:
            logger.debug("Unhandled Telnyx event: %s", event)

    async def _forward_audio_to_elevenlabs(self, data: dict[str, Any]) -> None:
        """Transcode and forward a single media chunk from Telnyx to ElevenLabs."""
        media: dict[str, Any] = data.get("media", {})
        payload_b64: str = media.get("payload", "")
        if not payload_b64:
            return

        try:
            pcmu_bytes: bytes = base64.b64decode(payload_b64)
            pcm16_bytes: bytes = pcmu_to_pcm16_16k(pcmu_bytes)
            pcm16_b64: str = base64.b64encode(pcm16_bytes).decode("ascii")
        except Exception:
            logger.warning("Audio transcode error (Telnyx→EL)", exc_info=True)
            return

        await self._send_to_elevenlabs(json.dumps({
            "user_audio_chunk": pcm16_b64,
        }))

    # ------------------------------------------------------------------
    # ElevenLabs → Telnyx
    # ------------------------------------------------------------------

    async def _listen_elevenlabs(self) -> None:
        """Listen for messages from ElevenLabs and forward audio to Telnyx."""
        if not self.elevenlabs_ws:
            return
        try:
            async for message in self.elevenlabs_ws:
                if self._closed:
                    break
                try:
                    data: dict[str, Any] = json.loads(message)
                except json.JSONDecodeError:
                    logger.warning("Non-JSON message from ElevenLabs, ignoring")
                    continue

                await self._handle_elevenlabs_event(data)

        except websockets.exceptions.ConnectionClosedOK:
            logger.info("ElevenLabs WebSocket closed normally")
        except websockets.exceptions.ConnectionClosed as exc:
            logger.warning("ElevenLabs WebSocket closed: code=%s reason=%s", exc.code, exc.reason)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.error("Error in ElevenLabs listener", exc_info=True)
        finally:
            if not self._closed:
                await self.close()

    async def _handle_elevenlabs_event(self, data: dict[str, Any]) -> None:
        """Route a single ElevenLabs event."""
        msg_type: Optional[str] = data.get("type")

        if msg_type == "conversation_initiation_metadata":
            meta: dict[str, Any] = data.get("conversation_initiation_metadata_event", {})
            self.conversation_id = meta.get("conversation_id")
            logger.info(
                "ElevenLabs conversation started: id=%s, output=%s, input=%s",
                self.conversation_id,
                meta.get("agent_output_audio_format", "?"),
                meta.get("user_input_audio_format", "?"),
            )

        elif msg_type == "audio":
            await self._forward_audio_to_telnyx(data)

        elif msg_type == "ping":
            ping_event: dict[str, Any] = data.get("ping_event", {})
            event_id = ping_event.get("event_id")
            await self._send_to_elevenlabs(json.dumps({
                "type": "pong",
                "event_id": event_id,
            }))

        elif msg_type == "user_transcript":
            transcript: str = data.get("user_transcription_event", {}).get("user_transcript", "")
            logger.info("User said: %s", transcript)

        elif msg_type == "agent_response":
            response: str = data.get("agent_response_event", {}).get("agent_response", "")
            logger.info("Agent said: %s", response)

        elif msg_type == "interruption":
            logger.info("User interruption — clearing Telnyx audio queue")
            await self._send_to_telnyx({"event": "clear"})

        elif msg_type == "agent_response_correction":
            correction: dict[str, Any] = data.get("agent_response_correction_event", {})
            logger.debug("Agent correction: %s", correction.get("corrected_agent_response", ""))

        elif msg_type == "internal_vad_score":
            pass  # high-frequency, ignore silently

        else:
            logger.debug("Unhandled ElevenLabs event: %s", msg_type)

    async def _forward_audio_to_telnyx(self, data: dict[str, Any]) -> None:
        """Transcode and forward audio from ElevenLabs to Telnyx."""
        audio_event: dict[str, Any] = data.get("audio_event", {})
        audio_b64: str = audio_event.get("audio_base_64", "")
        if not audio_b64:
            return

        try:
            pcm16_bytes: bytes = base64.b64decode(audio_b64)
            pcmu_bytes: bytes = elevenlabs_audio_to_pcmu(pcm16_bytes)
            pcmu_b64: str = base64.b64encode(pcmu_bytes).decode("ascii")
        except Exception:
            logger.warning("Audio transcode error (EL→Telnyx)", exc_info=True)
            return

        await self._send_to_telnyx({
            "event": "media",
            "media": {"payload": pcmu_b64},
        })

    # ------------------------------------------------------------------
    # Safe senders (handle closed connections gracefully)
    # ------------------------------------------------------------------

    async def _send_to_elevenlabs(self, message: str) -> None:
        """Send a message to ElevenLabs, swallowing errors if closed."""
        if self._closed or not self.elevenlabs_ws:
            return
        try:
            await self.elevenlabs_ws.send(message)
        except websockets.exceptions.ConnectionClosed:
            logger.debug("ElevenLabs WS already closed, dropping message")
        except Exception:
            logger.warning("Failed to send to ElevenLabs", exc_info=True)

    async def _send_to_telnyx(self, payload: dict[str, Any]) -> None:
        """Send JSON to Telnyx, swallowing errors if closed."""
        if self._closed:
            return
        try:
            await self.telnyx_ws.send_json(payload)
        except Exception:
            logger.debug("Failed to send to Telnyx (connection may be closed)")

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Clean up both WebSocket connections."""
        if self._closed:
            return
        self._closed = True
        _active_sessions.discard(self)
        logger.info(
            "Closing bridge session (stream_id=%s, conversation_id=%s)",
            self.stream_id,
            self.conversation_id,
        )

        if self.elevenlabs_ws:
            try:
                await self.elevenlabs_ws.close()
            except Exception:
                pass
            self.elevenlabs_ws = None

        if self._el_task and not self._el_task.done():
            self._el_task.cancel()
            try:
                await self._el_task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# FastAPI WebSocket endpoint — Telnyx connects here
# ---------------------------------------------------------------------------

@app.websocket("/media-stream")
async def telnyx_media_stream(websocket: WebSocket) -> None:
    """WebSocket endpoint for Telnyx Media Streaming.

    Configure your Telnyx call with::

        stream_url: wss://your-domain.com/media-stream
        stream_bidirectional_mode: rtp
        stream_track: inbound_track
    """
    await websocket.accept()
    logger.info("Telnyx WebSocket accepted (client=%s)", websocket.client)

    session = BridgeSession(websocket)
    _active_sessions.add(session)

    try:
        while True:
            raw: str = await websocket.receive_text()
            try:
                data: dict[str, Any] = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("Non-JSON from Telnyx, ignoring")
                continue
            await session.handle_telnyx_message(data)

    except WebSocketDisconnect:
        logger.info("Telnyx WebSocket disconnected (stream_id=%s)", session.stream_id)
    except Exception:
        logger.error("Error in Telnyx handler", exc_info=True)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict[str, str]:
    """Lightweight health-check endpoint."""
    return {
        "status": "ok",
        "agent_id": (ELEVENLABS_AGENT_ID[:8] + "…") if ELEVENLABS_AGENT_ID else "NOT SET",
        "sessions": str(len(_active_sessions)),
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logger.info("Starting Telnyx↔ElevenLabs bridge on %s:%d", HOST, PORT)
    uvicorn.run(app, host=HOST, port=PORT, log_level=LOG_LEVEL.lower())
