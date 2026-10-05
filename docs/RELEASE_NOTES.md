Fixes videos downloading without sound, and adds a one-click way to cancel a big batch.

**Download the file for your computer below:**

- Windows: `groupDL-windows.exe`
- Mac (Apple silicon): `groupDL-macos-arm64`
- Linux (x86-64): `groupDL-linux`

### Fixed: videos with no sound

YouTube sends the picture and the sound as separate files, and groupDL joins them with ffmpeg. The ffmpeg bundled in 1.0.0 could crash on some of YouTube's stream types, which left only the picture-only part (named like `… .f137.mp4`) in your folder.

- Release builds now bundle yt-dlp's own patched ffmpeg (Windows, Linux) and Martin Riedl's static ffmpeg (Mac), and every build is tested joining a separate video and sound track before it's published.
- Every finished video is checked for sound. If it has none, groupDL downloads the sound track and adds it.
- Stopped or failed downloads no longer leave silent picture-only files behind.
- When joining does fail, the error now says so in plain words.

If you have a silent video from 1.0.0, just download it again.

### New: bulk cancel

- **Stop all** appears in the bottom bar whenever downloads are running and stops every running and waiting download at once.
- **Stop waiting ones** in Downloads lets the videos already in progress finish and drops the rest of the queue.
- **Retry all** restarts everything that was stopped or failed.
