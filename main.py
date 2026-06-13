import os
import re
import json
import asyncio
import tempfile
import shutil
import zipfile
import uuid
import time
import glob
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Form, BackgroundTasks
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TDRC, TCON, TRCK, TPOS, TPUB, APIC
from mutagen.id3 import ID3NoHeaderError

TMP_PREFIX = "yt-music-job-"

MAX_KEPT_JOBS = 20
COMPLETED_TTL = 600
STALE_TMP_MAX_AGE = 3600

# Security
RATE_LIMIT_WINDOW = 60
RATE_LIMIT_MAX = 20
AUTH_TOKEN = os.environ.get("APP_AUTH_TOKEN", "")

_rate_limit_store: dict = defaultdict(list)

_YOUTUBE_RE = re.compile(
    r"^https?://(www\.)?(youtube\.com/(watch\?v=|embed/|v/|shorts/|playlist\?list=)|youtu\.be/)[\w-]+"
)


def _is_valid_youtube_url(url: str) -> bool:
    return bool(_YOUTUBE_RE.match(url.strip()))


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self'; "
            "style-src 'self' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; "
            "img-src 'self' data:; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; "
            "form-action 'self'"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = "downloads=(self)"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    _purge_stale_tmp_dirs()
    yield
    for d in list(job_dirs.values()):
        shutil.rmtree(d, ignore_errors=True)
    for d, _ in list(job_done.values()):
        shutil.rmtree(d, ignore_errors=True)


app = FastAPI(lifespan=lifespan)
app.add_middleware(SecurityHeadersMiddleware)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=BASE_DIR)


def _rate_limit(ip: str) -> bool:
    now = time.time()
    cutoff = now - RATE_LIMIT_WINDOW
    ts_list = _rate_limit_store[ip]
    _rate_limit_store[ip] = [t for t in ts_list if t > cutoff]
    if len(_rate_limit_store[ip]) >= RATE_LIMIT_MAX:
        return False
    _rate_limit_store[ip].append(now)
    return True


def _check_auth(request: Request) -> bool:
    if not AUTH_TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    return bool(auth == f"Bearer {AUTH_TOKEN}")


def _purge_stale_tmp_dirs() -> None:
    tmp_root = tempfile.gettempdir()
    cutoff = time.time() - STALE_TMP_MAX_AGE
    pattern = os.path.join(tmp_root, TMP_PREFIX + "*")
    removed = 0
    for path in glob.glob(pattern):
        try:
            if os.path.getmtime(path) < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"Startup cleanup error for {path}: {e}")
    if removed:
        print(f"Startup cleanup: removed {removed} stale work dir(s)")


progress_queues: dict[str, asyncio.Queue] = {}
job_dirs: dict[str, str] = {}
job_done: dict[str, tuple[str, float]] = {}


async def emit(job_id: str, payload: dict) -> None:
    q = progress_queues.get(job_id)
    if q is not None:
        await q.put(payload)


_FILENAME_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_TITLE_ARTIST_PATTERNS = [
    re.compile(r"^(?P<artist>.+?)\s*[-–—]\s*(?P<title>.+?)\s*(?:\(.*?\)|\[.*?])?\s*$"),
    re.compile(r"^(?P<title>.+?)\s+by\s+(?P<artist>.+?)\s*(?:\(.*?\)|\[.*?])?\s*$", re.IGNORECASE),
    re.compile(r"^(?P<artist>.+?)\s*\|\s*(?P<title>.+?)\s*(?:\(.*?\)|\[.*?])?\s*$"),
]
_NOISE_SUFFIXES = [
    "official video", "official audio", "official music video",
    "lyric video", "lyrics", "audio", "video", "hd", "hq",
    "full video", "official", "visualizer", "music video", "mv",
]


def safe_name(s: str, max_len: int = 180) -> str:
    if not s:
        return "song"
    s = _FILENAME_BAD.sub("", s)
    s = _WHITESPACE.sub(" ", s).strip().strip(".")
    if len(s) > max_len:
        s = s[:max_len].rsplit(" ", 1)[0] or s[:max_len]
    return s or "song"


