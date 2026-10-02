"""Resolve a Douyin share link and pull metadata + audio with yt-dlp.

Handles the three link shapes users actually paste:
  - https://v.douyin.com/<code>/          (App share short link)
  - https://www.iesdouyin.com/share/video/<id>/
  - https://www.douyin.com/video/<id>

Emits a JSON object on stdout with a `status` field so callers can branch
without parsing human-readable output.

Usage:
    python fetch_video.py --url URL --outdir DIR [--cookies JAR] [--meta-only]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media_env

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

VIDEO_ID_RE = re.compile(r"/(?:video|note)/(\d{15,25})")


def die(code: int, message: str, **extra):
    payload = {"status": "error", "code": code, "message": message}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0)  # emitting a clean JSON result is this script's success path


def resolve(url: str) -> str:
    """Follow short links until a Douyin video URL with an id appears."""
    seen = set()
    current = url.strip()
    for _ in range(6):
        if current in seen:
            break
        seen.add(current)

        match = VIDEO_ID_RE.search(current)
        if match:
            return f"https://www.douyin.com/video/{match.group(1)}"

        req = urllib.request.Request(current, headers={"User-Agent": MOBILE_UA})

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None

        opener = urllib.request.build_opener(NoRedirect)
        try:
            with opener.open(req, timeout=25) as resp:
                current = resp.geturl()
                continue
        except urllib.error.HTTPError as exc:
            location = exc.headers.get("Location")
            if exc.code in (301, 302, 303, 307, 308) and location:
                current = urllib.parse.urljoin(current, location)
                continue
            body = exc.read().decode("utf-8", "replace")
            match = VIDEO_ID_RE.search(body) or VIDEO_ID_RE.search(current)
            if match:
                return f"https://www.douyin.com/video/{match.group(1)}"
            die(10, f"could not resolve link (HTTP {exc.code})", url=url)
        except Exception as exc:  # noqa: BLE001
            die(10, f"could not resolve link: {type(exc).__name__}: {exc}", url=url)

    match = VIDEO_ID_RE.search(current)
    if match:
        return f"https://www.douyin.com/video/{match.group(1)}"
    die(11, "no video id found in link", url=url)


def run_ytdlp(args: list, cookies: str | None) -> subprocess.CompletedProcess:
    try:
        ffmpeg_dir = os.path.dirname(media_env.ensure_tools()["ffmpeg"])
    except FileNotFoundError as exc:
        die(23, str(exc))
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress",
           "--ffmpeg-location", ffmpeg_dir]
    if cookies and os.path.exists(cookies):
        cmd += ["--cookies", cookies]
    cmd += args
    return subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--cookies", default=None, help="Netscape cookie jar from fetch_cookies.py")
    ap.add_argument("--meta-only", action="store_true", help="skip media download")
    args = ap.parse_args()

    canonical = resolve(args.url)
    os.makedirs(args.outdir, exist_ok=True)

    if args.meta_only:
        proc = run_ytdlp(["--dump-single-json", "--skip-download", canonical], args.cookies)
        if proc.returncode != 0 or not proc.stdout.strip():
            die(20, "metadata fetch failed", detail=proc.stderr.strip()[:500], url=canonical)
        info = json.loads(proc.stdout)
        print(json.dumps({
            "status": "ok", "mode": "meta", "canonical_url": canonical,
            "id": info.get("id"), "title": info.get("title"),
            "uploader": info.get("uploader"), "duration": info.get("duration"),
            "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "description": info.get("description"),
        }, ensure_ascii=False))
        return 0

    stem = os.path.join(args.outdir, "audio")
    proc = run_ytdlp(
        ["-f", "bestaudio/best", "-x", "--audio-format", "mp3", "-o", stem + ".%(ext)s", canonical],
        args.cookies,
    )
    if proc.returncode != 0:
        die(21, "download failed", detail=proc.stderr.strip()[:500], url=canonical)

    audio = None
    for ext in (".mp3", ".m4a", ".webm", ".opus", ".wav"):
        if os.path.exists(stem + ext):
            audio = stem + ext
            break
    if not audio:
        die(22, "download succeeded but no audio file was produced", url=canonical)

    meta_proc = run_ytdlp(["--dump-single-json", "--skip-download", canonical], args.cookies)
    info = {}
    if meta_proc.returncode == 0 and meta_proc.stdout.strip():
        try:
            info = json.loads(meta_proc.stdout)
        except json.JSONDecodeError:
            info = {}

    print(json.dumps({
        "status": "ok", "mode": "full", "canonical_url": canonical,
        "id": info.get("id"), "title": info.get("title"),
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"), "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"), "save_count": info.get("save_count"),
        "description": info.get("description"),
        "audio_path": os.path.abspath(audio),
        "audio_bytes": os.path.getsize(audio),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
