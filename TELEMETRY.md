# Telemetria — app de voz nanobot

Objetivo: auditar **cada turno de voz** — a frase falada, quanto tempo em cada
etapa (STT, LLM, TTS, rede, reprodução) e o custo. O servidor grava o lado dele;
o app manda o que só o celular sabe (gravação, 1º áudio, reprodução).

## Fluxo

```
[celular] grava áudio ──► POST /ask_stream (X-Client-Id) ──► [servidor]
   │                                                             │
   │  NDJSON: {"type":"text"} {"type":"audio"} {"type":"done","turn_id"} 
   │◄────────────────────────────────────────────────────────────┘
   │
   └─► POST /report  (métricas do cliente, fire-and-forget)
```

O servidor junta as duas fontes por `turn_id` e grava **1 linha JSONL por turno**.

## Servidor

- Arquivo: `/workspace/projects/nanobot-voice-app/logs/voice-turns.jsonl`
  (bind mount real → persiste fora do container). Rotação > 5 MB.
- Clientes: `logs/voice-clients.jsonl` (último seen por `X-Client-Id`).

### Campos do JSONL (por turno)

| campo | descrição |
|---|---|
| `turn_id` | id do turno (gerado no servidor) |
| `endpoint` | `/ask` ou `/ask_stream` |
| `client_id` | header `X-Client-Id` |
| `model_llm` | modelo usado |
| `tts_engine` / `tts_voice` | engine e voz (ex.: kokoro / pm_santa) |
| `audio_in` | `{bytes, duration_s, rms, peak, dbfs}` do áudio recebido |
| `stt` | `{text (a frase falada), ok, ms, model, fallback_used}` |
| `llm` | `{first_token_ms, total_ms, prompt_tokens, completion_tokens, total_tokens, cost_usd, reasoning_tokens, tool_calls, tool_rounds}` |
| `tts` | `{tts_total_ms, sentences:[{index,text,ms,audio_bytes,fmt}]}` |
| `timings` | `{first_audio_ms, total_ms}` |
| `answer` | texto final respondido |
| `client` | métricas do `/report` mescladas (ver abaixo) |

### Campos do `/report` (cliente)

`turn_id`, `record_ms`, `audio_duration_ms`, `request_to_first_audio_ms`,
`first_chunk_play_ms`, `total_play_ms`, `chunks`, `app_version`, `device_model`,
`android_sdk`, `network` (wifi/mobile/ethernet/none/unknown).

## Endpoints de auditoria

```bash
TOKEN=nanobot-voice
BASE=https://nanobot-voice-api.lnyx9r.easypanel.host

# últimos 20 turnos + resumo agregado (avg/p50/p95/min/max) + frases
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs?limit=20" | jq

# só o resumo
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs/summary" | jq

# um turno específico
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs?turn_id=abc123" | jq

# turnos desde um instante (ISO)
curl -s -H "Authorization: Bearer $TOKEN" "$BASE/logs?since=2026-09-29T00:00:00" | jq
```

## App Android (client-side)

- `Prefs.clientId(ctx)` — id estável (UUID persistido; fallback `ANDROID_ID`).
- `NanobotClient` envia `X-Client-Id` em `/ask` e `/ask_stream`; `askStream`
  devolve `StreamMetrics{turnId, firstAudioMs, chunks, audioBytes}` e chama
  `onTurnId` no evento `done`.
- `NanobotClient.report(...)` — `POST /report` em thread daemon, **nunca lança**.
- `VoiceService` mede `record_ms`, `audio_duration_ms`, `first_chunk_play_ms`,
  `total_play_ms` e dispara o `/report` ao fim do turno (try/catch total).

> Regra: telemetria é **best-effort**. Falha de rede/telemetria nunca quebra a voz.