def clean_artist(artist: Optional[str]) -> Optional[str]:
    if not artist:
        return None
    a = artist.strip()
    a = re.sub(r"\s*-\s*Topic$", "", a, flags=re.IGNORECASE).strip()
    a = re.sub(r"\s+VEVO$", "", a, flags=re.IGNORECASE).strip()
    a = re.sub(r"\s+Official$", "", a, flags=re.IGNORECASE).strip()
    return a or None


def parse_title(raw_title: str) -> tuple[Optional[str], Optional[str]]:
    if not raw_title:
        return None, None
    t = raw_title.strip()
    for pattern in _TITLE_ARTIST_PATTERNS:
        m = pattern.match(t)
        if m:
            return clean_artist(m.group("artist")), m.group("title").strip()
    return None, t


async def lookup_itunes(artist: Optional[str], title: Optional[str]) -> Optional[dict]:
    if not (artist or title):
        return None

    queries: list[str] = []
    if artist and title:
        queries.append(f"{artist} {title}")
        queries.append(f"{title} {artist}")
    if title:
        queries.append(title)

    async with httpx.AsyncClient(timeout=10.0) as client:
        for q in queries:
            try:
                resp = await client.get(
                    "https://itunes.apple.com/search",
                    params={"term": q, "entity": "song", "limit": 5},
                    headers={"User-Agent": "Mozilla/5.0 yt-music-downloader"},
                )
                if resp.status_code != 200:
                    continue
                results = resp.json().get("results", [])
                if not results:
                    continue
                if artist and title:
                    la, lt = artist.lower(), title.lower()
                    for r in results:
                        ra = (r.get("artistName") or "").lower()
                        rt = (r.get("trackName") or "").lower()
                        if (la in ra or ra in la) and (lt in rt or rt in lt):
                            return r
                return results[0]
            except Exception as e:
                print(f"iTunes lookup error: {e}")
    return None


async def fetch_bytes(url: str, timeout: float = 20.0) -> Optional[bytes]:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            resp = await client.get(url)
        if resp.status_code == 200:
            return resp.content
    except Exception as e:
        print(f"Bytes fetch failed for {url}: {e}")
    return None


_ID3_KEYS_TO_CLEAR = ("TIT2", "TPE1", "TALB", "TPE2", "TDRC",
                      "TCON", "TRCK", "TPOS", "TPUB", "APIC")


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _is_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8"


def apply_metadata(mp3_path: str, tags: dict, cover: Optional[bytes]) -> bool:
    try:
        try:
            audio = ID3(mp3_path)
        except ID3NoHeaderError:
            audio = ID3()

        for key in _ID3_KEYS_TO_CLEAR:
            try:
                audio.delall(key)
            except Exception:
                pass

        def add(tag):
            audio.add(tag)

        if tags.get("title"):
            add(TIT2(encoding=3, text=str(tags["title"])))
        if tags.get("artist"):
            add(TPE1(encoding=3, text=str(tags["artist"])))
        if tags.get("album"):
            add(TALB(encoding=3, text=str(tags["album"])))
        if tags.get("album_artist"):
            add(TPE2(encoding=3, text=str(tags["album_artist"])))
        if tags.get("year"):
            add(TDRC(encoding=3, text=str(tags["year"])[:4]))
        if tags.get("genre"):
            add(TCON(encoding=3, text=str(tags["genre"])))
        if tags.get("track_number") is not None:
            add(TRCK(encoding=3, text=str(tags["track_number"])))
        if tags.get("disc_number") is not None:
            add(TPOS(encoding=3, text=str(tags["disc_number"])))
        if tags.get("publisher"):
            add(TPUB(encoding=3, text=str(tags["publisher"])))

        if cover:
            if _is_png(cover):
                mime = "image/png"
            elif _is_jpeg(cover):
                mime = "image/jpeg"
            else:
                mime = "image/jpeg"
            add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))

        audio.save(mp3_path, v2_version=3)
        return True
    except Exception as e:
        print(f"Failed to apply metadata to {mp3_path}: {e}")
        return False


