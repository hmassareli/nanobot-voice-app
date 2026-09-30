"""nanobot-voice-api — lightweight HTTP backend for a voice Android app.

Endpoints:
  GET  /health           -> {"status":"ok"}
  GET  /voices           -> selectable voices
  GET  /audio?path=...   -> serve an audio file from the workspace (Bearer auth)
  POST /tts              -> text -> audio, NO LLM (Bearer auth)
  POST /ask              -> transcribe (optional) + run agent + TTS
                            Accepts multipart "audio" (WAV) OR JSON {"text": "..."}.
                            Auth: Authorization: Bearer <VOICE_TOKEN>
  POST /ask_stream       -> same input, NDJSON streaming (sentence-level audio)
  GET  /logs, /logs/summary, POST /report -> telemetry

Agent tools: read_file, list_dir, exec, curl, play_audio, ouvir_livro.
  * play_audio  -> enqueue an existing audio file (or TTS of `text`) to be
                   played on the phone; the stream emits a {"type":"play"} event.
  * ouvir_livro -> synthesize the next book excerpt with the local voice engine
                   (no LLM tokens) and enqueue it for playback.

Pipeline: STT (Groq Whisper -> faster-whisper local) ->
          nanobot-style agent (OpenRouter, streaming SSE, workspace tools) ->
          edge-tts (MP3, no transcode) -> base64.

Latency design (v2):
  * The LLM is consumed as an SSE stream. As soon as a sentence boundary is
    detected in the token stream, that sentence is queued for TTS *while the
    LLM keeps generating the next sentence* (producer/consumer via asyncio.Queue).
  * TTS returns the raw MP3 from edge-tts directly (no ffmpeg transcode, no temp
    files). Android MediaPlayer plays MP3 natively, so the client is unchanged.
  * A tiny TTS warm-up runs at startup to warm DNS/TLS.

Latency is instrumented per stage (stt_ms, llm_ms, tts_ms, total_ms) and returned
in the JSON response under "timings" and logged to stdout as `TIMING ...`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import tempfile
import threading
import time
import uuid
import wave
from pathlib import Path

import re

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

import telemetry

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-api")

VOICE_TOKEN = os.environ.get("VOICE_TOKEN", "nanobot-voice")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = os.environ.get(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4.1-flash")
# Reasoning effort sent to OpenRouter. Voice replies must be snappy, so by
# default we turn chain-of-thought OFF entirely (measured: "off" -> 0 reasoning
# tokens, first token ~0.6-1.3s; "low" was no faster and sometimes slower).
# Overridable via env: low | medium | high | off.
REASONING_EFFORT = os.environ.get("REASONING_EFFORT", "off")
TTS_VOICE = os.environ.get("TTS_VOICE", "pt-BR-AntonioNeural")
# TTS engine: "kokoro" (self-hosted, cheap+fast) or "edge" (edge-tts, free fallback).
TTS_ENGINE = os.environ.get("TTS_ENGINE", "kokoro")
TTS_MODEL = os.environ.get("TTS_MODEL", "hexgrad/kokoro-82m")
TTS_KOKORO_VOICE = os.environ.get("TTS_KOKORO_VOICE", "pm_santa")
TTS_KOKORO_LANG = os.environ.get("TTS_KOKORO_LANG", "p")
OPENROUTER_SPEECH_URL = os.environ.get(
    "OPENROUTER_SPEECH_URL", "https://openrouter.ai/api/v1/audio/speech"
)
# Friendly voice names the client may request. Maps a stable key -> (engine, id).
# The app sends {"voice": "<key>"} (or a raw engine id, accepted for back-compat).
VOICE_ALIASES: dict[str, tuple[str, str]] = {
    # --- Kokoro (self-hosted, cheap + fast) ---
    "santa": ("kokoro", "pm_santa"),    # masculina, calma
    "dora": ("kokoro", "pf_dora"),      # feminina
    "alex": ("kokoro", "pm_alex"),      # masculina
    # --- edge-tts (free cloud fallback) ---
    "antonio": ("edge", "pt-BR-AntonioNeural"),
    "francisca": ("edge", "pt-BR-FranciscaNeural"),
    "thalita": ("edge", "pt-BR-ThalitaMultilingualNeural"),
}
DEFAULT_VOICE_KEY = os.environ.get("TTS_VOICE_KEY", "santa")
# Raw engine ids are also accepted (e.g. "pt-BR-AntonioNeural", "pm_santa").
_RAW_EDGE = {"pt-BR-AntonioNeural", "pt-BR-FranciscaNeural", "pt-BR-ThalitaMultilingualNeural"}
_RAW_KOKORO = {"pm_santa", "pf_dora", "pm_alex", "af_heart"}


def resolve_voice(name: str | None) -> tuple[str, str]:
    """Map a client-supplied voice name to (engine, voice_id).

    Accepts friendly keys (see VOICE_ALIASES), raw edge/Kokoro ids, or None/empty
    (falls back to the configured default). Unknown names fall back to default.
    """
    if not name:
        name = DEFAULT_VOICE_KEY
    key = name.strip()
    if key in VOICE_ALIASES:
        return VOICE_ALIASES[key]
    if key in _RAW_EDGE:
        return ("edge", key)
    if key in _RAW_KOKORO:
        return ("kokoro", key)
    log.warning("Unknown voice %r — using default %r", name, DEFAULT_VOICE_KEY)
    return VOICE_ALIASES[DEFAULT_VOICE_KEY]
WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "small")
WHISPER_LANGUAGE = os.environ.get("WHISPER_LANGUAGE", "pt")
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "25"))

app = FastAPI(title="nanobot-voice-api")

# --- conversation memory (single-user voice app) ---------------------------
HISTORY: list[dict] = []
MAX_HISTORY = 20


def build_system_prompt() -> str:
    parts = [
        "Você é o nanobot, um assistente pessoal de voz. Responda em português do "
        "Brasil, de forma concisa e falada (frases curtas, sem markdown, sem listas, "
        "sem emojis). Suas respostas serão convertidas em áudio.",
        "O usuário fala por voz. NUNCA peça para ele 'escrever' ou 'mandar por "
        "texto'. Se algo não ficou claro, peça para repetir em voz alta.",
        "Você tem acesso ao workspace do assistente com ferramentas: read_file, "
        "list_dir, exec (shell) e curl (requisições HTTP). Use-as quando precisar "
        "consultar arquivos, o ambiente ou a internet. Caminhos são relativos ao "
        "workspace (/workspace).",
    ]
    # SOUL.md gives the same personality as the main nanobot; USER.md carries the
    # user's profile/preferences. AGENTS.md is deliberately skipped: it documents
    # the nanobot chat plumbing (spawn/message/cron) that this voice endpoint
    # does not expose, and would only confuse the model.
    for fn in ("SOUL.md", "USER.md", "TOOLS.md"):
        p = WORKSPACE / fn
        if p.exists():
            try:
                parts.append(f"### {fn}\n{p.read_text(encoding='utf-8')[:4000]}")
            except Exception:  # noqa: BLE001
                pass
    return "\n\n".join(parts)


# --- workspace tools -------------------------------------------------------
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Lê um arquivo de texto do workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "Lista o conteúdo de um diretório do workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "exec",
            "description": "Executa um comando shell dentro do workspace.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "curl",
            "description": (
                "Faz uma requisição HTTP (GET por padrão) e devolve o corpo da "
                "resposta. Use para consultar APIs públicas, checar status de "
                "serviços/sites, buscar informações na web, etc. Ex.: "
                "curl(url='https://api.exemplo.com/dados')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL completa (https://...)"},
                    "method": {
                        "type": "string",
                        "description": "Método HTTP (GET, POST, HEAD...). Padrão GET.",
                    },
                    "headers": {
                        "type": "object",
                        "description": "Cabeçalhos HTTP opcionais (chave: valor).",
                    },
                    "data": {
                        "type": "string",
                        "description": "Corpo da requisição (para POST/PUT).",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_audio",
            "description": (
                "Toca um arquivo de áudio no alto-falante do celular do usuário. "
                "Use quando ele pedir para OUVIR um áudio/livro. O áudio toca por "
                "completo e depois a conversa continua normalmente."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": (
                            "Caminho do arquivo de áudio no workspace (absoluto ou "
                            "relativo). Ex.: skills/audio/livro-0001-0001.ogg"
                        ),
                    },
                    "text": {
                        "type": "string",
                        "description": (
                            "Texto a ser sintetizado e tocado, quando não há um "
                            "arquivo pronto. Opcional."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ouvir_livro",
            "description": (
                "Gera em voz o próximo trecho do livro (ou o trecho indicado) "
                "usando o motor de voz local, SEM gastar tokens de LLM, e toca o "
                "áudio no celular do usuário. Use quando ele pedir para "
                "ouvir/continuar o livro."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "slug": {
                        "type": "string",
                        "description": "Slug do livro (opcional se só houver um).",
                    },
                    "trechos": {
                        "type": "integer",
                        "description": "Quantos trechos juntar (padrão 1).",
                    },
                    "voz": {
                        "type": "string",
                        "description": "Voz: santa|dora|alex (padrão santa).",
                    },
                },
                "required": [],
            },
        },
    },
]


def _safe(path: str) -> Path:
    p = (WORKSPACE / (path or ".")).resolve()
    if WORKSPACE.resolve() not in p.parents and p != WORKSPACE.resolve():
        raise ValueError("path outside workspace")
    return p


# Sentinel returned by tools that want the streaming loop to enqueue an audio
# file for playback on the phone instead of speaking the tool result aloud.
PLAY_AUDIO_PREFIX = "__PLAY_AUDIO__:"


def _resolve_audio_path(path: str) -> Path:
    """Resolve a workspace audio path (absolute inside the workspace OR relative
    to it) and validate it stays inside the workspace. Raises ValueError."""
    raw = (path or "").strip()
    if not raw:
        raise ValueError("caminho vazio")
    p = Path(raw)
    if not p.is_absolute():
        p = WORKSPACE / p
    p = p.resolve()
    ws = WORKSPACE.resolve()
    if ws not in p.parents and p != ws:
        raise ValueError("path outside workspace")
    return p


async def _tool_play_audio(args: dict) -> str:
    """Tool `play_audio`: enqueue an existing file, or TTS `text` to a temp file.

    Returns the `__PLAY_AUDIO__:<host path>` sentinel on success."""
    path = (args.get("path") or "").strip()
    text = (args.get("text") or "").strip()
    if path:
        p = _resolve_audio_path(path)
        if not p.exists() or not p.is_file():
            return f"erro: arquivo não encontrado: {path}"
        return f"{PLAY_AUDIO_PREFIX}{p}"
    if text:
        voice = args.get("voice")
        data = await tts_to_mp3_bytes(text, voice)
        if not data:
            return "erro: falha ao sintetizar o áudio"
        out_dir = WORKSPACE / "skills" / "audio"
        out_dir.mkdir(parents=True, exist_ok=True)
        out = out_dir / f"_tts_{uuid.uuid4().hex}.mp3"
        out.write_bytes(data)
        return f"{PLAY_AUDIO_PREFIX}{out}"
    return "erro: informe 'path' ou 'text'"


async def _tool_ouvir_livro(args: dict) -> str:
    """Tool `ouvir_livro`: run the book reader script (local TTS, no LLM) and
    enqueue the generated audio. Returns the sentinel + a short summary."""
    slug = (args.get("slug") or "").strip()
    try:
        trechos = int(args.get("trechos") or 1)
    except (TypeError, ValueError):
        trechos = 1
    trechos = max(1, trechos)
    voz = (args.get("voz") or "santa").strip() or "santa"

    script = WORKSPACE / "skills" / "leitor-livros" / "scripts" / "livro_voz.py"
    if not script.exists():
        return f"erro: script do leitor de livros não encontrado ({script})"

    cmd = ["python3", str(script), "ouvir"]
    if slug:
        cmd.append(slug)
    cmd += ["--trechos", str(trechos), "--voz", voz]

    def _run() -> subprocess.CompletedProcess:
        # Synthesis is slow (Kokoro on CPU, ~60s per excerpt) — generous timeout.
        return subprocess.run(
            cmd, cwd=str(WORKSPACE), capture_output=True, text=True, timeout=300,
        )

    try:
        r = await asyncio.to_thread(_run)
    except subprocess.TimeoutExpired:
        return "erro: a geração do áudio do livro demorou demais (timeout de 300s)"

    out = (r.stdout or "") + (r.stderr or "")
    if r.returncode != 0:
        return f"erro ao gerar o áudio do livro: {out[-500:]}"

    # The script prints the generated file path on the last line as ARQUIVO:<path>.
    caminho = None
    for line in reversed((r.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("ARQUIVO:"):
            caminho = line[len("ARQUIVO:"):].strip()
            break
    if not caminho:
        return f"erro: não encontrei o arquivo gerado. Saída: {out[-500:]}"
    if not os.path.exists(caminho):
        return f"erro: arquivo gerado não existe: {caminho}"

    # Short spoken summary (title / excerpt range) for the model to comment on.
    resumo = ""
    for line in (r.stdout or "").splitlines():
        s = line.strip()
        if s.startswith("Trechos ") or s.startswith("==="):
            resumo = s
            break
    return f"{PLAY_AUDIO_PREFIX}{caminho}\n{resumo}".strip()


def _handle_tool_output(out: str, stats: dict) -> str:
    """Post-process a tool result before feeding it back to the model.

    If the result is a `__PLAY_AUDIO__:<path>` sentinel, register the path in
    `stats["play_audio"]` (so the HTTP layer can emit a `play` event) and return
    a short message for the model instead of the raw sentinel (which must never
    be spoken aloud)."""
    if isinstance(out, str) and out.startswith(PLAY_AUDIO_PREFIX):
        first, _, rest = out.partition("\n")
        path = first[len(PLAY_AUDIO_PREFIX):].strip()
        if path:
            stats.setdefault("play_audio", []).append(path)
        msg = "Áudio enfileirado para tocar no celular."
        if rest.strip():
            msg += f" {rest.strip()}"
        return msg
    return out


async def dispatch_tool(name: str, args: dict) -> str:
    try:
        if name == "read_file":
            p = _safe(args.get("path", ""))
            return p.read_text(encoding="utf-8", errors="replace")[:8000]
        if name == "list_dir":
            p = _safe(args.get("path", "."))
            return "\n".join(sorted(x.name for x in p.iterdir()))[:4000]
        if name == "exec":
            cmd = args.get("command", "")
            r = subprocess.run(
                cmd, shell=True, cwd=str(WORKSPACE),
                capture_output=True, text=True, timeout=30,
            )
            return (r.stdout + r.stderr)[:8000] or "(sem saída)"
        if name == "play_audio":
            return await _tool_play_audio(args)
        if name == "ouvir_livro":
            return await _tool_ouvir_livro(args)
        if name == "curl":
            url = (args.get("url") or "").strip()
            if not url:
                return "erro: url vazia"
            if not url.startswith(("http://", "https://")):
                url = "https://" + url
            method = (args.get("method") or "GET").upper()
            headers = args.get("headers") or {}
            data = args.get("data")
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.request(
                    method, url,
                    headers={str(k): str(v) for k, v in headers.items()} if headers else None,
                    content=data.encode() if isinstance(data, str) else data,
                )
            body = r.text[:8000]
            return f"HTTP {r.status_code}\n{body}" or f"HTTP {r.status_code} (corpo vazio)"
    except Exception as e:  # noqa: BLE001
        return f"erro: {e}"
    return "ferramenta desconhecida"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {OPENROUTER_API_KEY}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://nanobot-voice-api.lnyx9r.easypanel.host",
        "X-Title": "nanobot-voice-api",
    }


def _provider_route() -> dict:
    # Route to the fastest available upstream provider (OpenRouter "throughput"
    # sort). allow_fallbacks keeps things working if the top provider is down.
    return {"sort": "throughput", "allow_fallbacks": True}


def _reasoning() -> dict | None:
    # Ask the model to keep chain-of-thought short (or off) so the first spoken
    # token arrives fast. OpenRouter accepts {"effort": low|medium|high} or
    # {"enabled": false}. "off"/"none"/"" disables reasoning entirely.
    effort = (REASONING_EFFORT or "").strip().lower()
    if effort in ("", "off", "none", "false", "0"):
        return {"enabled": False}
    return {"effort": effort}


def _remember(user_text: str, answer: str) -> None:
    HISTORY.append({"role": "user", "content": user_text})
    HISTORY.append({"role": "assistant", "content": answer})
    del HISTORY[:-MAX_HISTORY]


def _remember_overflow(user_text: str, stats: dict | None) -> None:
    """Guarda no histórico um resumo do que já foi executado quando o limite de
    rodadas é atingido, para não perder o contexto entre as mensagens."""
    calls = []
    outputs = []
    if stats:
        calls = stats.get("tool_calls") or []
        outputs = stats.get("tool_outputs") or []
    # deduplica mantendo ordem
    seen, uniq = set(), []
    for c in calls:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    resumo = ("[CONTEXTO PRESERVADO] A tarefa anterior foi interrompida por atingir "
              "o limite de rodadas de ferramentas. Ferramentas já usadas: "
              + (", ".join(uniq) if uniq else "nenhuma") + ".")
    # Anexa os últimos resultados das ferramentas para o modelo conseguir
    # retomar de onde parou sem refazer tudo.
    if outputs:
        resumo += "\n\nÚltimos resultados obtidos (mais recentes por último):\n"
        for item in outputs[-6:]:
            resumo += f"- {item['name']}: {item['out']}\n"
    resumo += ("\nAo retomar, continue a partir daí sem recomeçar do zero.")
    HISTORY.append({"role": "user", "content": user_text})
    HISTORY.append({"role": "assistant", "content": resumo})
    del HISTORY[:-MAX_HISTORY]


def _overflow_message() -> str:
    """Mensagem falada quando o limite de rodadas de ferramentas é atingido.

    Curta e clara para TTS: explica o que aconteceu, que o contexto foi
    preservado e o que o usuário deve fazer."""
    return ("Essa tarefa precisou de mais passos do que eu consigo dar de uma vez "
            f"só (meu limite é de {MAX_TOOL_ROUNDS} rodadas de ferramentas). "
            "Já guardei o que fiz até aqui, então não precisa recomeçar. "
            "Me diga 'continua' que eu sigo de onde parei, ou divida o pedido "
            "em partes menores.")


async def run_agent(user_text: str, model: str | None = None,
                    stats: dict | None = None) -> str:
    """Non-streaming agent (kept for /ask and as a fallback).

    If `stats` is given it is filled in-place with telemetry (token usage, tool
    names) exactly like `stream_agent`."""
    if stats is None:
        stats = {}
    stats.setdefault("tool_calls", [])
    stats.setdefault("tool_rounds", 0)
    if not OPENROUTER_API_KEY:
        return "Serviço sem chave de LLM configurada."
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += HISTORY[-MAX_HISTORY:]
    messages.append({"role": "user", "content": user_text})

    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            payload = {
                "model": model or MODEL,
                "messages": messages,
                "tools": TOOLS,
                "provider": _provider_route(),
                "reasoning": _reasoning(),
            }
            r = await client.post(OPENROUTER_URL, headers=_headers(), json=payload)
            r.raise_for_status()
            body = r.json()
            usage = body.get("usage")
            if isinstance(usage, dict):
                # Accumulate across tool rounds (each round bills separately).
                prev = stats.get("usage") or {}
                merged = dict(prev)
                for k in ("prompt_tokens", "completion_tokens", "total_tokens",
                          "reasoning_tokens"):
                    if usage.get(k) is not None:
                        merged[k] = (prev.get(k) or 0) + usage[k]
                if usage.get("cost") is not None:
                    merged["cost"] = (prev.get("cost") or 0.0) + usage["cost"]
                if isinstance(usage.get("completion_tokens_details"), dict):
                    merged["completion_tokens_details"] = usage["completion_tokens_details"]
                if isinstance(usage.get("cost_details"), dict):
                    merged["cost_details"] = usage["cost_details"]
                stats["usage"] = merged
            msg = body["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                answer = (msg.get("content") or "").strip()
                _remember(user_text, answer)
                return answer
            messages.append(msg)
            for tc in tool_calls:
                fn = tc["function"]["name"]
                stats["tool_calls"].append(fn)
                stats["tool_rounds"] = stats.get("tool_rounds", 0) + 1
                try:
                    targs = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    targs = {}
                out = await dispatch_tool(fn, targs)
                out = _handle_tool_output(out, stats)
                stats.setdefault("tool_outputs", []).append(
                    {"name": fn, "out": (out or "")[:400]}
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": out,
                })
        # Limite atingido: NÃO descarta o contexto. Guarda um resumo do que já
        # foi executado no histórico, para continuar de onde parou.
        _remember_overflow(user_text, stats)
        return _overflow_message()


async def stream_agent(user_text: str, model: str | None = None,
                       stats: dict | None = None):
    """Streaming agent: yields content deltas as they arrive from OpenRouter.

    Tool-calling is supported across rounds: if the model emits tool_calls in a
    round, the accumulated calls are executed and the loop continues (the next
    round's content is streamed). In the common no-tool case the very first
    tokens are yielded immediately, so TTS can start before the answer is done.

    If `stats` is provided it is filled in-place with telemetry for the turn:
    token usage (prompt/completion/total/cost/reasoning), the list of tool names
    invoked and how many tool rounds were needed.
    """
    if stats is None:
        stats = {}
    stats.setdefault("tool_calls", [])
    stats.setdefault("tool_rounds", 0)
    if not OPENROUTER_API_KEY:
        yield "Serviço sem chave de LLM configurada."
        return
    messages = [{"role": "system", "content": build_system_prompt()}]
    messages += HISTORY[-MAX_HISTORY:]
    messages.append({"role": "user", "content": user_text})

    async with httpx.AsyncClient(timeout=120) as client:
        for _ in range(MAX_TOOL_ROUNDS):
            payload = {
                "model": model or MODEL,
                "messages": messages,
                "tools": TOOLS,
                "provider": _provider_route(),
                "reasoning": _reasoning(),
                "stream": True,
                # Ask OpenRouter to append a final chunk carrying token usage +
                # cost, so we can log exactly what each turn spent.
                "stream_options": {"include_usage": True},
            }
            content_parts: list[str] = []
            tool_calls: dict[int, dict] = {}
            async with client.stream(
                "POST", OPENROUTER_URL, headers=_headers(), json=payload
            ) as r:
                r.raise_for_status()
                async for line in r.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # Usage typically arrives on the final chunk (empty choices).
                    usage = obj.get("usage")
                    if isinstance(usage, dict):
                        stats["usage"] = usage
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)
                        yield piece
                    elif delta.get("reasoning"):
                        # Reasoning models stream their chain-of-thought too.
                        # Drop it: it would be spoken aloud (and is slow).
                        pass
                    for tc in delta.get("tool_calls") or []:
                        idx = tc.get("index", 0)
                        slot = tool_calls.setdefault(
                            idx, {"id": "", "name": "", "arguments": ""}
                        )
                        if tc.get("id"):
                            slot["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["arguments"] += fn["arguments"]

            if tool_calls:
                stats["tool_rounds"] = stats.get("tool_rounds", 0) + 1
                for s in tool_calls.values():
                    if s.get("name"):
                        stats["tool_calls"].append(s["name"])
                # Model wants tools: run them, then continue streaming next round.
                messages.append({
                    "role": "assistant",
                    "content": "".join(content_parts) or None,
                    "tool_calls": [
                        {
                            "id": s["id"],
                            "type": "function",
                            "function": {"name": s["name"], "arguments": s["arguments"]},
                        }
                        for s in tool_calls.values()
                    ],
                })
                for s in tool_calls.values():
                    try:
                        targs = json.loads(s["arguments"] or "{}")
                    except json.JSONDecodeError:
                        targs = {}
                    out = await dispatch_tool(s["name"], targs)
                    out = _handle_tool_output(out, stats)
                    stats.setdefault("tool_outputs", []).append(
                        {"name": s["name"], "out": (out or "")[:400]}
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": s["id"],
                        "content": out,
                    })
                continue

            answer = "".join(content_parts).strip()
            _remember(user_text, answer)
            return
        # Limite atingido no modo streaming: preserva o contexto executado.
        _remember_overflow(user_text, stats)
        yield _overflow_message()


# --- STT -------------------------------------------------------------------
# Lazy singleton: the faster-whisper model (~464MB for "base", more for "small")
# is loaded ONCE on first use and reused across requests. Loading it inside the
# request handler was reloading the whole model on every call.
_WHISPER_MODEL = None
_WHISPER_LOCK = threading.Lock()


def _get_whisper_model():
    global _WHISPER_MODEL
    if _WHISPER_MODEL is None:
        with _WHISPER_LOCK:
            if _WHISPER_MODEL is None:
                from faster_whisper import WhisperModel

                t0 = time.perf_counter()
                _WHISPER_MODEL = WhisperModel(
                    WHISPER_MODEL, device="cpu", compute_type="int8"
                )
                log.info(
                    "Loaded faster-whisper model=%s in %.0fms",
                    WHISPER_MODEL, (time.perf_counter() - t0) * 1000,
                )
    return _WHISPER_MODEL


async def transcribe_audio(path: str, detail: dict | None = None) -> str:
    """Groq Whisper (if GROQ_API_KEY) -> faster-whisper local (singleton).

    Phone mics (especially MIUI) often capture very quiet audio. We peak-normalise
    the WAV first so a faint recording is not silently discarded by Whisper's
    internal silence detection.

    If `detail` is given it is filled in-place with telemetry about the STT step:
    which model/engine produced the transcript and whether a fallback was used.
    """
    if detail is None:
        detail = {}
    detail.setdefault("fallback_used", False)
    norm_path = _normalize_wav(path)
    if GROQ_API_KEY:
        detail["model"] = "groq/whisper-large-v3-turbo"
        try:
            async with httpx.AsyncClient() as client:
                with open(norm_path, "rb") as f:
                    resp = await client.post(
                        "https://api.groq.com/openai/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                        files={"file": (Path(norm_path).name, f), "model": (None, "whisper-large-v3-turbo")},
                        timeout=60,
                    )
                resp.raise_for_status()
                text = resp.json().get("text", "")
                if text.strip():
                    return text.strip()
        except Exception as e:  # noqa: BLE001
            log.warning("Groq transcription failed: %s", e)
        detail["fallback_used"] = True

    detail["model"] = f"faster-whisper/{WHISPER_MODEL}"
    try:
        def _whisper() -> str:
            model = _get_whisper_model()
            # No vad_filter: VAD drops quiet/short speech, which is exactly the
            # case we are trying to recover here.
            segments, _ = model.transcribe(
                norm_path,
                language=WHISPER_LANGUAGE,
                vad_filter=False,
                no_speech_threshold=0.9,
                condition_on_previous_text=False,
            )
            return " ".join(s.text.strip() for s in segments).strip()

        return await asyncio.to_thread(_whisper)
    except Exception as e:  # noqa: BLE001
        log.warning("Local whisper transcription failed: %s", e)
        detail["model"] = detail.get("model") or "faster-whisper"
        return ""


def _audio_stats_from_file(path: str, size_bytes: int | None = None) -> dict:
    """Acoustic stats for a 16-bit PCM WAV: duration, RMS, peak and dBFS.

    Used to record `audio_in` per turn so a bad/quiet recording is auditable
    after the fact. Best-effort: returns what it can on partial failures.
    """
    out: dict = {"bytes": size_bytes, "duration_s": None, "rms": None,
                 "peak": None, "dbfs": None}
    try:
        if size_bytes is None:
            out["bytes"] = os.path.getsize(path)
        import numpy as np

        with wave.open(path, "rb") as w:
            sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
            data = w.readframes(n)
        if sw != 2 or n == 0:
            return out
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        if x.size == 0:
            return out
        peak = float(np.max(np.abs(x)))
        rms = float(np.sqrt(np.mean(np.square(x))))
        out["duration_s"] = round(n / float(sr), 3) if sr else None
        out["peak"] = round(peak, 1)
        out["rms"] = round(rms, 2)
        if rms > 0:
            import math
            out["dbfs"] = round(20.0 * math.log10(rms / 32768.0), 2)
        out["sample_rate"] = sr
        out["channels"] = ch
    except Exception as e:  # noqa: BLE001
        log.warning("audio stats failed for %s: %s", path, e)
    return out


def _usage_block(stats: dict) -> dict:
    """Normalise the OpenRouter usage dict into the telemetry `llm` fields."""
    usage = stats.get("usage") if isinstance(stats, dict) else None
    block = {
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cost_usd": None,
        "reasoning_tokens": None,
    }
    if isinstance(usage, dict):
        block["prompt_tokens"] = usage.get("prompt_tokens")
        block["completion_tokens"] = usage.get("completion_tokens")
        block["total_tokens"] = usage.get("total_tokens")
        # OpenRouter returns the spend either in the top-level usage or nested.
        cost = usage.get("cost")
        if cost is None and isinstance(usage.get("cost_details"), dict):
            cost = usage["cost_details"].get("upstream_inference_cost")
        block["cost_usd"] = cost
        details = usage.get("completion_tokens_details") or {}
        block["reasoning_tokens"] = details.get("reasoning_tokens")
    return block


def _normalize_wav(path: str) -> str:
    """Peak-normalise a 16-bit WAV to ~0.9 full-scale. Returns the new path
    (the original path when normalisation is not needed/possible)."""
    try:
        import numpy as np

        with wave.open(path, "rb") as w:
            sr, ch, sw, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
            data = w.readframes(n)
        if sw != 2 or n == 0:
            return path
        x = np.frombuffer(data, dtype=np.int16).astype(np.float32)
        peak = float(np.max(np.abs(x)))
        if peak < 1.0:
            return path
        if peak >= 32000:  # already loud enough
            return path
        gain = min(32000.0 / peak, 12.0)  # cap the boost at ~+21 dB
        if gain <= 1.2:
            return path
        y = np.clip(x * gain, -32768, 32767).astype(np.int16)
        out = tempfile.mktemp(suffix=".wav")
        with wave.open(out, "wb") as w:
            w.setnchannels(ch)
            w.setsampwidth(2)
            w.setframerate(sr)
            w.writeframes(y.tobytes())
        log.info("Normalised %s (peak=%d, gain=%.2f)", Path(path).name, int(peak), gain)
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("normalise failed: %s", e)
        return path


# --- TTS -------------------------------------------------------------------
# Two engines, both returning MP3 (Android MediaPlayer plays MP3 natively):
#   * "kokoro" -> self-hosted hexgrad/kokoro-82m (82M params, runs on CPU,
#                 free; ~1.2s/sentence on the 6-core VPS, i.e. > real-time).
#                 Audio is WAV (24kHz mono) -> transcoded to MP3 via ffmpeg.
#   * "edge"   -> edge-tts (free cloud, used as automatic fallback)
# The client is unchanged either way.
_KPIPE = None  # lazy-loaded Kokoro pipeline (heavy import + model download)

# Dedicated single-worker executor for Kokoro.
#
# WHY: torch/OpenMP binds its thread pool to the *first* thread that runs the
# model. When we called Kokoro via `asyncio.to_thread`, it ran on a generic
# ThreadPoolExecutor worker, and OpenMP ended up using only ~2 of the 6 cores
# (measured cpu/wall ≈ 2.0 vs 5.9 on the main thread) — turning a 1.2s
# synthesis into ~4.3s. Running it on a dedicated executor whose worker thread
# is warmed up *inside itself* restores full parallelism (cpu/wall ≈ 5.2,
# ~1.8s). See the benchmark notes in the repo.
_KOKORO_EXECUTOR = None
_KOKORO_EXECUTOR_LOCK = threading.Lock()


def _get_kokoro_executor():
    global _KOKORO_EXECUTOR
    if _KOKORO_EXECUTOR is None:
        with _KOKORO_EXECUTOR_LOCK:
            if _KOKORO_EXECUTOR is None:
                from concurrent.futures import ThreadPoolExecutor

                ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="kokoro")
                # Warm the pipeline *inside* the worker thread so OpenMP's pool
                # is initialised there (this is the whole point of the fix).
                try:
                    ex.submit(_tts_kokoro_sync, "ok", TTS_KOKORO_VOICE).result(timeout=120)
                except Exception as e:  # noqa: BLE001
                    log.warning("Kokoro executor warm-up failed: %s", e)
                _KOKORO_EXECUTOR = ex
    return _KOKORO_EXECUTOR


async def _tts_kokoro(text: str, voice_id: str) -> bytes:
    """Run Kokoro on the dedicated executor (keeps OpenMP parallelism intact)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_get_kokoro_executor(), _tts_kokoro_sync, text, voice_id)


