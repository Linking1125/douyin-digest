"""One-shot Douyin digest: link in, cleaned transcript + metadata out.

Pipeline: cookies -> metadata/audio -> local ASR -> cleanup.
Each stage is a separate script; this one just sequences them so a single
command handles the whole job.

Prints a JSON payload with `transcript` and `meta` so the caller can summarise.

Usage:
    python digest.py --url URL [--workdir DIR] [--hotwords "..."] [--meta-only]
"""

import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interpreter

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), ".douyin-digest")


def run(script: str, args: list) -> dict:
    cmd = [sys.executable, os.path.join(HERE, script)] + args
    proc = subprocess.run(
        cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    for line in reversed(proc.stdout.splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                continue
    return {"status": "error", "code": 50,
            "message": f"{script} produced no JSON",
            "detail": (proc.stderr or proc.stdout)[-400:]}


def main() -> int:
    interpreter.ensure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--workdir", default=DEFAULT_WORKDIR)
    ap.add_argument("--hotwords", default="")
    ap.add_argument("--meta-only", action="store_true")
    ap.add_argument("--refresh-cookies", action="store_true")
    ap.add_argument("--model", default="",
                    help="ASR model id; empty uses the installed default "
                         "(SenseVoiceSmall). Pass SeaCo-Paraformer only when "
                         "rare jargon accuracy matters more than memory.")
    ap.add_argument("--threads", type=int, default=4,
                    help="torch CPU threads for ASR; lower uses less memory")
    ap.add_argument("--max-minutes", type=float, default=4.0,
                    help="split longer audio into chunks of this many minutes "
                         "(0 = no split) to bound memory on long videos")
    args = ap.parse_args()

    workdir = os.path.abspath(args.workdir)
    os.makedirs(workdir, exist_ok=True)
    cookies = os.path.join(workdir, "cookies.txt")

    def ensure_cookies(force: bool = False) -> dict:
        return run("fetch_cookies.py", ["--out", cookies] + (["--force"] if force else []))

    cookies_result = ensure_cookies(force=args.refresh_cookies)
    if cookies_result.get("status") != "ok":
        print(json.dumps({"status": "error", "stage": "cookies", **cookies_result},
                         ensure_ascii=False))
        return 0

    media_args = ["--url", args.url, "--outdir", workdir, "--cookies", cookies]
    if args.meta_only:
        media_args.append("--meta-only")

    media = run("fetch_video.py", media_args)
    # Stale signature cookies are the usual failure. Re-collect once and retry.
    if media.get("status") != "ok" and "cookies" in str(media.get("detail", "")).lower():
        ensure_cookies(force=True)
        media = run("fetch_video.py", media_args)

    # Share links can point at an image post (/note/<id>) rather than a video.
    # Those carry no audio, so transcription has nothing to work with.
    if media.get("status") != "ok":
        note = run("fetch_note.py", ["--url", args.url, "--outdir", workdir])
        if note.get("status") == "ok":
            print(json.dumps({
                "status": "ok", "url": args.url, "kind": "note",
                "canonical_url": note.get("url"), "meta": {
                    "id": note.get("id"), "title": note.get("title"),
                    "uploader": None, "duration": None, "like_count": None,
                    "comment_count": None, "description": note.get("title"),
                },
                "images": note.get("images"),
                "note": ("Image post (图文). There is no audio; read the images to "
                         "summarise. Open them with the view_image tool."),
            }, ensure_ascii=False))
            return 0

    # yt-dlp's cookie gate is stricter than the site's; some perfectly playable
    # videos still get rejected. Fall back to a real browser session.
    if media.get("status") != "ok":
        canonical = media.get("url") or args.url
        fallback = run("fetch_video_browser.py",
                       ["--url", canonical, "--outdir", workdir] +
                       (["--meta-only"] if args.meta_only else []))
        # The browser fallback reports metadata under different keys in
        # --meta-only mode; normalise so downstream code sees one shape.
        if fallback.get("status") == "ok":
            if args.meta_only:
                fallback.setdefault("title", fallback.get("page_title"))
            fallback["canonical_url"] = canonical
            fallback["fallback"] = True
        media = fallback

    if media.get("status") != "ok":
        print(json.dumps({"status": "error", "stage": "download", **media},
                         ensure_ascii=False))
        return 0

    payload = {
        "status": "ok",
        "url": args.url,
        "canonical_url": media.get("canonical_url") or media.get("url"),
        "meta": {k: media.get(k) for k in
                 ("id", "title", "uploader", "duration", "like_count",
                  "comment_count", "save_count", "description")},
        "audio_path": media.get("audio_path"),
    }

    if args.meta_only:
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    # Feed the video's own title/hashtags in as hotwords: it already contains
    # the names and places the speaker is most likely to say out loud.
    hotwords = args.hotwords
    if not hotwords:
        bits = []
        for field in (media.get("title"), media.get("description"), media.get("uploader")):
            if field:
                bits += [w for w in str(field).split() if len(w) >= 2]
        hotwords = " ".join(dict.fromkeys(bits))[:400]

    asr = run("transcribe.py",
              ["--audio", media["audio_path"], "--hotwords", hotwords,
               "--threads", str(args.threads),
               "--max-minutes", str(args.max_minutes)]
              + (["--model", args.model] if args.model else []))
    if asr.get("status") != "ok":
        print(json.dumps({"status": "error", "stage": "transcribe", **asr},
                         ensure_ascii=False))
        return 0

    raw_path = asr["transcript_path"]
    cleaned = run("clean_transcript.py", ["--input", raw_path])
    if cleaned.get("status") != "ok":
        payload["transcript"] = asr.get("transcript", "")
        payload["cleanup"] = cleaned
    else:
        payload["transcript"] = cleaned.get("preview")
        payload["clean_transcript_path"] = cleaned.get("output_path")
        payload["raw_chars"] = cleaned.get("raw_chars")
        payload["clean_chars"] = cleaned.get("clean_chars")

    payload["timing"] = {"asr_secs": asr.get("transcribe_secs"),
                         "model_load_secs": asr.get("load_secs")}
    # Music-only or silent videos legitimately yield nothing; say so rather than
    # leaving the caller to guess whether transcription failed.
    if not payload.get("clean_chars"):
        payload["note"] = ("No speech detected. The video is likely music-only, "
                           "silent, or non-Chinese speech.")
    print(json.dumps(payload, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())





