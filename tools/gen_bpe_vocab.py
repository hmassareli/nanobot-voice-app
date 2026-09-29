#!/usr/bin/env python3
"""Regenerate android/.../BpeVocab.kt from assets/kws/bpe.model.

The sherpa-onnx AAR does not expose a text tokenizer to Kotlin, and the
sentencepiece .model file is a protobuf that is impractical to parse on-device.
So we bake the piece list and their log-probabilities into a Kotlin object and
reproduce sentencepiece's unigram Viterbi search in BpeTokenizer.kt.

Run from the project root:

    python3 tools/gen_bpe_vocab.py

Requires: pip install sentencepiece
"""
import json
import struct
import sys
from pathlib import Path

try:
    import sentencepiece as spm
except ImportError:
    sys.exit("sentencepiece is required: pip install sentencepiece")

ROOT = Path(__file__).resolve().parent.parent
KWS = ROOT / "android/app/src/main/assets/kws"
OUT = ROOT / "android/app/src/main/java/com/nanobot/voice/BpeVocab.kt"


def read_varint(b, i):
    r = 0
    s = 0
    while True:
        x = b[i]
        i += 1
        r |= (x & 0x7F) << s
        if not (x & 0x80):
            break
        s += 7
    return r, i


def parse_sentencepiece(b):
    """Extract (piece, score) from a serialized SentencePiece proto."""
    i = 0
    piece = None
    score = None
    while i < len(b):
        tag, i = read_varint(b, i)
        field, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = read_varint(b, i)
            val = b[i:i + ln]
            i += ln
            if field == 1:
                piece = val.decode("utf-8")
        elif wt == 5:
            val = b[i:i + 4]
            i += 4
            if field == 2:
                score = struct.unpack("<f", val)[0]
        elif wt == 0:
            _, i = read_varint(b, i)
        else:
            break
    return piece, score


def extract_scores(proto):
    pieces = {}
    i = 0
    while i < len(proto):
        tag, i = read_varint(proto, i)
        field, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, i = read_varint(proto, i)
            val = proto[i:i + ln]
            i += ln
            if field == 1:  # repeated SentencePiece pieces
                p, s = parse_sentencepiece(val)
                if p is not None:
                    pieces[p] = s
        elif wt == 0:
            _, i = read_varint(proto, i)
        else:
            break
    return pieces


def kesc(s):
    out = ""
    for ch in s:
        if ch == "\\":
            out += "\\\\"
        elif ch == '"':
            out += '\\"'
        elif ch == "\n":
            out += "\\n"
        elif ch == "\t":
            out += "\\t"
        elif ch == "$":
            out += "\\$"
        else:
            out += ch
    return out


def main():
    sp = spm.SentencePieceProcessor()
    sp.load(str(KWS / "bpe.model"))
    scores = extract_scores(sp.serialized_model_proto())

    # tokens.txt order must match the model id order; it does for this model.
    pieces = [
        line.rsplit(" ", 1)[0]
        for line in (KWS / "tokens.txt").read_text(encoding="utf-8").splitlines()
        if line
    ]
    assert pieces == [sp.id_to_piece(i) for i in range(sp.get_piece_size())], (
        "tokens.txt order differs from bpe.model id order"
    )

    lines = [
        "package com.nanobot.voice",
        "",
        "/**",
        " * BPE vocabulary of assets/kws/bpe.model, in exact id order (index == id).",
        " *",
        " * AUTO-GENERATED — do not edit by hand. Regenerate with the project's",
        " * tools/gen_bpe_vocab.py. The sentencepiece .model file is a protobuf that is",
        " * impracticable to parse on-device, so the piece list and their log",
        " * probabilities are baked in here. [SCORES] must stay index-aligned with",
        " * [PIECES].",
        " */",
        "internal object BpeVocab {",
        "",
        "    /** Number of BPE pieces (== tokens.txt line count). */",
        f"    const val SIZE = {len(pieces)}",
        "",
        "    /** Piece strings; index == sentencepiece id. */",
        "    val PIECES = arrayOf(",
    ]
    for i in range(0, len(pieces), 8):
        chunk = ", ".join('"%s"' % kesc(x) for x in pieces[i:i + 8])
        lines.append(f"        {chunk},")
    lines += [
        "    )",
        "",
        "    /** Log probabilities (natural log), index-aligned with [PIECES]. */",
        "    val SCORES = floatArrayOf(",
    ]
    sc = [scores.get(p, -10.0) for p in pieces]
    for i in range(0, len(sc), 8):
        chunk = ", ".join("%.5ff" % x for x in sc[i:i + 8])
        lines.append(f"        {chunk},")
    lines += ["    )", "}"]

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, {len(pieces)} pieces)")


if __name__ == "__main__":
    main()
