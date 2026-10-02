"""Fetch fresh Douyin guest cookies and write them as a Netscape cookie jar.

Douyin's anti-bot gate rejects requests that lack fresh signed cookies
(__ac_nonce / __ac_signature / ttwid). A plain anonymous page load is enough
to obtain them -- no login is required.

Usage:
    python fetch_cookies.py [--out PATH] [--ttl-days 180] [--force]
"""

import argparse
import json
import os
import sys
import time

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Cookies Douyin's edge security actually checks for.
REQUIRED = ("__ac_nonce", "__ac_signature", "ttwid")


def cookies_are_fresh(jar_path: str) -> bool:
    """Return True if the jar exists and still carries the required cookies."""
    if not os.path.exists(jar_path):
        return False
    try:
        with open(jar_path, encoding="utf-8") as fh:
            body = fh.read()
    except OSError:
        return False
    return all(name in body for name in REQUIRED)


def harvest() -> list:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            ctx = browser.new_context(
                user_agent=UA,
                viewport={"width": 1280, "height": 900},
                locale="zh-CN",
            )
            page = ctx.new_page()
            page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=60000)
            # The signature cookies are set during the initial challenge round-trips.
            page.wait_for_timeout(6000)
            return ctx.cookies()
        finally:
            browser.close()


def write_netscape(cookies: list, path: str) -> int:
    """Write cookies in Netscape format (yt-dlp reads this layout)."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    now = int(time.time())
    lines = ["# Netscape HTTP Cookie File", ""]
    for ck in cookies:
        if not ck.get("name"):
            continue
        expires = ck.get("expires") or 0
        expires = int(expires) if expires and expires > now else now + 86400
        domain = ck.get("domain", "")
        include_sub = "TRUE" if domain.startswith(".") else "FALSE"
        secure = "TRUE" if ck.get("secure") else "FALSE"
        lines.append(
            "\t".join(
                [domain, include_sub, ck.get("path", "/"), secure,
                 str(expires), ck["name"], ck["value"]]
            )
            + "\n"
        )
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(lines)
    return len(lines) - 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", required=True, help="output Netscape cookie jar path")
    ap.add_argument("--ttl-days", type=int, default=180,
                    help="re-fetch after this many days (default: 180)")
    ap.add_argument("--force", action="store_true", help="re-fetch even if the jar looks fresh")
    args = ap.parse_args()

    age_limit = args.ttl_days * 86400
    if not args.force and cookies_are_fresh(args.out) and \
            (time.time() - os.path.getmtime(args.out)) < age_limit:
        print(json.dumps({"status": "ok", "cached": True, "path": os.path.abspath(args.out)}))
        return 0

    try:
        cookies = harvest()
    except Exception as exc:  # noqa: BLE001 - surface any browser failure verbatim
        print(f"COOKIES_FAIL {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    names = {c.get("name") for c in cookies}
    missing = [n for n in REQUIRED if n not in names]
    if missing:
        print(f"COOKIES_FAIL missing={','.join(missing)} (Douyin may have tightened its gate)",
              file=sys.stderr)
        return 3

    count = write_netscape(cookies, args.out)
    print(json.dumps({"status": "ok", "cached": False,
                      "path": os.path.abspath(args.out), "count": count}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


