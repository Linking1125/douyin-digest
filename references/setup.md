# Setup and troubleshooting

Read this only when the pipeline fails or you are installing on a new machine.

## Install

```powershell
python -m pip install yt-dlp playwright "funasr==1.2.6" torchaudio
python -m playwright install chromium
```

Install FFmpeg and make sure `ffmpeg.exe` / `ffprobe.exe` exist. `media_env.py`
finds them under `%LOCALAPPDATA%\ffmpeg\bin` or `C:\Program Files\ffmpeg\bin`
even when they are not on PATH.

Verify:

```powershell
python scripts/check_env.py
```

The ASR model (~1 GB) downloads from ModelScope on first transcription and is
then cached under `~/.cache/modelscope`.

## Why funasr is pinned to 1.2.6

Newer FunASR wheels ship without a `funasr.layers` package, so importing any
model fails with `ModuleNotFoundError: No module named 'funasr.layers'` and a
cascade of related import errors. 1.2.6 is complete and stable.

## Why SeaCo, not SenseVoice

Measured on the same 6-minute clip:

| Model | Characters produced | Verdict |
|-------|--------------------|---------|
| SenseVoiceSmall | 191 | Drops most speech; mangles names |
| SeaCo-Paraformer | 1615 | Complete and readable |

SeaCo also accepts hotwords, which is what rescues proper nouns.

## Cookies

Douyin's edge security requires signed cookies (`__ac_nonce`, `__ac_signature`,
`ttwid`) or every request fails with "Fresh cookies are needed". They are issued
to anonymous visitors, so no login is needed. They stay valid for months.

If downloads start failing:

```powershell
python scripts/digest.py --url "<link>" --refresh-cookies
```

`digest.py` already refreshes once automatically when it sees a cookie error.

## When yt-dlp still refuses a valid video

yt-dlp's cookie validation is stricter than the site's actual requirement, so
some playable videos get rejected. `digest.py` falls back to
`fetch_video_browser.py`, which loads the page in a real browser, captures the
media URLs the page requests, and downloads through them. Results are marked
`"fallback": true`.

If a video fails both paths, it is most likely private, deleted, or
region-locked.

## Transcription quality

Raw output contains speech noise: stutters (`肉肉肉`, `对对对`) and dense
particles (`的`, `了`, `啊`). `clean_transcript.py` removes roughly 15% of
characters.

Cleanup deliberately preserves negation and other meaningful particles -- only
trailing fillers are stripped, so `不辣` stays `不辣` and `不要` stays `不要`.

Feed the video's own title and hashtags as hotwords (the pipeline does this
automatically) and pass extra domain terms with `--hotwords`.

## Performance

Roughly 5-7x faster than realtime on CPU. A 6-minute video transcribes in about
55 seconds; a 1-minute video in under 10 seconds. Model load adds ~5 seconds.

## Encoding

Set `PYTHONIOENCODING=utf-8` before capturing script output, or Chinese text
garbles in the console. The JSON on stdout is unaffected in content.


## Memory

Measured on this machine with SenseVoiceSmall on CPU (16 cores):

| Audio | Peak RSS | Wall time | Output |
|-------|----------|-----------|--------|
| 14-minute video, single pass | **12.1 GB** | 374 s | 2,058 chars |
| 14-minute video, 2-minute chunks | **3.2 GB** | 74 s | 4,821 chars |

Chunking is therefore the default (`--max-minutes 2.0`). It cuts peak memory by
about 73%, runs ~5x faster, and recovers more text -- the whole file is no
longer decoded into memory at once. Pass `--no-chunk` for short clips.

Steady-state model residency is ~1.5 GB; the ~1.7 GB above that is the one-off
load spike, and inference itself adds under 50 MB.

Two things that look like fixes but are not:

- `torch.set_num_threads()` does not change the load peak (3.2 GB at 2, 4 and 8
  threads alike). It is kept at 4 because it is still mildly cheaper to run.
- Calling `gc.collect()` between chunks deadlocks FunASR's audio loader -- CPU
  time flatlines and memory pins. Do not add it.

## Accuracy

`--itn` is off by default. Number normalisation rewrites Latin tokens for
readability and, in testing, corrupted technical terms:

| Audio | `use_itn=True` | `use_itn=False` |
|-------|---------------|----------------|
| segment 1 | `DNSoverttps`, `synick` | `DNS over HTTPS`, `SYN` |
| segment 2 | `DNSoverHCPconfigurationration` | `DNS over TLS`, `DHCP` |

Turn `--itn` on only when readable digits matter more than exact terms.

SenseVoice emits lowercase Latin, so `restore_capitalisation()` repairs
protocol and product names afterwards. It handles terms glued to CJK (`IP地址`),
run-together acronyms (`dnsovertlsdhcp`), doubled syllables (`ipp` -> `IP`), and
doubled acronyms (`DHCPdhcp`). Add new terms to `LATIN_TERMS` (upper case) or
`MIXED_CASE` (`WiFi`, `iOS`) rather than patching the function.
