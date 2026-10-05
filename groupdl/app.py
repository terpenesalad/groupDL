"""Local web server: serves the UI and a small JSON API on 127.0.0.1."""

from __future__ import annotations

import argparse
import json
import os
import platform
import secrets
import shutil
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from urllib.error import URLError
from urllib.request import Request, urlopen

from . import __version__
from .core import CHANNEL_TABS, FORMATS, DownloadManager, Lister, default_output_dir
from .tools import find_deno, find_ffmpeg

PREFERRED_PORT = 47862
CONFIG_DIR = os.path.join(os.path.expanduser("~"), ".groupdl")
SETTINGS_FILE = os.path.join(CONFIG_DIR, "settings.json")
INSTANCE_FILE = os.path.join(CONFIG_DIR, "instance.json")
IDLE_SHUTDOWN_SECONDS = 20 * 60

DEFAULT_SETTINGS = {
    "format": "best",
    "output_dir": "",
    "channel_folders": True,
    "skip_downloaded": True,
    "embed_metadata": True,
    "concurrency": 2,
    "cookies_browser": "none",
    "tabs": ["videos"],
}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def load_settings() -> dict:
    settings = dict(DEFAULT_SETTINGS)
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        settings.update({k: v for k, v in saved.items() if k in DEFAULT_SETTINGS})
    except (OSError, ValueError):
        pass
    if not settings["output_dir"]:
        settings["output_dir"] = default_output_dir()
    return settings


def save_settings(settings: dict) -> None:
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({k: settings[k] for k in DEFAULT_SETTINGS if k in settings}, f, indent=2)
    except OSError:
        pass


def sanitize_settings(incoming: dict, current: dict) -> dict:
    s = dict(current)
    if incoming.get("format") in FORMATS:
        s["format"] = incoming["format"]
    if isinstance(incoming.get("output_dir"), str) and incoming["output_dir"].strip():
        s["output_dir"] = os.path.expanduser(incoming["output_dir"].strip())
    for key in ("channel_folders", "skip_downloaded", "embed_metadata"):
        if key in incoming:
            s[key] = bool(incoming[key])
    if "concurrency" in incoming:
        try:
            s["concurrency"] = max(1, min(int(incoming["concurrency"]), 6))
        except (TypeError, ValueError):
            pass
    if incoming.get("cookies_browser") in ("none", "chrome", "firefox", "edge", "safari", "brave",
                                           "chromium", "opera", "vivaldi"):
        s["cookies_browser"] = incoming["cookies_browser"]
    if isinstance(incoming.get("tabs"), list):
        tabs = [t for t in incoming["tabs"] if t in CHANNEL_TABS]
        s["tabs"] = tabs or ["videos"]
    return s


# ---------------------------------------------------------------------------
# Native helpers
# ---------------------------------------------------------------------------

def open_folder(path: str) -> None:
    os.makedirs(path, exist_ok=True)
    if sys.platform.startswith("win"):
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])


def pick_folder(initial: str) -> str | None:
    """Show the operating system's folder picker. Returns None if cancelled or unavailable."""
    initial = initial if os.path.isdir(initial) else os.path.expanduser("~")
    if sys.platform == "darwin":
        script = ('POSIX path of (choose folder with prompt "Choose where groupDL saves videos" '
                  f'default location (POSIX file "{initial}"))')
        r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        return r.stdout.strip() or None if r.returncode == 0 else None
    if sys.platform.startswith("linux"):
        for cmd in (["zenity", "--file-selection", "--directory", f"--filename={initial}/"],
                    ["kdialog", "--getexistingdirectory", initial]):
            if shutil.which(cmd[0]):
                r = subprocess.run(cmd, capture_output=True, text=True)
                return r.stdout.strip() or None if r.returncode == 0 else None
    try:
        import tkinter
        from tkinter import filedialog

        root = tkinter.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(initialdir=initial, title="Choose where groupDL saves videos")
        root.destroy()
        return chosen or None
    except Exception:
        raise RuntimeError("No folder picker is available here; type the folder path instead.")


# ---------------------------------------------------------------------------
# App state
# ---------------------------------------------------------------------------

class FetchTask:
    def __init__(self, urls: list[str]):
        self.id = uuid.uuid4().hex
        self.urls = urls
        self.status = "running"
        self.message = "Starting…"
        self.sections: list[dict] = []
        self.done_urls = 0

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "message": self.message, "sections": self.sections,
                "done": self.done_urls, "total": len(self.urls)}


