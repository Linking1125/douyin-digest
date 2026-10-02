"""One-command environment setup for douyin-digest.

Run this once after downloading the skill:

    python scripts/install.py

It creates a self-contained virtual environment next to the skill (`.venv`),
installs pinned dependencies into it, downloads the ASR model, and fetches the
Playwright browser. Afterwards every script in this skill re-launches itself
under that venv automatically, so `python scripts/digest.py --url ...` just
works with whatever `python` happens to be on PATH.

Design notes:

  * We build a venv rather than installing into the caller's interpreter. The
    caller's Python may be a bare system install, a managed runtime owned by
    some other tool, or too new for funasr's wheels. Touching it would be rude
    at best and broken at worst.
  * We do NOT simply inherit the caller's version. funasr 1.2.6 pulls
    `editdistance`, which ships no wheel for Python 3.13+ and must compile from
    source — that build fails on a stock 3.14. So if the current interpreter is
    newer than MAX_PYTHON, we look for an older one on the machine and build
    the venv from that. If none exists we say exactly that, rather than failing
    15 minutes into a compile.
  * torch comes from the CPU wheel index. The default PyPI torch pulls ~2.5 GB
    of CUDA libraries that are useless here; SenseVoice on CPU is fast enough
    (a 76-second clip transcribes in ~6 seconds).
  * Everything is idempotent. Re-running skips finished steps, so a failed
    install can simply be run again.
  * We never prompt. This has to work when an agent runs it unattended.
"""

import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parent.parent
VENV_DIR = SKILL_ROOT / ".venv"

TORCH_INDEX = "https://download.pytorch.org/whl/cpu"

# Python versions we build the venv on. Above MAX_PYTHON, editdistance (a
# funasr dependency) has no wheel and its source build breaks. Below MIN_PYTHON,
# funasr's own wheels do not exist.
MIN_PYTHON = (3, 10)
MAX_PYTHON = (3, 12)

# Interpreters to consider when the caller's Python is too new.
# Ordered by trust: a real python.org install can always create a venv, whereas
# a managed IDE runtime (TRAE, and similar tool-shipped pythons) may be a
# stripped build with no `venv` module at all -- usable to RUN this skill, but
# not to build one. So we probe for venv support, not just for a version.
BASE_GLOBS = [
    r"C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe",
    r"C:\Python3*\python.exe",
    r"C:\Program Files\Python3*\python.exe",
    r"C:\Users\*\AppData\Roaming\TRAE*\ModularData\ai-agent\vm\tools\python\python.exe",
]

# funasr is pinned: 1.3+ ships a wheel missing funasr.layers and fails on
# import. Everything else is a lower bound, not a lock -- these are stable.
#
# The numpy/scipy/librosa trio is pinned together and MUST stay consistent.
# funasr 1.2.6 needs numpy<2 (it uses APIs numpy 2.x removed), but librosr and
# scipy ship wheels compiled against numpy>=2 -- pip resolves those to the
# latest versions, which import but crash on any real call with
# "module 'numpy' has no attribute 'long'". The main transcribe path hides this
# (it decodes via ffmpeg), so it surfaces later on --model sea or any resample.
# These caps are the last versions with numpy<2 wheels.
PACKAGES = [
    f"torch torchaudio --index-url {TORCH_INDEX}",
    "funasr==1.2.6",
    "yt-dlp",
    "modelscope",
    "numpy<2",  # funasr 1.2.6 uses APIs removed in numpy 2.x
    "scipy<1.14",
    "librosa<0.11",
]

# Import names -> pip names, for the verification step.
VERIFY = ["torch", "torchaudio", "funasr", "yt_dlp", "numpy"]

# Importing numpy and scipy proves nothing -- the broken pairing imports fine
# and only raises when called. Exercise each one so a bad resolve is caught
# during install instead of mid-transcription.
SMOKE = """
import numpy as np, warnings
warnings.filterwarnings("ignore")
assert np.__version__.startswith("1."), f"expected numpy<2, got {np.__version__}"
import librosa, scipy.signal
librosa.stft(np.zeros(1600, dtype=np.float32))
scipy.signal.resample_poly(np.arange(16, dtype=np.float64), 1, 2)
print("numpy", np.__version__, "| librosa", librosa.__version__,
      "| scipy", scipy.__version__)
"""

