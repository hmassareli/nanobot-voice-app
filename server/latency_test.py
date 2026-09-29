#!/usr/bin/env python3
"""Latency test for /ask_stream: reads NDJSON line-by-line, no buffering."""
import json
import time
import urllib.request

URL = "https://nanobot-voice-api.lnyx9r.easypanel.host/ask_stream"
TOKEN = "nanobot-voice"
PHRASES = [
    "Que horas sao?",
    "Quanto e dois mais dois?",
    "Quem descobriu o Brasil?",
    "Qual e a capital da Franca?",
]

results = []
for phrase in PHRASES:
    body = json.dumps({"text": phrase}).encode()
    req = urllib.request.Request(
        URL, data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    first_audio = None
    total = None
    n_audio = 0
    answer = ""
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            for raw in resp:  # iterates line by line, unbuffered
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                now = (time.perf_counter() - t0) * 1000
                if obj.get("type") == "audio":
                    n_audio += 1
                    if first_audio is None:
                        first_audio = now
                elif obj.get("type") == "text":
                    answer = obj.get("text", "")
                elif obj.get("type") == "done":
                    total = now
                elif obj.get("type") == "error":
                    print(f"  ERROR: {obj.get('message')}")
    except Exception as e:  # noqa: BLE001
        print(f"  REQUEST FAILED: {type(e).__name__}: {e}")
    results.append((phrase, first_audio, total, n_audio, answer))
    fa_s = f"{first_audio:.0f}ms" if first_audio is not None else "N/A"
    to_s = f"{total:.0f}ms" if total is not None else "N/A"
    print(f"{phrase!r}: first_audio={fa_s} total={to_s} "
          f"chunks={n_audio} answer={answer[:60]!r}")

fa = [r[1] for r in results if r[1] is not None]
print(f"\nAVG first_audio = {sum(fa)/len(fa):.0f}ms over {len(fa)} tests")
