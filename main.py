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
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, Request, Form, BackgroundTasks
from starlette.background import BackgroundTask
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import yt_dlp
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TDRC, TCON, TRCK, TPOS, TPUB, APIC
from mutagen.id3 import ID3NoHeaderError

TMP_PREFIX = "yt-music-job-"

# Cap on retained jobs that the user hasn't downloaded yet
MAX_KEPT_JOBS = 20
# TTL for completed jobs that the user never fetched (seconds)
COMPLETED_TTL = 600  # 10 minutes
# Stale tmp dirs left over from a crash are removed if older than this (seconds)
STALE_TMP_MAX_AGE = 3600  # 1 hour


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: nuke any leftover work dirs from a previous container run
    _purge_stale_tmp_dirs()
    yield
    # Shutdown: best-effort cleanup of all current work dirs
    for d in list(job_dirs.values()):
        shutil.rmtree(d, ignore_errors=True)
    for d, _ in list(job_done.values()):
        shutil.rmtree(d, ignore_errors=True)


app = FastAPI(lifespan=lifespan)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")
templates = Jinja2Templates(directory=BASE_DIR)


def _purge_stale_tmp_dirs() -> None:
    """Remove any leftover job work dirs from a previous run.

    We only target dirs whose mtime is older than STALE_TMP_MAX_AGE so we don't
    race with another in-flight container (defensive, even though we run a
    single instance).
    """
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


# Per-job progress queues, keyed by job_id
progress_queues: dict[str, asyncio.Queue] = {}
# Per-job work directory, so the file endpoint can serve the result
job_dirs: dict[str, str] = {}
# Completed-but-not-yet-fetched jobs: job_id -> (work_dir, expires_at_epoch)
job_done: dict[str, tuple[str, float]] = {}


# ============================================================
# Progress helpers
# ============================================================

async def emit(job_id: str, payload: dict) -> None:
    q = progress_queues.get(job_id)
    if q is not None:
        await q.put(payload)


# ============================================================
# Filename / title helpers
# ============================================================

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
    """Sanitize a string so it's safe to use as a filename."""
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
    """Try to split a YouTube title into (artist, title)."""
    if not raw_title:
        return None, None
    t = raw_title.strip()
    for pattern in _TITLE_ARTIST_PATTERNS:
        m = pattern.match(t)
        if m:
            return clean_artist(m.group("artist")), m.group("title").strip()
    return None, t


# ============================================================
# External lookups (iTunes Search API)
# ============================================================

async def lookup_itunes(artist: Optional[str], title: Optional[str]) -> Optional[dict]:
    """Search the iTunes catalog for the best-matching song."""
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


# ============================================================
# ID3 metadata
# ============================================================

_ID3_KEYS_TO_CLEAR = ("TIT2", "TPE1", "TALB", "TPE2", "TDRC",
                      "TCON", "TRCK", "TPOS", "TPUB", "APIC")


def _is_png(data: bytes) -> bool:
    return data[:8] == b"\x89PNG\r\n\x1a\n"


def _is_jpeg(data: bytes) -> bool:
    return data[:2] == b"\xff\xd8"


def apply_metadata(mp3_path: str, tags: dict, cover: Optional[bytes]) -> bool:
    """Apply rich ID3v2.3 tags + cover art to an MP3 file."""
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


def extract_embedded_cover(mp3_path: str) -> Optional[bytes]:
    try:
        audio = ID3(mp3_path)
        for key in audio.keys():
            if key.startswith("APIC"):
                return audio[key].data
    except Exception:
        pass
    return None


# ============================================================
# Core metadata enrichment
# ============================================================

async def enrich_metadata(mp3_path: str, info: dict, job_id: Optional[str] = None) -> dict:
    """Enrich an MP3 file with proper title, artist, album, year, genre, and cover art.

    yt-dlp only gives us the YouTube title and uploader, which is rarely correct for
    the song's actual metadata. This function:

      1. Parses the YouTube title to guess (artist, song).
      2. Queries the iTunes Search API for the canonical track metadata.
      3. Downloads 600x600 cover art from iTunes (falling back to the embedded
         thumbnail that yt-dlp already placed in the file).
      4. Writes ID3v2.3 tags: title, artist, album, album artist, year, genre,
         track number, disc number, and cover art.

    Returns the final tag dict that was written.
    """
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
            art_url = re.sub(r"\d+x\d+bb", "600x600bb", art_url)
            cover = await fetch_bytes(art_url)
    if not cover:
        cover = extract_embedded_cover(mp3_path)

    apply_metadata(mp3_path, tags, cover)

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


# ============================================================
# yt-dlp wrapper
# ============================================================

def _find_mp3s(work_dir: str) -> list[str]:
    out: list[str] = []
    for root, _, files in os.walk(work_dir):
        for f in files:
            if f.lower().endswith(".mp3"):
                out.append(os.path.join(root, f))
    out.sort()
    return out


