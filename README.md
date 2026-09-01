# Wiki Video Pipeline

Topic → English Wikipedia → Deep Synchronized Narration → Local MP4 → Optional YouTube Upload.

Supports both **Short (9:16 vertical)** and **Full Documentary Video (16:9 widescreen)** formats.

---

## 🎙️ Narration & Synchronization

* **Primary Engine**: **Google Cloud Text-to-Speech (Chirp 3 HD)**
  * Voice: `en-US-Chirp3-HD-Fenrir` (Deep, calm, documentary narrator voice).
  * High-definition expressive speech with human-like breathing and natural pauses.
* **Backup Engine**: **Microsoft Edge-TTS**
  * Voice: `en-US-ChristopherNeural` (Pitch: `-8Hz`, Rate: `-4%`).
  * Automatic zero-cost fallback if GCP credentials/quota are unavailable.
* **Synchronization**: Frame-accurate chapter slide and imagery synchronization via `narration_segments.json`.

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

Ensure your `GOOGLE_API_KEY` is present in `.env`:
```ini
GOOGLE_API_KEY=AIzaSyAbVP...
GROQ_API_KEY=gsk_...
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
* **`.env`**: API keys for Google Cloud TTS, LLMs, and YouTube tokens.
