"""Re-launch under a Python that actually has funasr/yt_dlp/playwright.

The ASR stack lives in a different interpreter than whatever `python` happens
to be on PATH -- on this machine the deps sit in a TRAE-managed runtime while
the default `python` is a bare 3.14 with none of them. Calling `digest.py`
with the default interpreter therefore fails at the transcribe step with a
bare ModuleNotFoundError, which reads like a broken install rather than a
missing interpreter.

`ensure()` re-execs the current script under the first interpreter that can
import the real dependency set. Call it once at the top of each entry point,
before argparse. If no interpreter qualifies it prints actionable JSON and
exits, instead of failing later on a confusing import error.

Environment override: set DOUYIN_PYTHON to an absolute interpreter path.
"""

import importlib.util
import json
import os
import sys

REQUIRED = ("funasr", "torch", "torchaudio", "yt_dlp", "playwright")

# Interpreters to try when the current one is short of the required modules.
# Ordered best-first: an explicit override, the current one, then known
# alternate runtimes on Windows. Directories are globbed so a TRAE/VSCode
# style managed runtime is found even after its version bump.
SEARCH_GLOBS = [
    r"C:\Users\*\AppData\Roaming\TRAE*\ModularData\ai-agent\vm\tools\python\python.exe",
    r"C:\Users\*\AppData\Local\Programs\Python\Python3*\python.exe",
    r"C:\Users\*\AppData\Local\Microsoft\WindowsApps\python.exe",
    r"C:\Python3*\python.exe",
]

_GUARD = "DOUDYIN_INTERPRETER_REEXEC"


def missing(required=REQUIRED) -> list:
    """Return the required modules this interpreter cannot import."""
    return [m for m in required if importlib.util.find_spec(m) is None]


def _candidates():
    seen, out = set(), []
    override = os.environ.get("DOUYIN_PYTHON")
    if override:
        out.append(override)
    # The venv install.py builds beside the skill. It comes before the
    # globbed system runtimes on purpose: it is the one environment this skill
    # controls end to end, so it is the most likely to be complete AND the one
    # least likely to be broken by someone else's uninstall.
    bundled = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".venv"
    )
    out.append(os.path.join(bundled, "Scripts", "python.exe"))
    out.append(os.path.join(bundled, "bin", "python"))
    out.append(sys.executable)
    import glob
    for pattern in SEARCH_GLOBS:
        out.extend(sorted(glob.glob(pattern), reverse=True))
    for path in out:
        real = os.path.normcase(os.path.abspath(path)) if os.path.isfile(path) else None
        if real and real not in seen:
            seen.add(real)
            yield path


def _probe(path: str) -> bool:
    """True when `path` can import every required module."""
    import subprocess
    script = "import " + ",".join(REQUIRED)
    try:
        proc = subprocess.run([path, "-c", script], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def ensure(script_name: str = "", required=REQUIRED) -> None:
    """Re-exec the calling script under an interpreter that has the deps.

    Safe to call unconditionally at the top of an entry point: it returns
    immediately when the current interpreter is already correct.
    """
    if not missing(required):
        return

    if os.environ.get(_GUARD):
        # We already tried; this is the fallback interpreter and it is still
        # short. Report it precisely instead of recursing.
        print(json.dumps({
            "status": "error", "stage": "interpreter",
            "message": "No Python on this machine can import: "
                       + ", ".join(missing(required)),
            "detail": "Run the bundled installer, which builds a private "
                      "environment for this skill: python scripts/install.py",
        }, ensure_ascii=False))
        raise SystemExit(2)

    for path in _candidates():
        if path == sys.executable or not _probe(path):
            continue
        env = {**os.environ, _GUARD: "1"}
        script = os.path.abspath(sys.argv[0]) if sys.argv and sys.argv[0] else script_name
        print(f"[douyin-digest] re-launching under {path}", file=sys.stderr)
        # Use subprocess, not os.execve: on Windows execve re-parses the command
        # through the shell, which splits an install path like "...TRAE SOLO CN"
        # on the space. subprocess passes argv as a list and quotes correctly.
        import subprocess
        try:
            code = subprocess.run([path, script] + sys.argv[1:], env=env).returncode
        except OSError as exc:
            print(json.dumps({
                "status": "error", "stage": "interpreter",
                "message": f"could not re-launch under {path}",
                "detail": f"{type(exc).__name__}: {exc}",
            }, ensure_ascii=False))
            raise SystemExit(2)
        raise SystemExit(code)

    print(json.dumps({
        "status": "error", "stage": "interpreter",
        "message": "No Python on this machine can import: " + ", ".join(missing(required)),
        "detail": "Set DOUDYIN_PYTHON to an interpreter that has them, or run "
                  "python scripts/install.py to build a private environment "
                  "for this skill automatically.",
    }, ensure_ascii=False))
    raise SystemExit(2)