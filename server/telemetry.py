"""Structured per-turn telemetry for the nanobot voice backend.

Every voice turn (``/ask`` or ``/ask_stream``) is recorded as **one JSON line**
in ``<LOG_DIR>/voice-turns.jsonl`` (append-only, thread/async safe) so Henrique
can audit later "each thing that took long, the phrases said, and how much time
was spent in each stage".

Client-side metrics sent by the Android app (``POST /report``) are stored as
separate append-only JSON lines in ``<LOG_DIR>/voice-clients.jsonl`` and are
merged into the matching turn by ``turn_id`` when the logs are read. Keeping the
two streams in separate files means:
  * a late/duplicate ``/report`` never rewrites (or corrupts) an existing turn;
  * the app can retry / arrive out of order without breaking concurrency.

Storage layout (inside the bind-mounted workspace, so it survives redeploys):

    /workspace/projects/nanobot-voice-app/logs/
        voice-turns.jsonl                # one line per turn (server side)
        voice-turns.<data>.jsonl         # rotated rollovers (> ~5 MB)
        voice-clients.jsonl              # one line per app /report
        voice-clients.<data>.jsonl       # rotated rollovers

Files rotate automatically once they exceed ROTATE_BYTES. Everything is
best-effort: telemetry must never break the voice flow, so callers ignore
exceptions (see ``record_turn_safe``).
"""
from __future__ import annotations

import json
import logging
import os
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

log = logging.getLogger("voice-telemetry")

# --- configuration ---------------------------------------------------------
_DEFAULT_DIR = "/workspace/projects/nanobot-voice-app/logs"
# Fallback for local dev / tests: the real repository path.
_REPO_FALLBACK = "/root/.nanobot/workspace/projects/nanobot-voice-app/logs"


def _resolve_log_dir() -> Path:
    env = os.environ.get("TELEMETRY_DIR")
    if env:
        return Path(env)
    if Path("/workspace").exists():
        return Path(_DEFAULT_DIR)
    return Path(_REPO_FALLBACK)


LOG_DIR = _resolve_log_dir()
TURNS_FILE = "voice-turns.jsonl"
CLIENTS_FILE = "voice-clients.jsonl"

# ~5 MB rollover, matching the requirement.
ROTATE_BYTES = int(os.environ.get("TELEMETRY_ROTATE_BYTES", str(5 * 1024 * 1024)))

# One process-wide lock guards every append AND rotation, so concurrent turns
# (the server is async but file IO runs in a threadpool) cannot interleave a
# write with a rename.
_LOCK = threading.RLock()


def _ensure_dir() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


def _day_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# --- low level append + rotation ------------------------------------------
def _rotate_if_needed(path: Path) -> None:
    """Rename `path` to `path.<data>.jsonl` when it grows past ROTATE_BYTES.

    Caller must hold ``_LOCK``. A numeric suffix is appended if a rollover with
    the same date already exists (e.g. two rotations on one day).
    """
    try:
        if not path.exists() or path.stat().st_size < ROTATE_BYTES:
            return
        stamp = _day_stamp()
        stem = path.stem  # e.g. "voice-turns"
        rotated = path.with_name(f"{stem}.{stamp}.jsonl")
        n = 1
        while rotated.exists():
            rotated = path.with_name(f"{stem}.{stamp}.{n}.jsonl")
            n += 1
        path.rename(rotated)
        log.info("telemetry rotated %s -> %s", path.name, rotated.name)
    except Exception as e:  # noqa: BLE001
        log.warning("telemetry rotation failed for %s: %s", path, e)


def _append_line(filename: str, obj: dict) -> None:
    """Append one JSON object as a line to `filename` (creating the dir)."""
    _ensure_dir()
    path = LOG_DIR / filename
    with _LOCK:
        _rotate_if_needed(path)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(",", ":")) + "\n")
            f.flush()


# --- public write API ------------------------------------------------------
def record_turn(record: dict) -> None:
    """Persist one turn record. Caller supplies a fully-formed dict; we stamp
    the reception time if missing and log it to stdout too."""
    rec = dict(record)
    rec.setdefault("ts", _now_iso())
    rec.setdefault("type", "turn")
    _append_line(TURNS_FILE, rec)
    # Human-readable mirror on stdout (kept compact).
    try:
        log.info(
            "TURN %s endpoint=%s stt=%sms llm_first=%sms llm=%sms tts=%sms "
            "first_audio=%sms total=%sms stt_text=%r",
            rec.get("turn_id"), rec.get("endpoint"),
            _ms(rec, "stt", "ms"), _get(rec, "llm", "first_token_ms"),
            _ms(rec, "llm", "total_ms"), _get(rec, "tts", "tts_total_ms"),
            _get(rec, "timings", "first_audio_ms"), _get(rec, "timings", "total_ms"),
            (rec.get("stt") or {}).get("text", "")[:120],
        )
    except Exception:  # noqa: BLE001
        pass


def record_turn_safe(record: dict) -> None:
    """Best-effort wrapper: never raises into the request path."""
    try:
        record_turn(record)
    except Exception as e:  # noqa: BLE001
        log.warning("record_turn failed: %s", e)


def record_client_report(turn_id: str | None, metrics: dict) -> None:
    """Persist the Android-side metrics for a turn (or a brand-new record when
    turn_id is unknown). Best-effort."""
    rec = {
        "type": "client",
        "ts": _now_iso(),
        "turn_id": turn_id,
        "client": metrics,
    }
    _append_line(CLIENTS_FILE, rec)
    try:
        log.info("CLIENT_REPORT turn=%s wake=%sms record=%sms upload=%sms "
                 "req_first_audio=%sms play=%sms chunks=%s device=%s",
                 turn_id, metrics.get("wake_detect_ms"), metrics.get("record_ms"),
                 metrics.get("upload_ms"), metrics.get("request_to_first_audio_ms"),
                 metrics.get("total_play_ms"), metrics.get("chunks"),
                 metrics.get("device_model"))
    except Exception:  # noqa: BLE001
        pass


