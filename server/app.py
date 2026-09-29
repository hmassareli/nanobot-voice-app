"""nanobot-voice-api — lightweight HTTP backend for a voice Android app.

Endpoints:
  GET  /health           -> {"status":"ok"}
  POST /ask              -> transcribe (optional) + run agent + TTS
                            Accepts multipart "audio" (WAV) OR JSON {"text": "..."}.
                            Auth: Authorization: Bearer <VOICE_TOKEN>
  POST /ask_stream       -> same input, NDJSON streaming (sentence-level audio)

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
import wave
from pathlib import Path

import re

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voice-api")

VOICE_TOKEN = os.environ.get("VOICE_TOKEN", "nanobot-voice")
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
OPENROUTER_URL = os.environ.get(
    "OPENROUTER_URL", "https://openrouter.ai/api/v1/chat/completions"
)
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4.1-flash")
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
MAX_TOOL_ROUNDS = int(os.environ.get("MAX_TOOL_ROUNDS", "6"))

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
        "list_dir e exec (shell). Use-as quando precisar consultar arquivos ou o "
        "ambiente. Caminhos são relativos ao workspace.",
    ]
    for fn in ("SOUL.md", "AGENTS.md", "USER.md", "TOOLS.md"):
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
]


def _safe(path: str) -> Path:
    p = (WORKSPACE / (path or ".")).resolve()
    if WORKSPACE.resolve() not in p.parents and p != WORKSPACE.resolve():
        raise ValueError("path outside workspace")
    return p


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


def _remember(user_text: str, answer: str) -> None:
    HISTORY.append({"role": "user", "content": user_text})
    HISTORY.append({"role": "assistant", "content": answer})
    del HISTORY[:-MAX_HISTORY]


async def run_agent(user_text: str, model: str | None = None) -> str:
    """Non-streaming agent (kept for /ask and as a fallback)."""
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
            }
            r = await client.post(OPENROUTER_URL, headers=_headers(), json=payload)
            r.raise_for_status()
            msg = r.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                answer = (msg.get("content") or "").strip()
                _remember(user_text, answer)
                return answer
            messages.append(msg)
            for tc in tool_calls:
                fn = tc["function"]["name"]
                try:
                    targs = json.loads(tc["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    targs = {}
                out = await dispatch_tool(fn, targs)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id"),
                    "content": out,
                })
        return "Desculpe, não consegui concluir a solicitação."


async def stream_agent(user_text: str, model: str | None = None):
    """Streaming agent: yields content deltas as they arrive from OpenRouter.

    Tool-calling is supported across rounds: if the model emits tool_calls in a
    round, the accumulated calls are executed and the loop continues (the next
    round's content is streamed). In the common no-tool case the very first
    tokens are yielded immediately, so TTS can start before the answer is done.
    """
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
                "stream": True,
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
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = choices[0].get("delta") or {}
                    piece = delta.get("content")
                    if piece:
                        content_parts.append(piece)
                        yield piece
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
                    messages.append({
                        "role": "tool",
                        "tool_call_id": s["id"],
                        "content": out,
                    })
                continue

            answer = "".join(content_parts).strip()
            _remember(user_text, answer)
            return
        yield "Desculpe, não consegui concluir a solicitação."


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


async def transcribe_audio(path: str) -> str:
    """Groq Whisper (if GROQ_API_KEY) -> faster-whisper local (singleton).

    Phone mics (especially MIUI) often capture very quiet audio. We peak-normalise
    the WAV first so a faint recording is not silently discarded by Whisper's
    internal silence detection.
    """
    norm_path = _normalize_wav(path)
    if GROQ_API_KEY:
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
        return ""


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
        data = await asyncio.to_thread(_tts_kokoro_sync, text, voice_id)
        if data:
            return data
        log.info("Kokoro returned no audio; falling back to edge-tts")
        return await _tts_edge(text, TTS_VOICE)
    data = await _tts_edge(text, voice_id)
    if data:
        return data
    # Last resort: default Kokoro voice.
    log.info("edge-tts returned no audio; falling back to Kokoro default")
    return await asyncio.to_thread(_tts_kokoro_sync, text, TTS_KOKORO_VOICE)


async def tts_to_mp3_base64(text: str, voice: str | None = None) -> str:
    data = await tts_to_mp3_bytes(text, voice)
    return base64.b64encode(data).decode() if data else ""


# --- sentence splitting (for streaming TTS) --------------------------------
_SENT_RE = re.compile(r"[^.!?…]+[.!?…]+|\S[^.!?…]*$")


def split_sentences(text: str, max_len: int = 220) -> list[str]:
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


async def _parse_input(request: Request) -> tuple[str, float, str | None, str | None]:
    """Extract the user text from either JSON {"text": ...} or multipart audio.
    Returns (text, stt_ms, voice, llm)."""
    ctype = request.headers.get("content-type", "")
    text = ""
    stt_ms = 0.0
    voice: str | None = None
    llm: str | None = None
    if ctype.startswith("application/json"):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            body = {}
        text = (body.get("text") or "").strip()
        voice = (body.get("voice") or "").strip() or None
        llm = (body.get("llm") or body.get("model") or "").strip() or None
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
            try:
                t_stt = time.perf_counter()
                text = (await transcribe_audio(tmp)).strip()
                stt_ms = (time.perf_counter() - t_stt) * 1000
            finally:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
    return text, stt_ms, voice, llm


@app.post("/ask")
async def ask(request: Request):
    t_start = time.perf_counter()
    if not _authed(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    text, stt_ms, voice, llm = await _parse_input(request)
    if not text:
        # Nothing intelligible was captured. Answer directly instead of feeding a
        # placeholder into the LLM (which used to produce "manda por texto").
        msg = "Não consegui te ouvir direito. Pode repetir, por favor?"
        audio_b64 = await tts_to_mp3_base64(msg, voice) if voice else ""
        return {
            "text": msg,
            "audio_base64": audio_b64,
            "timings": {"stt_ms": round(stt_ms, 1)},
            "stt_failed": True,
        }

    log.info("ask text=%r voice=%r llm=%r", text[:200], voice, llm)

    t_llm = time.perf_counter()
    answer = await run_agent(text, llm)
    llm_ms = (time.perf_counter() - t_llm) * 1000

    t_tts = time.perf_counter()
    audio_b64 = await tts_to_mp3_base64(answer, voice) if answer else ""
    tts_ms = (time.perf_counter() - t_tts) * 1000

    total_ms = (time.perf_counter() - t_start) * 1000
    timings = {
        "stt_ms": round(stt_ms, 1),
        "llm_ms": round(llm_ms, 1),
        "tts_ms": round(tts_ms, 1),
        "total_ms": round(total_ms, 1),
    }
    log.info(
        "TIMING stt=%.0fms llm=%.0fms tts=%.0fms total=%.0fms",
        stt_ms, llm_ms, tts_ms, total_ms,
    )
    return {"text": answer, "audio_base64": audio_b64, "timings": timings}


@app.post("/ask_stream")
async def ask_stream(request: Request):
    """Streaming variant of /ask.

    Same auth + input as /ask, but responds with NDJSON
    (application/x-ndjson), one JSON object per line:
      {"type":"text","text":"<full answer>"}
      {"type":"audio","data":"<base64 mp3 of one sentence>","index":i,"text":"..."}
      {"type":"done","timings":{...}}
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
    text, stt_ms, voice, llm = await _parse_input(request)

    async def gen():
        t_start = time.perf_counter()
        try:
            if not text:
                # Nothing intelligible captured — emit a short spoken apology
                # instead of pushing a placeholder through the LLM.
                msg = "Não consegui te ouvir direito. Pode repetir, por favor?"
                audio = await tts_to_mp3_base64(msg, voice) if voice else b""
                if audio:
                    yield json.dumps({
                        "type": "audio",
                        "data": base64.b64encode(audio).decode(),
                        "index": 0,
                        "text": msg,
                    }) + "\n"
                yield json.dumps({"type": "text", "text": msg}) + "\n"
                yield json.dumps({
                    "type": "done",
                    "timings": {"stt_ms": round(stt_ms, 1)},
                    "stt_failed": True,
                }) + "\n"
                return
            log.info("ask_stream text=%r voice=%r llm=%r", text[:200], voice, llm)

            t_llm = time.perf_counter()
            first_token_ms = None
            first_audio_ms = None
            tts_ms = 0.0
            index = 0
            full_answer: list[str] = []

            # Queue carries ("sent", sentence) or ("end", None).
            queue: asyncio.Queue = asyncio.Queue()

            async def producer():
                nonlocal first_token_ms
                buf = SentenceBuffer()
                try:
                    async for piece in stream_agent(text, llm):
                        if first_token_ms is None:
                            first_token_ms = (time.perf_counter() - t_llm) * 1000
                        full_answer.append(piece)
                        for sent in buf.feed(piece):
                            await queue.put(("sent", sent))
                    for sent in buf.flush():
                        await queue.put(("sent", sent))
                except Exception as e:  # noqa: BLE001
                    log.exception("stream_agent failed")
                    await queue.put(("err", str(e)))
                finally:
                    await queue.put(("end", None))

            prod = asyncio.create_task(producer())

            while True:
                kind, payload = await queue.get()
                if kind == "end":
                    break
                if kind == "err":
                    yield json.dumps({"type": "error", "message": payload}) + "\n"
                    break
                sentence = payload
                t_tts = time.perf_counter()
                data = await tts_to_mp3_bytes(sentence, voice)
                tts_ms += (time.perf_counter() - t_tts) * 1000
                if not data:
                    continue
                if first_audio_ms is None:
                    first_audio_ms = (time.perf_counter() - t_start) * 1000
                yield json.dumps({
                    "type": "audio",
                    "data": base64.b64encode(data).decode(),
                    "index": index,
                    "text": sentence,
                }) + "\n"
                index += 1

            await prod

            answer = "".join(full_answer).strip()
            yield json.dumps({"type": "text", "text": answer}) + "\n"

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
            }
            log.info(
                "TIMING(stream) stt=%.0fms llm=%.0fms tts=%.0fms total=%.0fms "
                "first_token=%.0fms first_audio=%.0fms sentences=%d",
                stt_ms, llm_ms, tts_ms, total_ms,
                first_token_ms or -1, first_audio_ms or -1, index,
            )
            yield json.dumps({"type": "done", "timings": timings}) + "\n"
        except Exception as e:  # noqa: BLE001
            log.exception("ask_stream failed")
            yield json.dumps({"type": "error", "message": str(e)}) + "\n"

    return StreamingResponse(
        gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