def _image_dimensions(data: bytes) -> tuple[int, int]:
    """Extract (width, height) from JPEG or PNG image bytes without PIL."""
    if _is_jpeg(data):
        i = 2
        while i + 4 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker == 0xD9 or marker == 0xDA:
                break
            if marker in (0xC0, 0xC2):
                return (data[i + 9] << 8 | data[i + 10],
                        data[i + 7] << 8 | data[i + 8])
            i += 2
            if i + 1 >= len(data):
                break
            length = data[i] << 8 | data[i + 1]
            i += length
    elif _is_png(data):
        if len(data) >= 24:
            w = data[16] << 24 | data[17] << 16 | data[18] << 8 | data[19]
            h = data[20] << 24 | data[21] << 16 | data[22] << 8 | data[23]
            return (w, h)
    return (0, 0)


def extract_embedded_cover(file_path: str) -> Optional[bytes]:
    """Extract embedded cover art from an audio file (MP3 ID3 or FLAC)."""
    if file_path.lower().endswith(".flac"):
        try:
            from mutagen.flac import FLAC
            audio = FLAC(file_path)
            pics = audio.pictures
            if pics:
                return pics[0].data
        except Exception:
            pass
        return None
    try:
        audio = ID3(file_path)
        for key in audio.keys():
            if key.startswith("APIC"):
                return audio[key].data
    except Exception:
        pass
    return None


# ============================================================
# Hi-res source search
# ============================================================

def _clean_query(s: str) -> str:
    """Clean a string for use in a search query."""
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def search_higher_quality_source(artist: Optional[str], title: Optional[str]) -> Optional[str]:
    """Search YouTube for the official best-quality version of a track.

    Uses yt-dlp to search YouTube, preferring official audio/video uploads
    that typically deliver higher bitrate audio.
    """
    if not title:
        return None
    artist_part = _clean_query(artist or "")
    title_part = _clean_query(title)
    query = f"{artist_part} - {title_part} official audio" if artist_part else f"{title_part} official audio"

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "ignoreerrors": True,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            result = ydl.extract_info(f"ytsearch3:{query}", download=False)
            if result and result.get("entries"):
                for entry in result["entries"]:
                    if entry and entry.get("id"):
                        return f"https://www.youtube.com/watch?v={entry['id']}"
    except Exception as e:
        print(f"Hi-res source search error: {e}")
    return None


# ============================================================
# FLAC metadata
# ============================================================

def apply_flac_metadata(flac_path: str, tags: dict, cover: Optional[bytes]) -> bool:
    from mutagen.flac import FLAC, Picture
    try:
        audio = FLAC(flac_path)
        audio.clear()
        audio.clear_pictures()

        if tags.get("title"):
            audio["title"] = str(tags["title"])
        if tags.get("artist"):
            audio["artist"] = str(tags["artist"])
        if tags.get("album"):
            audio["album"] = str(tags["album"])
        if tags.get("album_artist"):
            audio["albumartist"] = str(tags["album_artist"])
        if tags.get("year"):
            audio["date"] = str(tags["year"])[:4]
        if tags.get("genre"):
            audio["genre"] = str(tags["genre"])
        if tags.get("track_number") is not None:
            audio["tracknumber"] = str(tags["track_number"])
        if tags.get("disc_number") is not None:
            audio["discnumber"] = str(tags["disc_number"])

        if cover:
            pic = Picture()
            pic.data = cover
            pic.type = 3
            pic.mime = "image/jpeg" if _is_jpeg(cover) else "image/png"
            pic.desc = "Cover"
            pic.width, pic.height = _image_dimensions(cover)
            pic.depth = 24
            audio.add_picture(pic)

        audio.save()
        return True
    except Exception as e:
        print(f"Failed to apply metadata to {flac_path}: {e}")
        return False


# ============================================================
# Core metadata enrichment
# ============================================================

