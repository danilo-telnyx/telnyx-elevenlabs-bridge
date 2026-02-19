"""
Telnyx Media Streaming <-> ElevenLabs Conversational AI Bridge

Accepts incoming WebSocket connections from Telnyx Media Streaming,
connects outbound to ElevenLabs Conversational AI WebSocket, and
translates between the two incompatible protocols in real-time.

Audio flow:
  Caller -> Telnyx (PCMU/8kHz base64) -> Bridge (transcode) -> ElevenLabs (PCM16/16kHz base64)
  ElevenLabs (PCM16/16kHz base64) -> Bridge (transcode) -> Telnyx (PCMU/8kHz base64 RTP) -> Caller
"""

import asyncio
import audioop
import base64
import json
import logging
import os
import struct
import time
from typing import Optional

import uvicorn
import websockets
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ELEVENLABS_AGENT_ID = os.getenv("ELEVENLABS_AGENT_ID", "")
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_OUTPUT_FORMAT = os.getenv("ELEVENLABS_OUTPUT_FORMAT", "pcm_16000")
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8765"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL), format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("bridge")

app = FastAPI(title="Telnyx-ElevenLabs Bridge")

# ---------------------------------------------------------------------------
# Audio conversion helpers
# ---------------------------------------------------------------------------

# PCMU (μ-law) uses 8kHz/8bit. ElevenLabs expects PCM16 (signed 16-bit LE) at 16kHz.
# We decode μ-law -> PCM16 @ 8kHz, then upsample to 16kHz.
# For the reverse path: downsample 16kHz -> 8kHz, then encode to μ-law.

def pcmu_to_pcm16_16k(pcmu_bytes: bytes) -> bytes:
    """Convert PCMU (μ-law 8kHz) to signed 16-bit PCM at 16kHz."""
    # Decode μ-law to linear PCM16 at 8kHz
    pcm_8k = audioop.ulaw2lin(pcmu_bytes, 2)
    # Upsample 8kHz -> 16kHz (ratio = 2)
    pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
    return pcm_16k


def pcm16_16k_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert signed 16-bit PCM at 16kHz to PCMU (μ-law 8kHz)."""
    # Downsample 16kHz -> 8kHz
    pcm_8k, _ = audioop.ratecv(pcm16_bytes, 2, 1, 16000, 8000, None)
    # Encode to μ-law
    return audioop.lin2ulaw(pcm_8k, 2)


def pcm16_44k_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert signed 16-bit PCM at 44.1kHz to PCMU (μ-law 8kHz)."""
    pcm_8k, _ = audioop.ratecv(pcm16_bytes, 2, 1, 44100, 8000, None)
    return audioop.lin2ulaw(pcm_8k, 2)


def elevenlabs_audio_to_pcmu(pcm16_bytes: bytes) -> bytes:
    """Convert ElevenLabs output audio to PCMU based on configured format."""
    if ELEVENLABS_OUTPUT_FORMAT == "pcm_44100":
        return pcm16_44k_to_pcmu(pcm16_bytes)
    else:  # pcm_16000 (default)
        return pcm16_16k_to_pcmu(pcm16_bytes)


# ---------------------------------------------------------------------------
# ElevenLabs WebSocket URL builder
# ---------------------------------------------------------------------------

def get_elevenlabs_ws_url() -> str:
    """Build the ElevenLabs Conversational AI WebSocket URL."""
    base = f"wss://api.elevenlabs.io/v1/convai/conversation?agent_id={ELEVENLABS_AGENT_ID}"
    return base


async def get_elevenlabs_signed_url() -> Optional[str]:
    """For private agents, get a signed URL using the API key."""
    if not ELEVENLABS_API_KEY:
        return None
    import aiohttp
    async with aiohttp.ClientSession() as session:
        url = f"https://api.elevenlabs.io/v1/convai/conversation/get-signed-url?agent_id={ELEVENLABS_AGENT_ID}"
        headers = {"xi-api-key": ELEVENLABS_API_KEY}
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("signed_url")
    return None


# ---------------------------------------------------------------------------
# Bridge session: manages one Telnyx call <-> one ElevenLabs conversation
# ---------------------------------------------------------------------------