def _get_kokoro():
    global _KPIPE
    if _KPIPE is None:
        import numpy as np  # noqa: F401
        from kokoro import KPipeline

        _KPIPE = KPipeline(lang_code=TTS_KOKORO_LANG)
    return _KPIPE


def _tts_kokoro_sync(text: str, voice_id: str) -> bytes:
    """Synthesize with self-hosted Kokoro. Returns MP3 bytes (empty on failure)."""
    try:
        import numpy as np
        import soundfile as sf
        import io

        pipe = _get_kokoro()
        chunks = []
        for _, _, audio in pipe(text, voice=voice_id):
            chunks.append(audio.numpy() if hasattr(audio, "numpy") else audio)
        if not chunks:
            return b""
        samples = np.concatenate(chunks)
        wav = io.BytesIO()
        sf.write(wav, samples, 24000, format="WAV")
        wav.seek(0)
        # WAV -> MP3 via ffmpeg (same tool already used for STT)
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
             "-f", "mp3", "-b:a", "64k", "pipe:1"],
            input=wav.read(), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if proc.returncode != 0:
            log.warning("ffmpeg WAV->MP3 failed: %s", proc.stderr[:200])
            return b""
        return proc.stdout
    except Exception as e:  # noqa: BLE001
        log.warning("Kokoro local TTS failed: %s", e)
        return b""


