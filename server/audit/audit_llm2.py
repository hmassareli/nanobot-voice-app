#!/usr/bin/env python3
"""Faithful production-payload benchmark: system prompt + tools + history."""
import json, time, urllib.request

KEY = "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"
URL = "https://openrouter.ai/api/v1/chat/completions"

SYSTEM = ("Você é o nanobot, um assistente pessoal de voz. Responda em português do "
"Brasil, de forma concisa e falada (frases curtas, sem markdown, sem listas, "
"sem emojis). Suas respostas serão convertidas em áudio.\n\n"
"O usuário fala por voz. NUNCA peça para ele 'escrever' ou 'mandar por "
"texto'. Se algo não ficou claro, peça para repetir em voz alta.\n\n"
"Você tem acesso ao workspace do assistente com ferramentas: read_file, "
"list_dir, exec (shell) e curl (requisições HTTP). Use-as quando precisar "
"consultar arquivos, o ambiente ou a internet. Caminhos são relativos ao "
"workspace (/workspace).\n\n" + "X"*6000)  # placeholder for SOUL/USER/TOOLS

TOOLS = [
 {"type":"function","function":{"name":"read_file","description":"Lê um arquivo de texto do workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}},
 {"type":"function","function":{"name":"list_dir","description":"Lista o conteúdo de um diretório do workspace.","parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":[]}}},
 {"type":"function","function":{"name":"exec","description":"Executa um comando shell dentro do workspace.","parameters":{"type":"object","properties":{"command":{"type":"string"}},"required":["command"]}}},
 {"type":"function","function":{"name":"curl","description":"Faz uma requisição HTTP (GET por padrão) e devolve o corpo da resposta. Use para consultar APIs públicas, checar status de serviços/sites, buscar informações na web, etc.","parameters":{"type":"object","properties":{"url":{"type":"string"},"method":{"type":"string"},"headers":{"type":"object"},"data":{"type":"string"}},"required":["url"]}}},
]

def bench(prompt, with_tools=True, with_system=True, n=3):
    for i in range(n):
        msgs = []
        if with_system: msgs.append({"role":"system","content":SYSTEM})
        msgs.append({"role":"user","content":prompt})
        payload = {"model":"deepseek/deepseek-v4.1-flash","messages":msgs,"stream":True,
                   "provider":{"sort":"throughput","allow_fallbacks":True},"reasoning":{"enabled":False}}
        if with_tools: payload["tools"] = TOOLS
        body = json.dumps(payload).encode()
        req = urllib.request.Request(URL, data=body, headers={"Authorization":f"Bearer {KEY}","Content-Type":"application/json"})
        t0=time.perf_counter(); ttft=None; ntok=0; text=""; toolcalls=0; reasoning_tok=0
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line=raw.decode("utf-8","replace").strip()
                    if not line.startswith("data:"): continue
                    d=line[5:].strip()
                    if d=="[DONE]": break
                    try: obj=json.loads(d)
                    except: continue
                    ch=obj.get("choices") or []
                    if not ch: continue
                    delta=ch[0].get("delta") or {}
                    if delta.get("content"):
                        if ttft is None: ttft=(time.perf_counter()-t0)*1000
                        ntok+=1; text+=delta["content"]
                    if delta.get("reasoning"): reasoning_tok+=1
                    if delta.get("tool_calls"): toolcalls+=1
        except Exception as e:
            print(f"  FAIL {type(e).__name__}: {e}"); continue
        tot=(time.perf_counter()-t0)*1000
        print(f"tools={with_tools} sys={with_system} ttft={ttft and round(ttft)}ms total={tot:.0f}ms chunks={ntok} toolcalls={toolcalls} reasoning={reasoning_tok} text={text[:60]!r}")

if __name__=="__main__":
    print("=== short math, WITH tools+system (production-like) ===")
    bench("Quanto é dois mais dois?")
    print("=== short math, NO tools ===")
    bench("Quanto é dois mais dois?", with_tools=False)
    print("=== short math, NO system ===")
    bench("Quanto é dois mais dois?", with_system=False, with_tools=False)
    print("=== capital, WITH tools+system ===")
    bench("Qual é a capital da França?")