class BridgeSession:
    """Bridges a single Telnyx media stream to an ElevenLabs conversation."""

    def __init__(self, telnyx_ws: WebSocket):
        self.telnyx_ws = telnyx_ws
        self.elevenlabs_ws: Optional[websockets.WebSocketClientProtocol] = None
        self.stream_id: Optional[str] = None
        self.call_control_id: Optional[str] = None
        self._closed = False
        self._el_task: Optional[asyncio.Task] = None

    async def connect_elevenlabs(self):
        """Open outbound WebSocket to ElevenLabs."""
        # Try signed URL first (for private agents), fall back to public URL
        ws_url = None
        if ELEVENLABS_API_KEY:
            try:
                ws_url = await get_elevenlabs_signed_url()
            except Exception:
                logger.warning("Failed to get signed URL, falling back to public URL")

        if not ws_url:
            ws_url = get_elevenlabs_ws_url()

        logger.info(f"Connecting to ElevenLabs: {ws_url[:80]}...")
        self.elevenlabs_ws = await websockets.connect(ws_url)

        # Send conversation initiation with audio format preferences
        init_msg = {
            "type": "conversation_initiation_client_data",
            "conversation_initiation_client_data": {
                "conversation_config_override": {
                    "agent": {
                        "tts": {
                            "output_format": ELEVENLABS_OUTPUT_FORMAT,
                        }
                    }
                }
            }
        }
        await self.elevenlabs_ws.send(json.dumps(init_msg))
        logger.info("Sent conversation_initiation_client_data to ElevenLabs")

    async def handle_telnyx_message(self, data: dict):
        """Process a message from Telnyx and forward to ElevenLabs."""
        event = data.get("event")

        if event == "connected":
            logger.info("Telnyx WebSocket connected")
            # Connect to ElevenLabs when Telnyx connects
            await self.connect_elevenlabs()
            # Start listening for ElevenLabs responses
            self._el_task = asyncio.create_task(self._listen_elevenlabs())

        elif event == "start":
            start_info = data.get("start", {})
            self.stream_id = data.get("stream_id")
            self.call_control_id = start_info.get("call_control_id")
            media_format = start_info.get("media_format", {})
            logger.info(
                f"Stream started: stream_id={self.stream_id}, "
                f"call_control_id={self.call_control_id}, "
                f"format={media_format}"
            )

        elif event == "media":
            # Forward audio from Telnyx to ElevenLabs
            if not self.elevenlabs_ws:
                return
            media = data.get("media", {})
            payload_b64 = media.get("payload", "")
            if not payload_b64:
                return

            # Decode base64 PCMU from Telnyx
            pcmu_bytes = base64.b64decode(payload_b64)

            # Convert PCMU 8kHz -> PCM16 16kHz
            pcm16_bytes = pcmu_to_pcm16_16k(pcmu_bytes)

            # Re-encode to base64 for ElevenLabs
            pcm16_b64 = base64.b64encode(pcm16_bytes).decode("utf-8")

            # Send as user_audio_chunk to ElevenLabs
            await self.elevenlabs_ws.send(json.dumps({
                "user_audio_chunk": pcm16_b64
            }))

        elif event == "stop":
            logger.info(f"Telnyx stream stopped: stream_id={self.stream_id}")
            await self.close()

        elif event == "dtmf":
            digit = data.get("dtmf", {}).get("digit", "")
            logger.info(f"DTMF received: {digit}")
            # Optionally send as contextual update to ElevenLabs
            if self.elevenlabs_ws:
                await self.elevenlabs_ws.send(json.dumps({
                    "type": "contextual_update",
                    "text": f"User pressed DTMF key: {digit}"
                }))

        elif event == "mark":
            # Mark events are echoed back by Telnyx when audio finishes playing
            mark_name = data.get("mark", {}).get("name", "")
            logger.debug(f"Mark received: {mark_name}")

    async def _listen_elevenlabs(self):
        """Listen for messages from ElevenLabs and forward audio to Telnyx."""
        try:
            async for message in self.elevenlabs_ws:
                if self._closed:
                    break

                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "conversation_initiation_metadata":
                    meta = data.get("conversation_initiation_metadata_event", {})
                    conv_id = meta.get("conversation_id", "unknown")
                    output_fmt = meta.get("agent_output_audio_format", "unknown")
                    input_fmt = meta.get("user_input_audio_format", "unknown")
                    logger.info(
                        f"ElevenLabs conversation started: id={conv_id}, "
                        f"output_format={output_fmt}, input_format={input_fmt}"
                    )

                elif msg_type == "audio":
                    # Forward audio from ElevenLabs to Telnyx
                    audio_event = data.get("audio_event", {})
                    audio_b64 = audio_event.get("audio_base_64", "")
                    if not audio_b64:
                        continue

                    # Decode PCM16 from ElevenLabs
                    pcm16_bytes = base64.b64decode(audio_b64)

                    # Convert to PCMU for Telnyx
                    pcmu_bytes = elevenlabs_audio_to_pcmu(pcm16_bytes)

                    # Send as base64-encoded RTP payload to Telnyx
                    # Telnyx bidirectional streaming accepts chunks of 20ms to 30s
                    pcmu_b64 = base64.b64encode(pcmu_bytes).decode("utf-8")
                    await self.telnyx_ws.send_json({
                        "event": "media",
                        "media": {
                            "payload": pcmu_b64
                        }
                    })

                elif msg_type == "ping":
                    # Respond to ElevenLabs ping with pong
                    ping_event = data.get("ping_event", {})
                    event_id = ping_event.get("event_id")
                    ping_ms = ping_event.get("ping_ms", 0)

                    # ElevenLabs expects pong after ping_ms delay
                    if ping_ms and ping_ms > 0:
                        await asyncio.sleep(ping_ms / 1000.0)

                    await self.elevenlabs_ws.send(json.dumps({
                        "type": "pong",
                        "event_id": event_id
                    }))

                elif msg_type == "user_transcript":
                    transcript = data.get("user_transcription_event", {}).get("user_transcript", "")
                    logger.info(f"User said: {transcript}")

                elif msg_type == "agent_response":
                    response = data.get("agent_response_event", {}).get("agent_response", "")
                    logger.info(f"Agent said: {response}")

                elif msg_type == "interruption":
                    # User interrupted the agent - clear Telnyx audio queue
                    logger.info("User interruption detected, clearing Telnyx audio queue")
                    await self.telnyx_ws.send_json({"event": "clear"})

                elif msg_type == "agent_response_correction":
                    correction = data.get("agent_response_correction_event", {})
                    logger.debug(f"Agent response corrected: {correction.get('corrected_agent_response', '')}")

                else:
                    logger.debug(f"ElevenLabs event (unhandled): {msg_type}")

        except websockets.exceptions.ConnectionClosed as e:
            logger.info(f"ElevenLabs WebSocket closed: {e}")
        except Exception as e:
            logger.error(f"Error in ElevenLabs listener: {e}", exc_info=True)
        finally:
            await self.close()

    async def close(self):
        """Clean up both WebSocket connections."""
        if self._closed:
            return
        self._closed = True
        logger.info("Closing bridge session")

        if self.elevenlabs_ws:
            try:
                await self.elevenlabs_ws.close()
            except Exception:
                pass

        if self._el_task and not self._el_task.done():
            self._el_task.cancel()


