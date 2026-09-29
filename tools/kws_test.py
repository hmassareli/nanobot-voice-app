#!/usr/bin/env python3
"""Testa o modelo KWS sherpa-onnx com um wav e uma keyword.

Uso:
  python3 kws_test.py <wav> "<KEYWORD_LINE>" [threshold]
  python3 kws_test.py --selftest
"""
import sys, os, wave, tempfile
import numpy as np
import sherpa_onnx

KWS_DIR = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "android", "app", "src", "main", "assets", "kws"))


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr, n, ch, sw = w.getframerate(), w.getnframes(), w.getnchannels(), w.getsampwidth()
        data = w.readframes(n)
    assert sw == 2, f"esperado 16-bit, veio {sw*8}-bit"
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


def run(wav, keyword_line, threshold=0.25, score=1.0):
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(keyword_line + "\n")
        kf = f.name
    try:
        kws = build(kf, threshold, score)
        s = kws.create_stream()
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


def selftest():
    print("== SELFTEST com test_wavs do pacote ==")
    kws = [l.strip() for l in open(os.path.join(KWS_DIR, "test_wavs", "test_keywords.txt")) if l.strip()]
    for kw in kws:
        for wav in ["0.wav", "1.wav"]:
            p = os.path.join(KWS_DIR, "test_wavs", wav)
            print(f"  kw={kw!r:30} {wav}: {run(p, kw)}")


if __name__ == "__main__":
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        selftest()
    else:
        wav, kw = sys.argv[1], sys.argv[2]
        thr = float(sys.argv[3]) if len(sys.argv) > 3 else 0.25
        print(f"wav={wav} kw={kw!r} thr={thr}")
        print("hits:", run(wav, kw, thr))
