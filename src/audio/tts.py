"""Text-to-speech via edge-tts with per-segment timing."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any

from src.config import load_pipeline_config

logger = logging.getLogger(__name__)


def _tts_settings() -> tuple[str, str, float]:
    config = load_pipeline_config()
    tts_cfg = config.get("tts", {})
    voice = str(tts_cfg.get("voice") or "en-US-JennyNeural")
    rate = str(tts_cfg.get("rate", "-8%"))
    pause = float(tts_cfg.get("segment_pause_sec", 0.4))
    return voice, rate, max(0.0, pause)


def _load_segments(script_path: Path, output_dir: Path) -> list[dict[str, Any]]:
    segments_path = output_dir / "script_segments.json"
    if segments_path.exists():
        data = json.loads(segments_path.read_text(encoding="utf-8"))
        ordered: list[dict[str, Any]] = [
            {"id": "hook", "text": str(data.get("hook") or "").strip()}
        ]
        headings = list(data.get("headings") or [])
        for idx, beat in enumerate(data.get("chapters") or []):
            text = str(beat).strip()
            if not text:
                continue
            heading = headings[idx] if idx < len(headings) else f"Chapter {idx + 1}"
            ordered.append(
                {
                    "id": f"chapter_{idx + 1}",
                    "heading": heading,
                    "text": text,
                }
            )
        ordered.append({"id": "outro", "text": str(data.get("outro") or "").strip()})
        return [s for s in ordered if s.get("text")]

    text = script_path.read_text(encoding="utf-8").strip()
    return [{"id": "full", "text": text}] if text else []


def _prepare_tts_text(text: str) -> str:
    import re

    from src.script.generator import _clean_for_speech

    cleaned = _clean_for_speech(text or "")
    cleaned = re.sub(r"(?<=\w)[-–—](?=\w)", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


async def _generate_async(text: str, voice: str, output_path: Path, rate: str) -> None:
    import edge_tts

    speak = _prepare_tts_text(text)
    communicate = edge_tts.Communicate(speak, voice, rate=rate)
    await communicate.save(str(output_path))


def _audio_duration(path: Path) -> float:
    from moviepy import AudioFileClip

    clip = AudioFileClip(str(path))
    try:
        return float(clip.duration or 0.0)
    finally:
        clip.close()


def _make_silence_mp3(path: Path, duration: float) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to build narration pauses")
    if duration <= 0:
        duration = 0.05
    cmd = [
        ffmpeg,
        "-y",
        "-f",
        "lavfi",
        "-i",
        "anullsrc=channel_layout=mono:sample_rate=24000",
        "-t",
        f"{duration:.3f}",
        "-q:a",
        "9",
        "-acodec",
        "libmp3lame",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not path.exists():
        raise RuntimeError(f"Failed to create silence audio: {result.stderr[-400:]}")


def _concat_mp3(parts: list[Path], output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to concatenate narration")
    if not parts:
        raise RuntimeError("No audio parts to concatenate")

    list_path = output_path.parent / "_narration_concat.txt"
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("'", "'\\''")
        lines.append(f"file '{escaped}'")
    list_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        cmd = [
            ffmpeg,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(list_path),
            "-c",
            "copy",
            str(output_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if result.returncode != 0 or not output_path.exists():
            cmd = [
                ffmpeg,
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "4",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not output_path.exists():
                raise RuntimeError(
                    f"Failed to concatenate narration: {result.stderr[-400:]}"
                )
    finally:
        list_path.unlink(missing_ok=True)


def generate_narration(script_path: Path, output_dir: Path) -> str:
    voice, rate, pause_sec = _tts_settings()
    segments = _load_segments(script_path, output_dir)
    if not segments:
        raise RuntimeError("No narration text found for TTS")

    audio_path = output_dir / "narration.mp3"
    seg_dir = output_dir / "tts_segments"
    seg_dir.mkdir(exist_ok=True)

    speech_paths: list[Path] = []
    for idx, segment in enumerate(segments):
        out = seg_dir / f"{idx:02d}_{segment['id']}.mp3"
        try:
            asyncio.run(_generate_async(segment["text"], voice, out, rate=rate))
        except Exception as exc:
            logger.error("TTS failed for segment %s: %s", segment["id"], exc)
            raise RuntimeError("Text-to-speech generation failed") from exc
        if not out.exists():
            raise RuntimeError(f"TTS output missing for {segment['id']}")
        speech_paths.append(out)

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
        if "heading" in segment:
            entry["heading"] = segment["heading"]
        timed.append(entry)

    _concat_mp3(concat_parts, audio_path)
    (output_dir / "narration_segments.json").write_text(
        json.dumps(
            {
                "voice": voice,
                "rate": rate,
                "segment_pause_sec": pause_sec,
                "segments": timed,
                "total_sec": round(sum(s["duration_sec"] for s in timed), 3),
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    if not audio_path.exists():
        raise RuntimeError("TTS output file was not created")
    logger.info("Narration ready: %s segments → %s", len(segments), audio_path)
    return str(audio_path)