async def enrich_metadata(file_path: str, info: dict, job_id: Optional[str] = None, hires: bool = False) -> dict:
    raw_title = (info.get("title") or info.get("track") or "").strip()
    raw_artist = info.get("artist") or info.get("creator") or info.get("uploader")

    parsed_artist, parsed_title = parse_title(raw_title)
    artist = clean_artist(raw_artist) or parsed_artist
    title = parsed_title or raw_title

    if job_id:
        await emit(job_id, {"type": "metadata",
                            "stage": "looking_up",
                            "title": title, "artist": artist})

    itunes = await lookup_itunes(artist, title)

    tags: dict = {}
    if title:
        tags["title"] = title
    if artist:
        tags["artist"] = artist

    if itunes:
        if itunes.get("trackName"):
            tags["title"] = itunes["trackName"]
        if itunes.get("artistName"):
            tags["artist"] = itunes["artistName"]
        if itunes.get("collectionName"):
            tags["album"] = itunes["collectionName"]
        if itunes.get("trackNumber") is not None:
            tags["track_number"] = itunes["trackNumber"]
        if itunes.get("discNumber") is not None:
            tags["disc_number"] = itunes["discNumber"]
        release = itunes.get("releaseDate") or ""
        if release:
            tags["year"] = release[:4]
        if itunes.get("primaryGenreName"):
            tags["genre"] = itunes["primaryGenreName"]

    cover: Optional[bytes] = None
    if itunes:
        art_url = itunes.get("artworkUrl100", "")
        if art_url:
            size = "1200x1200bb" if hires else "600x600bb"
            art_url = re.sub(r"\d+x\d+bb", size, art_url)
            cover = await fetch_bytes(art_url)
    if not cover:
        cover = extract_embedded_cover(file_path)

    if file_path.lower().endswith(".flac"):
        apply_flac_metadata(file_path, tags, cover)
    else:
        apply_metadata(file_path, tags, cover)

    if job_id:
        await emit(job_id, {
            "type": "metadata",
            "stage": "done",
            "title": tags.get("title"),
            "artist": tags.get("artist"),
            "album": tags.get("album"),
            "year": tags.get("year"),
            "genre": tags.get("genre"),
            "cover": bool(cover),
        })

    return tags


def _find_output_files(work_dir: str) -> list[str]:
    out: list[str] = []
    for root, _, files in os.walk(work_dir):
        for f in files:
            if f.lower().endswith((".mp3", ".flac", ".mp4", ".webm")):
                out.append(os.path.join(root, f))
    out.sort()
    return out


def _video_format_str(video_quality: str) -> str:
    if video_quality == "best":
        return "bestvideo+bestaudio/best"
    return f"bestvideo[height<={video_quality}]+bestaudio/best[height<={video_quality}]"


def yt_dlp_run(url: str, is_playlist: bool, work_dir: str,
               status_cb, quality: str = "standard",
               dl_format: str = "audio", video_quality: str = "720") -> dict:
    is_video = (dl_format == "video")
    hires = (quality == "hires")

    if is_video:
        ydl_opts = {
            "format": _video_format_str(video_quality),
            "outtmpl": f"{work_dir}/%(title).180s.%(ext)s",
            "merge_output_format": "mp4",
            "noplaylist": not is_playlist,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "concurrent_fragment_downloads": 4,
            "postprocessors": [
                {"key": "FFmpegMetadata"},
            ],
        }
    else:
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": f"{work_dir}/%(title).180s.%(ext)s",
            "writethumbnail": True,
            "postprocessors": [
                {"key": "FFmpegExtractAudio",
                 "preferredcodec": "flac" if hires else "mp3",
                 "preferredquality": "0" if hires else "192"},
                {"key": "EmbedThumbnail"},
                {"key": "FFmpegMetadata"},
            ],
            "noplaylist": not is_playlist,
            "quiet": True,
            "no_warnings": True,
            "ignoreerrors": True,
            "writelocalthumbnail": False,
            "extractor-args": "youtube:player_client=web,mweb",
            "concurrent_fragment_downloads": 4,
        }

    def progress_hook(d):
        status = d.get("status")
        if status == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes", 0)
            pct = (done / total * 100.0) if total else None
            status_cb({
                "type": "downloading",
                "percent": pct,
                "speed": d.get("speed"),
                "eta": d.get("eta"),
                "filename": os.path.basename(d.get("filename", "")),
            })
        elif status == "finished":
            status_cb({
                "type": "postprocess",
                "filename": os.path.basename(d.get("filename", "")),
            })

    ydl_opts["progress_hooks"] = [progress_hook]

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        if info is None:
            return {"entries": []}

    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries", []) if e]
    else:
        entries = [info]

    return {"entries": entries, "title": info.get("title")}


# ============================================================
# Routes
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def read_root(request: Request):
    return templates.TemplateResponse(request, "index.html")