async def _tts_edge(text: str, voice_id: str) -> bytes:
    """Synthesize with edge-tts. Returns MP3 bytes (empty on failure)."""
    try:
        import edge_tts

        buf = bytearray()
        async for chunk in edge_tts.Communicate(text, voice_id).stream():
            if chunk["type"] == "audio":
                buf += chunk["data"]
        return bytes(buf)
    except Exception as e:  # noqa: BLE001
        log.warning("edge-tts failed: %s", e)
        return b""


async def tts_to_mp3_bytes(text: str, voice: str | None = None) -> bytes:
    """Synthesize `text` to MP3 for the requested `voice` (friendly name or raw
    id). Falls back to the other engine, then to the configured default voice."""
    engine, voice_id = resolve_voice(voice)
    if engine == "kokoro":
        data = await _tts_kokoro(text, voice_id)
        if data:
            return data
        log.info("Kokoro returned no audio; falling back to edge-tts")
        return await _tts_edge(text, TTS_VOICE)
    data = await _tts_edge(text, voice_id)
    if data:
        return data
    # Last resort: default Kokoro voice.
    log.info("edge-tts returned no audio; falling back to Kokoro default")
    return await _tts_kokoro(text, TTS_KOKORO_VOICE)


async def tts_to_mp3_base64(text: str, voice: str | None = None) -> str:
    data = await tts_to_mp3_bytes(text, voice)
    return base64.b64encode(data).decode() if data else ""


