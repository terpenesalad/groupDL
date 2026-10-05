import json
import threading
import time
import urllib.request

import pytest

from groupdl import app as appmod
from groupdl.core import DownloadManager, Lister, download_opts, expand_url, normalize_url


# ---------------------------------------------------------------- fake yt-dlp data
def _flat_video(i, **extra):
    vid = f"vid{i:08d}"[:11].ljust(11, "x")
    return {"_type": "url", "ie_key": "Youtube", "id": vid, "url": f"https://www.youtube.com/watch?v={vid}",
            "title": f"Video number {i}", "duration": 60 * i + 5, "view_count": 1000 * i, **extra}


FAKE = {
    "https://www.youtube.com/@demo/videos": {
        "_type": "playlist", "id": "UCdemo", "title": "Demo Channel - Videos", "channel": "Demo Channel",
        "webpage_url": "https://www.youtube.com/@demo/videos",
        "entries": [_flat_video(i) for i in range(1, 6)] + [_flat_video(9, live_status="is_upcoming")],
    },
    "https://www.youtube.com/@demo/shorts": None,  # channel without shorts
    "https://www.youtube.com/@demo/playlists": {
        "_type": "playlist", "title": "Demo Channel - Playlists", "channel": "Demo Channel",
        "entries": [{"_type": "url", "ie_key": "YoutubeTab", "url": "https://www.youtube.com/playlist?list=PL1",
                     "title": "Best of"}],
    },
    "https://www.youtube.com/playlist?list=PL1": {
        "_type": "playlist", "title": "Best of", "entries": [_flat_video(2), _flat_video(7)],
    },
    "https://www.youtube.com/watch?v=abcdefghijk": {
        "id": "abcdefghijk", "title": "One video", "channel": "Someone", "duration": 42,
        "webpage_url": "https://www.youtube.com/watch?v=abcdefghijk", "extractor_key": "Youtube",
    },
}


def fake_extract(url, opts):
    if url not in FAKE:
        raise Exception("ERROR: [generic] Unsupported URL: " + url)
    data = FAKE[url]
    if data is None:
        opts["logger"].error("ERROR: [youtube:tab] @demo: This channel does not have a shorts tab")
    return data


# ---------------------------------------------------------------- URL handling
def test_normalize_url():
    assert normalize_url("@demo") == "https://www.youtube.com/@demo"
    assert normalize_url("youtube.com/@demo") == "https://youtube.com/@demo"
    assert normalize_url("dQw4w9WgXcQ") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_expand_channel_root_only():
    assert expand_url("https://www.youtube.com/@demo", ["videos", "shorts"]) == [
        ("https://www.youtube.com/@demo/videos", "Videos"), ("https://www.youtube.com/@demo/shorts", "Shorts")]
    assert expand_url("https://youtube.com/channel/UC123/featured", []) == [
        ("https://www.youtube.com/channel/UC123/videos", "Videos")]
    # Specific tabs and other pages are left alone.
    assert expand_url("https://www.youtube.com/@demo/playlists", ["videos"]) == [
        ("https://www.youtube.com/@demo/playlists", None)]
    assert expand_url("https://www.youtube.com/watch?v=abc", ["videos"])[0][1] is None


# ---------------------------------------------------------------- listing
def test_list_channel_with_missing_tab():
    secs = Lister(extract=fake_extract).list_url("@demo", ["videos", "shorts"])
    assert [s.tag for s in secs] == ["Videos", "Shorts"]
    assert secs[0].title == "Demo Channel"
    assert len(secs[0].videos) == 6
    assert secs[0].videos[0].thumbnail.startswith("https://i.ytimg.com/vi/")
    assert secs[0].videos[-1].live_status == "is_upcoming"
    assert secs[1].error and "shorts tab" in secs[1].error


def test_list_nested_playlists():
    secs = Lister(extract=fake_extract).list_url("https://www.youtube.com/@demo/playlists")
    assert [s.title for s in secs] == ["Best of"]
    assert len(secs[0].videos) == 2


def test_list_single_video_and_bad_url():
    secs = Lister(extract=fake_extract).list_url("https://www.youtube.com/watch?v=abcdefghijk")
    assert secs[0].tag == "Video" and secs[0].videos[0].title == "One video"
    bad = Lister(extract=fake_extract).list_url("https://example.com/x")
    assert bad[0].error.startswith("Unsupported URL")


# ---------------------------------------------------------------- options
def test_download_opts(tmp_path):
    o = download_opts({"format": "720", "output_dir": str(tmp_path), "channel_folders": True})
    assert o["format_sort"][0] == "res:720"
    assert o["merge_output_format"] == "mp4/mkv"
    assert o["outtmpl"]["default"].startswith("%(channel")
    assert o["download_archive"].endswith(".groupdl-archive.txt")
    a = download_opts({"format": "mp3", "output_dir": str(tmp_path), "channel_folders": False,
                       "skip_downloaded": False})
    assert a["postprocessors"][0] == {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "0"}
    assert "download_archive" not in a
    assert "ffmpeg_location" in a  # bundled/installed ffmpeg is found


