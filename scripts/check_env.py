"""Report whether every douyin-digest dependency is present, in one pass.

Run this before debugging a pipeline failure -- it distinguishes "the script is
broken" from "the environment is not set up".

Usage:
    python check_env.py
"""

import importlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import interpreter
import media_env

# Re-launch under the installer's venv before probing anything. Without this,
# running check_env.py with the system python reports every dependency as
# missing even though digest.py works fine -- the most misleading possible
# failure, since check_env exists to rule things out.
interpreter.ensure()

PACKAGES = [
    ("yt_dlp", "video download"),
    ("playwright", "cookie collection + browser fallback"),
    ("funasr", "local speech recognition"),
    ("torchaudio", "audio decoding for FunASR"),
]

# Versions known to work; a mismatch here is the usual cause of import errors.
EXPECTED = {"funasr": "1.2.6"}


def main() -> int:
    report = {"python": sys.version.split()[0], "packages": {}, "ffmpeg": None,
              "problems": []}

    for name, purpose in PACKAGES:
        try:
            mod = importlib.import_module(name)
            version = getattr(mod, "__version__", "unknown")
            entry = {"ok": True, "version": version, "purpose": purpose}
            expected = EXPECTED.get(name)
            if expected and version != expected:
                entry["warning"] = f"expected {expected}"
                report["problems"].append(f"{name} {version} (expected {expected})")
            report["packages"][name] = entry
        except Exception as exc:  # noqa: BLE001
            report["packages"][name] = {"ok": False, "purpose": purpose,
                                        "error": f"{type(exc).__name__}: {exc}"}
            report["problems"].append(f"{name} missing")

    try:
        tools = media_env.ensure_tools()
        report["ffmpeg"] = {"ok": True, "ffmpeg": tools["ffmpeg"], "ffprobe": tools["ffprobe"]}
    except FileNotFoundError as exc:
        report["ffmpeg"] = {"ok": False, "error": str(exc)}
        report["problems"].append("ffmpeg missing")

    # Chromium is a separate download from the playwright package.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
        report["chromium"] = {"ok": True}
    except Exception as exc:  # noqa: BLE001
        report["chromium"] = {"ok": False,
                              "fix": "python -m playwright install chromium",
                              "error": f"{type(exc).__name__}"}
        report["problems"].append("chromium missing")

    cache = os.path.join(os.path.expanduser("~"), ".cache", "modelscope")
    report["modelscope_cache"] = cache
    report["model_downloaded"] = os.path.isdir(cache) and bool(os.listdir(cache)) if os.path.isdir(cache) else False

    # Call, don't just import: a numpy 1.x / scipy 2.x pairing imports fine and
    # only raises on first use, so a package listing that looks clean can still
    # break every audio op. This is the cheapest place to catch that.
    try:
        import numpy as np, librosa, scipy.signal
        librosa.stft(np.zeros(1600, dtype=np.float32))
        scipy.signal.resample_poly(np.arange(16, dtype=np.float64), 1, 2)
        report["numeric_stack"] = (
            f"numpy {np.__version__} | librosa {librosa.__version__} "
            f"| scipy {scipy.__version__}"
        )
    except Exception as exc:  # noqa: BLE001
        report["numeric_stack"] = f"BROKEN: {type(exc).__name__}: {exc}"
        report["problems"].append("numpy/scipy/librosa are mutually incompatible")

    report["ready"] = not report["problems"]
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
