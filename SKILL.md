---
name: douyin-digest
description: Summarise a Douyin (抖音) video from a share link. Paste a v.douyin.com short link or a douyin.com/video URL and this downloads the audio, transcribes it locally with FunASR (no API key, no cost, no upload), and returns metadata plus a cleaned transcript to summarise. Use when the user shares a Douyin/TikTok-style short-video link and wants its content understood, summarised, or extracted. Not for uploading, reposting, or scraping at scale.
---

# Douyin Digest

Turn a Douyin share link into readable text you can summarise. Everything runs
locally: no API key, no paid service, and the audio never leaves the machine.

## Setup

Run this once, right after downloading:

```
python scripts/install.py
```

That's the whole installation. It builds a private `.venv` beside the skill,
installs the pinned dependencies (CPU-only torch — no 2.5GB CUDA download),
fetches the Chromium build, and pre-downloads the 1.9GB ASR model. Expect
10–20 minutes and ~3.5GB on the first run, almost all of it downloading;
later runs are instant. Re-running is safe — finished steps are skipped.

You also need **FFmpeg** on PATH (`winget install Gyan.FFmpeg` or
`choco install ffmpeg`). It's the one thing that can't be pip-installed
reliably. `install.py` reports whether it was found, and
`scripts/check_env.py` re-checks on demand.

