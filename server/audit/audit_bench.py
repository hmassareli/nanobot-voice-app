#!/usr/bin/env python3
"""Audit benchmark: isolate STT / LLM / TTS stages on the production endpoint."""
import json, time, urllib.request, statistics, sys

URL = "https://nanobot-voice-api.lnyx9r.easypanel.host/ask_stream"
TOKEN = "nanobot-voice"

def call(payload, label):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST")
    t0 = time.perf_counter()
    first_audio = None; total = None; n_audio = 0; answer = ""; timings = {}
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", "replace").strip()
                if not line: continue
                try: obj = json.loads(line)
                except json.JSONDecodeError: continue
                now = (time.perf_counter() - t0) * 1000
                if obj.get("type") == "audio":
                    n_audio += 1
                    if first_audio is None: first_audio = now
                elif obj.get("type") == "text": answer = obj.get("text", "")
                elif obj.get("type") == "done": total = now; timings = obj.get("timings", {})
                elif obj.get("type") == "error": print(f"  ERROR: {obj.get('message')}")
    except Exception as e:
        print(f"  FAILED: {type(e).__name__}: {e}")
    fa = f"{first_audio:.0f}" if first_audio is not None else "N/A"
    to = f"{total:.0f}" if total is not None else "N/A"
    print(f"[{label}] wall_first_audio={fa}ms wall_total={to}ms chunks={n_audio} "
          f"timings={timings} answer={answer[:70]!r}")
    return first_audio, total, timings

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "all"
    if mode in ("all", "tts"):
        print("=== TTS isolation (fixed short text, no LLM variability) ===")
        for voice in ["santa", "antonio"]:
            for txt in ["Paris.", "Quatro.", "Sim, claro.", "Bom dia, tudo bem com você hoje?"]:
                call({"text": f"Repita exatamente: {txt}", "voice": voice}, f"tts-{voice}")
    if mode in ("all", "llm"):
        print("=== LLM isolation (short prompts) ===")
        for p in ["Diga apenas: oi", "Quanto é dois mais dois?", "Qual a capital da França?"]:
            call({"text": p}, "llm")
