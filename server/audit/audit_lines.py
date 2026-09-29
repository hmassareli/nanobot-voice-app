#!/usr/bin/env python3
"""Timestamp every NDJSON line from production /ask_stream to localize delay."""
import json, time, urllib.request, sys

URL = "https://nanobot-voice-api.lnyx9r.easypanel.host/ask_stream"
TOKEN = "nanobot-voice"

def call(payload, label):
    body = json.dumps(payload).encode()
    req = urllib.request.Request(URL, data=body,
        headers={"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}, method="POST")
    t0 = time.perf_counter()
    print(f"\n--- {label} ---")
    with urllib.request.urlopen(req, timeout=180) as resp:
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line: continue
            now = (time.perf_counter()-t0)*1000
            try: obj = json.loads(line)
            except: continue
            t = obj.get("type")
            if t == "audio":
                print(f"  {now:7.0f}ms AUDIO idx={obj.get('index')} b64len={len(obj.get('data',''))} text={obj.get('text','')[:50]!r}")
            elif t == "text":
                print(f"  {now:7.0f}ms TEXT {obj.get('text','')[:70]!r}")
            elif t == "done":
                print(f"  {now:7.0f}ms DONE {obj.get('timings')}")
            elif t == "error":
                print(f"  {now:7.0f}ms ERROR {obj.get('message')}")

if __name__ == "__main__":
    call({"text":"Qual é a capital da França?"}, "capital (fresh)")
    call({"text":"Qual é a capital da França?"}, "capital (repeat)")
    call({"text":"Diga apenas: oi"}, "diga oi")
    call({"text":"Quanto é dois mais dois?"}, "2+2")
