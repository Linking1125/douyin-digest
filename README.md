# douyin-digest

Paste a Douyin share link, get the knowledge points back.

Everything runs locally — no API key, no paid service, and the audio never
leaves your machine. Built as an agent skill, but the scripts work standalone
from any terminal.

## Install

```
python scripts/install.py
```

That is the whole procedure. The script finds a Python it can use, builds a
private `.venv`, installs pinned dependencies (CPU-only torch, so no 2.5GB CUDA
download), fetches Chromium, and pre-downloads the 1.9GB SenseVoice model.
10–20 minutes and ~3.5GB the first time, nearly all of it download. Safe to
re-run — finished steps are skipped.

### Sharing this with someone

Zip the folder and send it. It is 91KB of text across 18 files — no binaries,
no model weights, nothing from your machine. On their side:

```
unzip douyin-digest.zip
cd douyin-digest
python scripts/install.py
python scripts/digest.py --url "<link>"
```

Nothing from your install leaks into the zip. `.venv/`, `__pycache__/`, and
`work/` are listed in `.gitignore` precisely so `git init && git commit`
produces the same clean folder — the 3.5GB is rebuilt on their machine, which
is the only way to get wheels that actually match their OS and Python.

Keep the `.venv` out of any archive you send. It is full of absolute paths to
your user directory and will not run anywhere else.

**Python:** you need 3.10–3.12. Newer versions have no wheels for funasr's
dependencies. If your `python` is too new, the installer searches for another
one; if it finds none, it will use [uv](https://docs.astral.sh/uv/) to fetch a
managed interpreter automatically. Installing uv first (`winget install
astral-sh.uv`) makes that path bulletproof.

Note the two requirements are separate: an interpreter must be the right
version *and* be able to create a venv. Some IDEs ship a stripped runtime with
no `venv` module at all (TRAE's bundled Python, for example), which looks
perfectly fine to `python -V`. The installer skips those with a printed reason
rather than failing later with a confusing error. Set `DOUYIN_PYTHON` to pin a
specific interpreter if you want to skip the search.

Plus **FFmpeg** on PATH:

```
winget install Gyan.FFmpeg      # or: choco install ffmpeg
```

## Use

```
python scripts/digest.py --url "https://v.douyin.com/xxxxxxx/"
```

One line of JSON: video title, author, duration, engagement counts, and the
transcript. Works for image posts (图文) too — it returns local image paths
instead.

Useful flags:

| Flag | What it does |
| --- | --- |
| `--meta-only` | Stop after metadata. No download, no model load — instant. |
| `--model sea` | Use SeaCo instead of SenseVoice. Roughly 2× memory, noticeably better on dense terminology. |
| `--max-minutes N` | Transcribe only the first N minutes. |
| `--threads N` | ASR thread count. Lower it if memory is tight. |
| `--workdir PATH` | Where to keep audio and transcripts. |

Run `python scripts/digest.py --help` for the full list.

## What it's for

The transcript is an input, not the deliverable. This skill's job is speech →
text; turning that into knowledge is the agent's job, and `SKILL.md` tells it
how — read the full transcript, output a knowledge-point list, keep the
specifics (names, versions, numbers), attribute claims, flag anything
SenseVoice likely garbled.

## How it works

`fetch_video.py` resolves the short link and pulls the media with `yt-dlp`,
falling back to a Playwright browser session when the CDN needs a cookie.
`transcribe.py` runs SenseVoice through FunASR, chunked into four-minute
segments so memory stays flat (~2.9GB steady, ~4.1GB peak) no matter how long
the video is. `clean_transcript.py` strips filler and timestamps.

## Troubleshooting

`python scripts/check_env.py` reports every dependency and its status in one
pass. Common cases:

- **`No Python on this machine can import: funasr, torch…`** — run
  `python scripts/install.py`.
- **Download works, transcription fails** — usually ffmpeg missing. Check
  `check_env.py`'s `ffmpeg` field.
- **`module 'numpy' has no attribute 'long'`** — the numpy/scipy/librosa trio
  resolved inconsistently. They are pinned together on purpose (`numpy<2` is
  required by funasr, while `scipy<1.14` and `librosa<0.11` are the last
  versions with numpy 1.x wheels). Re-run `install.py`; it notices the package
  list changed and reinstalls automatically. Confirm the result with
  `check_env.py`'s `numeric_stack` field.
- **Wrong `python` picked** — the scripts re-launch themselves under the venv
  first, then any interpreter that has the deps. Set `DOUYIN_PYTHON` to pin
  one explicitly.
- **Out of memory** — drop `--threads` to 2, or use `--max-minutes` to
  transcribe in sections.

## Legal

Transcribes videos you chose to open yourself. Nothing is republished,
transcripts stay local. You're responsible for the platform's terms and the
content's copyright. MIT licensed — see `LICENSE`.
