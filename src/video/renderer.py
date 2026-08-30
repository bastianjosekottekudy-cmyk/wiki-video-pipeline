"""Slide + narration renderer for Short (9:16) and Video (16:9)."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

# Ensure moviepy / imageio-ffmpeg uses system ffmpeg (supporting NVENC / hardware acceleration)
_system_ffmpeg = shutil.which("ffmpeg")
if _system_ffmpeg and "IMAGEIO_FFMPEG_EXE" not in os.environ:
    os.environ["IMAGEIO_FFMPEG_EXE"] = _system_ffmpeg

from PIL import Image, ImageDraw, ImageEnhance, ImageFont

from src.config import format_profile, load_pipeline_config
from src.naming import build_video_title, video_filename

logger = logging.getLogger(__name__)


def _get_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = (
        [
            "C:/Windows/Fonts/segoeuib.ttf",
            "C:/Windows/Fonts/arialbd.ttf",
            "C:/Windows/Fonts/calibrib.ttf",
        ]
        if bold
        else [
            "C:/Windows/Fonts/segoeui.ttf",
            "C:/Windows/Fonts/arial.ttf",
            "C:/Windows/Fonts/calibri.ttf",
        ]
    )
    candidates += [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        if bold
        else "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    ]
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _cover_resize(img: Image.Image, width: int, height: int) -> Image.Image:
    src_w, src_h = img.size
    scale = max(width / src_w, height / src_h)
    new_w, new_h = int(src_w * scale), int(src_h * scale)
    img = img.resize((new_w, new_h), Image.Resampling.LANCZOS)
    left = (new_w - width) // 2
    top = (new_h - height) // 2
    return img.crop((left, top, left + width, top + height))


def _draw_gradient(draw: ImageDraw.ImageDraw, width: int, height: int) -> None:
    for y in range(height // 3):
        alpha = int(180 * (1 - y / (height / 3)))
        draw.rectangle([(0, y), (width, y + 1)], fill=(0, 0, 0, alpha))
    for i, y in enumerate(range(height - int(height * 0.5), height)):
        alpha = min(230, int(240 * (i / (height * 0.5))))
        draw.rectangle([(0, y), (width, y + 1)], fill=(0, 0, 0, alpha))


def _wrap_text(
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    draw: ImageDraw.ImageDraw,
) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _draw_text_with_shadow(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.ImageFont,
    fill: tuple[int, int, int],
    shadow: tuple[int, int, int] = (0, 0, 0),
) -> None:
    x, y = xy
    for dx, dy in ((3, 3), (2, 2), (-1, 2)):
        draw.text((x + dx, y + dy), text, font=font, fill=shadow)
    draw.text((x, y), text, font=font, fill=fill)


def _draw_title_block(
    draw: ImageDraw.ImageDraw,
    width: int,
    height: int,
    title: str,
    subtitle: str,
    *,
    label: str,
    vertical: bool,
) -> None:
    margin = 48 if not vertical else 56
    title_size = 48 if not vertical else 54
    sub_size = 28 if not vertical else 30
    title_font = _get_font(title_size, bold=True)
    sub_font = _get_font(sub_size, bold=False)
    label_font = _get_font(24, bold=True)
    max_text_width = width - margin * 2
    label_y = int(height * (0.62 if not vertical else 0.55))
    _draw_text_with_shadow(draw, (margin, label_y), label, label_font, fill=(180, 210, 255))
    title_y = label_y + 40
    title_lines = _wrap_text(title, title_font, max_text_width, draw)[:4]
    line_h = 58 if vertical else 54
    for i, line in enumerate(title_lines):
        _draw_text_with_shadow(
            draw,
            (margin, title_y + i * line_h),
            line,
            title_font,
            fill=(255, 255, 255),
        )
    if subtitle:
        sub_y = title_y + len(title_lines) * line_h + 16
        for i, line in enumerate(_wrap_text(subtitle, sub_font, max_text_width, draw)[:2]):
            _draw_text_with_shadow(
                draw,
                (margin, sub_y + i * 36),
                line,
                sub_font,
                fill=(230, 235, 240),
            )
    draw.rectangle([(0, height - 10), (width, height)], fill=(120, 170, 230))


def _make_solid_slide(
    width: int,
    height: int,
    title: str,
    subtitle: str,
    label: str,
) -> Image.Image:
    img = Image.new("RGB", (width, height), color=(14, 20, 34))
    draw = ImageDraw.Draw(img, "RGBA")
    for y in range(220):
        alpha = int(50 * (1 - y / 220))
        draw.rectangle([(0, y), (width, y + 1)], fill=(40, 80, 140, alpha))
    draw_rgb = ImageDraw.Draw(img)
    _draw_title_block(
        draw_rgb, width, height, title, subtitle, label=label, vertical=height > width
    )
    return img.convert("RGB")


def _make_image_slide(
    width: int,
    height: int,
    image_path: str,
    title: str,
    subtitle: str,
    label: str,
) -> Image.Image:
    try:
        base = Image.open(image_path).convert("RGB")
        base = _cover_resize(base, width, height)
        base = ImageEnhance.Brightness(base).enhance(0.68)
        base = ImageEnhance.Contrast(base).enhance(1.08)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not open image %s: %s", image_path, exc)
        return _make_solid_slide(width, height, title, subtitle, label)

    overlay = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    _draw_gradient(draw, width, height)
    composed = Image.alpha_composite(base.convert("RGBA"), overlay).convert("RGB")
    draw2 = ImageDraw.Draw(composed)
    _draw_title_block(
        draw2, width, height, title, subtitle, label=label, vertical=height > width
    )
    return composed


def _nvenc_available() -> bool:
    try:
        import imageio_ffmpeg

        ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    try:
        enc = subprocess.run(
            [ffmpeg, "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if "h264_nvenc" not in (enc.stdout or ""):
            return False
        gpu = subprocess.run(
            ["nvidia-smi", "-L"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if gpu.returncode != 0 or not bool(gpu.stdout.strip()):
            return False
        # Quick test to ensure nvenc encoder is fully functional with current ffmpeg
        test = subprocess.run(
            [
                ffmpeg,
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=256x256:d=0.04",
                "-c:v",
                "h264_nvenc",
                "-preset",
                "p4",
                "-rc",
                "vbr",
                "-cq",
                "23",
                "-b:v",
                "0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            timeout=10,
            check=False,
        )
        return test.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _resolve_encoder(video_cfg: dict[str, Any]) -> tuple[str, list[str]]:
    preferred = str(video_cfg.get("codec", "auto")).lower()
    if preferred in ("h264_nvenc", "nvenc", "auto") and _nvenc_available():
        logger.info("Using NVIDIA NVENC (GPU) for video encode")
        return "h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", "23", "-b:v", "0"]
    if preferred == "h264_nvenc":
        logger.warning("h264_nvenc requested but unavailable; falling back to libx264")
    logger.info("Using CPU libx264 for video encode")
    return "libx264", ["-preset", "veryfast", "-crf", "23"]


def _safe_write_videofile(video: Any, target_path: Path, write_kwargs: dict[str, Any]) -> None:
    try:
        video.write_videofile(str(target_path), **write_kwargs)
    except Exception as exc:
        if write_kwargs.get("codec") == "h264_nvenc":
            logger.warning("NVENC encode failed (%s); falling back to CPU libx264...", exc)
            fallback_kwargs = dict(write_kwargs)
            fallback_kwargs["codec"] = "libx264"
            fallback_kwargs["threads"] = 4
            fallback_kwargs["ffmpeg_params"] = ["-preset", "veryfast", "-crf", "23"]
            video.write_videofile(str(target_path), **fallback_kwargs)
        else:
            raise


def _load_segment_durations(output_dir: Path) -> list[dict[str, Any]] | None:
    meta_path = output_dir / "narration_segments.json"
    if not meta_path.exists():
        return None
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    segments = data.get("segments")
    if not isinstance(segments, list) or not segments:
        return None
    return segments


def _ffmpeg_concat(parts: list[Path], output_path: Path) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg is required to concatenate video parts")
    list_path = output_path.parent / "_video_concat.txt"
    lines = []
    for part in parts:
        escaped = str(part.resolve()).replace("\\", "/").replace("'", "'\\''")
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
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "23",
                "-c:a",
                "aac",
                str(output_path),
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            if result.returncode != 0 or not output_path.exists():
                raise RuntimeError(f"ffmpeg concat failed: {result.stderr[-400:]}")
    finally:
        list_path.unlink(missing_ok=True)


def render_video(
    wiki_title: str,
    fmt: str,
    run_date: str,
    audio_path: str,
    output_dir: Path,
    *,
    script: dict[str, Any] | None = None,
    image_paths: list[str] | None = None,
) -> str:
    config = load_pipeline_config()
    video_cfg = config.get("video", {})
    profile = format_profile(fmt)
    width = int(profile.get("width") or 1080)
    height = int(profile.get("height") or 1920)
    fps = int(video_cfg.get("fps", 24))
    max_duration = float(profile.get("max_video_duration_sec") or 0)
    concat_after = int(video_cfg.get("concat_after_clips") or 12)
    title = build_video_title(wiki_title, fmt, run_date)
    label = "WIKI SHORT" if fmt == "short" else "WIKIPEDIA"
    subtitle = str((script or {}).get("title") or wiki_title)
    codec, ffmpeg_params = _resolve_encoder(video_cfg)
    image_paths = [p for p in (image_paths or []) if p]
    chapters = list((script or {}).get("chapters") or [])

    from moviepy import AudioFileClip, ImageClip, concatenate_videoclips

    slides_dir = output_dir / "slides"
    slides_dir.mkdir(exist_ok=True)
    groups: list[list[Path]] = []
    img_i = 0

    def _reuse_image() -> str | None:
        nonlocal img_i
        if not image_paths:
            return None
        path = image_paths[img_i % len(image_paths)]
        img_i += 1
        return path

    def _compose_slide(dest: Path, heading: str, sub: str, still: str | None) -> Path:
        if still:
            slide = _make_image_slide(width, height, still, heading, sub, label)
        else:
            slide = _make_solid_slide(width, height, heading, sub, label)
        slide.save(dest, optimize=True)
        return dest

    intro_path = _compose_slide(
        slides_dir / "00_intro.png",
        subtitle,
        "From Wikipedia",
        _reuse_image(),
    )
    groups.append([intro_path])

    if not chapters:
        chapters = [{"heading": wiki_title, "narration": ""}]
    for idx, chapter in enumerate(chapters, start=1):
        heading = str(chapter.get("heading") or wiki_title)
        n_stills = 2 if fmt == "video" and len(image_paths) >= 2 else 1
        group: list[Path] = []
        for j in range(n_stills):
            path = _compose_slide(
                slides_dir / f"{idx:02d}_ch_{j + 1}.png",
                heading,
                wiki_title,
                _reuse_image(),
            )
            group.append(path)
        groups.append(group)

    outro_path = _compose_slide(
        slides_dir / "99_outro.png",
        "Thanks for watching",
        "Adapted from Wikipedia · CC BY-SA",
        _reuse_image(),
    )
    groups.append([outro_path])

    audio = AudioFileClip(audio_path)
    audio_duration = float(audio.duration)
    if max_duration > 0 and audio_duration > max_duration:
        logger.warning(
            "Audio %.1fs exceeds Shorts cap %.1fs — trimming",
            audio_duration,
            max_duration,
        )
        audio = audio.subclipped(0, max_duration)
        audio_duration = max_duration

    timed_segments = _load_segment_durations(output_dir)
    clip_specs: list[tuple[Path, float]] = []
    if timed_segments and len(timed_segments) == len(groups):
        for group, segment in zip(groups, timed_segments):
            group_dur = float(segment.get("duration_sec") or 0.0)
            if group_dur <= 0:
                group_dur = max(audio_duration / len(groups), 1.2)
            per_slide = max(group_dur / len(group), 0.35)
            for path in group:
                clip_specs.append((path, per_slide))
    else:
        if timed_segments:
            logger.warning(
                "Segment count (%s) != slide groups (%s); equal timing",
                len(timed_segments),
                len(groups),
            )
        all_paths = [p for g in groups for p in g]
        per_slide = max(audio_duration / len(all_paths), 1.2)
        clip_specs = [(path, per_slide) for path in all_paths]

    output_path = output_dir / video_filename(wiki_title, fmt, run_date)
    write_kwargs: dict[str, Any] = {
        "fps": fps,
        "codec": codec,
        "audio_codec": "aac",
        "logger": None,
        "ffmpeg_params": ffmpeg_params,
    }
    if codec == "libx264":
        write_kwargs["threads"] = 4

    if len(clip_specs) > concat_after:
        parts_dir = output_dir / "video_parts"
        parts_dir.mkdir(exist_ok=True)
        part_files: list[Path] = []
        cursor = 0.0
        chunk: list[tuple[Path, float]] = []
        chunk_dur = 0.0
        chunk_i = 0

        def flush_chunk(items: list[tuple[Path, float]], index: int, start_t: float) -> float:
            clips = [
                ImageClip(str(path)).with_duration(dur).with_fps(fps) for path, dur in items
            ]
            video = concatenate_videoclips(clips, method="compose")
            end_t = start_t + float(video.duration or 0)
            audio_slice = audio.subclipped(start_t, min(end_t, audio_duration))
            video = video.with_audio(audio_slice)
            if video.duration and video.duration > float(audio_slice.duration or 0):
                video = video.subclipped(0, float(audio_slice.duration))
            part_path = parts_dir / f"part_{index:02d}.mp4"
            _safe_write_videofile(video, part_path, write_kwargs)
            video.close()
            part_files.append(part_path)
            return end_t

        for spec in clip_specs:
            if chunk and len(chunk) >= concat_after:
                cursor = flush_chunk(chunk, chunk_i, cursor)
                chunk_i += 1
                chunk = []
            chunk.append(spec)
        if chunk:
            flush_chunk(chunk, chunk_i, cursor)
        audio.close()
        _ffmpeg_concat(part_files, output_path)
        logger.info("Wrote %s via ffmpeg concat (%s, %.1fs)", fmt, codec, audio_duration)
        return str(output_path)

    clips = [
        ImageClip(str(path)).with_duration(dur).with_fps(fps) for path, dur in clip_specs
    ]
    video = concatenate_videoclips(clips, method="compose")
    video = video.with_audio(audio)
    if video.duration and video.duration > audio_duration:
        video = video.subclipped(0, audio_duration)
    _safe_write_videofile(video, output_path, write_kwargs)
    logger.info("Wrote %s (%s, %.1fs): %s", fmt, codec, audio_duration, output_path)
    video.close()
    audio.close()
    return str(output_path)