# ---------------------------------------------------------------------------
# FastAPI WebSocket endpoint - Telnyx connects here
# ---------------------------------------------------------------------------

@app.websocket("/media-stream")
async def telnyx_media_stream(websocket: WebSocket):
    """
    WebSocket endpoint that Telnyx Media Streaming connects to.
    
    Configure your Telnyx call with:
      stream_url: wss://your-domain.com/media-stream
      stream_bidirectional_mode: rtp
      stream_track: inbound_track
    """
    await websocket.accept()
    logger.info("Telnyx WebSocket connection accepted")

    session = BridgeSession(websocket)

    try:
        while True:
            # Receive message from Telnyx
            raw = await websocket.receive_text()
            data = json.loads(raw)
            await session.handle_telnyx_message(data)

    except WebSocketDisconnect:
        logger.info("Telnyx WebSocket disconnected")
    except Exception as e:
        logger.error(f"Error in Telnyx handler: {e}", exc_info=True)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "status": "ok",
        "agent_id": ELEVENLABS_AGENT_ID[:8] + "..." if ELEVENLABS_AGENT_ID else "NOT SET",
    }


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not ELEVENLABS_AGENT_ID:
        logger.error("ELEVENLABS_AGENT_ID is required! Set it in .env or environment.")
        exit(1)
    logger.info(f"Starting Telnyx-ElevenLabs bridge on {HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT)
