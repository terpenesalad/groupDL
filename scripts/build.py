"""Build a single-file groupDL executable with ffmpeg and deno bundled inside.

Usage:  python scripts/build.py [--ffmpeg PATH]
        (release builds pass the ffmpeg that scripts/fetch_ffmpeg.py downloads;
         without --ffmpeg the one groupDL would find on this computer is used)
Needs:  pip install -r requirements.txt pyinstaller
Output: dist/groupDL (or dist/groupDL.exe on Windows)
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXE = ".exe" if os.name == "nt" else ""


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ffmpeg", help="ffmpeg binary to bundle")
    args_in = parser.parse_args()

    import deno
    import PyInstaller.__main__

    sys.path.insert(0, str(ROOT))
    from groupdl.tools import find_ffmpeg

    ffmpeg = args_in.ffmpeg or find_ffmpeg()
    if not ffmpeg or not os.path.isfile(ffmpeg):
        raise SystemExit("No ffmpeg to bundle. Run scripts/fetch_ffmpeg.py and pass --ffmpeg.")

    bin_dir = ROOT / "build" / "bin"
    if bin_dir.exists():
        shutil.rmtree(bin_dir)
    bin_dir.mkdir(parents=True)
    for name, src in (("ffmpeg", ffmpeg), ("deno", deno.find_deno_bin())):
        dst = bin_dir / f"{name}{EXE}"
        shutil.copy2(src, dst)
        dst.chmod(dst.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"bundling {name}: {src}")

    sep = os.pathsep
    args = [
        str(ROOT / "run_groupdl.py"),
        "--name", "groupDL",
        "--onefile",
        "--console",
        "--noconfirm",
        "--clean",
        "--distpath", str(ROOT / "dist"),
        "--workpath", str(ROOT / "build" / "pyinstaller"),
        "--specpath", str(ROOT / "build"),
        "--add-data", f"{ROOT / 'groupdl' / 'static'}{sep}groupdl/static",
        "--add-binary", f"{bin_dir / ('ffmpeg' + EXE)}{sep}bin",
        "--add-binary", f"{bin_dir / ('deno' + EXE)}{sep}bin",
        "--collect-all", "yt_dlp_ejs",
        "--collect-submodules", "yt_dlp",
        "--exclude-module", "imageio_ffmpeg",
        "--exclude-module", "deno",
    ]
    PyInstaller.__main__.run(args)


if __name__ == "__main__":
    main()
