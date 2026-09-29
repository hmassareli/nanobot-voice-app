#!/usr/bin/env python3
"""Testa VOSK (KaldiRecognizer com gramática restrita) como wake-word PT.

Uso:
  python3 vosk_test.py <model_dir> <wav> [frase ...]
  python3 vosk_test.py --suite <model_dir>

Gramática restrita = lista de frases permitidas + [unk]. O VOSK só pode
transcrever uma dessas frases; se "opa amigo" aparece no texto, é hit.

Instalar:  pip install vosk
Modelo:    vosk-model-small-pt-0.3 (~52 MB descompactado, Apache-2.0)
"""
import sys, os, wave, json
import numpy as np


def read_wav(path):
    with wave.open(path, "rb") as w:
        sr = w.getframerate()
        n = w.getnframes()
        ch = w.getnchannels()
        sw = w.getsampwidth()
        data = w.readframes(n)
    if sw != 2:
        raise ValueError(f"esperado 16-bit, veio {sw*8}-bit")
    x = np.frombuffer(data, dtype=np.int16)
    if ch == 2:
        x = x.reshape(-1, 2).mean(axis=1).astype(np.int16)
    return x.tobytes(), sr


def recognize(model, wav, phrases):
    # gramática restrita: as frases + token de desconhecido
    grammar = json.dumps(phrases + ["[unk]"], ensure_ascii=False)
    rec = __import__("vosk").KaldiRecognizer(model, 16000, grammar)
    rec.SetWords(True)
    pcm, sr = read_wav(wav)
    assert sr == 16000, f"VOSK espera 16k, veio {sr}"
    results = []
    if rec.AcceptWaveform(pcm):
        results.append(json.loads(rec.Result()))
    results.append(json.loads(rec.FinalResult()))
    text = " ".join(r.get("text", "") for r in results).strip()
    words = []
    for r in results:
        for w in r.get("result", []):
            words.append((w["word"], round(w["conf"], 3)))
    return text, words


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)
    if sys.argv[1] == "--suite":
        suite(sys.argv[2])
        return
    model_dir = sys.argv[1]
    wav = sys.argv[2]
    phrases = sys.argv[3:] or ["opa amigo"]
    import vosk
    vosk.SetLogLevel(-1)
    model = vosk.Model(model_dir)
    text, words = recognize(model, wav, phrases)
    hit = "opa amigo" in text.lower()
    print(f"wav={os.path.basename(wav)} phrases={phrases}")
    print(f"  texto: {text!r}")
    print(f"  palavras: {words}")
    print(f"  HIT 'opa amigo'? {'SIM' if hit else 'nao'}")


# ---------------- suite: mede precisão em todos os wavs ----------------
POSITIVOS = [
    "/tmp/wa_1790680203553_7f45d665.wav",
    "/tmp/wa_1790680199782_5be7d5a5.wav",
    "/tmp/tts_opa_amigo.wav",
    "/tmp/opa_amigo.wav",
    "/tmp/tts_opa.wav",
    "/tmp/opa.wav",
]
NEGATIVOS = [
    "/tmp/hey_nanobot.wav",
    "/tmp/tts_hey_nanobot_pt.wav",
    "/tmp/tts_hey_nano.wav",
    "/tmp/c_alexa.wav",
    "/tmp/k_alexa.wav",
    "/tmp/tts_ei_nanobot.wav",
    "/tmp/tts_oi_nanobot.wav",
    "/tmp/tts_nanobot.wav",
    "/tmp/k_hey_nanobot.wav",
    "/tmp/k_hey_nano.wav",
    "/tmp/k_hey_bot.wav",
    "/tmp/k_nano.wav",
    "/tmp/k_oi_nano.wav",
    "/tmp/k_opa_nano.wav",
    "/tmp/k_ei_nano.wav",
    "/tmp/k_fala_nano.wav",
    "/tmp/teste_stt.wav",
    "/tmp/test_pt.wav",
]


def suite(model_dir):
    import vosk
    vosk.SetLogLevel(-1)
    model = vosk.Model(model_dir)
    # variações de grafia que o decoder PT pode emitir para "opa amigo"
    phrases = ["opa amigo", "opa", "amigo", "opá amigo"]
    tp = fp = fn = tn = 0
    print("=== POSITIVOS (deveriam acender) ===")
    for w in POSITIVOS:
        if not os.path.exists(w):
            continue
        text, words = recognize(model, w, phrases)
        h = text.lower().replace(" ", "") in ("opaamigo", "opaaamigo")
        ok = h
        tp += ok
        fn += not ok
        print(f"  [{'HIT ' if ok else 'MISS'}] {os.path.basename(w):42} -> {text!r}")
    print("\n=== NEGATIVOS (nao deveriam acender) ===")
    for w in NEGATIVOS:
        if not os.path.exists(w):
            continue
        text, words = recognize(model, w, phrases)
        fired = text.lower().replace(" ", "").startswith("opaamigo")
        fp += fired
        tn += not fired
        print(f"  [{'FALSO+' if fired else 'ok   '}] {os.path.basename(w):42} -> {text!r}")
    print(f"\nAcerto (recall) positivos: {tp}/{tp+fn}")
    print(f"Falso positivo negativos:  {fp}/{fp+tn}")


if __name__ == "__main__":
    main()
