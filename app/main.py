from __future__ import annotations

import ipaddress
import os
import re
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_NAME = "Personal Song Downloader API"
APP_VERSION = "0.2.0"
YTDLP_VERSION = yt_dlp.version.__version__
BASE_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/song-downloader")).resolve()
BASE_DIR.mkdir(parents=True, exist_ok=True)

FRONTEND_ORIGINS = [
    value.strip().rstrip("/")
    for value in os.getenv("FRONTEND_ORIGINS", "*").split(",")
    if value.strip()
] or ["*"]
ALLOWED_MEDIA_DOMAINS = [
    value.strip().lower().lstrip(".").rstrip(".")
    for value in os.getenv(
        "ALLOWED_MEDIA_DOMAINS",
        "youtube.com,youtu.be,music.youtube.com,soundcloud.com,bandcamp.com",
    ).split(",")
    if value.strip()
]
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(300 * 1024 * 1024)))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "1800"))
MAX_ACTIVE_JOB_AGE_SECONDS = int(os.getenv("MAX_ACTIVE_JOB_AGE_SECONDS", "7200"))
REMOTE_COMPONENTS = [
    value.strip()
    for value in os.getenv("YTDLP_REMOTE_COMPONENTS", "ejs:github").split(",")
    if value.strip()
]

app = FastAPI(title=APP_NAME, version=APP_VERSION)
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-job")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
active_download_lock = threading.Lock()


class MediaRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)


class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    format_id: str = Field(default="bestaudio/best", min_length=1, max_length=100)


def validate_media_url(raw_url: str) -> str:
    """Accept only HTTP(S) URLs on the explicitly configured media host allowlist."""
    url = raw_url.strip()
    try:
        parsed = urlparse(url)
        host = parsed.hostname
        # Accessing .port also validates malformed port declarations.
        _ = parsed.port
    except ValueError:
        raise HTTPException(status_code=400, detail="The media URL is malformed.")

    if parsed.scheme not in ("https", "http") or not host:
        raise HTTPException(status_code=400, detail="Enter a valid public HTTP(S) media URL.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URLs containing embedded credentials are not accepted.")

    host = host.lower().rstrip(".")
    allowed = any(host == domain or host.endswith("." + domain) for domain in ALLOWED_MEDIA_DOMAINS)
    if not allowed:
        raise HTTPException(status_code=400, detail="This media site is not enabled on this backend.")

    # Defense in depth against obvious DNS-to-private-network destinations.
    try:
        addresses = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80))
        if not addresses:
            raise HTTPException(status_code=400, detail="Could not resolve the media host.")
        for address in addresses:
            ip = ipaddress.ip_address(address[4][0])
            if not ip.is_global:
                raise HTTPException(status_code=400, detail="The media host resolves to a non-public address.")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve the media host.")

    return url


def ydl_base_options() -> dict:
    options = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 25,
        "retries": 2,
        "extractor_retries": 2,
        "remote_components": REMOTE_COMPONENTS,
    }
    return options


def cleanup_old_jobs() -> None:
    now = time.time()
    with jobs_lock:
        stale_ids = []
        for job_id, job in jobs.items():
            age = now - job.get("created_at", now)
            terminal = job.get("status") in ("completed", "failed")
            abandoned = job.get("status") in ("queued", "running") and age > MAX_ACTIVE_JOB_AGE_SECONDS
            if (terminal and age > JOB_TTL_SECONDS) or abandoned:
                stale_ids.append(job_id)
        for job_id in stale_ids:
            job = jobs.pop(job_id, None)
            if job:
                shutil.rmtree(job.get("dir", ""), ignore_errors=True)


def audio_format_options(url: str) -> dict:
    options = ydl_base_options()
    options.update(skip_download=True)
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=False)

    audio_formats = [
        item
        for item in info.get("formats", [])
        if item.get("acodec") not in (None, "none")
        and item.get("vcodec") in (None, "none")
    ]
    audio_formats.sort(
        key=lambda item: (item.get("abr") or 0, item.get("tbr") or 0),
        reverse=True,
    )

    choices = [
        {
            "format_id": "bestaudio/best",
            "label": "Best available audio → MP3",
            "estimated_size_bytes": None,
            "audio_bitrate_kbps": None,
        }
    ]
    seen: set[str] = set()
    for item in audio_formats:
        format_id = str(item.get("format_id", ""))
        if (
            not format_id
            or format_id in seen
            or not re.fullmatch(r"[A-Za-z0-9._+,-]{1,100}", format_id)
        ):
            continue
        seen.add(format_id)
        size = item.get("filesize") or item.get("filesize_approx")
        bitrate = item.get("abr") or item.get("tbr")
        size_label = f"{int(size / 1024 / 1024)} MB" if size else "size unknown"
        bitrate_label = f"{int(bitrate)} kbps" if bitrate else "bitrate unknown"
        choices.append(
            {
                "format_id": format_id,
                "label": f"{item.get('ext', 'audio')} · {bitrate_label} · {size_label}",
                "estimated_size_bytes": size,
                "audio_bitrate_kbps": bitrate,
            }
        )

    return {
        "title": info.get("title", "Audio"),
        "duration_seconds": info.get("duration"),
        "source": urlparse(url).hostname,
        "formats": choices[:80],
    }


