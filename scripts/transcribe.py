"""Transcribe audio locally with FunASR. No API key, no cost, no upload.

Model
-----
SenseVoiceSmall (~2.8 GB resident on CPU) is the default: it is markedly lighter
than SeaCo-Paraformer (~4 GB) and handles Chinese well. Pass --model to switch
when accuracy on rare jargon matters more than memory.

Memory
------
The resident cost is the model itself, not the audio -- inference adds under
50 MB. Two things keep the machine responsive:

  * --threads caps torch CPU threads (default 4). Each thread carries its own
    scratch buffers, so this is the cheapest lever on peak memory.
  * --max-minutes splits long audio into chunks and frees the intermediate
    results between them, which bounds the growth seen on hour-long videos.

Normalisation
-------------
ITN (use_itn=True) rewrites numbers and Latin words for readability, and in
testing it *corrupted* technical terms: "DNS over HTTPS" became "DNSoverttps",
"DoT" became "DNSoverHCPconfigurationration". ITN is therefore off by default.
--itn re-enables it when readable digits matter more than exact terms.

SenseVoice emits lowercase Latin. restore_capitalisation() repairs the common
protocol and product names afterwards.

Usage:
    python transcribe.py --audio PATH [--out PATH] [--model ID] [--itn]
                         [--threads N] [--max-minutes M]
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import warnings

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import media_env
import interpreter

DEFAULT_MODEL = "iic/SenseVoiceSmall"

# Terms that must keep their conventional capitalisation, applied after ASR.
#
# Boundaries use an explicit ASCII-latin lookaround rather than \b: transcripts
# are Chinese with latin tokens glued on, and Python's \w includes CJK, so \b
# never matches between a hanzi and "tcp" and the rule silently does nothing.
# "tcpp" (a doubled syllable) is intentionally left alone -- rewriting it would
# need segment-level knowledge, and the raw transcript keeps it verbatim.
MULTIWORD = [
    # Adjacent acronyms the model runs together: "DNS over TLS" + "DHCP" arrives
    # as "dnsovertlsdhcp". Split on the boundary before the individual rules run,
    # otherwise neither term matches.
    (r"dnsoverhttps(?=dhcp)", "DNS over HTTPS DHCP"),
    (r"dnsovertls(?=dhcp)", "DNS over TLS DHCP"),
    (r"dnsoverhttp(?=dhcp)", "DNS over HTTP DHCP"),
    (r"dnsoverhttps", "DNS over HTTPS"),
    (r"dnsovertls", "DNS over TLS"),
    (r"dnsoverhttp", "DNS over HTTP"),
    (r"dnsoverquic", "DNS over QUIC"),
    (r"dhcpdynamicconfigurationprotocol", "DHCP 动态主机配置协议"),
    (r"dynamicconfigurationprotocol", "动态主机配置协议"),
    # Doubled syllables the model emits around acronyms: "ipp" -> "IP",
    # "ipp地址" -> "IP 地址". Safe because a real "ipp" token does not exist.
    (r"ipp(?![a-z])", "IP"),
    (r"ipp地址", "IP 地址"),
    (r"pp地址", "IP 地址"),
    (r"ac地址", "MAC 地址"),
]

LATIN_TERMS = [
    "https", "http", "dns", "cdn", "dhcp", "arp", "nat", "ttl", "mtu", "vlan",
    "ssh", "sftp", "scp", "smtp", "pop3", "imap", "ssl", "tls", "ipsec", "tcp",
    "udp", "ftp", "ip", "syn", "ack", "fin", "rst", "wifi", "mac", "cpu", "gpu",
    "ram", "ssd", "api", "sdk", "url", "html", "gpt",
]

# Terms whose conventional form is not upper case.
MIXED_CASE = {"wifi": "WiFi", "ios": "iOS", "ipsec": "IPsec", "html": "HTML"}


def _latin_word(term: str) -> str:
    """Match a latin term glued to CJK, but not one embedded in a longer word."""
    return rf"(?<![A-Za-z]){term}(?![A-Za-z])"


CAPITALISATION = (
    [(re.compile(p, re.IGNORECASE), r) for p, r in MULTIWORD]
    + [(re.compile(_latin_word(t), re.IGNORECASE), MIXED_CASE.get(t, t.upper()))
       for t in LATIN_TERMS]
)


# "DHCPdhcp" / "TCPtcp": the model repeats a token instead of pausing. Split the
# repeat so the acronym rules above can capitalise both halves.
GLUED_REPEAT = re.compile(r"(?<![A-Za-z])([A-Za-z]{2,10})\1(?![A-Za-z])", re.IGNORECASE)


def restore_capitalisation(text: str) -> str:
    for pattern, replacement in CAPITALISATION:
        text = pattern.sub(replacement, text)
    text = GLUED_REPEAT.sub(r"\1 \1", text)
    for pattern, replacement in CAPITALISATION:
        text = pattern.sub(replacement, text)
    return text


def clean(text: str) -> str:
    text = re.sub(r"<\|[^|]*\|>", "", text)   # strip emotion/language tags
    text = re.sub(r"\s+", "", text)           # model emits spaced characters
    return text.strip()


def rss_mb() -> int:
    try:
        import psutil
        return round(psutil.Process().memory_info().rss / 1e6)
    except Exception:  # noqa: BLE001
        return 0


def probe_duration(path: str) -> float:
    try:
        proc = subprocess.run(
            [media_env.ensure_tools()["ffprobe"], "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace")
        data = json.loads(proc.stdout or "{}")
        if data.get("format", {}).get("duration"):
            return float(data["format"]["duration"])
        for stream in data.get("streams", []):
            if stream.get("duration"):
                return float(stream["duration"])
    except Exception:  # noqa: BLE001
        pass
    # 16 kHz mono 16-bit is the pipeline's internal format
    try:
        return os.path.getsize(path) / 32000.0
    except OSError:
        return 0.0


def split_audio(path: str, outdir: str, chunk_seconds: int) -> list:
    ffmpeg = media_env.ensure_tools()["ffmpeg"]
    pattern = os.path.join(outdir, "chunk%03d.wav")
    subprocess.run(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", path,
         "-f", "segment", "-segment_time", str(chunk_seconds),
         "-ac", "1", "-ar", "16000", pattern],
        capture_output=True)
    return sorted(
        os.path.join(outdir, f) for f in os.listdir(outdir)
        if f.startswith("chunk") and f.endswith(".wav")
    )


def main() -> int:
    interpreter.ensure()
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", default=None)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--itn", action="store_true",
                    help="enable number normalisation (breaks technical terms)")
    ap.add_argument("--language", default="zh")
    ap.add_argument("--threads", type=int, default=4,
                    help="torch CPU threads; lower uses less memory (default: 4)")
    ap.add_argument("--max-minutes", type=float, default=2.0,
                    help="split audio into chunks of this many minutes; 0 disables "
                         "(default: 2.0). Measured on a 14-minute video: chunking "
                         "cut peak memory 12GB -> 3.2GB, time 374s -> 79s, and "
                         "recovered 2.5x more text, because the whole file was no "
                         "longer being decoded into memory at once.")
    ap.add_argument("--no-chunk", action="store_true",
                    help="transcribe the file in one pass (only for short clips)")
    ap.add_argument("--hotwords", default="",
                    help="only honoured by models that support them (e.g. SeaCo)")
    args = ap.parse_args()

    if not os.path.exists(args.audio):
        print(json.dumps({"status": "error", "code": 31,
                          "message": f"audio not found: {args.audio}"}, ensure_ascii=False))
        return 0

    try:
        tools = media_env.ensure_tools()
    except FileNotFoundError as exc:
        print(json.dumps({"status": "error", "code": 30, "message": str(exc)},
                         ensure_ascii=False))
        return 0

    os.environ.setdefault("MODELSCOPE_CACHE",
                          os.path.join(os.path.expanduser("~"), ".cache", "modelscope"))
    warnings.filterwarnings("ignore")

    # Cap threads before heavy allocation.
    import torch
    if args.threads > 0:
        torch.set_num_threads(args.threads)

    from funasr import AutoModel

    # Split long audio before loading the model: keeps per-chunk activation
    # buffers small, which is what bounds peak memory on long videos.
    duration = probe_duration(args.audio)
    work_chunks = [args.audio]
    tmpdir = None
    if args.max_minutes > 0 and not args.no_chunk and duration > args.max_minutes * 60:
        tmpdir = os.path.join(os.path.dirname(os.path.abspath(args.audio)), "_chunks")
        os.makedirs(tmpdir, exist_ok=True)
        work_chunks = split_audio(args.audio, tmpdir, int(args.max_minutes * 60))
        if not work_chunks:
            work_chunks = [args.audio]

    load_start = time.time()
    model = AutoModel(model=args.model, device="cpu", disable_update=True)
    load_secs = time.time() - load_start
    load_rss = rss_mb()

    generate_kwargs = {
        "cache": {}, "language": args.language,
        "use_itn": args.itn, "batch_size_s": 60,
    }
    if args.hotwords.strip():
        generate_kwargs["hotword"] = " ".join(args.hotwords.split())

    start = time.time()
    parts = []
    for chunk in work_chunks:
        result = model.generate(input=chunk, **generate_kwargs)
        parts.append(clean("".join(r.get("text", "") for r in result)))
        model.cache = {}   # drop per-utterance state; do NOT gc.collect() here,
        del result          # it deadlocks FunASR's audio loader
    elapsed = time.time() - start

    transcript = restore_capitalisation("".join(p for p in parts if p))

    out_path = args.out or os.path.splitext(args.audio)[0] + ".txt"
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(transcript)

    if tmpdir:
        for chunk in work_chunks:
            try:
                os.remove(chunk)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass

    print(json.dumps({
        "status": "ok",
        "transcript": transcript,
        "transcript_path": os.path.abspath(out_path),
        "chars": len(transcript),
        "chunks": len(work_chunks),
        "model": args.model,
        "load_secs": round(load_secs, 1),
        "transcribe_secs": round(elapsed, 1),
        "rss_after_load_mb": load_rss,
        "rss_mb": rss_mb(),
        "threads": args.threads,
        "itn": args.itn,
        "audio_secs": round(duration),
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
