from __future__ import annotations
import os, re, time, uuid, shutil, threading, ipaddress, socket
from pathlib import Path
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor
import yt_dlp
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

APP_NAME = "Personal Song Downloader API"
BASE_DIR = Path(os.getenv("DOWNLOAD_DIR", "/tmp/song-downloader"))
BASE_DIR.mkdir(parents=True, exist_ok=True)
FRONTEND_ORIGINS = [x.strip().rstrip("/") for x in os.getenv("FRONTEND_ORIGINS", "*").split(",") if x.strip()] or ["*"]
ALLOWED_MEDIA_DOMAINS = [x.strip().lower().lstrip(".") for x in os.getenv(
    "ALLOWED_MEDIA_DOMAINS", "youtube.com,youtu.be,music.youtube.com,soundcloud.com,bandcamp.com"
).split(",") if x.strip()]
MAX_FILE_BYTES = int(os.getenv("MAX_FILE_BYTES", str(300 * 1024 * 1024)))
JOB_TTL_SECONDS = int(os.getenv("JOB_TTL_SECONDS", "1800"))

app = FastAPI(title=APP_NAME, version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=FRONTEND_ORIGINS, allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"]
)
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="media-job")
jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
active_download_lock = threading.Lock()

class MediaRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
class DownloadRequest(BaseModel):
    url: str = Field(min_length=8, max_length=2048)
    format_id: str = Field(default="bestaudio/best", min_length=1, max_length=40)

def validate_media_url(raw_url: str) -> str:
    url = raw_url.strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise HTTPException(status_code=400, detail="Enter a valid public HTTP(S) media URL.")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="URLs containing embedded credentials are not accepted.")
    host = parsed.hostname.lower().rstrip(".")
    if not any(host == d or host.endswith("." + d) for d in ALLOWED_MEDIA_DOMAINS):
        raise HTTPException(status_code=400, detail="This media site is not enabled on this backend.")
    try:
        for info in socket.getaddrinfo(host, None):
            if not ipaddress.ip_address(info[4][0]).is_global:
                raise HTTPException(status_code=400, detail="The media host resolves to a non-public address.")
    except socket.gaierror:
        raise HTTPException(status_code=400, detail="Could not resolve the media host.")
    return url

def cleanup_old_jobs():
    cutoff = time.time() - JOB_TTL_SECONDS
    with jobs_lock:
        old = [jid for jid, j in jobs.items() if j.get("created_at", time.time()) < cutoff and j.get("status") in ("completed", "failed")]
        for jid in old:
            job = jobs.pop(jid, None)
            if job:
                shutil.rmtree(job.get("dir", ""), ignore_errors=True)

def audio_format_options(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 20}
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    audio = [f for f in info.get("formats", []) if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")]
    audio.sort(key=lambda f: (f.get("abr") or 0, f.get("tbr") or 0), reverse=True)
    choices = [{"format_id": "bestaudio/best", "label": "Best available audio → MP3", "estimated_size_bytes": None, "audio_bitrate_kbps": None}]
    seen = set()
    for f in audio:
        fid = str(f.get("format_id", ""))
        if not fid or fid in seen or not re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", fid):
            continue
        seen.add(fid)
        size = f.get("filesize") or f.get("filesize_approx")
        bitrate = f.get("abr") or f.get("tbr")
        choices.append({"format_id": fid, "label": f"{f.get('ext','?')} · {int(bitrate or 0)} kbps · {size or 'size unknown'} bytes",
                        "estimated_size_bytes": size, "audio_bitrate_kbps": bitrate})
    return {"title": info.get("title", "Audio"), "duration_seconds": info.get("duration"),
            "source": urlparse(url).hostname, "formats": choices[:80]}

def run_download(job_id: str, url: str, format_id: str):
    job_dir = BASE_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)
    with jobs_lock:
        jobs[job_id].update(status="running", message="Downloading and processing audio.")
    try:
        if not active_download_lock.acquire(blocking=False):
            raise RuntimeError("Another download is already running. Try again shortly.")
        try:
            opts = {
                "format": format_id, "outtmpl": str(job_dir / "%(title).120B [%(id)s].%(ext)s"),
                "noplaylist": True, "quiet": True, "no_warnings": True, "socket_timeout": 30,
                "max_filesize": MAX_FILE_BYTES, "overwrites": False,
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            }
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                expected = Path(ydl.prepare_filename(info)).with_suffix(".mp3")
            if not expected.is_file():
                candidates = [p for p in job_dir.iterdir() if p.is_file() and p.suffix.lower() == ".mp3"]
                if candidates:
                    expected = max(candidates, key=lambda p: p.stat().st_mtime)
            if not expected.is_file():
                raise RuntimeError("Processing finished but the MP3 file was not found.")
            if expected.stat().st_size > MAX_FILE_BYTES:
                raise RuntimeError("The resulting file exceeds the configured size limit.")
            with jobs_lock:
                jobs[job_id].update(status="completed", message="Audio is ready.", filename=expected.name,
                                    file_path=str(expected), size_bytes=expected.stat().st_size, title=info.get("title", expected.stem))
        finally:
            active_download_lock.release()
    except Exception as exc:
        shutil.rmtree(job_dir, ignore_errors=True)
        with jobs_lock:
            if job_id in jobs:
                jobs[job_id].update(status="failed", message=str(exc).splitlines()[0][:300])

@app.get("/health")
def health():
    return {"ok": True, "service": APP_NAME, "version": app.version}

@app.get("/")
def root():
    return {"service": APP_NAME, "ok": True, "health": "/health", "docs": "/docs"}

@app.post("/api/formats")
def get_formats(body: MediaRequest):
    cleanup_old_jobs()
    url = validate_media_url(body.url)
    try:
        return audio_format_options(url)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc).splitlines()[0][:250])

@app.post("/api/downloads", status_code=202)
def create_download(body: DownloadRequest, request: Request):
    cleanup_old_jobs()
    url = validate_media_url(body.url)
    fmt = body.format_id.strip()
    if fmt != "bestaudio/best" and not re.fullmatch(r"[A-Za-z0-9._+-]{1,40}", fmt):
        raise HTTPException(status_code=400, detail="Invalid format selection.")
    with jobs_lock:
        if any(j.get("status") in ("queued", "running") for j in jobs.values()):
            raise HTTPException(status_code=429, detail="A download is already queued or running.")
        jid = uuid.uuid4().hex
        jobs[jid] = {"id": jid, "client_ip": (request.client.host if request.client else "unknown"), "status": "queued", "message": "Download queued.",
                     "created_at": time.time(), "dir": str(BASE_DIR / jid)}
    executor.submit(run_download, jid, url, fmt)
    return {"id": jid, "status": "queued", "status_url": f"/api/downloads/{jid}"}

@app.get("/api/downloads/{job_id}")
def get_download(job_id: str):
    cleanup_old_jobs()
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="Download job not found.")
        result = {k: job.get(k) for k in ("id", "status", "message", "title", "filename", "size_bytes")}
        if job["status"] == "completed":
            result["file_url"] = f"/api/downloads/{job_id}/file"
        return result

@app.get("/api/downloads/{job_id}/file")
def download_file(job_id: str):
    with jobs_lock:
        job = jobs.get(job_id)
        if not job or job.get("status") != "completed":
            raise HTTPException(status_code=404, detail="Completed file not found or expired.")
        path, filename = Path(job["file_path"]), job["filename"]
    if not path.is_file():
        raise HTTPException(status_code=404, detail="File expired or server restarted.")
    return FileResponse(path, media_type="audio/mpeg", filename=filename, headers={"Cache-Control": "no-store"})
