#!/usr/bin/env python3
"""Build the Dockerfile (embedding app.py + workspace files as base64) and
deploy the nanobot-voice-api service to EasyPanel via the ep.py helper."""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
EP = "/root/.nanobot/workspace/skills/easypanel-deploy/scripts/ep.py"
sys.path.insert(0, os.path.dirname(EP))
import importlib.util

spec = importlib.util.spec_from_file_location("ep", EP)
ep = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ep)

PROJECT = "mapa-de-membros"
SERVICE = "nanobot-voice-api"
HOST = "nanobot-voice-api.lnyx9r.easypanel.host"
PORT = 8000


def load_secrets() -> dict:
    """Load secrets from server/secrets.env (git-ignored). Falls back to
    environment variables so CI/deploy boxes can inject them instead."""
    secrets: dict = {}
    path = os.path.join(HERE, "secrets.env")
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            secrets[k.strip()] = v.strip()
    for k in ("OPENROUTER_API_KEY", "GROQ_API_KEY", "VOICE_TOKEN"):
        if os.environ.get(k):
            secrets[k] = os.environ[k]
    return secrets


SECRETS = load_secrets()
OPENROUTER_KEY = SECRETS.get("OPENROUTER_API_KEY", "")
GROQ_KEY = SECRETS.get("GROQ_API_KEY", "")
VOICE_TOKEN = SECRETS.get("VOICE_TOKEN", "nanobot-voice")
if not OPENROUTER_KEY:
    print("WARNING: OPENROUTER_API_KEY missing (server/secrets.env)", file=sys.stderr)


def b64_chunks(data: bytes, size: int = 3000):
    s = base64.b64encode(data).decode()
    return [s[i:i + size] for i in range(0, len(s), size)]


def embed(path_in_image: str, data: bytes) -> str:
    lines = [f"RUN printf '%s' '{c}' > /tmp/e.b64" if i == 0
             else f"RUN printf '%s' '{c}' >> /tmp/e.b64"
             for i, c in enumerate(b64_chunks(data))]
    lines.append(f"RUN base64 -d /tmp/e.b64 > {path_in_image} && rm /tmp/e.b64")
    return "\n".join(lines)


def build_dockerfile() -> str:
    app = open(os.path.join(HERE, "app.py"), "rb").read()
    telemetry = open(os.path.join(HERE, "telemetry.py"), "rb").read()
    parts = [
        "FROM python:3.12-slim",
        "ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1",
        "RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg "
        "espeak-ng && rm -rf /var/lib/apt/lists/*",
        "RUN pip install fastapi 'uvicorn[standard]' python-multipart edge-tts "
        "faster-whisper httpx",
        # CPU-only torch keeps the image small (no CUDA wheels).
        "RUN pip install torch --index-url https://download.pytorch.org/whl/cpu",
        "RUN pip install kokoro soundfile misaki espeakng-loader phonemizer-fork",
        # Pre-download the Kokoro model + pt voice into the image so the first
        # real request doesn't pay the ~14s cold load.
        "RUN python -c \"from kokoro import KPipeline; "
        "p=KPipeline(lang_code='p'); list(p('ok', voice='pm_santa'))\"",
        "WORKDIR /app",
        "RUN mkdir -p /workspace",
        embed("/app/app.py", app),
        embed("/app/telemetry.py", telemetry),
    ]
    # NOTE: SOUL.md / USER.md / TOOLS.md are NOT baked in anymore — the real
    # nanobot workspace is bind-mounted at /workspace (EasyPanel mount), so the
    # voice assistant sees the same files (and edits) as the main assistant.
    parts += [
        f"ENV VOICE_TOKEN={VOICE_TOKEN}",
        f"ENV OPENROUTER_API_KEY={OPENROUTER_KEY}",
        "ENV MODEL=deepseek/deepseek-v4.1-flash",
        "ENV TTS_VOICE=pt-BR-AntonioNeural",
        "ENV TTS_ENGINE=kokoro",
        "ENV TTS_MODEL=hexgrad/kokoro-82m",
        "ENV TTS_KOKORO_VOICE=pm_santa",
        "ENV TTS_VOICE_KEY=santa",
        "ENV WORKSPACE=/workspace",
        "ENV WHISPER_MODEL=small",
        "ENV WHISPER_LANGUAGE=pt",
        f"ENV GROQ_API_KEY={GROQ_KEY}",
        "EXPOSE 8000",
        'CMD ["uvicorn","app:app","--host","0.0.0.0","--port","8000"]',
    ]
    return "\n".join(parts) + "\n"


def main():
    dockerfile = build_dockerfile()
    print(f"Dockerfile: {len(dockerfile)} chars, "
          f"max line {max(len(l) for l in dockerfile.splitlines())} chars")
    open(os.path.join(HERE, "Dockerfile"), "w").write(dockerfile)

    s, r = ep.call(f"/inspectAppService?projectName={PROJECT}&serviceName={SERVICE}")
    exists = s == 200 and isinstance(r, dict) and r.get("name") == SERVICE

    if exists:
        print("Service exists — updating Dockerfile source")
        s, r = ep.call("/updateAppSourceDockerfile", {
            "projectName": PROJECT, "serviceName": SERVICE, "dockerfile": dockerfile,
        })
        if s != 200:
            print(f"Update failed ({s}): {r}", file=sys.stderr)
            sys.exit(1)
    else:
        print("Creating service")
        payload = {
            "projectName": PROJECT,
            "serviceName": SERVICE,
            "source": {"type": "dockerfile", "dockerfile": dockerfile},
            "build": {"type": "dockerfile"},
            "deploy": {"replicas": 1, "zeroDowntime": True},
            "env": "",
            "mounts": [],
            "ports": [],
            "domains": [{
                "host": HOST, "https": True, "port": PORT, "path": "/",
                "internalProtocol": "http", "certificateResolver": "",
                "wildcard": False, "middlewares": [],
            }],
        }
        s, r = ep.call("/createAppService", payload)
        if s != 200:
            print(f"Create failed ({s}): {r}", file=sys.stderr)
            sys.exit(1)

    print("Deploying...")
    ep.call("/deployAppService", {"projectName": PROJECT, "serviceName": SERVICE})

    url = f"https://{HOST}/health"
    for i in range(60):
        time.sleep(10)
        code = ep.http_status(url, timeout=20)
        print(f"  [{i}] {url} -> {code}")
        if code == 200:
            print("\nONLINE ✅", url)
            return
    print("\nNot responding yet — check logs")


if __name__ == "__main__":
    main()
