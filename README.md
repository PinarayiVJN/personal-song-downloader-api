# Personal Song Downloader API

Standalone FastAPI backend for a personal audio downloader. Uses `yt-dlp` to inspect supported media pages and retrieve audio, and FFmpeg to convert output to MP3. Use only for media you have permission to download and in accordance with source-platform terms.

## Features
- Public API: no login/authentication in this initial version.
- `GET /health` for the frontend's connection test.
- Format lookup, download creation, job status, and MP3 file retrieval.
- MP3 output, 300 MiB default output limit, one queued/running job at a time, and temporary file cleanup.
- Configurable CORS and media-domain allowlist.
- FastAPI docs at `/docs`.

## Important security and reliability notes
- Because this API is unauthenticated, anyone who discovers the backend URL can attempt to use it. The single-job limit and media-domain allowlist reduce some risk but are not a complete abuse-prevention system. Do not treat the backend URL as a secret or as authentication.
- CORS is set to `*` by default so a frontend can connect during setup. Once the Vercel frontend exists, set `FRONTEND_ORIGINS` to its exact origin if you want browser-based requests restricted to that origin. CORS is not a security boundary for non-browser clients.
- Job IDs are random, but status and file endpoints are public to anyone who knows the ID. Do not put private media or sensitive files through this service.
- Job state and ownership are held in memory. A restart loses job state and temporary files.
- Render Free has an ephemeral filesystem, limited CPU/RAM and outbound bandwidth, and may restart or spin down.
- The default media allowlist is YouTube, SoundCloud, and Bandcamp domains. Some extractors may require additional hostnames. Add domains only when you intend to support them.
- URL validation is defense-in-depth, not a complete sandbox for every extractor behavior.

## Local setup
Requires Python 3.11+ and FFmpeg installed locally.

```bash
python -m venv .venv
# Windows: .venv\\Scripts\\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

Open `http://localhost:8000/health` and `http://localhost:8000/docs`.

## API contract

### `GET /health`
Returns `{"ok":true,...}`. The frontend should call this to test the entered backend URL. A successful connection test only verifies that the API responds; it does not prove that the backend is trusted or authorized.

### `POST /api/formats`
JSON body: `{"url":"https://www.youtube.com/watch?v=..."}`. Returns title, duration, source host, and audio format options.

### `POST /api/downloads`
JSON body: `{"url":"https://www.youtube.com/watch?v=...","format_id":"bestaudio/best"}`. Returns HTTP 202 with a job ID.

### `GET /api/downloads/{id}`
Returns `queued`, `running`, `completed`, or `failed`; includes `file_url` on completion.

### `GET /api/downloads/{id}/file`
Returns the MP3 as an attachment. The frontend can fetch this endpoint and save the response as a Blob.

## Render deployment
1. Push this project to a GitHub repository.
2. In Render, choose **New → Web Service** and connect that repository. Alternatively, use the included `render.yaml` as a Blueprint.
3. Choose the **Free** plan.
4. The project uses Docker; Render will build the included `Dockerfile`, which installs FFmpeg and Python dependencies.
5. The health check path is `/health`. The service listens on Render's assigned `$PORT`.
6. After deployment, test `https://YOUR-SERVICE.onrender.com/health` and `https://YOUR-SERVICE.onrender.com/docs`.
7. When the Vercel frontend is deployed, optionally change `FRONTEND_ORIGINS` from `*` to the exact Vercel origin (for example `https://your-app.vercel.app`) and redeploy.

The backend intentionally allows only one queued/running job at a time. This reduces contention but does not guarantee every source or long download will succeed.
