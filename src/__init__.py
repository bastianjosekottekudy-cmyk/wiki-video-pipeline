"""Wikipedia narrative video pipeline."""

from __future__ import annotations

import os
import shutil

# Ensure imageio-ffmpeg and moviepy always prefer system ffmpeg (enabling GPU/NVENC acceleration)
_system_ffmpeg = shutil.which("ffmpeg")
if _system_ffmpeg and "IMAGEIO_FFMPEG_EXE" not in os.environ:
    os.environ["IMAGEIO_FFMPEG_EXE"] = _system_ffmpeg