# --- sentence splitting (for streaming TTS) --------------------------------
_SENT_RE = re.compile(r"[^.!?…]+[.!?…]+|\S[^.!?…]*$")

# The very first spoken chunk is what the user actually waits for, so we slice
# it finer than the rest. Kokoro has a ~0.9s fixed cost per synthesis call plus
# ~29ms/char (measured: 15ch=0.9s, 26ch=1.0s, 48ch=1.6s, 87ch=2.7s), so a short
# opener (~25-30 chars) makes the first audio land in ~1s instead of ~2.2s.
# Only the leading sentence is sliced; the rest keeps natural boundaries so the
# prosody stays intact and we don't pay the fixed cost on every fragment.
FIRST_CHUNK_MAX = int(os.environ.get("FIRST_CHUNK_MAX", "40"))


def split_first_chunk(text: str, max_len: int = FIRST_CHUNK_MAX) -> list[str]:
    """Split the first sentence into small speakable pieces (commas/spaces).

    Only used for the leading sentence of a streamed answer, where shaving the
    time-to-first-audio matters most. Never breaks a word.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    out: list[str] = []
    parts = re.split(r"(?<=,)\s+", text)
    buf = ""
    for p in parts:
        if len(buf) + len(p) + 1 <= max_len:
            buf = f"{buf} {p}".strip()
        else:
            if buf:
                out.append(buf)
            while len(p) > max_len:
                cut = p.rfind(" ", 0, max_len)
                if cut <= 0:
                    cut = max_len
                out.append(p[:cut].strip())
                p = p[cut:].strip()
            buf = p
    if buf:
        out.append(buf)
    # Merge a tiny leading fragment (e.g. "Olá,") into the next piece so the
    # first chunk is never a 3-4 char blip that sounds chopped. A slightly
    # longer first chunk is still far cheaper than a second round-trip.
    if len(out) >= 2 and len(out[0]) < 12 and len(out[0]) + len(out[1]) + 1 <= max_len + 30:
        out = [f"{out[0]} {out[1]}"] + out[2:]
    return [s for s in out if s]


def split_sentences(text: str, max_len: int = 120) -> list[str]:
    """Split `text` into speakable sentences. Keeps punctuation, drops empties,
    and further splits any sentence longer than `max_len` on commas/spaces so
    the first audio chunk arrives as early as possible."""
    text = (text or "").strip()
    if not text:
        return []
    raw = [m.group(0).strip() for m in _SENT_RE.finditer(text)]
    raw = [s for s in raw if s]
    if not raw:
        raw = [text]

    out: list[str] = []
    for s in raw:
        if len(s) <= max_len:
            out.append(s)
            continue
        # split long sentence on commas, then on spaces as a last resort
        parts = re.split(r"(?<=,)\s+", s)
        buf = ""
        for p in parts:
            if len(buf) + len(p) + 1 <= max_len:
                buf = f"{buf} {p}".strip()
            else:
                if buf:
                    out.append(buf)
                while len(p) > max_len:
                    cut = p.rfind(" ", 0, max_len)
                    if cut <= 0:
                        cut = max_len
                    out.append(p[:cut].strip())
                    p = p[cut:].strip()
                buf = p
        if buf:
            out.append(buf)
    return [s for s in out if s]


# --- incremental sentence detector (for LLM streaming) ---------------------
# Minimum characters before a sentence boundary is accepted, so we don't fire
# TTS on abbreviations / stray dots early in the stream.
MIN_SENT_CHARS = int(os.environ.get("MIN_SENT_CHARS", "15"))
_BOUNDARY_RE = re.compile(r"[.!?…]+(?=\s|$)|\n\n")


class SentenceBuffer:
    """Accumulates streamed LLM tokens and emits complete sentences as soon as
    a boundary (. ! ? … or blank line) is seen and the pending text is long
    enough. `flush()` returns whatever remains at end-of-stream."""

    def __init__(self, min_chars: int = MIN_SENT_CHARS):
        self.buf = ""
        self.min_chars = min_chars

    def feed(self, piece: str) -> list[str]:
        self.buf += piece
        out: list[str] = []
        while True:
            # Find the first boundary whose accumulated text is long enough to
            # be a real sentence (skip short fragments like "Claro!").
            chosen = None
            for m in _BOUNDARY_RE.finditer(self.buf):
                candidate = self.buf[:m.end()].strip()
                if len(candidate) >= self.min_chars:
                    chosen = m
                    break
            if chosen is None:
                break
            out.append(self.buf[:chosen.end()].strip())
            self.buf = self.buf[chosen.end():].lstrip()
        return out

    def flush(self) -> list[str]:
        rest = self.buf.strip()
        self.buf = ""
        return [rest] if rest else []


# --- HTTP ------------------------------------------------------------------
@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/voices")
async def voices():
    """List the selectable voices (friendly key + engine) for the app."""
    return {
        "default": DEFAULT_VOICE_KEY,
        "voices": [
            {"key": k, "engine": eng, "id": vid}
            for k, (eng, vid) in VOICE_ALIASES.items()
        ],
    }


@app.get("/audio")
async def audio(request: Request, path: str = ""):
    """Serve an audio file from the workspace (Bearer auth).

    `path` may be absolute inside the workspace or relative to it. Path traversal
    is blocked by `_resolve_audio_path`. Content-type is chosen by extension.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        p = _resolve_audio_path(path)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    if not p.exists() or not p.is_file():
        return JSONResponse({"error": "not found"}, status_code=404)
    ext = p.suffix.lower()
    media = {
        ".ogg": "audio/ogg",
        ".opus": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".wav": "audio/wav",
        ".m4a": "audio/mp4",
        ".aac": "audio/aac",
    }.get(ext, "application/octet-stream")
    try:
        data = p.read_bytes()
    except OSError as e:
        return JSONResponse({"error": f"read failed: {e}"}, status_code=500)
    return Response(content=data, media_type=media)


