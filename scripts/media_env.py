"""Locate ffmpeg/ffprobe and give Playwright a writable temp directory.

Two environment traps this absorbs:

  1. ffmpeg often lives outside PATH on Windows, and a wrapper such as
     ffmpeg.CMD may shadow the real ffmpeg.exe. yt-dlp only recognises the
     native .exe, so prefer real executables over shims.
  2. Chromium creates its artifact directory under %TEMP%. In sandboxed shells
     that path is not writable and launch fails with
     "EPERM: operation not permitted, mkdtemp". Fall back to a directory beside
     this script.

Import this module before invoking yt-dlp, FunASR or Playwright.
"""

import os
import shutil
import tempfile

CANDIDATE_DIRS = [
    # Resolved at call time, not import time: the user home is not always
    # LOCALAPPDATA's parent (portable installs, redirected profiles), and
    # os.path.expandvars is not applied by isdir() for us.
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "ffmpeg", "bin"),
    os.path.join(os.path.expanduser("~"), "AppData", "Local", "ffmpeg", "bin"),
    r"C:\Program Files\ffmpeg\bin",
    r"C:\ProgramData\chocolatey\bin\ffmpeg",
]


def ensure_temp_dir() -> str:
    """Return a writable temp directory, redirecting %TEMP% if necessary."""
    try:
        probe = tempfile.NamedTemporaryFile(dir=tempfile.gettempdir())
        probe.close()
        return tempfile.gettempdir()
    except Exception:  # noqa: BLE001 - any failure means we need our own
        fallback = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp")
        os.makedirs(fallback, exist_ok=True)
        os.environ["TEMP"] = fallback
        os.environ["TMP"] = fallback
        tempfile.tempdir = fallback
        return fallback


def _real_exe(directory: str, stem: str) -> str | None:
    """Prefer a native executable over any .cmd/.bat wrapper beside it."""
    candidate = os.path.join(directory, stem + ".exe")
    return candidate if os.path.exists(candidate) else None


def ensure_tools(verbose: bool = False) -> dict:
    """Put a usable ffmpeg on PATH; return resolved ffmpeg/ffprobe paths."""
    resolved = {"ffmpeg": None, "ffprobe": None}

    for directory in CANDIDATE_DIRS:
        if not directory or not os.path.isdir(directory):
            continue
        ffmpeg = _real_exe(directory, "ffmpeg")
        if ffmpeg:
            ffprobe = _real_exe(directory, "ffprobe")
            resolved = {"ffmpeg": ffmpeg, "ffprobe": ffprobe or ffmpeg}
            os.environ["PATH"] = directory + os.pathsep + os.environ.get("PATH", "")
            break

    if not resolved["ffmpeg"]:
        found = shutil.which("ffmpeg")
        if found:
            resolved["ffmpeg"] = found
            resolved["ffprobe"] = shutil.which("ffprobe") or found
            os.environ["PATH"] = os.path.dirname(found) + os.pathsep + os.environ.get("PATH", "")

    if not resolved["ffmpeg"]:
        raise FileNotFoundError(
            "ffmpeg not found. Install FFmpeg and add its bin directory to PATH."
        )

    ensure_temp_dir()

    if verbose:
        print(f"ffmpeg  -> {resolved['ffmpeg']}")
        print(f"ffprobe -> {resolved['ffprobe']}")
    return resolved
