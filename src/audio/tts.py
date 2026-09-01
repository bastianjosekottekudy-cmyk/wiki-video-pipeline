"""
Dual-Engine Text-to-Speech Narration Module.
Primary: Google Cloud Text-to-Speech (Chirp 3 HD: en-US-Chirp3-HD-Fenrir)
Backup:  Microsoft Edge-TTS (en-US-ChristopherNeural with -8Hz pitch / -4% rate)

Synchronizes per-segment duration and metadata (narration_segments.json) for video rendering.
"""

from __future__ import annotations

import os
import json
import base64
import logging
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

DEFAULT_GCP_VOICE = "en-US-Chirp3-HD-Fenrir"
DEFAULT_EDGE_VOICE = "en-US-ChristopherNeural"
DEFAULT_EDGE_PITCH = "-8Hz"
DEFAULT_EDGE_RATE = "-4%"


def _get_google_api_key() -> Optional[str]:
    """Retrieves Google API Key from environment, .env, or central ~/.cursor/llm-keys.env"""
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
    """Fast, accurate audio duration probing using ffprobe."""
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


def _synthesize_google_chirp(text: str, output_path: Path, voice_name: str) -> bool:
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
            logger.warning("Google Cloud Chirp 3 HD REST synthesis failed: %s", e)

    try:
        from google.cloud import texttospeech
        client = texttospeech.TextToSpeechClient()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_params = texttospeech.VoiceSelectionParams(language_code="en-US", name=voice_name)
        audio_config = texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3)
        response = client.synthesize_speech(input=synthesis_input, voice=voice_params, audio_config=audio_config)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.audio_content)
        return True
    except Exception as e:
        logger.warning("Google Cloud SDK synthesis failed: %s", e)
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

    if _synthesize_google_chirp(clean_text, output_path, settings["gcp_voice"]):
        return "google-cloud-chirp3"

    logger.info("Falling back to Edge-TTS (%s, pitch=%s, rate=%s)...",
                settings["backup_voice"], settings["backup_pitch"], settings["backup_rate"])

    if _synthesize_edge_tts(clean_text, output_path, settings["backup_voice"],
                            settings["backup_pitch"], settings["backup_rate"]):
        return "edge-tts"

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


def generate_narration(
    script_path: Path,
    *args,
    **kwargs
) -> str:
    """
    Universal generate_narration entrypoint compatible with all pipelines.
    Accepts (script_path, output_dir) or (script_path, section/country, output_dir).
    """
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
                "primary_engine": "google-cloud-chirp3",
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