@app.post("/tts")
async def tts(request: Request):
    """Sintetiza texto -> áudio SEM passar pelo LLM.

    Usado pelo leitor de livros (e por qualquer cliente que queira só a voz).
    O texto vai direto para o motor de TTS (Kokoro/edge), sem gastar tokens.

    Body JSON: {"text": "...", "voice": "santa", "format": "mp3"|"ogg"}
    Retorna o áudio cru (audio/mpeg ou audio/ogg) no corpo da resposta.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "invalid json"}, status_code=400)
    text = (body.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "empty text"}, status_code=400)
    voice = body.get("voice")
    fmt = (body.get("format") or "mp3").lower()

    t0 = time.perf_counter()
    mp3 = await tts_to_mp3_bytes(text, voice)
    if not mp3:
        return JSONResponse({"error": "tts failed"}, status_code=500)

    if fmt == "ogg":
        data = await _mp3_to_ogg(mp3)
        if not data:
            return JSONResponse({"error": "ogg transcode failed"}, status_code=500)
        media = "audio/ogg"
    else:
        data = mp3
        media = "audio/mpeg"

    log.info("tts %d chars -> %d bytes (%s) in %.0fms",
             len(text), len(data), fmt, (time.perf_counter() - t0) * 1000)
    return Response(content=data, media_type=media)


async def _mp3_to_ogg(mp3: bytes) -> bytes:
    """MP3 -> OGG/Opus (WhatsApp voice-note format) via ffmpeg."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", "pipe:0",
        "-c:a", "libopus", "-b:a", "32k", "-ar", "48000", "-ac", "1",
        "-f", "ogg", "pipe:1",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, err = await proc.communicate(mp3)
    if proc.returncode != 0:
        log.warning("ffmpeg MP3->OGG failed: %s", err[:200])
        return b""
    return out


