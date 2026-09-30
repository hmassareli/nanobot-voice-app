#!/usr/bin/env python3
"""Direct OpenRouter benchmark: separate network/TTFT from model generation."""
import json, time, urllib.request, os

KEY = os.environ.get("OPENROUTER_API_KEY", "")
URL = "https://openrouter.ai/api/v1/chat/completions"

def bench(model, prompt, reasoning=None, n=3):
    for i in range(n):
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": True,
            "provider": {"sort": "throughput", "allow_fallbacks": True},
        }
        if reasoning is not None:
            payload["reasoning"] = reasoning
        body = json.dumps(payload).encode()
        req = urllib.request.Request(URL, data=body, headers={
            "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"})
        t0 = time.perf_counter()
        ttft = None; ntok = 0; text = ""
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"): continue
                    d = line[5:].strip()
                    if d == "[DONE]": break
                    try: obj = json.loads(d)
                    except: continue
                    ch = obj.get("choices") or []
                    if not ch: continue
                    delta = ch[0].get("delta") or {}
                    if delta.get("content"):
                        if ttft is None: ttft = (time.perf_counter()-t0)*1000
                        ntok += 1; text += delta["content"]
        except Exception as e:
            print(f"  FAIL {type(e).__name__}: {e}"); continue
        tot = (time.perf_counter()-t0)*1000
        print(f"[{model}] ttft={ttft:.0f}ms total={tot:.0f}ms chunks={ntok} text={text[:50]!r}")

if __name__ == "__main__":
    print("=== deepseek-v4.1-flash, reasoning off ===")
    bench("deepseek/deepseek-v4.1-flash", "Quanto é dois mais dois? Responda em uma frase curta.", {"enabled": False})
    print("=== deepseek-v4.1-flash, no reasoning field ===")
    bench("deepseek/deepseek-v4.1-flash", "Quanto é dois mais dois? Responda em uma frase curta.")
    print("=== gpt-4o-mini ===")
    bench("openai/gpt-4o-mini", "Quanto é dois mais dois? Responda em uma frase curta.")
    print("=== gemini-2.5-flash ===")
    bench("google/gemini-2.5-flash", "Quanto é dois mais dois? Responda em uma frase curta.")
    print("=== llama-3.1-8b-instruct ===")
    bench("meta-llama/llama-3.1-8b-instruct", "Quanto é dois mais dois? Responda em uma frase curta.")
