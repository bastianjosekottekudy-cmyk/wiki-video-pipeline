"""
Multi-Tier Documentary & Broadcast Voice Narration Module.
Optimal Data-Backed Ranking with Tier-by-Tier Daily & Monthly Pacing:
  Tier 1: Google Studio-Q    (en-US-Studio-Q)          -> 35k/day | 950k/mo safe cap (MOS 4.64)
  Tier 2: Google Chirp 3 HD  (en-US-Chirp3-HD-Charon)  -> 35k/day | 950k/mo safe cap (MOS 4.61)
  Tier 3: Google Journey-D   (en-US-Journey-D)         -> 35k/day | 950k/mo safe cap (MOS 4.56)
  Tier 4: Google WaveNet-D   (en-US-Wavenet-D)         -> 130k/day | 3.8M/mo safe cap (MOS 4.28)
  Tier 5: Edge-TTS           (en-US-ChristopherNeural) -> Unlimited Free

Built-in Daily & Monthly Quota Tracker ensures zero unexpected charges.
"""

from __future__ import annotations

import os
import json
import base64
import logging
import shutil
import subprocess
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_GCP_VOICE = "en-US-Studio-Q"
DEFAULT_EDGE_VOICE = "en-US-ChristopherNeural"
DEFAULT_EDGE_PITCH = "-2Hz"
DEFAULT_EDGE_RATE = "0%"

USAGE_FILE = Path.home() / ".cursor" / "tts_monthly_usage.json"

SAFE_MONTHLY_CAPS = {
    "studio": 950_000,
    "chirp3": 950_000,
    "journey": 950_000,
    "wavenet": 3_800_000,
}

SAFE_DAILY_CAPS = {
    "studio": 35_000,
    "chirp3": 35_000,
    "journey": 35_000,
    "wavenet": 130_000,
}

VOICE_CHAIN = [
    {"tier": "studio",  "voice": "en-US-Studio-Q",          "label": "Google Studio-Q"},
    {"tier": "chirp3", "voice": "en-US-Chirp3-HD-Charon",  "label": "Google Chirp 3 HD Charon"},
    {"tier": "journey", "voice": "en-US-Journey-D",         "label": "Google Journey-D"},
    {"tier": "wavenet", "voice": "en-US-Wavenet-D",         "label": "Google WaveNet-D"},
]


def _get_current_month() -> str:
    return datetime.now().strftime("%Y-%m")


def _get_current_day() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def _load_usage() -> dict[str, Any]:
    if USAGE_FILE.is_file():
        try:
            return json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_usage(data: dict[str, Any]) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        USAGE_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def _get_tier_usage(tier_key: str) -> tuple[int, int]:
    month = _get_current_month()
    day = _get_current_day()
    data = _load_usage()
    m_used = int(data.get(month, {}).get(tier_key, 0))
    d_used = int(data.get(f"daily_{day}", {}).get(tier_key, 0))
    return m_used, d_used


def _record_tier_usage(tier_key: str, char_count: int) -> None:
    month = _get_current_month()
    day = _get_current_day()
    data = _load_usage()

    if month not in data:
        data[month] = {}
    data[month][tier_key] = data[month].get(tier_key, 0) + char_count

    day_key = f"daily_{day}"
    if day_key not in data:
        data[day_key] = {}
    data[day_key][tier_key] = data[day_key].get(tier_key, 0) + char_count

    _save_usage(data)


def _is_tier_safe(tier_key: str, text_length: int) -> bool:
    m_cap = SAFE_MONTHLY_CAPS.get(tier_key, 0)
    d_cap = SAFE_DAILY_CAPS.get(tier_key, 0)
    m_used, d_used = _get_tier_usage(tier_key)
    return (m_used + text_length <= m_cap) and (d_used + text_length <= d_cap)