@app.on_event("startup")
async def _warmup():
    """Warm the Kokoro pipeline + edge-tts TLS so the first real request isn't
    slowed by a cold start. Best-effort; failures are ignored."""
    async def _w():
        try:
            t0 = time.perf_counter()
            await tts_to_mp3_bytes("ok", DEFAULT_VOICE_KEY)
            log.info("TTS warm-up done in %.0fms", (time.perf_counter() - t0) * 1000)
        except Exception as e:  # noqa: BLE001
            log.info("TTS warm-up skipped: %s", e)

    asyncio.create_task(_w())


def _authed(request: Request) -> bool:
    auth = request.headers.get("authorization", "")
    return auth == f"Bearer {VOICE_TOKEN}"


def _sniff_audio_format(path: str, declared: str | None = None) -> str:
    """Best-effort container detection for an uploaded utterance.

    Returns "wav", "opus", "aac" or "unknown". The phone sends the format it
    encoded (Opus/AAC) but we sniff the magic bytes too so an old client (raw
    WAV) keeps working and a mislabelled upload is still handled.
    """
    d = (declared or "").strip().lower()
    if d in ("opus", "ogg"):
        return "opus"
    if d in ("aac", "m4a", "mp4"):
        return "aac"
    if d == "wav":
        return "wav"
    try:
        with open(path, "rb") as f:
            head = f.read(16)
    except OSError:
        return "unknown"
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE":
        return "wav"
    if head[:4] == b"OggS":
        return "opus"
    if head[:4] == b"ftyp" or head[4:8] == b"ftyp":
        return "aac"
    # ADTS AAC starts with 0xFFF (sync word).
    if len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xF0) == 0xF0:
        return "aac"
    return "unknown"


def _decode_to_wav(path: str, fmt: str) -> str | None:
    """Transcode a compressed utterance (Opus/AAC) to 16 kHz mono PCM WAV.

    Returns the path of a temp WAV (caller removes it) or None on failure. WAV
    input is returned unchanged. ffmpeg is already present in the image (used
    for STT), so this adds no dependency.
    """
    if fmt == "wav":
        return path
    out = tempfile.mktemp(suffix=".wav")
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-i", path, "-ar", "16000", "-ac", "1", "-f", "wav", out],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
        )
        if proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 44:
            log.warning("ffmpeg decode (%s) falhou: %s", fmt, proc.stderr[:200])
            try:
                os.remove(out)
            except OSError:
                pass
            return None
        return out
    except Exception as e:  # noqa: BLE001
        log.warning("decode %s -> wav falhou: %s", fmt, e)
        try:
            os.remove(out)
        except OSError:
            pass
        return None


async def _parse_input(request: Request) -> tuple[str, float, str | None, str | None, dict]:
    """Extract the user text from either JSON {"text": ...} or multipart audio.
    Returns (text, stt_ms, voice, llm, meta).

    `meta` carries telemetry for the input side:
      meta["audio_in"] -> {bytes, duration_s, rms, peak, dbfs, ...} or None
      meta["stt"]      -> {model, fallback_used}
    """
    ctype = request.headers.get("content-type", "")
    text = ""
    stt_ms = 0.0
    voice: str | None = None
    llm: str | None = None
    meta: dict = {"audio_in": None, "stt": {"model": None, "fallback_used": False}}
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = (body.get("text") or "").strip()
        voice = (body.get("voice") or "").strip() or None
        llm = (body.get("llm") or body.get("model") or "").strip() or None
        meta["stt"]["model"] = "text-input"
    else:
        form = await request.form()
        voice = (form.get("voice") or "").strip() or None
        llm = (form.get("llm") or form.get("model") or "").strip() or None
        up = form.get("audio")
        if up is not None:
            suffix = Path(getattr(up, "filename", "") or "a.wav").suffix or ".wav"
            tmp = tempfile.mktemp(suffix=suffix)
            with open(tmp, "wb") as f:
                f.write(await up.read())
            decoded_tmp: str | None = None
            try:
                declared = (form.get("audio_format") or "").strip() or None
                fmt = _sniff_audio_format(tmp, declared)
                meta["audio_in"] = {"format": fmt}
                # Compressed uploads (Opus/AAC) are transcoded to 16 kHz mono WAV
                # before STT; raw WAV passes straight through.
                stt_path = tmp
                if fmt in ("opus", "aac"):
                    decoded_tmp = _decode_to_wav(tmp, fmt)
                    if decoded_tmp:
                        stt_path = decoded_tmp
                    else:
                        log.warning("não decodifiquei %s; tentando STT direto", fmt)
                meta["audio_in"] = {**_audio_stats_from_file(stt_path), "format": fmt}
                t_stt = time.perf_counter()
                text = (await transcribe_audio(stt_path, meta["stt"])).strip()
                stt_ms = (time.perf_counter() - t_stt) * 1000
            finally:
                for p in (tmp, decoded_tmp):
                    if p:
                        try:
                            os.remove(p)
                        except OSError:
                            pass
    return text, stt_ms, voice, llm, meta


