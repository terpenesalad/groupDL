"""Listing videos from URLs and downloading a selection of them, built on yt-dlp."""

from __future__ import annotations

import os
import queue
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Callable, Iterable

from .tools import ytdlp_runtime_opts

# ---------------------------------------------------------------------------
# URL handling
# ---------------------------------------------------------------------------

CHANNEL_TABS = {"videos": "Videos", "shorts": "Shorts", "streams": "Live"}

_YT_HOST = r"https?://(?:www\.|m\.|music\.)?youtube\.com"
_CHANNEL_ROOT_RE = re.compile(
    _YT_HOST + r"/(?P<base>@[^/?#]+|channel/[^/?#]+|c/[^/?#]+|user/[^/?#]+)"
    r"(?:/(?P<tab>featured|home))?/?(?:[?#].*)?$",
    re.IGNORECASE,
)
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def normalize_url(raw: str) -> str:
    """Tidy a user-typed URL: add a scheme, accept bare @handles and video IDs."""
    url = raw.strip()
    if not url:
        return ""
    if url.startswith("@"):
        return "https://www.youtube.com/" + url
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", url):
        return "https://www.youtube.com/watch?v=" + url
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.IGNORECASE):
        url = "https://" + url
    return url


def expand_url(url: str, tabs: Iterable[str]) -> list[tuple[str, str | None]]:
    """Turn a channel's home URL into one URL per wanted tab (Videos/Shorts/Live).

    Returns (url, tab_label) pairs. Anything that isn't a bare channel URL is returned unchanged.
    """
    m = _CHANNEL_ROOT_RE.match(url)
    if not m:
        return [(url, None)]
    base = "https://www.youtube.com/" + m.group("base")
    wanted = [t for t in tabs if t in CHANNEL_TABS] or ["videos"]
    return [(f"{base}/{t}", CHANNEL_TABS[t]) for t in wanted]


def clean_error(msg: str) -> str:
    msg = _ANSI_RE.sub("", str(msg)).strip()
    msg = re.sub(r"^(ERROR|WARNING):\s*", "", msg)
    msg = re.sub(r"^\[[^\]]+\]\s*(?:[\w@.-]+:\s+)?", "", msg)  # "[youtube] abc123: " prefix
    return msg


class _Logger:
    """Collects yt-dlp's warnings and errors instead of printing them."""

    def __init__(self, on_message: Callable[[str, str], None] | None = None):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.messages: list[str] = []
        self._on_message = on_message

    def debug(self, msg: str) -> None:
        if msg.startswith("[debug] "):
            return
        self.messages.append(msg)
        if self._on_message:
            self._on_message("info", msg)

    def info(self, msg: str) -> None:
        self.debug(msg)

    def warning(self, msg: str) -> None:
        self.warnings.append(clean_error(msg))
        if self._on_message:
            self._on_message("warning", msg)

    def error(self, msg: str) -> None:
        self.errors.append(clean_error(msg))
        if self._on_message:
            self._on_message("error", msg)


def base_opts(settings: dict | None = None) -> dict:
    """Options shared by listing and downloading."""
    settings = settings or {}
    opts = {
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "color": {"stdout": "never", "stderr": "never"},
        **ytdlp_runtime_opts(),
    }
    browser = (settings.get("cookies_browser") or "").strip().lower()
    if browser and browser != "none":
        opts["cookiesfrombrowser"] = (browser,)
    return opts


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


@dataclass
class Video:
    id: str
    title: str
    url: str
    duration: float | None = None
    views: int | None = None
    channel: str | None = None
    upload_date: str | None = None  # YYYYMMDD
    timestamp: float | None = None
    thumbnail: str | None = None
    live_status: str | None = None
    availability: str | None = None
    source: str = ""  # which section of the page it came from

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Section:
    """One group of videos: a channel tab, a playlist or a single video."""

    id: str
    title: str
    url: str
    tag: str = ""  # "Videos", "Shorts", "Live", "Playlist", "Video"
    videos: list[Video] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "tag": self.tag,
            "error": self.error,
            "videos": [v.to_dict() for v in self.videos],
        }


def _is_youtube_video_id(value) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{11}", value) is not None


