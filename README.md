# Telnyx Media Streaming ↔ ElevenLabs Conversational AI Bridge

A Python bridge service that translates between Telnyx Media Streaming WebSocket and ElevenLabs Conversational AI WebSocket protocols, enabling ElevenLabs voice agents to handle Telnyx phone calls.

## Architecture

```
Phone Call → Telnyx → Media Stream WS → Bridge → ElevenLabs Conversational AI WS
                                           ↕ (audio transcoding)
Phone Call ← Telnyx ← Media Stream WS ← Bridge ← ElevenLabs Conversational AI WS
```

**Audio transcoding:**
- **Inbound (caller → AI):** PCMU μ-law 8kHz → PCM16 signed 16-bit 16kHz
- **Outbound (AI → caller):** PCM16 16kHz → PCMU μ-law 8kHz

## Requirements

- **Python 3.9+** (tested on 3.9–3.13)
- On **Python 3.13+**, the `audioop` module was removed from the standard library. The bridge automatically falls back to [`audioop-lts`](https://pypi.org/project/audioop-lts/), which is included in `requirements.txt` for Python ≥3.13.

## Quick Start

### 1. Install dependencies

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env with your credentials
```

| Variable | Required | Description |
|----------|----------|-------------|
| `ELEVENLABS_AGENT_ID` | ✅ | Your ElevenLabs Conversational AI agent ID |
| `ELEVENLABS_API_KEY` | ❌ | Only needed for private agents (to get signed URLs) |
| `PORT` | ❌ | Server port (default: 8765) |
| `HOST` | ❌ | Bind address (default: 0.0.0.0) |
| `ELEVENLABS_OUTPUT_FORMAT` | ❌ | `pcm_16000` (default) or `pcm_44100` |
| `LOG_LEVEL` | ❌ | `DEBUG`, `INFO` (default), `WARNING`, `ERROR` |

### 3. Run the bridge

```bash
python bridge.py
```

The service will start on `ws://0.0.0.0:8765/media-stream`.

### 4. Expose publicly (for development)

Use ngrok or similar to expose the WebSocket:

```bash
ngrok http 8765
# Note the wss:// URL, e.g. wss://abc123.ngrok.io
```

## Telnyx Configuration

### Option A: Stream on Dial

When placing an outbound call via the Telnyx API, include streaming parameters:

```bash
curl -X POST https://api.telnyx.com/v2/calls \
  -H "Authorization: Bearer $TELNYX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "connection_id": "your-connection-uuid",
    "to": "+18005550199",
    "from": "+18005550100",
    "stream_url": "wss://your-domain.com/media-stream",
    "stream_track": "inbound_track",
    "stream_bidirectional_mode": "rtp",
    "stream_bidirectional_codec": "PCMU"
  }'
```

### Option B: Stream on Answer

When answering an inbound call:

```bash
curl -X POST https://api.telnyx.com/v2/calls/{call_control_id}/actions/answer \
  -H "Authorization: Bearer $TELNYX_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "stream_url": "wss://your-domain.com/media-stream",
    "stream_track": "inbound_track",
    "stream_bidirectional_mode": "rtp",
    "stream_bidirectional_codec": "PCMU"
  }'
```

### Key Telnyx parameters

| Parameter | Value | Why |
|-----------|-------|-----|
| `stream_url` | `wss://your-domain.com/media-stream` | Points to this bridge |
| `stream_track` | `inbound_track` | We only need the caller's audio |
| `stream_bidirectional_mode` | `rtp` | Enables sending audio back to the caller |
| `stream_bidirectional_codec` | `PCMU` | μ-law codec (default, matches our transcoding) |

## Protocol Translation Reference

### Telnyx → ElevenLabs

| Telnyx Event | Action |
|-------------|--------|
| `connected` | Open ElevenLabs WS, send `conversation_initiation_client_data` |
| `start` | Log stream metadata (encoding, sample rate) |
| `media` | Decode PCMU base64 → transcode to PCM16 16kHz → send as `user_audio_chunk` |
| `stop` | Close ElevenLabs WS |
| `dtmf` | Send as `contextual_update` to ElevenLabs |

### ElevenLabs → Telnyx

| ElevenLabs Event | Action |
|-----------------|--------|
| `conversation_initiation_metadata` | Log conversation ID and formats |
| `audio` | Decode PCM16 base64 → transcode to PCMU → send as Telnyx `media` event |
| `ping` | Respond with `pong` (after optional delay) |
| `interruption` | Send `clear` event to Telnyx to stop audio playback |
| `user_transcript` | Log transcription |
| `agent_response` | Log agent response |

## Production Deployment

For production, deploy behind a reverse proxy (nginx/Caddy) with TLS:

```nginx
# nginx example
location /media-stream {
    proxy_pass http://127.0.0.1:8765;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_read_timeout 86400;
}
```

Or use Caddy (automatic TLS):

```
your-domain.com {
    reverse_proxy /media-stream localhost:8765
}
```

## Troubleshooting

- **No audio from agent:** Check `ELEVENLABS_AGENT_ID` is correct and the agent exists
- **Garbled audio:** Ensure `stream_bidirectional_codec` is set to `PCMU` in your Telnyx call
- **Connection drops:** Check firewall allows WebSocket upgrades; increase proxy timeouts
- **One-way audio:** Ensure `stream_bidirectional_mode: "rtp"` is set in your Telnyx call

## License

MIT