@app.post("/ask")
async def ask(request: Request):
    t_start = time.perf_counter()
    t_start_epoch_ms = int(time.time() * 1000)
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    turn_id = uuid.uuid4().hex[:12]
    client_id = (request.headers.get("x-client-id") or "").strip() or None

    body = await _parse_input(request)
    text, stt_ms, voice, llm, meta = body
    engine, voice_id = resolve_voice(voice)

    def _record(answer_text: str, llm_block: dict | None = None,
                tts_block: dict | None = None, first_audio_ms=None,
                stt_failed: bool = False) -> None:
        telemetry.record_turn_safe({
            "turn_id": turn_id,
            "endpoint": "/ask",
            "client_id": client_id,
            "model_llm": llm or MODEL,
            "tts_engine": engine,
            "tts_voice": voice_id,
            "stt_failed": stt_failed,
            "audio_in": meta.get("audio_in"),
            "stt": {**(meta.get("stt") or {}), "text": text, "ok": bool(text),
                    "ms": round(stt_ms, 1)},
            "llm": llm_block,
            "tts": tts_block,
            "answer": answer_text,
            "timings": {
                "first_audio_ms": round(first_audio_ms, 1) if first_audio_ms else None,
                "total_ms": round((time.perf_counter() - t_start) * 1000, 1),
                "request_received_epoch_ms": t_start_epoch_ms,
            },
        })

    if not text:
        # Nothing intelligible was captured. Answer directly instead of feeding a
        # placeholder into the LLM (which used to produce "manda por texto").
        msg = "Não consegui te ouvir direito. Pode repetir, por favor?"
        audio_b64 = await tts_to_mp3_base64(msg, voice) if voice else ""
        _record(msg, tts_block={"tts_total_ms": None, "sentences": [
            {"index": 0, "text": msg, "ms": None}]}, stt_failed=True)
        return {
            "text": msg,
            "audio_base64": audio_b64,
            "timings": {"stt_ms": round(stt_ms, 1)},
            "stt_failed": True,
            "turn_id": turn_id,
        }

    log.info("ask turn=%s text=%r voice=%r llm=%r", turn_id, text[:200], voice, llm)

    t_llm = time.perf_counter()
    llm_stats: dict = {}
    answer = await run_agent(text, llm, llm_stats)
    llm_ms = (time.perf_counter() - t_llm) * 1000

    t_tts = time.perf_counter()
    audio_b64 = await tts_to_mp3_base64(answer, voice) if answer else ""
    tts_ms = (time.perf_counter() - t_tts) * 1000

    total_ms = (time.perf_counter() - t_start) * 1000
    timing = {
        "stt_ms": round(stt_ms, 1),
        "llm_ms": round(llm_ms, 1),
        "tts_ms": round(tts_ms, 1),
        "total_ms": round(total_ms, 1),
    }
    log.info(
        "TIMING turn=%s stt=%.0fms llm=%.0fms tts=%.0fms total=%.0fms",
        turn_id, stt_ms, llm_ms, tts_ms, total_ms,
    )

    sents = split_sentences(answer) if answer else []
    tts_block = {
        "tts_total_ms": round(tts_ms, 1),
        "sentences": [
            {"index": i, "text": s, "ms": None,
             "audio_bytes": None, "fmt": "mp3"}
            for i, s in enumerate(sents)
        ],
    }
    llm_block = {
        **_usage_block(llm_stats),
        "first_token_ms": None,  # not applicable: /ask is non-streaming
        "total_ms": round(llm_ms, 1),
        "tool_calls": llm_stats.get("tool_calls") or [],
        "tool_rounds": llm_stats.get("tool_rounds", 0),
    }
    _record(answer, llm_block=llm_block, tts_block=tts_block)

    return {"text": answer, "audio_base64": audio_b64, "timings": timing,
            "turn_id": turn_id,
            "play_audio": llm_stats.get("play_audio") or []}