def _video_from_entry(entry: dict, source: str, parent: dict | None) -> Video | None:
    vid = entry.get("id")
    if not vid:
        return None
    url = entry.get("webpage_url") or entry.get("url") or ""
    extractor = (entry.get("ie_key") or entry.get("extractor_key") or "").lower()
    if _is_youtube_video_id(vid) and (extractor.startswith("youtube") or "youtube" in url or "youtu.be" in url):
        url = f"https://www.youtube.com/watch?v={vid}"
    if not url:
        return None
    thumb = None
    if _is_youtube_video_id(vid) and "youtube" in url:
        thumb = f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"
    else:
        thumb = entry.get("thumbnail")
        if not thumb and entry.get("thumbnails"):
            thumb = (entry["thumbnails"][-1] or {}).get("url")
    parent = parent or {}
    title = entry.get("title") or vid
    if title.startswith("[") and title in ("[Private video]", "[Deleted video]"):
        return None
    return Video(
        id=str(vid),
        title=title,
        url=url,
        duration=entry.get("duration"),
        views=entry.get("view_count"),
        channel=entry.get("channel") or entry.get("uploader")
        or parent.get("channel") or parent.get("uploader") or parent.get("title"),
        upload_date=entry.get("upload_date"),
        timestamp=entry.get("timestamp") or entry.get("release_timestamp"),
        thumbnail=thumb,
        live_status=entry.get("live_status"),
        availability=entry.get("availability"),
        source=source,
    )


def _looks_like_playlist(entry: dict) -> bool:
    if entry.get("_type") in ("playlist", "multi_video"):
        return True
    if "entries" in entry:
        return True
    ie = (entry.get("ie_key") or "").lower()
    return entry.get("_type") == "url" and ie in ("youtubetab", "youtubeplaylist")


class Lister:
    """Turns URLs into sections of videos using yt-dlp's fast "flat" extraction."""

    MAX_DEPTH = 2

    def __init__(self, settings: dict | None = None, extract: Callable[[str, dict], dict | None] | None = None):
        self.settings = settings or {}
        self._extract_fn = extract or self._extract_with_ytdlp

    def _extract_with_ytdlp(self, url: str, opts: dict) -> dict | None:
        import yt_dlp

        with yt_dlp.YoutubeDL(opts) as ydl:
            return ydl.extract_info(url, download=False)

    def _extract(self, url: str, logger: _Logger) -> dict | None:
        opts = {
            **base_opts(self.settings),
            "extract_flat": "in_playlist",
            "skip_download": True,
            "ignoreerrors": "only_download",
            "lazy_playlist": False,
            "logger": logger,
        }
        return self._extract_fn(url, opts)

    def list_url(self, url: str, tabs: Iterable[str] = ("videos",),
                 on_progress: Callable[[str], None] | None = None) -> list[Section]:
        url = normalize_url(url)
        sections: list[Section] = []
        for target, tab_label in expand_url(url, tabs):
            if on_progress:
                on_progress(f"Reading {target}")
            sections.extend(self._list_one(target, tab_label, on_progress))
        return sections

    def _list_one(self, url: str, tab_label: str | None, on_progress) -> list[Section]:
        logger = _Logger()
        label = url
        if tab_label:
            m = _CHANNEL_ROOT_RE.match(url.rsplit("/", 1)[0])
            label = m.group("base").split("/")[-1] if m else url
        try:
            info = self._extract(url, logger)
        except Exception as exc:  # yt-dlp raises DownloadError etc.
            return [Section(id=uuid.uuid4().hex, title=label, url=url, tag=tab_label or "", error=clean_error(exc))]
        if not info:
            err = logger.errors[-1] if logger.errors else "Nothing found at this URL."
            return [Section(id=uuid.uuid4().hex, title=label, url=url, tag=tab_label or "", error=err)]

        title = info.get("title") or info.get("channel") or info.get("uploader") or url
        tag = "Playlist"
        if tab_label:
            title = info.get("channel") or info.get("uploader") or re.sub(r"\s*-\s*\w+$", "", title)
            tag = tab_label

        sections: list[Section] = []
        if _looks_like_playlist(info) and info.get("entries") is not None:
            main = Section(id=uuid.uuid4().hex, title=title, url=info.get("webpage_url") or url, tag=tag)
            self._walk(info, main, sections, depth=0, on_progress=on_progress)
            if main.videos or not sections:
                sections.insert(0, main)
            if not main.videos and not sections[1:] and not main.error:
                main.error = logger.errors[-1] if logger.errors else "No videos found on this page."
        else:
            # A single video.
            v = _video_from_entry(info, title, None)
            sec = Section(id=uuid.uuid4().hex, title=info.get("channel") or info.get("uploader") or title,
                          url=url, tag="Video")
            if v:
                sec.videos.append(v)
            else:
                sec.error = "Couldn't read this video."
            sections.append(sec)
        return sections

    def _walk(self, info: dict, section: Section, out: list[Section], depth: int, on_progress) -> None:
        for entry in info.get("entries") or []:
            if not entry:
                continue
            if _looks_like_playlist(entry):
                if depth >= self.MAX_DEPTH:
                    continue
                sub_info = entry
                if entry.get("entries") is None:
                    sub_url = entry.get("url") or entry.get("webpage_url")
                    if not sub_url:
                        continue
                    if on_progress:
                        on_progress(f"Reading {entry.get('title') or sub_url}")
                    try:
                        sub_info = self._extract(sub_url, _Logger())
                    except Exception:
                        sub_info = None
                    if not sub_info:
                        continue
                sub_title = sub_info.get("title") or entry.get("title") or "Playlist"
                sub = Section(id=uuid.uuid4().hex, title=sub_title, tag="Playlist",
                              url=sub_info.get("webpage_url") or entry.get("url") or "")
                self._walk(sub_info, sub, out, depth + 1, on_progress)
                if sub.videos:
                    out.append(sub)
                continue
            v = _video_from_entry(entry, section.title, info)
            if v:
                section.videos.append(v)


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