def yt_dlp_run(url: str, is_playlist: bool, work_dir: str,
               status_cb) -> dict:
    """Run yt-dlp with progress callbacks. status_cb is thread-safe."""
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": f"{work_dir}/%(title).180s.%(ext)s",
        "writethumbnail": True,
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3",
             "preferredquality": "192"},
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
    """Server-Sent Events stream for a running job."""
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
            pass  # cleanup happens in run_job

    return StreamingResponse(event_gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/download/start")
async def download_start(
    background_tasks: BackgroundTasks,
    url: str = Form(...),
    type: str = Form("single"),
):
    if not url or not url.lower().startswith(("http://", "https://")):
        return JSONResponse(status_code=400,
                            content={"error": "Please paste a valid http(s) URL."})

    is_playlist = (type == "playlist")
    job_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()
    progress_queues[job_id] = queue

    def status_cb(event: dict):
        # Called from yt-dlp's thread; marshal onto the event loop.
        loop.call_soon_threadsafe(queue.put_nowait, event)

    background_tasks.add_task(run_job, job_id, url, is_playlist, status_cb)
    return {"job_id": job_id}


async def run_job(job_id: str, url: str, is_playlist: bool, status_cb) -> None:
    """The actual download + enrichment pipeline, run in the background."""
    # Per-job tmp dir name makes it easy to spot/clean leftovers
    work_dir = tempfile.mkdtemp(prefix=TMP_PREFIX + job_id[:8] + "-")
    job_dirs[job_id] = work_dir

    try:
        await emit(job_id, {"type": "stage", "stage": "starting"})

        result = await asyncio.to_thread(
            yt_dlp_run, url, is_playlist, work_dir, status_cb
        )
        entries = result.get("entries", [])
        if not entries:
            await emit(job_id, {"type": "error",
                                "message": "No tracks could be downloaded."})
            return

        await emit(job_id, {"type": "stage",
                            "stage": "tagging",
                            "total": len(entries)})

        mp3_paths = _find_mp3s(work_dir)
        if len(mp3_paths) == len(entries):
            pairs = list(zip(mp3_paths, entries))
        else:
            by_title = {os.path.splitext(os.path.basename(p))[0].lower(): p
                        for p in mp3_paths}
            pairs = []
            for e in entries:
                key = (e.get("title") or "").lower()
                p = by_title.get(key) or by_title.get(key[:60])
                if p:
                    pairs.append((p, e))
            if not pairs:
                pairs = [(p, {}) for p in mp3_paths]

        for i, (mp3_path, info) in enumerate(pairs, 1):
            if not os.path.exists(mp3_path):
                continue
            await emit(job_id, {"type": "track",
                                "index": i,
                                "total": len(pairs),
                                "title": info.get("title") or
                                         os.path.splitext(os.path.basename(mp3_path))[0]})
            try:
                await enrich_metadata(mp3_path, info, job_id)
            except Exception as e:
                print(f"Enrich error on {mp3_path}: {e}")

        if is_playlist and len(pairs) > 1:
            zip_path = os.path.join(work_dir, "playlist.zip")
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                for mp3_path, _ in pairs:
                    if os.path.exists(mp3_path):
                        base = os.path.splitext(os.path.basename(mp3_path))[0]
                        zf.write(mp3_path, safe_name(base) + ".mp3")
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
            mp3_path, _ = pairs[0]
            base = os.path.splitext(os.path.basename(mp3_path))[0]
            job_done[job_id] = (work_dir, time.time() + COMPLETED_TTL)
            _trim_old_jobs()
            _schedule_expiry(job_id)
            await emit(job_id, {
                "type": "done",
                "kind": "file",
                "url": f"/download/file/{job_id}",
                "filename": safe_name(base) + ".mp3",
                "title": pairs[0][1].get("title") or base,
            })
    except Exception as e:
        print(f"Job {job_id} failed: {e}")
        await emit(job_id, {"type": "error", "message": str(e)})
    finally:
        # Remove the queue; the work_dir stays around so the client can fetch
        # the file via /download/file/{job_id}.
        progress_queues.pop(job_id, None)


def _trim_old_jobs() -> None:
    """Evict the oldest kept jobs once we exceed the cap."""
    while len(job_dirs) > MAX_KEPT_JOBS:
        oldest_id, oldest_dir = next(iter(job_dirs.items()))
        job_dirs.pop(oldest_id, None)
        v = job_done.pop(oldest_id, None)
        if v:
            shutil.rmtree(v[0], ignore_errors=True)
        elif oldest_dir:
            shutil.rmtree(oldest_dir, ignore_errors=True)


def _schedule_expiry(job_id: str) -> None:
    """Sweep job_done entries whose TTL has expired."""
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
        # Job hasn't finished yet — still let the user fetch
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

    mp3s = _find_mp3s(work_dir)
    if not mp3s:
        return JSONResponse(status_code=404, content={"error": "file missing"},
                            background=cleanup)
    fp = mp3s[0]
    base = os.path.splitext(os.path.basename(fp))[0]
    return FileResponse(fp, media_type="audio/mpeg",
                        filename=safe_name(base) + ".mp3",
                        background=cleanup)


def _cleanup_job(work_dir: str) -> None:
    """Remove a job's work directory and any temp files."""
    try:
        shutil.rmtree(work_dir, ignore_errors=True)
    except Exception as e:
        print(f"Cleanup error for {work_dir}: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
