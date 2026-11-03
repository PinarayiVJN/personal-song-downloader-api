# Personal Song Downloader API

FastAPI backend for a personal audio downloader. It uses **yt-dlp 2026.10.26** to inspect supported media pages and retrieve audio, and FFmpeg to convert the result to MP3.

Use this service only for media you are legally permitted to download and in accordance with the source platform's terms.

## Runtime and media dependencies

- Python 3.11 or later (yt-dlp 2026.10.26 no longer supports Python 3.10).
- FFmpeg and ffprobe for audio post-processing.
- `yt-dlp-ejs` and Deno for the current yt-dlp YouTube JavaScript challenge support path.
- The container installs Python dependencies from `requirements.txt`, FFmpeg from Debian packages, and Deno using its official installer.
- yt-dlp's EJS remote component is enabled with `YTDLP_REMOTE_COMPONENTS=ejs:github`.

The upstream release notes and dependency guidance are linked below:
- https://github.com/yt-dlp/yt-dlp/releases/tag/2026.10.26
- https://github.com/yt-dlp/yt-dlp#dependencies

## Features

- No login/authentication in this initial version.
- `GET /health` for the frontend connection test, including backend and yt-dlp versions.
- Format lookup, download creation, job status, and MP3 file retrieval.
- One queued/running job at a time.
- 300 MiB default maximum output size.
- Temporary job files cleaned up after the configured TTL.
- Configurable CORS and media-domain allowlist.
- FastAPI docs at `/docs`.

## API contract

### `GET /health`
Returns `ok: true`, service version, and installed yt-dlp version.

### `POST /api/formats`
JSON body:

```json
{"url":"https://www.youtube.com/watch?v=..."}
```

Returns `title`, `duration_seconds`, `source`, and `formats`. Each format contains `format_id`, `label`, `estimated_size_bytes`, and `audio_bitrate_kbps`.

### `POST /api/downloads`
JSON body:

```json
{"url":"https://www.youtube.com/watch?v=...","format_id":"bestaudio/best"}
```

Returns HTTP 202 with `id`, `status`, and `status_url`.

### `GET /api/downloads/{id}`
Returns `queued`, `running`, `completed`, or `failed`; includes `file_url` after completion.

### `GET /api/downloads/{id}/file`
Returns the MP3 attachment. The frontend should fetch this endpoint, turn the response into a Blob, and save it through a temporary object URL.

The existing API paths and the principal response field names are preserved for compatibility with the planned frontend.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `FRONTEND_ORIGINS` | `*` | Comma-separated browser origins allowed by CORS. |
| `ALLOWED_MEDIA_DOMAINS` | `youtube.com,youtu.be,music.youtube.com,soundcloud.com,bandcamp.com` | Allowed media hostnames. |
| `MAX_FILE_BYTES` | `314572800` | Maximum permitted media/output size (300 MiB). |
| `JOB_TTL_SECONDS` | `1800` | Retention for completed/failed jobs. |
| `MAX_ACTIVE_JOB_AGE_SECONDS` | `7200` | Cleanup threshold for abandoned in-memory jobs. |
| `YTDLP_REMOTE_COMPONENTS` | `ejs:github` | yt-dlp remote EJS component source. |
| `DOWNLOAD_DIR` | `/tmp/song-downloader` | Temporary working directory. |

To enable another media site, add its exact parent hostname to `ALLOWED_MEDIA_DOMAINS` in Render. A domain being allowed by this application does not guarantee that yt-dlp currently supports it.

## Local setup

Requires Python 3.11+, FFmpeg, Deno, and the requirements in `requirements.txt`.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/health` and `http://localhost:8000/docs`.

## Render deployment

The repository includes `render.yaml` for Blueprint deployment and a Dockerfile. The service listens on Render's assigned `$PORT`, has `/health` configured as its health check, and requests the Free plan.

After pushing an update to the connected branch, Render should automatically rebuild the Docker image. Verify:

1. The deployment logs show successful installation of `yt-dlp==2026.10.26` and `yt-dlp-ejs`.
2. Deno is available in the image (`deno --version` can be checked in a shell if available).
3. `https://YOUR-SERVICE.onrender.com/health` returns `ok: true` and reports `yt_dlp_version: 2026.10.26`.
4. `https://YOUR-SERVICE.onrender.com/docs` shows the expected API endpoints.

## Security and free-tier limitations

- This is a public unauthenticated API. Anyone who discovers the service URL can attempt to use its CPU, bandwidth, and temporary storage. The URL entry screen on the frontend is not authentication.
- The single-active-download restriction and media-domain allowlist are basic safeguards, not comprehensive abuse protection. CORS is not a security boundary for non-browser clients.
- Job state is held in memory. A process restart loses job state and temporary files; Render's filesystem is ephemeral.
- The allowlist and DNS checks provide defense in depth, not a complete sandbox against all extractor/network behaviors.
- Render Free has constrained memory, CPU, and outbound bandwidth. Avoid concurrent downloads and monitor usage.
- A successful `/health` response confirms process availability, not that every media extractor or a particular media URL will work.