FORMATS = {
    "best": "Best quality video",
    "1080": "Video up to 1080p",
    "720": "Video up to 720p",
    "480": "Video up to 480p",
    "mp3": "Audio only (MP3)",
    "m4a": "Audio only (M4A)",
}


def download_opts(settings: dict) -> dict:
    """yt-dlp options for the chosen format and output settings."""
    fmt = settings.get("format") or "best"
    out_dir = os.path.expanduser(settings.get("output_dir") or default_output_dir())
    name = "%(title).150B [%(id)s].%(ext)s"
    if settings.get("channel_folders", True):
        name = os.path.join("%(channel,uploader|Unknown channel)s", name)

    opts: dict = {
        **base_opts(settings),
        "paths": {"home": out_dir},
        "outtmpl": {"default": name},
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "concurrent_fragment_downloads": 4,
        "postprocessors": [],
        "overwrites": False,
    }
    if fmt in ("mp3", "m4a"):
        opts["format"] = "ba[ext=m4a]/ba/b" if fmt == "m4a" else "ba/b"
        opts["postprocessors"].append(
            {"key": "FFmpegExtractAudio", "preferredcodec": fmt, "preferredquality": "0" if fmt == "mp3" else None}
        )
    else:
        opts["format"] = "bv*+ba/b"
        opts["merge_output_format"] = "mp4/mkv"
        if fmt in ("1080", "720", "480"):
            # Prefer the highest resolution not above the cap, and H.264/AAC where it's available,
            # because those play everywhere.
            opts["format_sort"] = [f"res:{fmt}", "vcodec:h264", "acodec:m4a"]
        else:
            opts["format_sort"] = ["res", "fps", "vcodec:h264", "acodec:m4a"]
    if settings.get("embed_metadata", True):
        opts["postprocessors"].append({"key": "FFmpegMetadata", "add_metadata": True})
    if settings.get("skip_downloaded", True):
        opts["download_archive"] = os.path.join(out_dir, ".groupdl-archive.txt")
    # Drop None values in postprocessor dicts.
    opts["postprocessors"] = [{k: v for k, v in pp.items() if v is not None} for pp in opts["postprocessors"]]
    return opts


def default_output_dir() -> str:
    home = os.path.expanduser("~")
    downloads = os.path.join(home, "Downloads")
    return os.path.join(downloads if os.path.isdir(downloads) else home, "groupDL")


@dataclass
class Job:
    id: str
    video: dict
    settings: dict
    status: str = "queued"  # queued, downloading, processing, done, skipped, error, cancelled
    progress: float = 0.0  # 0..1
    speed: float | None = None  # bytes/s
    eta: float | None = None
    downloaded: int = 0
    total: int | None = None
    filename: str | None = None
    error: str | None = None
    note: str | None = None
    created: float = field(default_factory=time.time)
    cancel_requested: bool = False

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("settings")
        d.pop("cancel_requested")
        return d


ACTIVE = ("queued", "downloading", "processing")