@app.get("/download/stream")
async def stream_progress(job_id: str):
    q = progress_queues.get(job_id)
    if q is None:
        return JSONResponse(status_code=404, content={"error": "unknown job"})

    async def event_gen():
        try:
            while True:
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=25.0)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"data: {json.dumps(msg)}\n\n"
                if msg.get("type") in ("done", "error"):
                    break
        finally:
            pass

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/download/start")
async def download_start(
    background_tasks: BackgroundTasks,
    request: Request,
    url: str = Form(...),
    type: str = Form("single"),
    quality: str = Form("standard"),
    format: str = Form("audio"),
    video_quality: str = Form("720"),
):
    if not _check_auth(request):
        return JSONResponse(status_code=401, content={"error": "Unauthorized. Set APP_AUTH_TOKEN environment variable to enable access."})

    client_ip = request.client.host if request.client else "unknown"
    if not _rate_limit(client_ip):
        return JSONResponse(status_code=429, content={"error": "Too many requests. Please wait before trying again."})

    if not url or not url.lower().startswith(("http://", "https://")):
        return JSONResponse(status_code=400,
                            content={"error": "Please paste a valid http(s) URL."})

    if quality not in ("standard", "hires"):
        quality = "standard"
    if format not in ("audio", "video"):
        format = "audio"

    is_playlist = (type == "playlist")
    job_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    progress_queues[job_id] = queue

    def status_cb(event: dict):
        loop.call_soon_threadsafe(queue.put_nowait, event)

    background_tasks.add_task(run_job, job_id, url, is_playlist, status_cb, quality, format, video_quality)
    return {"job_id": job_id}


async def run_job(job_id: str, url: str, is_playlist: bool, status_cb,
                  quality: str = "standard", dl_format: str = "audio",
                  video_quality: str = "720") -> None:
    hires = (quality == "hires")
    is_video = (dl_format == "video")
    work_dir = tempfile.mkdtemp(prefix=TMP_PREFIX + job_id[:8] + "-")
    job_dirs[job_id] = work_dir

    try:
        await emit(job_id, {"type": "stage", "stage": "starting"})

        # For hi-res audio: try to find a better source first
        if hires and not is_video:
            await emit(job_id, {"type": "stage", "stage": "searching_hires",
                                "message": "Searching for best quality source..."})
            try:
                probe_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": True,
                    "ignoreerrors": True,
                }
                with yt_dlp.YoutubeDL(probe_opts) as ydl:
                    probe = ydl.extract_info(url, download=False)
                    if probe:
                        probe_title = probe.get("title", "")
                        probe_artist = probe.get("artist") or probe.get("uploader", "")
                        parsed_a, parsed_t = parse_title(probe_title)
                        search_artist = clean_artist(probe_artist) or parsed_a
                        search_title = parsed_t or probe_title
                        if search_title:
                            better_url = search_higher_quality_source(search_artist, search_title)
                            if better_url and better_url != url:
                                info_title = probe.get("title", "")
                                await emit(job_id, {
                                    "type": "hires_found",
                                    "source": better_url,
                                    "original": info_title,
                                })
                                url = better_url
            except Exception as e:
                print(f"Hi-res probe error: {e}")

        result = await asyncio.to_thread(
            yt_dlp_run, url, is_playlist, work_dir, status_cb, quality, dl_format, video_quality
        )
        entries = result.get("entries", [])
        if not entries:
            await emit(job_id, {"type": "error",
                                "message": "No tracks could be downloaded."})
            return

        await emit(job_id, {"type": "stage",
                            "stage": "tagging",
                            "total": len(entries)})

        output_files = _find_output_files(work_dir)
        if len(output_files) == len(entries):
            pairs = list(zip(output_files, entries))
        else:
            by_title = {os.path.splitext(os.path.basename(p))[0].lower(): p
                        for p in output_files}
            pairs = []
            for e in entries:
                key = (e.get("title") or "").lower()
                p = by_title.get(key) or by_title.get(key[:60])
                if p:
                    pairs.append((p, e))
            if not pairs:
                pairs = [(p, {}) for p in output_files]

        for i, (file_path, info) in enumerate(pairs, 1):
            if not os.path.exists(file_path):
                continue
            await emit(job_id, {"type": "track",
                                "index": i,
                                "total": len(pairs),
                                "title": info.get("title") or
                                         os.path.splitext(os.path.basename(file_path))[0]})
            try:
                if file_path.lower().endswith((".mp3", ".flac")):
                    await enrich_metadata(file_path, info, job_id, hires=hires)
            except Exception as e:
                print(f"Enrich error on {file_path}: {e}")

        if is_playlist and len(pairs) > 1:
            zip_path = os.path.join(work_dir, "playlist.zip")
            display_ext = ".mp4" if is_video else (".flac" if hires else ".mp3")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for file_path, _ in pairs:
                    if os.path.exists(file_path):
                        base = os.path.splitext(os.path.basename(file_path))[0]
                        zf.write(file_path, safe_name(base) + display_ext)
            job_done[job_id] = (work_dir, time.time() + COMPLETED_TTL)
            _trim_old_jobs()
            _schedule_expiry(job_id)
            await emit(job_id, {
                "type": "done",
                "kind": "zip",
                "url": f"/download/file/{job_id}?kind=zip",
                "filename": "playlist.zip",
                "count": len(pairs),
            })
        else:
            file_path, _ = pairs[0]
            base, actual_ext = os.path.splitext(os.path.basename(file_path))
            ext = ".mp4" if is_video else (".flac" if hires else ".mp3")
            job_done[job_id] = (work_dir, time.time() + COMPLETED_TTL)
            _trim_old_jobs()
            _schedule_expiry(job_id)
            await emit(job_id, {
                "type": "done",
                "kind": "file",
                "url": f"/download/file/{job_id}",
                "filename": safe_name(base) + ext,
                "title": pairs[0][1].get("title") or base,
            })
    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        await emit(job_id, {"type": "error", "message": "Download failed unexpectedly."})
    finally:
        progress_queues.pop(job_id, None)