# ---------------------------------------------------------------- download queue
def _wait(pred, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_manager_runs_cancels_and_retries():
    attempts = {}

    def fake_download(job, mgr):
        url = job.video["url"]
        attempts[url] = attempts.get(url, 0) + 1
        if url.endswith("bad") and attempts[url] == 1:
            raise Exception("ERROR: [youtube] bad: Video unavailable")
        for i in range(10):
            if job.cancel_requested:
                raise Exception("cancelled")
            mgr.update(job, progress=i / 10)
            time.sleep(0.03 if not url.endswith("slow") else 0.3)

    m = DownloadManager(download_fn=fake_download)
    vids = [{"id": "a", "url": "u/a"}, {"id": "b", "url": "u/bad"}, {"id": "c", "url": "u/slow"}]
    m.add(vids, {"concurrency": 2})
    assert m.add([{"id": "a", "url": "u/a"}], {}) == []  # already queued
    assert _wait(lambda: any(j["status"] == "downloading" for j in m.snapshot() if j["video"]["id"] == "c"))
    m.cancel([j["id"] for j in m.snapshot() if j["video"]["id"] == "c"])
    assert _wait(lambda: not m.busy())
    st = {j["video"]["id"]: j for j in m.snapshot()}
    assert st["a"]["status"] == "done" and st["a"]["progress"] == 1.0
    assert st["b"]["status"] == "error" and st["b"]["error"] == "Video unavailable"
    assert st["c"]["status"] == "cancelled"
    m.retry()
    assert _wait(lambda: not m.busy() and all(j["status"] == "done" for j in m.snapshot()))
    m.clear_finished()
    assert m.snapshot() == []


# ---------------------------------------------------------------- HTTP API
@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(appmod, "CONFIG_DIR", str(tmp_path))
    monkeypatch.setattr(appmod, "SETTINGS_FILE", str(tmp_path / "settings.json"))

    def fake_download(job, mgr):
        for i in range(5):
            mgr.update(job, progress=i / 5)
            time.sleep(0.02)

    a = appmod.App(lister_factory=lambda s: Lister(s, extract=fake_extract),
                   manager=DownloadManager(download_fn=fake_download))
    srv = appmod._bind(a, 0)
    a.server, a.port = srv, srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield a
    srv.shutdown()


def _call(a, path, body=None, token=True, host=None):
    req = urllib.request.Request(f"http://127.0.0.1:{a.port}{path}",
                                 data=None if body is None else json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    if token:
        req.add_header("X-GroupDL-Token", a.token)
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if path.startswith("/api") else raw.decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_http_flow(server):
    code, html = _call(server, "/")
    assert code == 200 and server.token in html
    assert _call(server, "/api/state", token=False)[0] == 403
    assert _call(server, "/api/state", host="evil.example:80")[0] == 403

    code, task = _call(server, "/api/fetch", {"urls": ["@demo"], "tabs": ["videos"]})
    assert code == 200
    assert _wait(lambda: _call(server, f"/api/fetch/{task['id']}")[1]["status"] == "done")
    task = _call(server, f"/api/fetch/{task['id']}")[1]
    vids = task["sections"][0]["videos"]
    assert len(vids) == 6

    code, r = _call(server, "/api/download", {"videos": vids[:3], "settings": {"format": "mp3", "concurrency": 3}})
    assert code == 200 and r["added"] == 3
    assert _wait(lambda: not _call(server, "/api/state")[1]["busy"])
    st = _call(server, "/api/state")[1]
    assert [j["status"] for j in st["jobs"]] == ["done"] * 3
    assert st["settings"]["format"] == "mp3" and st["settings"]["concurrency"] == 3
    assert _call(server, "/api/download", {"videos": []})[0] == 400


def test_stop_waiting_only_and_stop_all():
    gate = threading.Event()

    def slow(job, mgr):
        while not gate.is_set():
            if job.cancel_requested:
                raise Exception("cancelled")
            time.sleep(0.02)

    m = DownloadManager(download_fn=slow)
    m.add([{"id": str(i), "url": f"u/{i}"} for i in range(6)], {"concurrency": 2})
    assert _wait(lambda: sum(j["status"] == "downloading" for j in m.snapshot()) == 2)
    assert m.cancel(waiting_only=True) == 4
    st = [j["status"] for j in m.snapshot()]
    assert st.count("cancelled") == 4 and st.count("downloading") == 2
    assert m.cancel() == 2
    assert _wait(lambda: not m.busy())
    assert all(j["status"] == "cancelled" for j in m.snapshot())


def test_failed_job_removes_silent_parts(tmp_path):
    video_part = tmp_path / "Clip [abcdefghijk].f137.mp4"
    audio_part = tmp_path / "Clip [abcdefghijk].f140.m4a.part"
    keep = tmp_path / "Other [zzzzzzzzzzz].mp4"
    for f in (video_part, audio_part, keep):
        f.write_bytes(b"x")

    def failing(job, mgr):
        job.temp_files.update({str(video_part), str(audio_part)[:-5], str(keep)})
        raise Exception("ERROR: Postprocessing:   libpostproc    58.  1.100 / 58.  1.100")

    m = DownloadManager(download_fn=failing)
    m.add([{"id": "a", "url": "u/a"}], {})
    assert _wait(lambda: not m.busy())
    job = m.snapshot()[0]
    assert job["status"] == "error" and job["error"] == "Couldn't join the video and its sound into one file."
    assert not video_part.exists() and not audio_part.exists() and keep.exists()


def test_media_streams(tmp_path):
    from groupdl.tools import find_ffmpeg, media_streams, run_ffmpeg

    if not find_ffmpeg():
        pytest.skip("no ffmpeg")
    silent, both = str(tmp_path / "silent.mkv"), str(tmp_path / "both.mkv")
    run_ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-c:v", "ffv1", silent])
    run_ffmpeg(["-y", "-f", "lavfi", "-i", "color=c=blue:s=64x64:d=1", "-f", "lavfi", "-i", "sine=d=1",
                "-c:v", "ffv1", "-c:a", "flac", "-shortest", both])
    assert media_streams(silent) == {"video": 1, "audio": 0}
    assert media_streams(both) == {"video": 1, "audio": 1}