def record_client_report_safe(turn_id: str | None, metrics: dict) -> None:
    try:
        record_client_report(turn_id, metrics)
    except Exception as e:  # noqa: BLE001
        log.warning("record_client_report failed: %s", e)


# --- reading ---------------------------------------------------------------
def _iter_files(filename: str) -> list[Path]:
    """All files belonging to a logical log stream (main + rotated), oldest
    first. Ordering by mtime keeps rotated history chronological."""
    _ensure_dir()
    stem = Path(filename).stem  # voice-turns / voice-clients
    files = sorted(
        LOG_DIR.glob(f"{stem}*.jsonl"),
        key=lambda p: (p.stat().st_mtime if p.exists() else 0, p.name),
    )
    return files


def _read_jsonl(paths: Iterable[Path]) -> list[dict]:
    out: list[dict] = []
    for p in paths:
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except FileNotFoundError:
            continue
        except Exception as e:  # noqa: BLE001
            log.warning("failed reading %s: %s", p, e)
    return out


def load_turns(limit: int = 20, since: str | None = None,
               turn_id: str | None = None) -> list[dict]:
    """Return turns (newest last), each merged with its client report."""
    turns = [r for r in _read_jsonl(_iter_files(TURNS_FILE)) if r.get("type") != "client"]
    clients = _read_jsonl(_iter_files(CLIENTS_FILE))

    by_id: dict[str, dict] = {}
    for c in clients:
        tid = c.get("turn_id")
        if tid:
            by_id[tid] = c.get("client") or {}

    # Merge (and surface orphan client reports as synthetic turns so nothing is
    # silently lost when a /report arrives for an unknown turn).
    seen: set[str] = set()
    for t in turns:
        tid = t.get("turn_id")
        if tid:
            seen.add(tid)
            if tid in by_id and "client" not in t:
                t["client"] = by_id[tid]
    for tid, metrics in by_id.items():
        if tid not in seen:
            turns.append({
                "type": "turn", "turn_id": tid, "orphan": True,
                "ts": _now_iso(), "client": metrics,
            })

    if since:
        turns = [t for t in turns if (t.get("ts") or "") >= since]
    if turn_id:
        turns = [t for t in turns if t.get("turn_id") == turn_id]
    turns.sort(key=lambda t: t.get("ts") or "")
    if limit and limit > 0:
        turns = turns[-limit:]
    return turns


def _vals(turns: list[dict], path: tuple[str, ...]) -> list[float]:
    out: list[float] = []
    for t in turns:
        cur: Any = t
        for k in path:
            if not isinstance(cur, dict):
                cur = None
                break
            cur = cur.get(k)
        if isinstance(cur, (int, float)):
            out.append(float(cur))
    return out


def _stat(vals: list[float]) -> dict | None:
    if not vals:
        return None
    vals_sorted = sorted(vals)

    def pct(p: float) -> float:
        if len(vals_sorted) == 1:
            return vals_sorted[0]
        idx = (len(vals_sorted) - 1) * p
        lo = int(idx)
        hi = min(lo + 1, len(vals_sorted) - 1)
        frac = idx - lo
        return vals_sorted[lo] * (1 - frac) + vals_sorted[hi] * frac

    return {
        "n": len(vals),
        "avg": round(statistics.fmean(vals), 1),
        "p50": round(pct(0.50), 1),
        "p95": round(pct(0.95), 1),
        "min": round(min(vals), 1),
        "max": round(max(vals), 1),
    }


def summarize(turns: list[dict]) -> dict:
    """Aggregate metrics (avg/p50/p95/min/max) over `turns`."""
    real = [t for t in turns if not t.get("orphan")]
    return {
        "count": len(real),
        "count_with_client": sum(1 for t in real if t.get("client")),
        "stt_ms": _stat(_vals(real, ("stt", "ms"))),
        "llm_first_token_ms": _stat(_vals(real, ("llm", "first_token_ms"))),
        "llm_total_ms": _stat(_vals(real, ("llm", "total_ms"))),
        "tts_total_ms": _stat(_vals(real, ("tts", "tts_total_ms"))),
        "first_audio_ms": _stat(_vals(real, ("timings", "first_audio_ms"))),
        "total_ms": _stat(_vals(real, ("timings", "total_ms"))),
        "request_to_first_audio_ms": _stat(_vals(real, ("client", "request_to_first_audio_ms"))),
        "record_ms": _stat(_vals(real, ("client", "record_ms"))),
    }


def phrases(turns: list[dict]) -> list[dict]:
    """The most important part: what the user *said* and what was answered."""
    out = []
    for t in turns:
        out.append({
            "turn_id": t.get("turn_id"),
            "ts": t.get("ts"),
            "stt_text": (t.get("stt") or {}).get("text"),
            "answer": t.get("answer")
                      or " ".join(s.get("text", "") for s in (t.get("tts") or {}).get("sentences") or []),
            "endpoint": t.get("endpoint"),
        })
    return out


# --- helpers ---------------------------------------------------------------
def _get(rec: dict, *path: str) -> Any:
    cur: Any = rec
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def _ms(rec: dict, *path: str) -> Any:
    return _get(rec, *path)