def run_download(job_id: str, url: str, format_id: str) -> None:
    job_dir = BASE_DIR / job_id
    acquired_lock = False

    try:
        if not active_download_lock.acquire(blocking=False):
            raise RuntimeError("Another download is already running. Try again shortly.")
        acquired_lock = True
        job_dir.mkdir(parents=True, exist_ok=True)

        with jobs_lock:
            if job_id not in jobs:
                raise RuntimeError("Download job expired before it started.")
            jobs[job_id].update(status="running", message="Downloading and processing audio.")

        options = ydl_base_options()
        options.update(
            {
                "format": format_id,
                "outtmpl": str(job_dir / "%(title).120B [%(id)s].%(ext)s"),
                "max_filesize": MAX_FILE_BYTES,
                "overwrites": False,
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "192",
                    }
                ],
            }
        )

        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
            expected = Path(ydl.prepare_filename(info)).with_suffix(".mp3")

        if not expected.is_file():
            candidates = [
                path for path in job_dir.iterdir()
                if path.is_file() and path.suffix.lower() == ".mp3"
            ]
            if candidates:
                expected = max(candidates, key=lambda path: path.stat().st_mtime)

        if not expected.is_file():
            raise RuntimeError("Processing finished but the MP3 file was not found.")
        if expected.stat().st_size > MAX_FILE_BYTES:
            raise RuntimeError("The resulting file exceeds the configured size limit.")

        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(
                    status="completed",
                    message="Audio is ready.",
                    filename=expected.name,
                    file_path=str(expected),
                    size_bytes=expected.stat().st_size,
                    title=info.get("title", expected.stem),
                )

    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(
                    status="failed",
                    message=str(exc).splitlines()[0][:300] or "Download failed.",
                )
    finally:
        if acquired_lock:
            active_download_lock.release()


@app.get("/health")
def health() -> dict:
    return {
        "ok": True,
        "service": APP_NAME,
        "version": APP_VERSION,
        "yt_dlp_version": YTDLP_VERSION,
    }


@app.get("/")
def root() -> dict:
    return {"service": APP_NAME, "ok": True, "health": "/health", "docs": "/docs"}


@app.post("/api/formats")
def get_formats(body: MediaRequest) -> dict:
    cleanup_old_jobs()
    url = validate_media_url(body.url)
    try:
        return audio_format_options(url)
    except HTTPException:
        raise
    except Exception as exc:
        message = str(exc).splitlines()[0][:250] or "Could not retrieve media formats."
        raise HTTPException(status_code=400, detail=message)


@app.post("/api/downloads", status_code=202)
def create_download(body: DownloadRequest, request: Request) -> dict:
    cleanup_old_jobs()
    url = validate_media_url(body.url)
    format_id = body.format_id.strip()
    if format_id != "bestaudio/best" and not re.fullmatch(
        r"[A-Za-z0-9._+,-]{1,100}", format_id
    ):
        raise HTTPException(status_code=400, detail="Invalid format selection.")

    with jobs_lock:
        if any(job.get("status") in ("queued", "running") for job in jobs.values()):
            raise HTTPException(status_code=429, detail="A download is already queued or running.")
        job_id = uuid.uuid4().hex
        jobs[job_id] = {
            "id": job_id,
            "client_ip": request.client.host if request.client else "unknown",
            "status": "queued",
            "message": "Download queued.",
            "created_at": time.time(),
            "dir": str(BASE_DIR / job_id),
        }

    executor.submit(run_download, job_id, url, format_id)
    return {
        "id": job_id,
        "status": "queued",
        "status_url": f"/api/downloads/{job_id}",
    }


@app.get("/api/downloads/{job_id}")
def get_download(job_id: str) -> dict:
    cleanup_old_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Download job not found or expired.")
        result = {
            key: job.get(key)
            for key in ("id", "status", "message", "title", "filename", "size_bytes")
        }
        if job.get("status") == "completed":
            result["file_url"] = f"/api/downloads/{job_id}/file"
        return result


@app.get("/api/downloads/{job_id}/file")
def download_file(job_id: str) -> FileResponse:
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "completed":
            raise HTTPException(status_code=404, detail="Completed file not found or expired.")
        path = Path(job["file_path"])
        filename = job["filename"]

    if not path.is_file():
        raise HTTPException(status_code=404, detail="File expired or server restarted.")
    return FileResponse(
        path,
        media_type="audio/mpeg",
        filename=filename,
        headers={"Cache-Control": "no-store"},
    )
