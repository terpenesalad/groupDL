"""Locate the helper programs yt-dlp needs: ffmpeg (merging/conversion) and a JS runtime (YouTube)."""

from __future__ import annotations

import os
import shutil
import sys


def _bundle_dir() -> str | None:
    """Directory PyInstaller unpacked the app into, if running as a frozen build."""
    return getattr(sys, "_MEIPASS", None)


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


def find_ffmpeg() -> str | None:
    """Return a path to ffmpeg, preferring a copy bundled with the app."""
    bundle = _bundle_dir()
    if bundle:
        candidate = os.path.join(bundle, "bin", _exe("ffmpeg"))
        if os.path.isfile(candidate):
            return candidate
    try:
        import imageio_ffmpeg  # type: ignore

        path = imageio_ffmpeg.get_ffmpeg_exe()
        if path and os.path.isfile(path):
            return path
    except Exception:
        pass
    return shutil.which("ffmpeg")


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
