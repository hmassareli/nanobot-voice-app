#!/usr/bin/env python3
"""Stitch the client and server telemetry into one exact per-turn timeline.

The Android app and the server keep separate clocks, so we anchor everything to
the *client's* end-of-speech instant (``speech_end_epoch_ms``) and use the
server's ``request_received_epoch_ms`` to measure the real network leg.

Usage:
    python3 timeline.py [--last N] [--turn TURN_ID] [--json]

Reads the same JSONL files the server writes (voice-turns.jsonl +
voice-clients.jsonl), merges by turn_id, and prints a human timeline:

    fim da fala ──► requisição enviada ──► servidor recebeu ──► 1º áudio
    recebido ──► 1º som no alto-falante
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

LOG_DIR = Path(os.environ.get(
    "TELEMETRY_DIR",
    "/root/.nanobot/workspace/projects/nanobot-voice-app/logs",
))


def _read(name: str) -> list[dict]:
    out: list[dict] = []
    for p in sorted(LOG_DIR.glob(f"{name}*.jsonl")):
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _merge() -> list[dict]:
    turns = {t.get("turn_id"): t for t in _read("voice-turns") if t.get("turn_id")}
    for c in _read("voice-clients"):
        tid = c.get("turn_id")
        if tid and tid in turns:
            turns[tid]["client"] = c.get("client")
        elif tid:
            turns[tid] = {"turn_id": tid, "client": c.get("client"),
                          "ts": c.get("ts"), "type": "client-only"}
    return sorted(turns.values(), key=lambda t: t.get("ts") or "")


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:.0f}"
    return str(v)


def render(t: dict) -> str:
    c = t.get("client") or {}
    srv = t.get("timings") or {}
    lines = []
    tid = t.get("turn_id")
    lines.append(f"── turn {tid}  ({t.get('ts', '')[:19]}) ──")
    lines.append(f"  voz: {t.get('tts_engine')}/{t.get('tts_voice')}   "
                 f"formato: {c.get('audio_format', '?')}   "
                 f"device: {c.get('device_model', '?')}")

    anchor = c.get("speech_end_epoch_ms")
    if anchor:
        lines.append("  [âncora = instante em que você parou de falar]")
        lines.append(f"    +{_fmt(c.get('request_sent_ms')):>6} ms  requisição enviada pelo app")
        recv = srv.get("request_received_epoch_ms")
        if recv:
            net = recv - anchor
            lines.append(f"    +{net:>6} ms  servidor recebeu (rede+upload = {net - (c.get('request_sent_ms') or 0):.0f} ms)")
        lines.append(f"    +{_fmt(c.get('first_audio_received_ms')):>6} ms  1º áudio CHEGOU no celular")
        lines.append(f"    +{_fmt(c.get('first_audio_played_ms')):>6} ms  1º SOM saiu no alto-falante  ◄── o que você sente")
    else:
        lines.append("  (sem âncora de fim de fala — APK antigo)")

    lines.append("  --- servidor (relativo ao recebimento) ---")
    lines.append(f"    stt={_fmt(srv.get('stt_ms'))}ms  "
                 f"llm_1º_token={_fmt(srv.get('first_token_ms'))}ms  "
                 f"llm_total={_fmt(srv.get('llm_ms'))}ms  "
                 f"tts_total={_fmt(srv.get('tts_ms'))}ms")
    lines.append(f"    1º áudio pronto={_fmt(srv.get('first_audio_ms'))}ms  "
                 f"total={_fmt(srv.get('total_ms'))}ms  "
                 f"frases={_fmt(srv.get('sentences'))}")
    lines.append(f"  --- cliente ---")
    lines.append(f"    gravação={_fmt(c.get('record_ms'))}ms  "
                 f"duração_áudio={_fmt(c.get('audio_duration_ms'))}ms  "
                 f"chunks={_fmt(c.get('chunks'))}  "
                 f"play_total={_fmt(c.get('total_play_ms'))}ms")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--last", type=int, default=5)
    ap.add_argument("--turn")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    turns = _merge()
    if args.turn:
        turns = [t for t in turns if t.get("turn_id") == args.turn]
    else:
        turns = turns[-args.last:]

    if args.json:
        print(json.dumps(turns, ensure_ascii=False, indent=2))
        return
    for t in turns:
        print(render(t))
        print()


if __name__ == "__main__":
    main()
