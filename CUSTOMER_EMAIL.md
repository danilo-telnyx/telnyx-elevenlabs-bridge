# Customer Email — Telnyx + ElevenLabs Integration

---

**Subject:** Telnyx + ElevenLabs Voice AI — Integration Options & Bridge Service

Hi [Customer Name],

Thanks for your interest in using ElevenLabs Conversational AI with Telnyx! There are two approaches depending on your needs:

---

## Option 1: Telnyx AI Assistant (Recommended — Easiest)

Telnyx offers a native **AI Assistant** feature that supports **ElevenLabs voices** out of the box — no custom code required.

- Configure an AI Assistant in the [Telnyx Mission Control Portal](https://portal.telnyx.com/)
- Select ElevenLabs as the TTS provider and choose your preferred voice
- Assign it to a phone number — calls are handled automatically

This is the fastest path to production with zero infrastructure to manage.

---

## Option 2: Custom Bridge (Full Control)

If you need deeper customization — such as using your own **ElevenLabs Conversational AI agent** with custom tools, knowledge bases, or conversation flows — we've built an open-source bridge service that connects the two platforms:

🔗 **GitHub:** [https://github.com/danilo-telnyx/telnyx-elevenlabs-bridge](https://github.com/danilo-telnyx/telnyx-elevenlabs-bridge)

### What it does

The bridge translates between Telnyx Media Streaming WebSocket and ElevenLabs Conversational AI WebSocket in real-time, handling:

- **Audio transcoding:** PCMU μ-law 8 kHz (Telnyx) ↔ PCM16 16 kHz (ElevenLabs)
- **Protocol translation:** Telnyx media events ↔ ElevenLabs conversation events
- **Interruption handling:** When the caller speaks over the agent, audio playback is cleared instantly
- **DTMF forwarding:** Keypad presses are sent to the agent as context

### Quick start

```bash
git clone https://github.com/danilo-telnyx/telnyx-elevenlabs-bridge.git
cd telnyx-elevenlabs-bridge
pip install -r requirements.txt
cp .env.example .env   # Add your ELEVENLABS_AGENT_ID
python bridge.py
```

Then point your Telnyx call's `stream_url` to the bridge's WebSocket endpoint. Full configuration details are in the README.

### Requirements

- Python 3.9+ (compatible through Python 3.13)
- A Telnyx account with Call Control
- An ElevenLabs Conversational AI agent

---

## Next Steps

Let me know which approach fits your use case best, and I'm happy to help with:

- Setting up the Telnyx AI Assistant with ElevenLabs voices
- Deploying and customizing the bridge service
- Any questions about the architecture or configuration

Looking forward to hearing from you!

Best regards,
Danilo