class App:
    def __init__(self, lister_factory=Lister, manager: DownloadManager | None = None):
        self.token = secrets.token_urlsafe(24)
        self.settings = load_settings()
        self.manager = manager or DownloadManager()
        self.lister_factory = lister_factory
        self.fetches: dict[str, FetchTask] = {}
        self.last_seen = time.time()
        self.server: ThreadingHTTPServer | None = None
        self.port = 0
        self._ffmpeg = find_ffmpeg()
        self._deno = find_deno()

    def start_fetch(self, urls: list[str], tabs: list[str]) -> FetchTask:
        task = FetchTask(urls)
        self.fetches[task.id] = task
        # Keep only recent fetches.
        for old in list(self.fetches)[:-10]:
            self.fetches.pop(old, None)

        def run():
            lister = self.lister_factory(self.settings)
            for url in urls:
                def progress(msg, _url=url):
                    task.message = msg
                try:
                    secs = lister.list_url(url, tabs, on_progress=progress)
                    task.sections.extend(s.to_dict() for s in secs)
                except Exception as exc:  # noqa: BLE001
                    task.sections.append({"id": uuid.uuid4().hex, "title": url, "url": url,
                                          "error": str(exc), "videos": []})
                task.done_urls += 1
            task.status = "done"
            task.message = "Done"

        threading.Thread(target=run, daemon=True).start()
        return task

    def state(self) -> dict:
        return {
            "version": __version__,
            "settings": self.settings,
            "formats": FORMATS,
            "tabs": CHANNEL_TABS,
            "jobs": self.manager.snapshot(),
            "busy": self.manager.busy(),
            "ffmpeg": bool(self._ffmpeg),
            "deno": bool(self._deno),
            "platform": platform.system(),
        }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _index_html() -> str:
    return resources.files("groupdl").joinpath("static/index.html").read_text(encoding="utf-8")


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "groupDL"

        def log_message(self, fmt, *args):  # quiet
            pass

        # -- helpers --
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            return host in (f"127.0.0.1:{app.port}", f"localhost:{app.port}")

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data, code: int = 200) -> None:
            self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

        def _body(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                data = json.loads(self.rfile.read(min(length, 5_000_000)).decode("utf-8"))
                return data if isinstance(data, dict) else {}
            except ValueError:
                return {}

        def _authorized(self) -> bool:
            if not self._host_ok():
                self._json({"error": "Bad host"}, 403)
                return False
            if not secrets.compare_digest(self.headers.get("X-GroupDL-Token") or "", app.token):
                self._json({"error": "Bad token"}, 403)
                return False
            app.last_seen = time.time()
            return True

        # -- routes --
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/api/ping":
                self._json({"app": "groupDL", "version": __version__})
                return
            if path in ("/", "/index.html"):
                if not self._host_ok():
                    self._send(403, b"Forbidden", "text/plain")
                    return
                app.last_seen = time.time()
                html = _index_html().replace("__GROUPDL_TOKEN__", app.token)
                self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/favicon.ico":
                self._send(204, b"", "image/x-icon")
                return
            if not self._authorized():
                return
            if path == "/api/state":
                self._json(app.state())
            elif path.startswith("/api/fetch/"):
                task = app.fetches.get(path.rsplit("/", 1)[-1])
                self._json(task.to_dict() if task else {"error": "Unknown task"}, 200 if task else 404)
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            body = self._body()
            try:
                if path == "/api/fetch":
                    urls = [u.strip() for u in body.get("urls") or [] if isinstance(u, str) and u.strip()]
                    if not urls:
                        self._json({"error": "Paste at least one URL."}, 400)
                        return
                    tabs = [t for t in body.get("tabs") or ["videos"] if t in CHANNEL_TABS] or ["videos"]
                    app.settings = sanitize_settings({"tabs": tabs}, app.settings)
                    save_settings(app.settings)
                    self._json(app.start_fetch(urls[:50], tabs).to_dict())
                elif path == "/api/settings":
                    app.settings = sanitize_settings(body, app.settings)
                    save_settings(app.settings)
                    self._json({"settings": app.settings})
                elif path == "/api/download":
                    videos = [v for v in body.get("videos") or [] if isinstance(v, dict) and v.get("url")]
                    if not videos:
                        self._json({"error": "Tick at least one video."}, 400)
                        return
                    app.settings = sanitize_settings(body.get("settings") or {}, app.settings)
                    save_settings(app.settings)
                    clean = [{k: v.get(k) for k in ("id", "title", "url", "channel", "thumbnail", "duration")}
                             for v in videos]
                    ids = app.manager.add(clean, app.settings)
                    self._json({"added": len(ids)})
                elif path == "/api/cancel":
                    stopped = app.manager.cancel(body.get("ids") or None,
                                                 waiting_only=bool(body.get("waiting_only")))
                    self._json({"stopped": stopped})
                elif path == "/api/retry":
                    app.manager.retry(body.get("ids") or None)
                    self._json({"ok": True})
                elif path == "/api/clear":
                    app.manager.clear_finished()
                    self._json({"ok": True})
                elif path == "/api/open-folder":
                    target = body.get("path") or app.settings["output_dir"]
                    if body.get("file") and os.path.exists(body["file"]):
                        target = os.path.dirname(body["file"])
                    open_folder(target)
                    self._json({"ok": True})
                elif path == "/api/pick-folder":
                    chosen = pick_folder(app.settings["output_dir"])
                    if chosen:
                        app.settings["output_dir"] = chosen
                        save_settings(app.settings)
                    self._json({"path": chosen})
                elif path == "/api/quit":
                    self._json({"ok": True})
                    threading.Thread(target=app.server.shutdown, daemon=True).start()
                else:
                    self._json({"error": "Not found"}, 404)
            except Exception as exc:  # noqa: BLE001
                self._json({"error": str(exc)}, 500)

    return Handler


def _existing_instance() -> str | None:
    """If groupDL is already running, return its URL so we can just reopen it."""
    try:
        with open(INSTANCE_FILE, encoding="utf-8") as f:
            info = json.load(f)
        port, token = int(info["port"]), info["token"]
        req = Request(f"http://127.0.0.1:{port}/api/ping")
        with urlopen(req, timeout=1.5) as r:
            if json.loads(r.read()).get("app") == "groupDL":
                return f"http://127.0.0.1:{port}/#t={token}"
    except (OSError, ValueError, KeyError, URLError):
        pass
    return None


def _bind(app: App, port: int | None) -> ThreadingHTTPServer:
    handler = make_handler(app)
    for candidate in ([port] if port is not None else [PREFERRED_PORT, 0]):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", candidate), handler)
            server.daemon_threads = True
            return server
        except OSError:
            continue
    raise SystemExit("groupDL couldn't open a local port.")


def _idle_watch(app: App) -> None:
    while True:
        time.sleep(30)
        if time.time() - app.last_seen > IDLE_SHUTDOWN_SECONDS and not app.manager.busy():
            print("No browser tab has been open for a while, so groupDL is closing.")
            app.server.shutdown()
            return


def self_test() -> int:
    """Join a separate video and sound track the way a YouTube download does, and check the result."""
    import tempfile

    from .tools import media_streams, run_ffmpeg

    ffmpeg, deno = find_ffmpeg(), find_deno()
    print(f"ffmpeg: {ffmpeg}\ndeno: {deno}")
    if not ffmpeg or not deno:
        print("FAIL: helper program missing")
        return 1
    r = subprocess.run([deno, "--version"], capture_output=True, text=True)
    print(r.stdout.splitlines()[0] if r.stdout else r.stderr)
    if r.returncode != 0:
        print("FAIL: deno doesn't run")
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        v, a = os.path.join(tmp, "v.f1.mp4"), os.path.join(tmp, "a.f2.mp4")
        steps = [
            ["-y", "-f", "lavfi", "-i", "testsrc=d=2:s=320x240:r=25", "-c:v", "mpeg2video", "-f", "mpegts", v],
            ["-y", "-f", "lavfi", "-i", "sine=d=2", "-c:a", "aac", "-f", "mpegts", a],
        ]
        for fmt, ext in (("mp4", ".mp4"), ("mkv", ".mkv")):
            out = os.path.join(tmp, "joined" + ext)
            steps.append(["-y", "-i", v, "-i", a, "-c", "copy", "-map", "0:v:0", "-map", "1:a:0",
                          *(["-bsf:a:0", "aac_adtstoasc"] if fmt == "mp4" else []), out])
        for step in steps:
            r = run_ffmpeg(step, timeout=120)
            if r.returncode != 0:
                print("FAIL: ffmpeg", " ".join(step), "\n", r.stderr[-800:])
                return 1
        for ext in (".mp4", ".mkv"):
            streams = media_streams(os.path.join(tmp, "joined" + ext))
            print(f"joined{ext}: {streams}")
            if streams != {"video": 1, "audio": 1}:
                print("FAIL: joined file is missing a stream")
                return 1
    print("OK")
    return 0


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="groupdl", description="Pick and bulk-download YouTube videos.")
    parser.add_argument("--port", type=int, default=None, help="port to listen on (default 47862)")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    parser.add_argument("--version", action="version", version=f"groupDL {__version__}")
    parser.add_argument("--self-test", action="store_true",
                        help="check that the bundled ffmpeg and Deno work, then exit")
    args = parser.parse_args(argv)

    if args.self_test:
        raise SystemExit(self_test())

    if args.port is None and (url := _existing_instance()):
        print(f"groupDL is already running: {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return

    app = App()
    server = _bind(app, args.port)
    app.server = server
    app.port = server.server_address[1]
    url = f"http://127.0.0.1:{app.port}/"
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(INSTANCE_FILE, "w", encoding="utf-8") as f:
            json.dump({"port": app.port, "token": app.token}, f)
    except OSError:
        pass

    print(f"groupDL {__version__} is running at {url}")
    print("Keep this window open while you use it. Close it (or click Quit in the app) to stop.")
    if not app._ffmpeg:
        print("Note: ffmpeg wasn't found, so merging video+audio and MP3 conversion won't work.")
    threading.Thread(target=_idle_watch, args=(app,), daemon=True).start()
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        app.manager.cancel()
        server.server_close()
        try:
            os.remove(INSTANCE_FILE)
        except OSError:
            pass
        print("groupDL stopped.")


if __name__ == "__main__":
    main()
