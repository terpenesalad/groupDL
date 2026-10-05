"""Download a static ffmpeg build for this computer, for bundling into groupDL.

Windows and Linux use yt-dlp's own patched builds; macOS (Apple silicon) uses
Martin Riedl's signed static builds. Prints the path of the extracted ffmpeg.

Usage:  python scripts/fetch_ffmpeg.py [dest_dir]   (default: build/ffmpeg)
"""

from __future__ import annotations

import io
import os
import platform
import stat
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

YTDLP = "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
SOURCES = {
    ("Windows", "AMD64"): YTDLP + "ffmpeg-master-latest-win64-gpl.zip",
    ("Windows", "ARM64"): YTDLP + "ffmpeg-master-latest-winarm64-gpl.zip",
    ("Linux", "x86_64"): YTDLP + "ffmpeg-master-latest-linux64-gpl.tar.xz",
    ("Linux", "aarch64"): YTDLP + "ffmpeg-master-latest-linuxarm64-gpl.tar.xz",
    ("Darwin", "arm64"): "https://ffmpeg.martin-riedl.de/redirect/latest/macos/arm64/release/ffmpeg.zip",
    ("Darwin", "x86_64"): "https://ffmpeg.martin-riedl.de/redirect/latest/macos/amd64/release/ffmpeg.zip",
}


def main() -> None:
    dest = Path(sys.argv[1] if len(sys.argv) > 1 else "build/ffmpeg")
    dest.mkdir(parents=True, exist_ok=True)
    key = (platform.system(), platform.machine())
    url = SOURCES.get(key)
    if not url:
        raise SystemExit(f"No ffmpeg build known for {key}")
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    print(f"Downloading {url}", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "groupDL-build"})
    with urllib.request.urlopen(req, timeout=600) as r:
        data = r.read()

    out = dest / exe
    if url.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            member = next(n for n in z.namelist() if n.rsplit("/", 1)[-1] == exe)
            out.write_bytes(z.read(member))
    else:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:xz") as t:
            member = next(m for m in t.getmembers() if m.name.rsplit("/", 1)[-1] == exe and m.isfile())
            out.write_bytes(t.extractfile(member).read())
    out.chmod(out.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    print(out.resolve())


if __name__ == "__main__":
    main()