ASR_MODEL = "iic/SenseVoiceSmall"


def venv_python() -> Path:
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def say(step: str, message: str) -> None:
    print(f"[douyin-digest] {step}: {message}", flush=True)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)


def _version(path: str) -> tuple[int, ...] | None:
    try:
        res = run([path, "-c",
                   "import sys;print('%d.%d'%sys.version_info[:2])"],
                  capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        return tuple(int(p) for p in res.stdout.strip().split("."))
    except ValueError:
        return None


def _has_venv(path: str) -> bool:
    """True when this interpreter can create a virtual environment.

    A managed runtime can import everything we need yet still ship without the
    venv module, in which case it is only usable as a fallback to RUN under,
    never as a base to BUILD one.
    """
    try:
        res = run([path, "-c", "import venv"], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return res.returncode == 0


def find_base_python() -> str:
    """Return an interpreter that can both run this skill's wheels and build a
    venv for them.

    Prefers the caller's own Python when it qualifies, so a user on 3.11 gets
    exactly their version rather than something we picked for them.
    """
    # Honour an explicit pin first, and fail loudly if it is wrong. A user who
    # set DOUDYIN_PYTHON to work around a problem should not have us quietly
    # substitute something else and leave them debugging the wrong runtime.
    override = os.environ.get("DOUYIN_PYTHON")
    if override:
        if not os.path.isfile(override):
            raise SystemExit(json.dumps({
                "status": "error", "stage": "install",
                "message": f"DOUYIN_PYTHON points at a missing file: {override}",
                "detail": "Unset it to let the installer choose, or correct "
                          "the path.",
            }, ensure_ascii=False))
        ver = _version(override)
        if not ver or ver < MIN_PYTHON or ver > MAX_PYTHON:
            raise SystemExit(json.dumps({
                "status": "error", "stage": "install",
                "message": f"DOUYIN_PYTHON is "
                           f"{'.'.join(map(str, ver)) if ver else 'not a python'}"
                           f", need {'.'.join(map(str, MIN_PYTHON))}-"
                           f"{'.'.join(map(str, MAX_PYTHON))}.",
                "detail": "funasr's dependencies have no wheels outside that "
                          "range.",
            }, ensure_ascii=False))
        if not _has_venv(override):
            raise SystemExit(json.dumps({
                "status": "error", "stage": "install",
                "message": f"DOUYIN_PYTHON ({override}) cannot create a venv.",
                "detail": "Stripped runtimes (some IDEs ship one) omit the venv "
                          "module. Point DOUYIN_PYTHON at a full python.org "
                          "install instead.",
            }, ensure_ascii=False))
        say("python", f"using DOUYIN_PYTHON {'.'.join(map(str, ver))}")
        return override

    own = _version(sys.executable)
    if own and own <= MAX_PYTHON and _has_venv(sys.executable):
        return sys.executable
    if own and own <= MAX_PYTHON:
        say("python", f"your python {'.'.join(map(str, own))} has no venv "
                      "module; looking for a full install instead")

    say("python", f"need Python {'.'.join(map(str, MIN_PYTHON))}-"
                  f"{'.'.join(map(str, MAX_PYTHON))} with venv support; "
                  f"yours is {'.'.join(map(str, own or (0,)))}")
    seen = {os.path.normcase(os.path.abspath(sys.executable))}
    best = None
    for pattern in BASE_GLOBS:
        for path in sorted(glob.glob(pattern), reverse=True):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen or not os.path.isfile(path):
                continue
            seen.add(key)
            ver = _version(path)
            if not ver or ver < MIN_PYTHON or ver > MAX_PYTHON:
                continue
            if not _has_venv(path):
                say("python", f"skipping {path} (no venv module)")
                continue
            # Prefer the newest qualifying interpreter.
            if best is None or ver > best[0]:
                best = (ver, path)

    if best:
        say("python", f"using {'.'.join(map(str, best[0]))} at {best[1]}")
        return best[1]

    # Last resort, and the one that makes this script work on machines we have
    # never seen: let uv fetch a managed interpreter. `uv python install` is a
    # single command with no installer UI, no admin rights and no PATH edits --
    # it is the difference between "download and run" and "install Python first".
    uv = shutil.which("uv")
    if uv:
        target = ".".join(map(str, MAX_PYTHON))
        say("python", f"no local match; asking uv for a managed {target}")
        res = run([uv, "python", "install", target])
        if res.returncode == 0:
            found = run([uv, "python", "find", target],
                        capture_output=True, text=True)
            if found.returncode == 0 and found.stdout.strip():
                path = found.stdout.strip()
                if os.path.isfile(path) and _has_venv(path):
                    say("python", f"uv provided {path}")
                    return path
                say("python", f"uv returned {path} but it cannot build a venv")
        else:
            say("python", f"uv python install failed (exit {res.returncode})")

    raise SystemExit(json.dumps({
        "status": "error", "stage": "install",
        "message": "No usable Python found on this machine.",
        "detail": f"This skill needs Python {'.'.join(map(str, MIN_PYTHON))}-"
                  f"{'.'.join(map(str, MAX_PYTHON))} (newer versions have no "
                  "wheels for funasr's dependencies) and needs it to be a full "
                  "install, not a stripped IDE runtime. Easiest fix: install uv "
                  "(https://docs.astral.sh/uv/) and re-run this script, and it "
                  "will fetch a managed interpreter for you. Or install Python "
                  + ".".join(map(str, MAX_PYTHON)) + " from python.org, then "
                  "re-run python scripts/install.py.",
    }, ensure_ascii=False))


def build_venv() -> Path:
    py = venv_python()
    if py.exists():
        ver = _version(str(py))
        if ver and ver <= MAX_PYTHON:
            say("venv", f"reusing {VENV_DIR} (python {'.'.join(map(str, ver))})")
            return py
        # A venv built by an earlier run on too-new a Python, or a half-finished
        # install. Either way it cannot host funasr's wheels, and reusing it
        # would skip straight back into the same failure.
        say("venv", f"existing {VENV_DIR} is unusable "
                    f"(python {'.'.join(map(str, ver)) if ver else 'unknown'}); "
                    "rebuilding")
        shutil.rmtree(VENV_DIR, ignore_errors=True)

    base = find_base_python()
    say("venv", f"creating {VENV_DIR} with {base} (one-time, ~30s)")
    # Delegate to the chosen interpreter's own -m venv. venv.EnvBuilder cannot
    # target a different base interpreter (create() takes no base_python), and
    # we need exactly that: the caller's Python may be too new to host these
    # wheels even though another interpreter on the machine can.
    code = run([base, "-m", "venv", "--copies", str(VENV_DIR)]).returncode
    if code != 0 or not py.exists():
        shutil.rmtree(VENV_DIR, ignore_errors=True)
        raise SystemExit(json.dumps({
            "status": "error", "stage": "install",
            "message": f"could not create a virtual environment from {base}",
            "detail": f"`{base} -m venv --copies {VENV_DIR}` exited {code}. "
                      "Check that the path is writable and that the "
                      "interpreter is a complete Python installation.",
        }, ensure_ascii=False))
    return py


def install_packages(py: Path) -> None:
    # The marker records WHICH specs produced this venv, not merely that
    # something was installed. Without that, editing PACKAGES leaves a stale
    # marker and the new pins are silently never applied -- which is exactly
    # how a broken numpy/scipy pairing survives a "successful" install.
    wanted = "\n".join(PACKAGES)
    marker = VENV_DIR / ".deps-ok"
    if marker.exists() and marker.read_text(encoding="utf-8").strip() == wanted:
        say("deps", "already installed")
        return
    if marker.exists():
        say("deps", "package list changed since the last run; reinstalling")
    for spec in PACKAGES:
        say("deps", f"pip install {spec}")
        code = run(
            [str(py), "-m", "pip", "install", "--disable-pip-version-check"]
            + spec.split(),
        ).returncode
        # numpy is listed separately from torch on purpose (different index),
        # so a failure here is fatal while the others are retried.
        if code != 0:
            raise SystemExit(f"pip install failed for: {spec}")
    marker.write_text(wanted, encoding="utf-8")


def install_playwright(py: Path) -> None:
    """Fetch Chromium for the cookie fallback path.

    Playwright is deliberately NOT in PACKAGES: it is only needed when the CDN
    wants a cookie, which is the uncommon case, and it costs ~150MB plus a
    Chromium build. So we install it lazily here -- and must install it before
    invoking it, since nothing earlier in the run will have.
    """
    marker = VENV_DIR / ".playwright-ok"
    if marker.exists():
        say("playwright", "browser already installed")
        return

    probe = run([str(py), "-c", "import playwright"],
                capture_output=True, text=True)
    if probe.returncode != 0:
        say("playwright", "installing the package (needed only for the "
                          "browser fallback)")
        got = run([str(py), "-m", "pip", "install", "playwright"]).returncode
        if got != 0:
            say("playwright", "WARNING: could not install playwright")
            say("playwright", "video downloads still work; only "
                              "login/cookie collection and the browser "
                              "fallback will not")
            return

    say("playwright", "downloading Chromium (~150MB, one-time)")
    code = run([str(py), "-m", "playwright", "install", "chromium"]).returncode
    if code != 0:
        say("playwright", "WARNING: Chromium install failed")
        say("playwright", "video downloads still work; only login/cookie "
                          "collection and the browser fallback will not")
        return
    marker.write_text("ok", encoding="utf-8")


def prefetch_model(py: Path) -> None:
    """Download the ASR model now so the first real run is not a 1.9GB stall."""
    marker = VENV_DIR / ".model-ok"
    if marker.exists():
        say("model", "already downloaded")
        return
    say("model", f"fetching {ASR_MODEL} (~1.9GB, one-time — be patient)")
    script = (
        "from modelscope import snapshot_download;"
        f"snapshot_download('{ASR_MODEL}')"
    )
    code = run([str(py), "-c", script]).returncode
    if code != 0:
        # Not fatal: FunASR would fetch it lazily on first transcription anyway.
        say("model", "WARNING: prefetch failed; it will retry on first use")
        return
    marker.write_text("ok", encoding="utf-8")


def verify(py: Path) -> None:
    say("verify", "importing every dependency")
    script = (
        "import json,importlib;"
        f"mods={VERIFY!r};"
        "out={};"
        "\nfor m in mods:\n"
        "    try:\n"
        "        importlib.import_module(m); out[m]='ok'\n"
        "    except Exception as e:\n"
        "        out[m]=f'{type(e).__name__}: {e}'\n"
        "print(json.dumps(out))"
    )
    res = run([str(py), "-c", script], capture_output=True, text=True)
    try:
        report = json.loads(res.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise SystemExit("verification did not produce output:\n" + res.stderr[-2000:])

    bad = {m: e for m, e in report.items() if e != "ok"}
    if bad:
        print(json.dumps({
            "status": "error", "stage": "install",
            "message": "some dependencies did not import",
            "detail": bad,
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)

    # Imports passing is not sufficient -- a numpy 1.x / scipy 2.x pairing
    # imports cleanly and only raises on the first real call. Exercise them.
    say("verify", "exercising numpy/scipy/librosa (import alone proves nothing)")
    smoke = run([str(py), "-c", SMOKE], capture_output=True, text=True)
    if smoke.returncode != 0:
        print(json.dumps({
            "status": "error", "stage": "install",
            "message": "numpy/scipy/librosa are mutually incompatible",
            "detail": (smoke.stderr or smoke.stdout)[-800:],
        }, ensure_ascii=False, indent=2))
        raise SystemExit(1)
    versions = smoke.stdout.strip().splitlines()[-1] if smoke.stdout else ""

    ffmpeg = shutil_which("ffmpeg")
    print(json.dumps({
        "status": "ok",
        "venv": str(VENV_DIR),
        "python": str(py),
        "packages": report,
        "numeric_stack": versions,
        "ffmpeg": ffmpeg or "NOT FOUND — install FFmpeg and add it to PATH",
    }, ensure_ascii=False, indent=2))


def shutil_which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    # ffmpeg on Windows frequently lives outside PATH; reuse the skill's own
    # search so the install report matches what the scripts will actually find.
    sys.path.insert(0, str(SKILL_ROOT / "scripts"))
    try:
        import media_env
        return media_env.ensure_tools(verbose=False).get(name)
    except Exception:
        return None


def main() -> int:
    print("[douyin-digest] setting up. This takes 10-20 minutes and ~3.5 GB on")
    print("[douyin-digest] the first run, almost all of it downloading. Later")
    print("[douyin-digest] runs are instant.\n")
    py = build_venv()
    install_packages(py)
    install_playwright(py)
    prefetch_model(py)
    verify(py)
    say("done", "run: python scripts/digest.py --url <douyin link>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
