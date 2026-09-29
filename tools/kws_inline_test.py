#!/usr/bin/env python3
"""Testa create_stream com keyword inline (sem @display) vs keywords_file."""
import os, wave, tempfile
import numpy as np
import sherpa_onnx

KWS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "android", "app", "src", "main", "assets", "kws"))


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(n)
    x = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if ch == 2:
        x = x.reshape(-1, 2).mean(axis=1)
    return x, sr


def build(keywords_file, threshold=0.25, score=1.0):
    return sherpa_onnx.KeywordSpotter(
        tokens=os.path.join(KWS_DIR, "tokens.txt"),
        encoder=os.path.join(KWS_DIR, "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        decoder=os.path.join(KWS_DIR, "decoder-epoch-12-avg-2-chunk-16-left-64.onnx"),
        joiner=os.path.join(KWS_DIR, "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"),
        keywords_file=keywords_file,
        num_threads=2,
        keywords_score=score,
        keywords_threshold=threshold,
        num_trailing_blanks=1,
    )


def run_inline(wav, inline_kw, threshold=0.25, score=1.0):
    """keywords_file vazio + keyword passada no create_stream (como o app faz)."""
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write("")  # vazio
        kf = f.name
    try:
        kws = build(kf, threshold, score)
        s = kws.create_stream(inline_kw)
        x, sr = read_wav(wav)
        s.accept_waveform(sr, x)
        s.accept_waveform(sr, np.zeros(int(0.66 * sr), dtype=np.float32))
        s.input_finished()
        hits = []
        while kws.is_ready(s):
            kws.decode_stream(s)
            r = kws.get_result(s)
            if r:
                hits.append(r)
                kws.reset_stream(s)
        return hits
    finally:
        os.unlink(kf)


if __name__ == "__main__":
    wav = "/tmp/hey_nanobot.wav"
    print("== inline SEM @display ==")
    print("  ", run_inline(wav, "▁HE Y ▁NA N O B O T"))
    print("== inline COM @display ==")
    print("  ", run_inline(wav, "▁HE Y ▁NA N O B O T @HEY NANOBOT"))
    print("== inline com keywords_file NAO-vazio (keywords.txt) ==")
    kf = os.path.join(KWS_DIR, "keywords.txt")
    kws = build(kf)
    s = kws.create_stream("▁HE Y ▁NA N O B O T")
    x, sr = read_wav(wav)
    s.accept_waveform(sr, x)
    s.accept_waveform(sr, np.zeros(int(0.66 * sr), dtype=np.float32))
    s.input_finished()
    hits = []
    while kws.is_ready(s):
        kws.decode_stream(s)
        r = kws.get_result(s)
        if r:
            hits.append(r)
            kws.reset_stream(s)
    print("  ", hits)