class DownloadManager:
    """A queue of download jobs worked through by a small pool of threads."""

    def __init__(self, download_fn: Callable[[Job, "DownloadManager"], None] | None = None):
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._queue: "queue.Queue[str]" = queue.Queue()
        self._workers: list[threading.Thread] = []
        self._concurrency = 2
        self._download_fn = download_fn or _download_with_ytdlp

    # -- public API ---------------------------------------------------------
    def add(self, videos: list[dict], settings: dict) -> list[str]:
        ids = []
        with self._lock:
            active_urls = {self._jobs[j].video.get("url") for j in self._order if self._jobs[j].status in ACTIVE}
            for v in videos:
                if v.get("url") in active_urls:
                    continue
                job = Job(id=uuid.uuid4().hex, video=v, settings=dict(settings))
                self._jobs[job.id] = job
                self._order.append(job.id)
                self._queue.put(job.id)
                ids.append(job.id)
            self._concurrency = max(1, min(int(settings.get("concurrency") or 2), 6))
            self._ensure_workers()
        return ids

    def cancel(self, job_ids: list[str] | None = None) -> None:
        with self._lock:
            for jid in job_ids or list(self._order):
                job = self._jobs.get(jid)
                if job and job.status in ACTIVE:
                    job.cancel_requested = True
                    if job.status == "queued":
                        job.status = "cancelled"

    def retry(self, job_ids: list[str] | None = None) -> None:
        with self._lock:
            for jid in job_ids or list(self._order):
                job = self._jobs.get(jid)
                if job and job.status in ("error", "cancelled"):
                    job.status, job.error, job.note = "queued", None, None
                    job.progress, job.cancel_requested = 0.0, False
                    self._queue.put(job.id)
            self._ensure_workers()

    def clear_finished(self) -> None:
        with self._lock:
            keep = [j for j in self._order if self._jobs[j].status in ACTIVE]
            for j in self._order:
                if j not in keep:
                    del self._jobs[j]
            self._order = keep

    def snapshot(self) -> list[dict]:
        with self._lock:
            return [self._jobs[j].to_dict() for j in self._order]

    def busy(self) -> bool:
        with self._lock:
            return any(self._jobs[j].status in ACTIVE for j in self._order)

    def update(self, job: Job, **changes) -> None:
        with self._lock:
            for k, v in changes.items():
                setattr(job, k, v)

    # -- workers ------------------------------------------------------------
    def _ensure_workers(self) -> None:
        self._workers = [w for w in self._workers if w.is_alive()]
        while len(self._workers) < self._concurrency:
            t = threading.Thread(target=self._worker, daemon=True, name=f"groupdl-worker-{len(self._workers)}")
            self._workers.append(t)
            t.start()

    def _worker(self) -> None:
        while True:
            try:
                jid = self._queue.get(timeout=30)
            except queue.Empty:
                with self._lock:
                    # Exit when idle; _ensure_workers starts new ones when more work arrives.
                    if self._queue.empty():
                        self._workers = [w for w in self._workers if w is not threading.current_thread()]
                        return
                continue
            requeue = False
            with self._lock:
                job = self._jobs.get(jid)
                if not job or job.status != "queued" or job.cancel_requested:
                    continue
                # Respect a lowered concurrency limit.
                running = sum(1 for j in self._jobs.values() if j.status in ("downloading", "processing"))
                if running >= self._concurrency:
                    self._queue.put(jid)
                    requeue = True
                else:
                    job.status = "downloading"
            if requeue:
                time.sleep(0.3)
                continue
            try:
                self._download_fn(job, self)
                with self._lock:
                    if job.status not in ("skipped", "error", "cancelled"):
                        job.status, job.progress = "done", 1.0
            except Exception as exc:  # noqa: BLE001
                with self._lock:
                    if job.cancel_requested or type(exc).__name__ == "DownloadCancelled":
                        job.status, job.error = "cancelled", None
                    else:
                        job.status, job.error = "error", clean_error(exc) or "Download failed."
            finally:
                with self._lock:
                    job.speed = job.eta = None


def _download_with_ytdlp(job: Job, manager: DownloadManager) -> None:
    import yt_dlp
    from yt_dlp.utils import DownloadCancelled

    state = {"part": 0, "parts": 1, "last_file": None}

    def progress_hook(d: dict) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Cancelled")
        info = d.get("info_dict") or {}
        state["parts"] = max(1, len(info.get("requested_formats") or []) or 1)
        fname = d.get("filename")
        if d["status"] == "downloading":
            if fname != state["last_file"] and state["last_file"] is not None:
                state["part"] = min(state["part"] + 1, state["parts"] - 1)
            state["last_file"] = fname
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            frac = (done / total) if total else 0.0
            overall = (state["part"] + min(frac, 1.0)) / state["parts"]
            manager.update(job, status="downloading", progress=min(overall, 0.999), speed=d.get("speed"),
                           eta=d.get("eta"), downloaded=done, total=total)
        elif d["status"] == "finished":
            manager.update(job, progress=min((state["part"] + 1) / state["parts"], 0.999))

    def pp_hook(d: dict) -> None:
        if job.cancel_requested:
            raise DownloadCancelled("Cancelled")
        if d.get("status") == "started":
            manager.update(job, status="processing", speed=None, eta=None)

    def post_hook(filename: str) -> None:
        manager.update(job, filename=filename)

    def on_message(level: str, msg: str) -> None:
        low = msg.lower()
        if "already been recorded in the archive" in low:
            manager.update(job, status="skipped", note="Already downloaded earlier", progress=1.0)
        elif "has already been downloaded" in low:
            manager.update(job, note="File already exists", progress=1.0)

    logger = _Logger(on_message)
    opts = download_opts(job.settings)
    opts.update({
        "logger": logger,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "post_hooks": [post_hook],
        "ignoreerrors": False,
        "noplaylist": True,
    })
    os.makedirs(opts["paths"]["home"], exist_ok=True)
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([job.video["url"]])
