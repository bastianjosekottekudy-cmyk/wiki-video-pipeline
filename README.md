# Wiki Video Pipeline

Topic → English Wikipedia → Deep Synchronized Narration → Local MP4 → Optional YouTube Upload.

Supports both **Short (9:16 vertical)** and **Full Documentary Video (16:9 widescreen)** formats.

---

## 🎙️ Narration & Voice Architecture

* **Primary Narration Engine**: **Google Cloud Text-to-Speech (`en-US-Studio-Q`)**
  * **Studio-Q** (MOS 4.64): Highest-rated studio voice for documentary narration, historical storytelling, and educational videos.
* **Master Tier-by-Tier Fallback Chain**:
  1. **Google Studio-Q** (`en-US-Studio-Q`) — Premium studio documentary voice.
  2. **Google Chirp 3 HD** (`en-US-Chirp3-HD-Charon`) — Authoritative deep documentary narrator.
  3. **Google Journey** (`en-US-Journey-D`) — Expressive conversational storytelling.
  4. **Google WaveNet** (`en-US-Wavenet-D`) — High-reliability voice with 4M monthly character free tier.
  5. **Microsoft Edge-TTS** (`en-US-ChristopherNeural`, pitch `-2Hz`) — Automatic zero-cost unlimited fallback.
* **🛡️ Dual-Shield Quota Protection (Zero Surprise Billing)**:
  * **Shield 1 (Live Pricing Audit)**: Automatic live pricing verification against Google Cloud TTS pricing endpoints.
  * **Shield 2 (Safe Caps & Daily Pacing)**: Strict 95% monthly threshold (50,000 character buffer before limits) and daily pacing caps (`~/.cursor/tts_quota_config.json`, `~/.cursor/tts_monthly_usage.json`). Seamlessly steps down through Google tiers to Edge-TTS before any paid tier is touched.
* **Synchronization**: Frame-accurate chapter slide and imagery synchronization via `narration_segments.json`.

---

## 🔐 Multi-Client YouTube OAuth

* **Client Fallback Chain**: `primary` → `backup1` → `backup2` Desktop OAuth clients.
* **Token Resilience**: Auto-refreshing credentials synced with central store (`~/.agents/skills/google-auth/secrets/tokens/youtube`).
* **Web Auth Management**: Real-time token status, browser-based re-authorization, and quota failover from the local dashboard.

---

## 📦 Installation & Setup

### 1. Prerequisites
* **Python 3.11+**
* **FFmpeg & ffprobe** (`sudo pacman -S ffmpeg` or `sudo apt install ffmpeg`)

### 2. Environment Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure API Keys

Copy `.env.example` to `.env`:
```bash
cp .env.example .env
```

Configure your environment keys in `.env` (never committed to git):
```ini
GOOGLE_API_KEY=your_google_api_key
GROQ_API_KEY=your_groq_api_key
```

---

## 🚀 Running the Pipeline

### Start Web Dashboard
```bash
# Run server at http://127.0.0.1:8082
python3 -m src.main
```

### Manual Trigger CLI
```bash
# Generate 9:16 Short on a topic
python3 -m src.pipeline --topic "Voyager 1" --format short

# Generate 16:9 Full Documentary Video
python3 -m src.pipeline --topic "James Webb Space Telescope" --format video

# Test run with mock assets
python3 -m src.pipeline --topic "Quantum computing" --format short --mock
```

---

## ⚙️ Configuration Files

* **`config/pipeline.yaml`**: Video dimensions (9:16 and 16:9 profiles), TTS voice presets, and YouTube upload metadata.
* **`.env`**: Local credentials for Google Cloud TTS, LLMs, and YouTube tokens (gitignored).
