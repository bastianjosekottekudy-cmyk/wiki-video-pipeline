# Wiki Video Pipeline

Topic → English Wikipedia → interesting narration → local MP4 → optional YouTube upload.

Same **LLM chain** and **YouTube OAuth clients** as [news-shorts-pipeline](../news-shorts-pipeline).

## Formats

- **Short** — 1080×1920, YouTube Shorts ceiling 180s
- **Video** — 1920×1080, duration follows the story

Dashboard: **http://127.0.0.1:8082**

## Prerequisites (Windows)

1. **Python 3.11+**
2. **FFmpeg**
3. LLM keys via the personal **llm-chain** skill
4. YouTube secrets via the personal **google-auth** skill

## Quick Start

```powershell
cd C:\Users\USER\Projects\wiki-video-pipeline
.\scripts\setup-windows.ps1
.\scripts\run.ps1
```

Open **http://127.0.0.1:8082**. Type a topic and generate a Short or a Video.

Start in the background and with Windows (same pattern as news-shorts):

```powershell
.\scripts\restart-app.ps1 -Background
.\scripts\restart-app.ps1 -RegisterStartup
```

## CLI

```powershell
.\.venv\Scripts\python.exe -m src.pipeline --topic "Voyager 1" --format short
.\.venv\Scripts\python.exe -m src.pipeline --topic "Voyager 1" --format video --upload
.\.venv\Scripts\python.exe -m src.pipeline --topic "Black holes" --format short --mock
```

## Secrets

```powershell
python "$env:USERPROFILE\.cursor\skills\llm-chain\scripts\sync_env.py" --project .
python "$env:USERPROFILE\.cursor\skills\llm-chain\scripts\install_module.py" --project . --package src.llm
python "$env:USERPROFILE\.cursor\skills\google-auth\scripts\sync.py" --project . --service youtube --write-yaml
```

Authorize each YouTube client once if needed:

```powershell
.\.venv\Scripts\python.exe -m src.youtube.auth --client primary
```

## License

Narration is adapted from Wikipedia (CC BY-SA 4.0). Uploads use YouTube’s Creative Commons license and credit the article plus image authors in the description.