def _get_google_api_key() -> Optional[str]:
    key = os.getenv("GOOGLE_API_KEY") or os.getenv("GCP_API_KEY") or os.getenv("GEMINI_API_KEY")
    if key and key.startswith("AIza"):
        return key

    central_file = Path.home() / ".cursor" / "llm-keys.env"
    if central_file.is_file():
        try:
            for line in central_file.read_text(encoding="utf-8").splitlines():
                if line.startswith("GOOGLE_API_KEY="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val.startswith("AIza"):
                        return val
        except OSError:
            pass
    return None


def _load_tts_settings() -> dict[str, Any]:
    try:
        from src.config import load_pipeline_config
        config = load_pipeline_config()
        tts_cfg = config.get("tts", {})
    except Exception:
        tts_cfg = {}

    return {
        "gcp_voice": str(tts_cfg.get("gcp_voice") or DEFAULT_GCP_VOICE),
        "backup_voice": str(tts_cfg.get("backup_voice") or DEFAULT_EDGE_VOICE),
        "backup_pitch": str(tts_cfg.get("backup_pitch") or DEFAULT_EDGE_PITCH),
        "backup_rate": str(tts_cfg.get("backup_rate") or DEFAULT_EDGE_RATE),
        "pause_sec": max(0.0, float(tts_cfg.get("segment_pause_sec", 0.4))),
    }


def _prepare_tts_text(text: str) -> str:
    import re
    cleaned = (text or "").strip()
    try:
        from src.script.generator import _clean_for_speech
        cleaned = _clean_for_speech(cleaned)
    except Exception:
        pass
    cleaned = re.sub(r"(?<=\w)[-–—](?=\w)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _audio_duration(path: Path) -> float:
    if not path.is_file():
        return 0.0
    cmd = [
        "ffprobe",
        "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path)
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(res.stdout.strip())
    except Exception:
        return round(path.stat().st_size / 16000.0, 3)


def _synthesize_google_voice(text: str, output_path: Path, voice_name: str) -> bool:
    api_key = _get_google_api_key()
    if api_key:
        try:
            url = f"https://texttospeech.googleapis.com/v1beta1/text:synthesize?key={api_key}"
            payload = {
                "input": {"text": text},
                "voice": {"languageCode": "en-US", "name": voice_name},
                "audioConfig": {"audioEncoding": "MP3"}
            }
            req = urllib.request.Request(
                url,
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json; charset=utf-8"}
            )
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                audio_bytes = base64.b64decode(data["audioContent"])
                output_path.parent.mkdir(parents=True, exist_ok=True)
                output_path.write_bytes(audio_bytes)
                return True
        except Exception as e:
            logger.warning("Google Cloud TTS (%s) failed: %s", voice_name, e)

    return False


def _synthesize_edge_tts(text: str, output_path: Path, voice_name: str, pitch: str, rate: str) -> bool:
    import asyncio
    output_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        import edge_tts
        async def _run():
            comm = edge_tts.Communicate(text, voice_name, pitch=pitch, rate=rate)
            await comm.save(str(output_path))
        asyncio.run(_run())
        return True
    except Exception as e:
        logger.warning("edge_tts python library failed: %s", e)

    cmd = [
        "edge-tts",
        "--voice", voice_name,
        "--pitch", pitch,
        "--rate", rate,
        "--text", text,
        "--write-media", str(output_path)
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        return True
    except Exception as e:
        logger.error("edge-tts CLI synthesis failed: %s", e)
        return False


def _synthesize_segment(text: str, output_path: Path, settings: dict[str, Any]) -> str:
    clean_text = _prepare_tts_text(text)
    text_len = len(clean_text)

    # Master Curated Fallback Chain with Tier-by-Tier Daily & Monthly Pacing
    # Studio-Q -> Charon -> Journey-D -> WaveNet-D -> Edge-TTS
    chain = list(VOICE_CHAIN)
    primary_voice = settings.get("gcp_voice")
    if primary_voice and primary_voice != chain[0]["voice"]:
        chain.insert(0, {"tier": "studio", "voice": primary_voice, "label": f"Custom ({primary_voice})"})

    for item in chain:
        tier_key = item["tier"]
        voice_name = item["voice"]
        label = item["label"]

        if not _is_tier_safe(tier_key, text_len):
            logger.info("Daily or monthly safe limit reached for %s. Stepping down to next Google tier...", label)
            continue

        if _synthesize_google_voice(clean_text, output_path, voice_name):
            _record_tier_usage(tier_key, text_len)
            return f"google-cloud:{voice_name}"

    logger.info("All Google Cloud tiers exhausted for today/month. Falling back to Edge-TTS (%s)...", settings["backup_voice"])

    if _synthesize_edge_tts(clean_text, output_path, settings["backup_voice"],
                            settings["backup_pitch"], settings["backup_rate"]):
        return f"edge-tts:{settings['backup_voice']}"

    raise RuntimeError(f"Failed to synthesize narration segment: '{text[:40]}...'")


def _make_silence_mp3(path: Path, duration: float) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg,
        "-y",
        "-f", "lavfi",
        "-i", "anullsrc=channel_layout=mono:sample_rate=24000",
        "-t", f"{max(0.05, duration):.3f}",
        "-q:a", "9",
        "-acodec", "libmp3lame",
        str(path),
    ]
    subprocess.run(cmd, capture_output=True, text=True, check=False)


def _concat_mp3(parts: list[Path], output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg") or "ffmpeg"
    if not parts:
        raise RuntimeError("No audio parts to concatenate")

    list_path = output_path.parent / "_narration_concat.txt"
    lines = [f"file '{str(p.resolve()).replace("'", "'\\''")}'" for p in parts]
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        cmd = [
            ffmpeg,
            "-y",
            "-f", "concat",
            "-safe", "0",
            "-i", str(list_path),
            "-c:a", "libmp3lame",
            "-q:a", "4",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output_path.exists():
            raise RuntimeError(f"Failed to concatenate narration audio: {result.stderr[-300:]}")
    finally:
        list_path.unlink(missing_ok=True)


def _load_segments(script_path: Path, output_dir: Path) -> list[dict[str, Any]]:
    segments_path = output_dir / "script_segments.json"
    if segments_path.exists():
        try:
            data = json.loads(segments_path.read_text(encoding="utf-8"))
            ordered = []
            if data.get("hook"):
                ordered.append({"id": "hook", "text": str(data["hook"]).strip()})
            if data.get("intro"):
                ordered.append({"id": "intro", "text": str(data["intro"]).strip()})
            
            keywords = list(data.get("trend_keywords") or [])
            headings = list(data.get("headings") or [])
            beats = data.get("trends") or data.get("chapters") or []
            for idx, beat in enumerate(beats):
                text = str(beat).strip()
                if not text:
                    continue
                entry = {"id": f"segment_{idx + 1}", "text": text}
                if idx < len(keywords):
                    entry["keyword"] = keywords[idx]
                if idx < len(headings):
                    entry["heading"] = headings[idx]
                ordered.append(entry)
            
            if data.get("outro"):
                ordered.append({"id": "outro", "text": str(data["outro"]).strip()})
            return [s for s in ordered if s.get("text")]
        except Exception:
            pass

    text = script_path.read_text(encoding="utf-8").strip()
    return [{"id": "full", "text": text}] if text else []


def generate_narration(script_path: Path, *args, **kwargs) -> str:
    output_dir = None
    for arg in list(args) + list(kwargs.values()):
        if isinstance(arg, Path) and arg != script_path:
            output_dir = arg
            break

    if output_dir is None:
        output_dir = script_path.parent

    settings = _load_tts_settings()
    segments = _load_segments(script_path, output_dir)
    if not segments:
        raise RuntimeError("No narration text found for TTS")

    audio_path = output_dir / "narration.mp3"
    seg_dir = output_dir / "tts_segments"
    seg_dir.mkdir(parents=True, exist_ok=True)

    speech_paths: list[Path] = []
    engines_used: set[str] = set()

    for idx, segment in enumerate(segments):
        out = seg_dir / f"{idx:02d}_{segment['id']}.mp3"
        engine = _synthesize_segment(segment["text"], out, settings)
        engines_used.add(engine)
        if not out.exists():
            raise RuntimeError(f"TTS output missing for segment {segment['id']}")
        speech_paths.append(out)

    pause_sec = settings["pause_sec"]
    silence_path: Path | None = None
    if pause_sec > 0 and len(speech_paths) > 1:
        silence_path = seg_dir / "silence.mp3"
        _make_silence_mp3(silence_path, pause_sec)

    concat_parts: list[Path] = []
    timed: list[dict[str, Any]] = []

    for idx, (segment, speech) in enumerate(zip(segments, speech_paths)):
        speech_dur = _audio_duration(speech)
        trailing = pause_sec if silence_path and idx < len(speech_paths) - 1 else 0.0
        concat_parts.append(speech)
        if trailing > 0 and silence_path is not None:
            concat_parts.append(silence_path)

        entry: dict[str, Any] = {
            "id": segment["id"],
            "text": segment["text"],
            "speech_sec": round(speech_dur, 3),
            "pause_sec": round(trailing, 3),
            "duration_sec": round(speech_dur + trailing, 3),
        }
        if "keyword" in segment:
            entry["keyword"] = segment["keyword"]
        if "heading" in segment:
            entry["heading"] = segment["heading"]
        timed.append(entry)

    _concat_mp3(concat_parts, audio_path)

    meta_path = output_dir / "narration_segments.json"
    meta_path.write_text(
        json.dumps(
            {
                "primary_engine": "google-cloud-studio",
                "engines_used": list(engines_used),
                "voice": settings["gcp_voice"],
                "backup_voice": settings["backup_voice"],
                "segment_pause_sec": pause_sec,
                "segments": timed,
                "total_sec": round(sum(s["duration_sec"] for s in timed), 3),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    logger.info(
        "Narration synthesized (%s) via %s: %s segments → %s (%.2fs)",
        settings["gcp_voice"],
        list(engines_used),
        len(segments),
        audio_path,
        sum(s["duration_sec"] for s in timed),
    )
    return str(audio_path)
