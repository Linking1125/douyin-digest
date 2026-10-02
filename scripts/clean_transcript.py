"""Normalise a raw FunASR transcript so it reads cleanly for summarisation.

FunASR output is faithful to the audio but noisy in two predictable ways:

  * syllable stutter   -- "肉肉肉", "对对对", "老老老" (real doubled audio)
  * filler words       -- 的/了/啊/吧 in dense conversational speech
  * stutter prefixes   -- "对对对", "谢谢谢谢", "这个这个"

Clean it up so the downstream summary sees facts rather than speech noise.
"""

import argparse
import json
import re

STUTTER_PATTERNS = [
    # >=2 repeats of a 1-2 char Chinese unit, e.g. 肉肉 / 对对对对 / 谢谢谢谢
    (re.compile(r"([一-鿿]{1,2})\1{1,}"), r"\1"),
    # >=3 repeats of a longer unit, e.g. 板板板板
    (re.compile(r"([一-鿿]{3,4})\1{2,}"), r"\1"),
]

# Filler that carries no meaning once the sentence is understood.
FILLER = "的了啊吧呢嘛呀哦嗯"

TRAILING = "，,。.！!？?、~～ 　"


def normalize(text: str, keep_fillers: bool = False) -> str:
    if not text:
        return ""

    previous = None
    # Iterate so collapsing "肉肉肉肉" fully rather than one pass at a time.
    while previous != text:
        previous = text
        for pattern, replacement in STUTTER_PATTERNS:
            text = pattern.sub(replacement, text)

    if not keep_fillers:
        # Only strip particles that trail a noun/verb. Never touch negation or
        # other meaningful uses: 不/没 must survive, and a filler inside a word
        # ("味道" not "味") would corrupt the text.
        text = re.sub(r"(?<=[一-鿿])[的了啊吧呢](?=[，。！？、\s]|$)", "", text)
        text = re.sub(r"(?<=谢)[了啊]", "", text)

    text = re.sub(r"([，,。.！!？?])\1+", r"\1", text)
    text = re.sub(r"\s+", "", text)
    text = text.strip(TRAILING)
    return text


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input", required=True, help="raw transcript file")
    ap.add_argument("--out", default=None, help="output path (default: alongside input)")
    ap.add_argument("--keep-fillers", action="store_true",
                    help="keep 的了啊吧 particles (they sometimes carry meaning)")
    args = ap.parse_args()

    try:
        with open(args.input, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        print(json.dumps({"status": "error", "code": 40, "message": str(exc)}, ensure_ascii=False))
        return 0

    cleaned = normalize(raw, keep_fillers=args.keep_fillers)

    out_path = args.out or re.sub(r"\.txt$", ".clean.txt", args.input)
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(cleaned)

    print(json.dumps({
        "status": "ok",
        "raw_chars": len(raw),
        "clean_chars": len(cleaned),
        "removed_pct": round((1 - len(cleaned) / len(raw)) * 100, 1) if raw else 0,
        "output_path": out_path,
        "preview": cleaned[:200],
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

