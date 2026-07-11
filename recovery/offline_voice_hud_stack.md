# Offline Voice + HUD Communication Stack
Source: ChatGPT recovery chat 33

**Canonical relationship:**
- [EXPANDS §6 application] operator-facing voice channel (alternative to chat-only UI)
- [DIVERGES from] `feedback_vm_deployment_model` memory: that pins LM Studio primary; this captures Ollama-based alternative
- [NEW] HUD overlay contract for OBS / browser source
- [NEW] air-gapped voice loop — no internet, no cloud API, fully local

## Offline voice loop
```
Mic → Whisper.cpp (STT) → Ollama (local LLM) → Piper TTS → audio out
                                ↑
                          No internet
                                ↓
                       HUD overlay updates
```

## Stack components
| Role | Tool | Notes |
|---|---|---|
| Speech → Text | Whisper.cpp | `base.en` model; ~140MB; offline |
| AI brain | Ollama | `llama3` or `mistral`; offline once pulled |
| Voice output | Piper TTS | `en_US-lessac-medium.onnx`; offline |
| Control layer | Python / Node | Bridge orchestrator |
| HUD | HTML overlay (Browser source in OBS) | Reads `samus_status.json` |

**Architectural rule:** No outbound network calls after install. All models, voices, weights bundled at deploy time.

## HUD status contract
Python writes `samus_status.json` after each turn:
```json
{
  "state": "SAMUS ONLINE | LISTENING | RESPONDING | OFFLINE",
  "reply": "<last samus utterance>",
  "user_text": "<last transcription>",
  "mic_level": 0.0,
  "tx_active": false,
  "rx_active": true,
  "link_state": "STABLE | DEGRADED | DOWN"
}
```

HUD polls or watches file → updates banner / chat panel / mic meter.

## HUD overlay (HTML structure)
Banner: `LET'S WRAP // SAMUS → CHAT` (Variant B selected)
Layout:
- **Top-left**: status banner + LED dot
- **Bottom-left**: mic level meter + TX/RX pills + link state
- **Bottom-right**: chat panel (8-message ring)
- **Corner brackets** for sci-fi framing

Fonts: Orbitron (titles) + Rajdhani (body). All caps, wide letter-spacing.

## Bridge orchestrator (samus_core.py skeleton)
```python
import subprocess, json, os

def transcribe():
    return subprocess.check_output([
        "./whisper/main", "-m", "./whisper/models/ggml-base.en.bin",
        "-f", "mic.wav", "-nt",
    ]).decode().strip()

def ask_samus(text):
    p = subprocess.Popen(["ollama", "run", "llama3"],
                         stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    out, _ = p.communicate(text)
    return out.strip()

def speak(text):
    subprocess.run(["piper", "--model", "en_US-lessac-medium.onnx",
                    "--output_file", "out.wav"], input=text, text=True)
    os.system("aplay out.wav")    # Linux; Windows uses Start-Process

def update_hud(state, reply, user_text=""):
    with open("samus_status.json", "w") as f:
        json.dump({"state": state, "reply": reply, "user_text": user_text}, f)
```

## Windows install (PowerShell, run as admin)
1. Install Chocolatey (one-time)
2. `choco install -y git python ffmpeg cmake`
3. Download Ollama installer → run → `ollama pull llama3`
4. Clone whisper.cpp → build → download `ggml-base.en.bin`
5. Clone piper → download `en_US-lessac-medium.onnx`
6. Generate `samus.py` bridge script
7. Run: `python samus.py`

## Security guarantees
- ✔ No internet calls after install
- ✔ No APIs / no telemetry
- ✔ Fully inspectable Python bridge
- ✔ Can be air-gapped post-install
- ✔ Samus only knows what operator tells her (no retrieval-augmented external sources)

## Roadmap upgrades (chat 33 next-step options)
- Push-to-talk hotkey
- Wake word ("Samus")
- Voice activity detection (VAD)
- HUD waveform animation (mic input visualization)
- Local memory (JSON-backed conversation history)
- Character personality / tactical-vs-assistant modes
- Emotion/priority tones in TTS output
- Background-service mode (systemd / Windows service)

## Relationship to canonical LLM choice
Canonical/memory says **LM Studio primary + Anthropic fallback for all 5 agents**.
This chat documents an **alternative Ollama-based stack** for operator-facing voice
interaction. Both can coexist:
- LM Studio = inference backend for agent cognitive loops (model_extended plane)
- Ollama = inference backend for operator chat/voice UI (application plane)
- Anthropic API = paid fallback for both