def _trim_old_jobs() -> None:
    while len(job_dirs) > MAX_KEPT_JOBS:
        oldest_id, oldest_dir = next(iter(job_dirs.items()))
        job_dirs.pop(oldest_id, None)
        v = job_done.pop(oldest_id, None)
        if v:
            shutil.rmtree(v[0], ignore_errors=True)
        elif oldest_dir:
            shutil.rmtree(oldest_dir, ignore_errors=True)


def _schedule_expiry(job_id: str) -> None:
    async def sweeper():
        try:
            await asyncio.sleep(COMPLETED_TTL + 5)
        except asyncio.CancelledError:
            return
        v = job_done.pop(job_id, None)
        if v:
            shutil.rmtree(v[0], ignore_errors=True)
    asyncio.create_task(sweeper())


@app.get("/download/file/{job_id}")
async def download_file(job_id: str, kind: str = "file"):
    entry = job_done.pop(job_id, None)
    if entry is None:
        work_dir = job_dirs.pop(job_id, None)
        if not work_dir or not os.path.isdir(work_dir):
            return JSONResponse(status_code=410, content={"error": "file expired"})
        entry = (work_dir, time.time() + COMPLETED_TTL)
    work_dir, _ = entry
    if not os.path.isdir(work_dir):
        return JSONResponse(status_code=410, content={"error": "file expired"})

    cleanup = BackgroundTask(_cleanup_job, work_dir)

    if kind == "zip":
        zp = os.path.join(work_dir, "playlist.zip")
        if not os.path.exists(zp):
            return JSONResponse(status_code=404, content={"error": "zip missing"},
                                background=cleanup)
        return FileResponse(zp, media_type="application/zip",
                            filename="playlist.zip", background=cleanup)

    output_files = _find_output_files(work_dir)
    if not output_files:
        return JSONResponse(status_code=404, content={"error": "file missing"},
                            background=cleanup)
    fp = output_files[0]
    base, ext = os.path.splitext(os.path.basename(fp))
    ext_lower = ext.lower()
    if ext_lower == ".flac":
        mime = "audio/flac"
    elif ext_lower in (".mp4", ".webm"):
        mime = "video/mp4"
    else:
        mime = "audio/mpeg"
    return FileResponse(fp, media_type=mime,
                        filename=safe_name(base) + ext,
                        background=cleanup)


def _cleanup_job(work_dir: str) -> None:
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as e:
        print(f"Cleanup error for {work_dir}: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
