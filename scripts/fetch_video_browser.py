"""Browser fallback: resolve a Douyin video when yt-dlp refuses the request.

yt-dlp sometimes rejects a perfectly valid video with "Fresh cookies are
needed" -- its cookie gate is stricter than what the page actually requires.
The page itself still plays fine, so we let a real browser load the video and
capture the media URLs it requests, then download through yt-dlp using the very
cookies that browser session produced.

This is the second line of defence; fetch_video.py is tried first.

Usage:
    python fetch_video_browser.py --url URL --outdir DIR [--meta-only]
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media_env

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

MEDIA_MARKERS = (".365yg.com", "amemv.com", "douyinvod.com", "bytecdn.com", "ixigua.com")


def die(code: int, message: str, **extra):
    payload = {"status": "error", "code": code, "message": message}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0)


def scrape(url: str, cookie_path: str):
    """Load the page in a real browser, returning cookies + observed media URLs."""
    from playwright.sync_api import sync_playwright

    media = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900},
                                      locale="zh-CN")
            page = ctx.new_page()

            def on_response(resp):
                target = resp.url
                if any(m in target for m in MEDIA_MARKERS):
                    media.append((resp.status, target))

            page.on("response", on_response)
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            title = page.title()
            cookies = ctx.cookies()
            return cookies, media, title
        finally:
            browser.close()


def write_netscape(cookies: list, path: str) -> None:
    import time
    now = int(time.time())
    lines = ["# Netscape HTTP Cookie File", ""]
    for ck in cookies:
        if not ck.get("name"):
            continue
        expires = ck.get("expires") or 0
        expires = int(expires) if expires and expires > now else now + 86400
        domain = ck.get("domain", "")
        lines.append("\t".join([
            domain, "TRUE" if domain.startswith(".") else "FALSE", ck.get("path", "/"),
            "TRUE" if ck.get("secure") else "FALSE", str(expires), ck["name"], ck["value"],
        ]) + "\n")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True, help="canonical douyin.com/video/<id> url")
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--meta-only", action="store_true")
    args = ap.parse_args()

    try:
        ffmpeg_dir = os.path.dirname(media_env.ensure_tools()["ffmpeg"])
    except FileNotFoundError as exc:
        die(23, str(exc))

    os.makedirs(args.outdir, exist_ok=True)
    cookie_path = os.path.join(args.outdir, "cookies.browser.txt")

    try:
        cookies, media, page_title = scrape(args.url, cookie_path)
    except Exception as exc:  # noqa: BLE001
        die(60, f"browser scrape failed: {type(exc).__name__}: {exc}")

    write_netscape(cookies, cookie_path)

    ok_media = [u for s, u in media if s == 206]
    if not ok_media and args.meta_only is False:
        die(61, "browser saw no playable media (video may be private or removed)",
            observed=len(media))

    if args.meta_only:
        # Real metadata via this session's cookies; page title as the floor.
        info = fetch_meta(args.url, cookie_path, ffmpeg_dir)
        print(json.dumps({
            "status": "ok", "mode": "meta-browser", "url": args.url,
            "page_title": page_title,
            "id": info.get("id"), "title": info.get("title") or page_title,
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": info.get("duration"), "like_count": info.get("like_count"),
            "comment_count": info.get("comment_count"),
            "description": info.get("description"),
            "media_urls_seen": len(set(ok_media)),
        }, ensure_ascii=False))
        return 0

    # Retry yt-dlp with the cookies this very browser session produced.
    info = fetch_meta(args.url, cookie_path, ffmpeg_dir)
    stem = os.path.join(args.outdir, "audio")
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--no-progress",
           "--ffmpeg-location", ffmpeg_dir, "--cookies", cookie_path,
           "-f", "bestaudio/best", "-x", "--audio-format", "mp3",
           "-o", stem + ".%(ext)s", args.url]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")

    audio = next((stem + e for e in (".mp3", ".m4a", ".webm", ".opus")
                  if os.path.exists(stem + e)), None)

    if audio is None:
        # Last resort: pull the stream the browser itself used.
        audio = download_direct(ok_media, stem)

    if audio is None:
        die(62, "no audio obtained even via browser session",
            detail=(proc.stderr or "")[-400:])

    print(json.dumps({
        "status": "ok", "mode": "full-browser", "url": args.url,
        "id": info.get("id"), "title": info.get("title") or page_title,
        "uploader": info.get("uploader") or info.get("channel"),
        "duration": info.get("duration"), "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"), "save_count": info.get("save_count"),
        "description": info.get("description"),
        "audio_path": os.path.abspath(audio),
        "audio_bytes": os.path.getsize(audio),
        "source": "browser-fallback",
    }, ensure_ascii=False))
    return 0


def fetch_meta(url: str, cookie_path: str, ffmpeg_dir: str) -> dict:
    """Ask yt-dlp for structured metadata using the given cookie jar."""
    cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--cookies", cookie_path,
           "--ffmpeg-location", ffmpeg_dir, "--dump-single-json", "--skip-download", url]
    proc = subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    if proc.returncode == 0 and proc.stdout.strip():
        try:
            return json.loads(proc.stdout)
        except json.JSONDecodeError:
            pass
    return {}


def download_direct(urls: list, stem: str) -> str | None:
    """Fetch a browser-observed media URL straight to disk."""
    import urllib.request

    ffmpeg = media_env.ensure_tools()["ffmpeg"]
    target = stem + ".mp4"
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://www.douyin.com/"})
            with urllib.request.urlopen(req, timeout=60) as resp, open(target, "wb") as fh:
                while chunk := resp.read(1 << 16):
                    fh.write(chunk)
        except Exception:  # noqa: BLE001 - try the next observed URL
            continue

        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", target,
             "-vn", "-ac", "1", "-ar", "16000", stem + ".mp3"],
            capture_output=True, text=True)
        if proc.returncode == 0 and os.path.exists(stem + ".mp3"):
            os.remove(target)
            return stem + ".mp3"
        if os.path.exists(target):
            os.remove(target)
    return None


if __name__ == "__main__":
    raise SystemExit(main())
