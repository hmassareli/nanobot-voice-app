import json, time, urllib.request, statistics
URL="https://nanobot-voice-api.lnyx9r.easypanel.host/ask_stream"; TOK="nanobot-voice"
def call(txt):
    req=urllib.request.Request(URL,data=json.dumps({"text":txt}).encode(),
        headers={"Authorization":f"Bearer {TOK}","Content-Type":"application/json"})
    t0=time.perf_counter(); fa=None; tm={}
    with urllib.request.urlopen(req,timeout=120) as r:
        for raw in r:
            line=raw.decode().strip()
            if not line: continue
            try: o=json.loads(line)
            except: continue
            if o.get("type")=="audio" and fa is None: fa=(time.perf_counter()-t0)*1000
            elif o.get("type")=="done": tm=o.get("timings",{})
    return fa, tm
for q in ["Quanto é dois mais dois?","Qual a capital da França?","Me conte uma curiosidade sobre gatos."]:
    fa,tm=call(q)
    print(f"{q[:32]:34} wall_first_audio={fa and round(fa)}ms timings={tm}")
