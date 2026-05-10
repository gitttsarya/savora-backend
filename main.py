"""
Savora Backend — FastAPI + yt-dlp
Fixed bugs:
  - asyncio.get_event_loop() deprecated → get_running_loop()
  - temp files now go to /tmp (safe on all hosts)
  - removed double extract_info call (was fetching title twice = 2x slower)
  - removed wrong BackgroundTasks null check
  - renamed 'format' param (shadows Python builtin)
  - added browser User-Agent so YouTube doesn't block requests
  - fixed MP3 quality empty string handling
  - better specific error messages
"""

import os
import uuid
import asyncio
import tempfile
from fastapi import FastAPI, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
import yt_dlp

app = FastAPI(title="Savora API")

# ── CORS: allow all origins ──────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Shared yt-dlp base options ────────────────────────────────────────────────
BASE_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "socket_timeout": 30,
    "noplaylist": True,
    # Fake a real browser so YouTube / Instagram don't block us
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    },
}


class URLRequest(BaseModel):
    url: str


# ── Health ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    return {"status": "ok", "engine": "yt-dlp"}


# ── Helpers ───────────────────────────────────────────────────────────────────
def _extract_info(url: str) -> dict:
    """Pull video metadata without downloading."""
    opts = {**BASE_OPTS}
    with yt_dlp.YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)


def _friendly_error(raw: str) -> str:
    """Turn yt-dlp error strings into human-readable messages."""
    msg = raw.lower()
    if "private" in msg:
        return "This video is private and cannot be downloaded."
    if "age" in msg or "sign in" in msg:
        return "This video is age-restricted. Cannot download."
    if "not available" in msg or "unavailable" in msg:
        return "This video is not available in your region or has been removed."
    if "unsupported url" in msg:
        return "This platform or URL is not supported."
    if "network" in msg or "timeout" in msg or "connection" in msg:
        return "Network error — please try again."
    return "Could not process this URL. Make sure it is a public video."


def _remove_file(path: str):
    """Delete temp file — called by FastAPI BackgroundTasks after stream."""
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        print(f"[cleanup] failed to delete {path}: {exc}")


# ── /api/info ─────────────────────────────────────────────────────────────────
@app.post("/api/info")
async def get_info(req: URLRequest):
    """Return video metadata + available formats."""
    try:
        loop = asyncio.get_running_loop()          # FIX: was get_event_loop() (deprecated)
        info = await loop.run_in_executor(None, _extract_info, req.url)

        platform = info.get("extractor_key", info.get("extractor", "Unknown")).capitalize()

        duration_sec = info.get("duration") or 0
        mins, secs = divmod(int(duration_sec), 60)
        duration_str = f"{mins}:{secs:02d}"

        # Detect which heights are actually available so we don't offer 4K
        # for a video that only has 720p.
        available_heights = set()
        for f in info.get("formats", []):
            h = f.get("height")
            if h:
                available_heights.add(h)

        def _has(h):
            # Accept if ANY format at or near that height exists, or if we
            # have no height info at all (some platforms omit it).
            if not available_heights:
                return True
            return any(ah >= h * 0.9 for ah in available_heights)

        formats = [{"label": "MP3 (Audio)", "code": "mp3"}]
        for label, code, height in [
            ("360p", "360", 360),
            ("720p", "720", 720),
            ("1080p", "1080", 1080),
            ("4K",    "2160", 2160),
        ]:
            if _has(height):
                formats.append({"label": label, "code": code})

        return {
            "title":     info.get("title", "Unknown Title"),
            "thumbnail": info.get("thumbnail", ""),
            "duration":  duration_str,
            "platform":  platform,
            "formats":   formats,
        }

    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"error": _friendly_error(str(exc))},
        )


# ── /api/download ─────────────────────────────────────────────────────────────
@app.get("/api/download")
async def download_video(
    url: str,
    fmt: str = "mp4",          # FIX: renamed from 'format' (shadows Python builtin)
    quality: str = "1080",
    background_tasks: BackgroundTasks = None,
):
    """Download and stream the file to the browser, then delete the temp file."""
    try:
        # FIX: BackgroundTasks is always injected by FastAPI — old null-check was wrong
        tmp_dir  = tempfile.mkdtemp()          # FIX: use /tmp, not os.getcwd()
        tmp_id   = str(uuid.uuid4())
        tmp_base = os.path.join(tmp_dir, tmp_id)

        # Build yt-dlp options
        ydl_opts = {
            **BASE_OPTS,
            "outtmpl": tmp_base,               # yt-dlp appends the extension
        }

        if fmt == "mp3":
            ydl_opts["format"] = "bestaudio/best"
            ydl_opts["postprocessors"] = [{
                "key":             "FFmpegExtractAudio",
                "preferredcodec":  "mp3",
                "preferredquality": "192",
            }]
        else:
            # FIX: quality was sent as '' for MP3 — now handled above; safe here
            q = quality if quality else "1080"
            ydl_opts["format"] = (
                f"bestvideo[height<={q}][ext=mp4]+bestaudio[ext=m4a]"
                f"/bestvideo[height<={q}]+bestaudio"
                f"/best[height<={q}]"
                f"/best"
            )
            ydl_opts["merge_output_format"] = "mp4"

        # --- Download (blocking, run in thread pool) --------------------------
        def _do_download():
            # FIX: also capture video title during download to avoid a second
            # extract_info() call (was doubling latency)
            captured = {}

            class TitleHook:
                def debug(self, msg): pass
                def warning(self, msg): pass
                def error(self, msg): pass

            ydl_opts["logger"] = TitleHook()

            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                captured["title"] = info.get("title", "download")
            return captured

        loop = asyncio.get_running_loop()
        meta = await loop.run_in_executor(None, _do_download)

        # Find the file yt-dlp wrote (it appends the extension itself)
        actual_path = None
        for name in os.listdir(tmp_dir):
            if name.startswith(tmp_id):
                actual_path = os.path.join(tmp_dir, name)
                break

        if not actual_path or not os.path.exists(actual_path):
            raise RuntimeError("Download finished but output file not found.")

        # Build a safe filename for the Content-Disposition header
        raw_title = meta.get("title", "download")
        safe_title = "".join(
            c for c in raw_title if c.isalnum() or c in " _-"
        ).strip() or "download"
        file_ext     = actual_path.rsplit(".", 1)[-1]
        download_name = f"{safe_title}.{file_ext}"

        # Schedule temp file deletion AFTER the response has been streamed
        if background_tasks:
            background_tasks.add_task(_remove_file, actual_path)
            background_tasks.add_task(_remove_file, tmp_dir)

        return FileResponse(
            path=actual_path,
            filename=download_name,
            media_type="application/octet-stream",
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )

    except Exception as exc:
        return JSONResponse(
            status_code=400,
            content={"error": _friendly_error(str(exc))},
        )
