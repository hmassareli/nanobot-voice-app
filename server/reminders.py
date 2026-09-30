"""Lembretes agendados do app de voz.

Armazenamento simples em JSON dentro do workspace (sobrevive a redeploys).
Thread-safe: o FastAPI pode chamar a partir de qualquer request.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

WORKSPACE = Path(os.environ.get("WORKSPACE", "/workspace"))
PATH = WORKSPACE / "reminders.json"
_LOCK = threading.Lock()
# America/Sao_Paulo (sem horario de verao desde 2019).
_TZ_DEFAULT = timezone(timedelta(hours=-3))
VALID_DELIVERY = ("auto", "voice", "popup")


def _load() -> list:
    try:
        return json.loads(PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(items: list) -> None:
    PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, PATH)


def parse_fire_at(value) -> int:
    """Aceita epoch ms/s (int/str) ou ISO 8601 (com ou sem fuso). Devolve ms."""
    if value is None or value == "":
        raise ValueError("informe o horário do lembrete")
    if isinstance(value, bool):
        raise ValueError("horário inválido")
    if isinstance(value, (int, float)):
        v = int(value)
        return v if v > 10_000_000_000 else v * 1000
    s = str(value).strip()
    if s.replace(".", "", 1).isdigit():
        return parse_fire_at(float(s))
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError(
            "horário inválido: %r (use ISO 8601, ex.: 2026-09-30T18:00:00-03:00)" % s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_TZ_DEFAULT)
    return int(dt.timestamp() * 1000)


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) \
        .astimezone(_TZ_DEFAULT).isoformat(timespec="minutes")


def add_reminder(text, fire_at, delivery="auto") -> dict:
    text = (str(text) if text is not None else "").strip()
    if not text:
        raise ValueError("texto vazio")
    delivery = (str(delivery) if delivery else "auto").strip().lower()
    if delivery not in VALID_DELIVERY:
        delivery = "auto"
    ms = parse_fire_at(fire_at)
    item = {
        "id": uuid.uuid4().hex[:12],
        "texto": text,
        "fire_at_ms": ms,
        "fire_at_iso": _iso(ms),
        "delivery": delivery,
        "status": "pending",
        "created_at_ms": int(time.time() * 1000),
    }
    with _LOCK:
        items = _load()
        items.append(item)
        _save(items)
    return item


def list_pending(now_ms=None) -> list:
    now = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    with _LOCK:
        items = _load()
    pend = [i for i in items
            if i.get("status") == "pending"
            and int(i.get("fire_at_ms") or 0) > now - 120_000]
    pend.sort(key=lambda i: int(i.get("fire_at_ms") or 0))
    return pend


def _update(rid: str, fn) -> bool:
    with _LOCK:
        items = _load()
        changed = False
        for i in items:
            if i.get("id") == rid:
                fn(i)
                changed = True
                break
        if changed:
            _save(items)
        return changed


def cancel_reminder(rid: str) -> bool:
    return _update(str(rid or ""), lambda i: i.update(status="cancelled"))


def mark_fired(rid: str) -> bool:
    return _update(
        str(rid or ""),
        lambda i: i.update(status="fired", fired_at_ms=int(time.time() * 1000)))
