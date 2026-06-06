# YT Music Downloader

A small FastAPI web app that downloads audio from YouTube links (single songs or
playlists) and serves them as 192 kbps MP3s with proper ID3 metadata and
embedded cover art.

Designed for deployment on a **remote server**: every download is fetched,
tagged, and **streamed straight back to the requesting browser**. Nothing is
persisted on the host — `/tmp` is mounted as `tmpfs` (RAM) inside the container
and each job's work directory is deleted the moment the response completes.
The final MP3 lands in your browser's default download folder on your local
machine.

## How metadata works

yt-dlp only gives you the YouTube title and uploader, which is rarely correct.
For every track we:

1. Parse the YouTube title to guess `(artist, song)`.
2. Query the iTunes Search API for the canonical track.
3. Download 600×600 cover art from iTunes (or fall back to the embedded
   thumbnail).
4. Write ID3v2.3 tags: title, artist, album, album artist, year, genre,
   track number, disc number, and cover art.

## Project layout

- `main.py` – FastAPI backend, Server-Sent Events progress, metadata enrichment.
- `index.html` + `static/` – the web UI (glassmorphism + animated background).
- `Dockerfile` – builds the production image (Python 3.12, FFmpeg, yt-dlp).
- `docker-compose.yml` – one-command deployment; mounts `/tmp` as `tmpfs`.
- `music-downloader.service` – optional systemd unit for bare-metal installs.

## Run with Docker (recommended)

Prerequisites: Docker 20+ and Docker Compose v2.

```bash
git clone https://github.com/saurit987/YT-music-downloader.git
cd YT-music-downloader
docker compose up -d --build
```

The app will be available on <http://localhost:8000>. To expose it behind a
reverse proxy or run it on a public host, just point nginx/Caddy at port 8000
inside the container.

To follow logs:

```bash
docker compose logs -f
```

To stop / remove:

```bash
docker compose down
```

### Plain `docker run`

```bash
docker build -t yt-music-downloader .
docker run -d --name yt-music-downloader \
    -p 8000:8000 \
    --tmpfs /tmp:size=256m,mode=1777 \
    yt-music-downloader
```

## Run without Docker (legacy / Raspberry Pi)

```bash
sudo apt update
sudo apt install -y python3 python3-venv ffmpeg
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python3 main.py
```

The server listens on `0.0.0.0:8000`.

### systemd autostart

Copy `music-downloader.service` to `/etc/systemd/system/`, then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now music-downloader.service
```
