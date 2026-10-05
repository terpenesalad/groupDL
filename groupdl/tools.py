"""Locate the helper programs yt-dlp needs: ffmpeg (merging/conversion) and a JS runtime (YouTube)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys


def _bundle_dir() -> str | None:
    """Directory PyInstaller unpacked the app into, if running as a frozen build."""
    return getattr(sys, "_MEIPASS", None)


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_ffmpeg() -> str | None:
    """Return a path to ffmpeg.

    Order: the copy bundled with a release build, then one installed on the system,
    then imageio-ffmpeg's copy as a last resort (it can crash on some stream types).
    """
    bundle = _bundle_dir()
    if bundle:
        candidate = os.path.join(bundle, "bin", _exe("ffmpeg"))
        if os.path.isfile(candidate):
            return candidate
    env = os.environ.get("GROUPDL_FFMPEG")
    if env and os.path.isfile(env):
        return env
    system = shutil.which("ffmpeg")
    if system:
        return system
    try:
        import imageio_ffmpeg  # type: ignore

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return None


def find_deno() -> str | None:
    """Return a path to deno, which yt-dlp uses to solve YouTube's JavaScript challenges."""
    bundle = _bundle_dir()
    if bundle:
        candidate = os.path.join(bundle, "bin", _exe("deno"))
        if os.path.isfile(candidate):
            return candidate
    try:
        import deno  # type: ignore

        path = deno.find_deno_bin()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return shutil.which("deno")


def ytdlp_runtime_opts() -> dict:
    """yt-dlp options pointing at the helper programs we found."""
    opts: dict = {}
    ffmpeg = find_ffmpeg()
    if ffmpeg:
        opts["ffmpeg_location"] = ffmpeg
    deno = find_deno()
    if deno:
        opts["js_runtimes"] = {"deno": {"path": deno}}
    return opts


_NO_WINDOW = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW: no console flash on Windows


def run_ffmpeg(args: list[str], timeout: float = 600) -> subprocess.CompletedProcess:
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        raise RuntimeError("ffmpeg isn't available.")
    return subprocess.run([ffmpeg, "-hide_banner", *args], capture_output=True, text=True,
                          errors="replace", timeout=timeout, creationflags=_NO_WINDOW)


def media_streams(path: str) -> dict[str, int]:
    """Count the video and audio streams in a media file, using ffmpeg."""
    r = run_ffmpeg(["-i", path], timeout=60)  # exits non-zero (no output file); we only read the header
    counts = {"video": 0, "audio": 0}
    for kind in re.findall(r"^\s*Stream #\d+:\d+.*?: (Video|Audio):", r.stderr, re.MULTILINE):
        counts[kind.lower()] += 1
    return counts


def has_audio(path: str) -> bool:
    return media_streams(path)["audio"] > 0
