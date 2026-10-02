"""Handle Douyin image posts (图文 / note), which have no audio track.

A share link may point at /note/<id> rather than /video/<id>. These carry their
content as a carousel of images, so there is nothing to transcribe -- the images
themselves are the content.

Two details make this work:
  * the CDN signature is HTML-escaped in page source (`&amp;` -> `&`), so the
    raw URL is rejected with HTTP 403 until it is unescaped;
  * the signed CDN rejects plain urllib requests, so downloads go through the
    browser context that already holds valid session state.

Usage:
    python fetch_note.py --url URL --outdir DIR [--max-images 20]
"""

import argparse
import json
import os
import re

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Carousel images carry this marker in their CDN path; it separates real post
# images from site chrome and recommended-feed thumbnails.
IMAGE_MARKER = "tplv-dy-aweme-images"
IMAGE_RE = re.compile(r'https://[a-z0-9\-]+\.douyinpic\.com/[^"\'\\ ]*?' + IMAGE_MARKER + r'[^"\'\\ ]*')
NOTE_ID_RE = re.compile(r"/note/(\d{15,25})")
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)


def die(code: int, message: str, **extra):
    payload = {"status": "error", "code": code, "message": message}
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False))
    raise SystemExit(0)


def order_images(html: str) -> list:
    """Carousel image URLs in document order, de-duplicated by object id."""
    seen = set()
    ordered = []
    for raw in IMAGE_RE.findall(html):
        url = raw.replace("&amp;", "&")   # signature params arrive entity-escaped
        key = re.search(r"/tos[^/]+/([^/?]+)", url)
        key = key.group(1) if key else url
        if key in seen:
            continue
        seen.add(key)
        ordered.append(url)
    return ordered


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", required=True)
    ap.add_argument("--outdir", required=True)
    ap.add_argument("--max-images", type=int, default=20)
    args = ap.parse_args()

    os.makedirs(args.outdir, exist_ok=True)

    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(user_agent=UA, viewport={"width": 1280, "height": 900},
                                      locale="zh-CN")
            page = ctx.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(10000)
            html = page.content()
            final_url, page_title = page.url, page.title()

            images = order_images(html)
            if not images:
                die(71, "no carousel images found (post may be private, deleted, or a video)",
                    url=final_url, title=page_title)

            saved = []
            for index, url in enumerate(images[: args.max_images], start=1):
                ext = ".webp" if ".webp" in url.lower() else ".jpg"
                target = os.path.join(args.outdir, f"slide{index:02d}{ext}")
                try:
                    resp = ctx.request.get(url, headers={"Referer": "https://www.douyin.com/"})
                    if resp.status != 200:
                        continue
                    with open(target, "wb") as fh:
                        fh.write(resp.body())
                    saved.append(os.path.abspath(target))
                except Exception:  # noqa: BLE001 - one bad slide is not fatal
                    continue
        finally:
            browser.close()

    if not saved:
        die(72, "carousel images were found but none could be downloaded", url=final_url)

    title_match = TITLE_RE.search(html)
    clean_title = re.sub(r"\s*[-|]\s*抖音\s*$", "", (title_match.group(1) if title_match else "").strip())
    note_match = NOTE_ID_RE.search(final_url)

    print(json.dumps({
        "status": "ok",
        "kind": "note",
        "id": note_match.group(1) if note_match else None,
        "url": final_url,
        "title": clean_title or page_title,
        "images_found": len(images),
        "images_saved": len(saved),
        "images": saved,
        "note": "Image post: the content is in the images; there is no audio to transcribe.",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