**Python version matters.** This skill needs 3.10–3.12. Python 3.13+ has no
wheels for funasr's dependencies (`editdistance`), so pip would try a source
build and fail. `install.py` handles this for you: if your `python` is out of
range it searches for another interpreter, and if it finds none it uses
[uv](https://docs.astral.sh/uv/) to fetch a managed one automatically. It will
never silently pick a version that cannot work. Full installs from python.org
work; stripped IDE runtimes (TRAE's bundled python, for instance) do **not**,
because they ship without the `venv` module — `install.py` detects and skips
those with a printed reason.

To pin an interpreter explicitly, set `DOUYIN_PYTHON` to its full path.

After that, every script works with whatever `python` you have — they detect
the venv and re-launch themselves under it. See "The `python` on PATH may not
be the right one" below for how that resolution works.

## Usage

One command does the whole pipeline:

```bash
python scripts/digest.py --url "<douyin-link>"
```

It prints a single JSON object on stdout. For a video it carries `meta` (title,
author, duration, like/comment counts, description) and `transcript` (cleaned
text) — summarise from those, and read the transcript file on disk when you
need more than the 200-character preview.

**The transcript is an input, not the deliverable.** The pipeline's job is to
turn speech into text; turning that text into knowledge is yours. Do not hand
the user a transcript or a paragraph summary — see "Summarising" below for what
they actually want out of this.

For an image post (图文) it instead returns `kind: "note"` and a list of local
image paths. There is no audio, so read the images with the view_image tool and
summarise from what they show.

Useful flags:

| Flag | Effect |
|------|--------|
| `--meta-only` | Metadata only — fast, no download or transcription |
| `--hotwords "词1 词2"` | Bias recognition toward proper nouns |
| `--workdir DIR` | Where to cache cookies and media (default `~/.douyin-digest`) |
| `--refresh-cookies` | Force fresh cookies before running |
| `--threads N` | torch CPU threads, default 4. Lower = less memory |
| `--max-minutes M` | Split audio longer than M minutes into chunks (default: no split) |

Set `PYTHONIOENCODING=utf-8` when capturing output, or Chinese text may garble in
the console.

## Summarising

This is the step that matters. The user shares a video to **learn what it
says**, not to prove the pipeline ran. Short videos are dense in points the
listener would miss: a licence clause named once, a number stated in passing, a
step that goes by unmentioned.

Read the **full** transcript from `clean_transcript_path`, not the 200-char
preview in the JSON — the preview is a sanity check that transcription ran, and
almost never contains the actual content.

Then produce a **knowledge-point list**, not prose. For each point:

- Lead with the claim, not a lead-in. "MIT licence lets you sell it" beats
  "The video talks about several open-source licences, including the MIT…".
- Keep the specifics the video gave: names, versions, numbers, licence terms.
  These are exactly what the user cannot recover by skimming.
- Mark uncertain items. SenseVoice drops clauses under 6 characters, so short
  legal or technical terms may be missing outright. If the sense of a point
  depends on a garbled term, say so rather than inventing a confident version.
- Separate what the video *claims* from what is *true*. Many videos state
  licences, prices, or legal conclusions loosely. Attribute them ("视频里说…")
  and flag anything you'd want to verify before acting on it.

Close with the one thing worth remembering if the user forgets the rest — the
practical takeaway, not a recap of the structure.

Skip this step entirely only when the transcript is empty or pure music; then
say so plainly instead of inventing content.

## How it works

1. **Cookies** — Douyin rejects requests without fresh signed cookies. A
   headless browser visit to the homepage yields them; no login is involved.
   Cached and reused (they last months).
2. **Resolve + fetch** — Short links are followed to a canonical URL, then
   yt-dlp pulls metadata and extracts audio as mp3.
3. **Transcribe** — FunASR SeaCo-Paraformer runs locally on CPU.
4. **Clean up** — Stutters (`肉肉肉`) and filler particles are collapsed so the
   summary sees facts, not speech noise.

Image posts short-circuit at step 2: `/note/<id>` links have no audio track, so
their carousel images are downloaded instead and returned for reading.

## Memory

Transcription is the memory-hungry step. Measured on a 16 GB Windows machine:

| Stage | Resident |
|-------|----------|
| Model loading (SenseVoiceSmall) | ~2.8-3.2 GB |
| Steady-state inference | ~2.9 GB, flat regardless of clip length |
| Transient peak during checkpoint load | ~4.1 GB, drops back within a second |
| SeaCo-Paraformer (if you switch) | ~4 GB resident, 11 GB peak under load |

The default model is SenseVoiceSmall precisely because it holds steady where
SeaCo does not. Two flags keep the machine responsive:

- `--threads 4` (default) caps torch CPU threads. Each thread carries its own
  scratch buffers, so this is the cheapest lever. Drop to `--threads 2` on a
small machine.
- `--max-minutes 5` splits long audio into chunks and clears per-chunk state in
between. Peak memory then stays flat no matter how long the video is; without
it, hour-long uploads grow the working set.

On a 14-minute video: 4 chunks, 104 s of transcription, 2.86 GB steady, 4.1 GB
transient peak.

The browser and the transcriber run as separate sequential processes, so their
memory never overlaps. Do not keep a browser open on the same machine while a
long video transcribes.

### When to switch back to SeaCo

`--model iic/speech_seaco_paraformer_large_asr_nat-zh-cn-16k-common-vocab8404-pytorch`

SeaCo accepts hotwords and handles rare jargon better, at roughly 1.4x the
memory. Reach for it when:

- the transcript mishears domain terms that hotwords would fix (SeaCo is the
only supported model that honours `--hotwords`);
- the content is dense in rare proper nouns and you have the RAM to spare;
- SenseVoice mangled something important enough to justify another pass.

Note SenseVoice ignores `--hotwords`; passing it there has no effect.

## Reading the output

`status` is `ok` or `error`. On error, `stage` says which step failed and
`message` explains why.

Two outcomes that look like failures but are not:

- **`kind` is `note`** — it is an image post. Open the returned image paths and
  read them; there is nothing to transcribe.
- **`transcript` is empty** with a `note` — the video is music-only or silent.
  Summarise from `meta` and say there was no speech.
- **Technical terms look wrong** — ITN is off by default because it corrupts
  Latin terms ("DNS over HTTPS" became "DNSoverttps"). Latin protocol names are
  capitalised after transcription; if digits matter more than exact terms, pass
  `--itn`.
- **`meta.fallback` is true** — yt-dlp refused the video, so a browser session
  was used instead. Results are equally valid.

If `stage` is `download` and the detail mentions cookies twice, Douyin
tightened its gate: re-run with `--refresh-cookies`. If that still fails, the
video may be private or deleted.

## Hotwords are worth using

Short-video speech is dense with names, places and shop names that generic ASR
mangles. The pipeline already feeds each video's own title and hashtags in as
hotwords; add your own with `--hotwords` when you know the domain.

## Requirements

Run `python scripts/check_env.py` first if anything misbehaves. It reports
missing dependencies in one pass. Known-good setup:

- Python 3.10+ with `yt-dlp`, `playwright` (Chromium installed), `funasr==1.2.6`,
  `torchaudio`
- `ffmpeg` and `ffprobe` reachable — `media_env.py` finds them in the usual
  Windows locations even when absent from PATH

Pin `funasr==1.2.6`. Later releases ship a wheel missing `funasr.layers` and
fail on import.

### The `python` on PATH may not be the right one

The ASR stack frequently lives in a different interpreter than the default
`python` — on this machine it sits in a TRAE-managed runtime while `python`
resolves to a bare 3.14 with none of it. `scripts/interpreter.py` handles this
for you: `digest.py` and `transcribe.py` check at startup whether the current
interpreter can import `funasr`, `torch`, `torchaudio`, `yt_dlp` and
`playwright`, and if not, re-launch themselves under the first one that can.
A one-line `[douyin-digest] re-launching under …` note on stderr is expected,
not an error.

So the plain `python scripts/digest.py --url …` works regardless of which
interpreter you call it with. To pin one explicitly, set `DOUYIN_PYTHON` to an
absolute interpreter path. If no interpreter on the machine qualifies, the
script exits with `stage: "interpreter"` and the exact `pip install` line to
run.

## Scope

Summarising for personal understanding is fine. Do not use this to republish
content, strip attribution, or scrape Douyin at volume — it is built for
one-off links the user chose. The skill downloads and transcribes only; it never
posts, comments, or interacts with the platform.