@app.post("/ask_stream")
async def ask_stream(request: Request):
    """Streaming variant of /ask.

    Same auth + input as /ask, but responds with NDJSON
    (application/x-ndjson), one JSON object per line:
      {"type":"text","text":"<full answer>"}
      {"type":"audio","data":"<base64 mp3 of one sentence>","index":i,"text":"..."}
      {"type":"done","timings":{...},"turn_id":"..."}
      {"type":"error","message":"..."}

    The LLM is streamed (SSE). A producer task feeds streamed tokens into a
    SentenceBuffer; each completed sentence is pushed onto an asyncio.Queue. A
    consumer task synthesizes TTS for each queued sentence and yields the audio
    chunk. Because producer and consumer run concurrently, the LLM keeps
    generating sentence N+1 while TTS speaks sentence N.
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    # IMPORTANT: read the request body HERE, before returning the
    # StreamingResponse. Reading it inside the generator deadlocks: once the
    # response has started, Starlette no longer drains the request body, so
    # `await request.json()` / `request.form()` blocks forever (the client sees
    # a 200 with zero bytes and eventually times out). /ask worked only because
    # it parses the body before responding.
    turn_id = uuid.uuid4().hex[:12]
    client_id = (request.headers.get("x-client-id") or "").strip() or None

    # Wall-clock anchor captured the instant the request reaches us — BEFORE the
    # (blocking) STT parse. This is what lets us measure the real network leg
    # against the client's request_sent_ms. Capturing it inside gen() would
    # wrongly include the STT time in the "network" figure.
    t_start_epoch_ms = int(time.time() * 1000)

    body = await _parse_input(request)
    text, stt_ms, voice, llm, meta = body
    engine, voice_id = resolve_voice(voice)

    async def gen():
        t_start = time.perf_counter()
        try:
            if not text:
                # Nothing intelligible captured — emit a short spoken apology
                # instead of pushing a placeholder through the LLM.
                msg = "Não consegui te ouvir direito. Pode repetir, por favor?"
                audio = await tts_to_mp3_bytes(msg, voice) if voice else b""
                first_audio_ms = None
                if audio:
                    first_audio_ms = (time.perf_counter() - t_start) * 1000
                    yield json.dumps({
                        "type": "audio",
                        "data": base64.b64encode(audio).decode(),
                        "index": 0,
                        "text": msg,
                    }) + "\n"
                yield json.dumps({"type": "text", "text": msg}) + "\n"
                telemetry.record_turn_safe({
                    "turn_id": turn_id, "endpoint": "/ask_stream",
                    "client_id": client_id, "model_llm": llm or MODEL,
                    "tts_engine": engine, "tts_voice": voice_id,
                    "stt_failed": True, "audio_in": meta.get("audio_in"),
                    "stt": {**(meta.get("stt") or {}), "text": text,
                            "ok": False, "ms": round(stt_ms, 1)},
                    "llm": None, "tts": None, "answer": msg,
                    "timings": {"first_audio_ms": first_audio_ms,
                                "total_ms": round((time.perf_counter() - t_start) * 1000, 1)},
                })
                yield json.dumps({
                    "type": "done",
                    "timings": {"stt_ms": round(stt_ms, 1)},
                    "stt_failed": True,
                    "turn_id": turn_id,
                }) + "\n"
                return
            log.info("ask_stream turn=%s text=%r voice=%r llm=%r",
                     turn_id, text[:200], voice, llm)

            t_llm = time.perf_counter()
            first_token_ms = None
            first_audio_ms = None
            tts_ms = 0.0
            index = 0
            full_answer: list[str] = []
            llm_stats: dict = {}
            tts_sentences: list[dict] = []

            # Queue carries ("sent", sentence) or ("end", None).
            queue: asyncio.Queue = asyncio.Queue()

            async def producer():
                nonlocal first_token_ms
                buf = SentenceBuffer()
                # Fast path for the very first chunk: emit a short opener as soon
                # as ~FIRST_CHUNK_MAX chars are available (cut at a comma/space),
                # instead of waiting for the whole first sentence. This is what
                # actually moves time-to-first-audio: the LLM's first sentence is
                # often ~80 chars, and Kokoro costs ~0.9s + 29ms/char, so waiting
                # for it meant ~2.3s before any sound.
                head = ""
                first_emitted = False
                try:
                    async for piece in stream_agent(text, llm, llm_stats):
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t_llm) * 1000
                        full_answer.append(piece)
                        if not first_emitted:
                            head += piece
                            if len(head) >= FIRST_CHUNK_MAX:
                                cut = max(head.rfind(",", 0, FIRST_CHUNK_MAX),
                                          head.rfind(" ", 0, FIRST_CHUNK_MAX))
                                if cut <= 0:
                                    cut = FIRST_CHUNK_MAX
                                opener = head[:cut + 1].strip()
                                rest = head[cut + 1:].lstrip()
                                if opener:
                                    await queue.put(("sent", opener))
                                    first_emitted = True
                                    head = ""
                                    for sent in buf.feed(rest):
                                        await queue.put(("sent", sent))
                            continue
                        for sent in buf.feed(piece):
                            await queue.put(("sent", sent))
                    if not first_emitted and head.strip():
                        await queue.put(("sent", head.strip()))
                    for sent in buf.flush():
                        await queue.put(("sent", sent))
                except Exception as e:  # noqa: BLE001
                    log.exception("stream_agent failed")
                    await queue.put(("err", str(e)))
                finally:
                    await queue.put(("end", None))

            prod = asyncio.create_task(producer())

            first_sentence_done = False
            while True:
                kind, payload = await queue.get()
                if kind == "end":
                    break
                if kind == "err":
                    yield json.dumps({"type": "error", "message": payload}) + "\n"
                    break
                sentence = payload
                # The first sentence is sliced finer so the first audio chunk
                # arrives as early as possible; later sentences keep their
                # natural boundaries (better prosody, no audible choppiness).
                pieces = [sentence]
                if not first_sentence_done:
                    pieces = split_first_chunk(sentence)
                    first_sentence_done = True
                for piece in pieces:
                    t_tts = time.perf_counter()
                    data = await tts_to_mp3_bytes(piece, voice)
                    sent_ms = (time.perf_counter() - t_tts) * 1000
                    tts_ms += sent_ms
                    tts_sentences.append({
                        "index": index, "text": piece, "ms": round(sent_ms, 1),
                        "audio_bytes": len(data) if data else 0, "fmt": "mp3",
                    })
                    if not data:
                        continue
                    if first_audio_ms is None:
                        first_audio_ms = (time.perf_counter() - t_start) * 1000
                    yield json.dumps({
                        "type": "audio",
                        "data": base64.b64encode(data).decode(),
                        "index": index,
                        "text": piece,
                    }) + "\n"
                    index += 1

            await prod

            answer = "".join(full_answer).strip()
            yield json.dumps({"type": "text", "text": answer}) + "\n"

            # Audio files enqueued by tools (play_audio / ouvir_livro): emit one
            # `play` event per file so the client downloads and plays them in
            # order, then returns to the normal conversation flow.
            play_audio = llm_stats.get("play_audio") or []
            for i, p in enumerate(play_audio):
                yield json.dumps({"type": "play", "path": p, "index": i}) + "\n"

            llm_ms = (time.perf_counter() - t_llm) * 1000
            total_ms = (time.perf_counter() - t_start) * 1000
            timings = {
                "stt_ms": round(stt_ms, 1),
                "llm_ms": round(llm_ms, 1),
                "tts_ms": round(tts_ms, 1),
                "total_ms": round(total_ms, 1),
                "first_token_ms": round(first_token_ms, 1) if first_token_ms else None,
                "first_audio_ms": round(first_audio_ms, 1) if first_audio_ms else None,
                "sentences": index,
                "request_received_epoch_ms": t_start_epoch_ms,
            }
            log.info(
                "TIMING(stream) turn=%s stt=%.0fms llm=%.0fms tts=%.0fms total=%.0fms "
                "first_token=%.0fms first_audio=%.0fms sentences=%d",
                turn_id, stt_ms, llm_ms, tts_ms, total_ms,
                first_token_ms or -1, first_audio_ms or -1, index,
            )
            telemetry.record_turn_safe({
                "turn_id": turn_id,
                "endpoint": "/ask_stream",
                "client_id": client_id,
                "model_llm": llm or MODEL,
                "tts_engine": engine,
                "tts_voice": voice_id,
                "stt_failed": False,
                "audio_in": meta.get("audio_in"),
                "stt": {**(meta.get("stt") or {}), "text": text, "ok": True,
                        "ms": round(stt_ms, 1)},
                "llm": {
                    **_usage_block(llm_stats),
                    "first_token_ms": round(first_token_ms, 1) if first_token_ms else None,
                    "total_ms": round(llm_ms, 1),
                    "tool_calls": llm_stats.get("tool_calls") or [],
                    "tool_rounds": llm_stats.get("tool_rounds", 0),
                },
                "tts": {
                    "tts_total_ms": round(tts_ms, 1),
                    "sentences": tts_sentences,
                },
                "answer": answer,
                "timings": timings,
            })
            yield json.dumps({"type": "done", "timings": timings,
                              "turn_id": turn_id,
                              "play_audio": llm_stats.get("play_audio") or []}) + "\n"
        except Exception as e:  # noqa: BLE001
            log.exception("ask_stream failed")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --- telemetry endpoints ---------------------------------------------------
@app.get("/logs")
async def logs(request: Request, limit: int = 20, since: str | None = None,
               turn_id: str | None = None):
    """Recent turns (with their Android-side metrics merged) + an aggregate
    summary. Auth: Bearer token, same as /ask."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    turns = telemetry.load_turns(limit=limit, since=since, turn_id=turn_id)
    return {
        "summary": telemetry.summarize(turns),
        "phrases": telemetry.phrases(turns),
        "turns": turns,
    }


@app.get("/logs/summary")
async def logs_summary(request: Request, limit: int = 100,
                       since: str | None = None):
    """Only the aggregate metrics (avg/p50/p95/min/max) over the last N turns."""
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    turns = telemetry.load_turns(limit=limit, since=since)
    return {"summary": telemetry.summarize(turns),
            "phrases": telemetry.phrases(turns)}


@app.post("/report")
async def report(request: Request):
    """Client-side telemetry from the Android app (best-effort, fire-and-forget).

    Body: JSON with a `turn_id` (optional) plus any client metrics
    (record_ms, upload_ms, request_to_first_audio_ms, total_play_ms, chunks,
    device_model, android_sdk, app_version, network, wake_detect_ms...).
    """
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        body = {}
    if not isinstance(body, dict):
        body = {}
    turn_id = (body.pop("turn_id", None) or "").strip() or None
    telemetry.record_client_report_safe(turn_id, body)
    return {"status": "ok"}
