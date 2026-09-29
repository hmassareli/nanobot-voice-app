# Auditoria de Otimização e Latência — App de Voz Nanobot

**Data:** 2026-09-29
**Autor:** auditoria comandada pelo Henrique
**Escopo:** do áudio saindo do celular → primeira palavra tocando → fim do turno.

---

## 0. Baseline medido (produção, hoje)

| Pergunta | STT | first_token | first_audio | total | sentenças |
|---|---|---|---|---|---|
| "Quanto é dois mais dois?" | 0 (texto) | **628ms** | **3107ms** | 3108ms | 1 |
| "Qual a capital da França?" | 0 (texto) | **679ms** | **2816ms** | 2816ms | 1 |
| "Me conte uma curiosidade sobre gatos." | 0 (texto) | 534ms | 4602ms | 8307ms | 2 |

**Diagnóstico-chave:** o 1º token do LLM chega em ~0,6s, mas o 1º áudio só sai em ~2,8–3,1s.
Ou seja: **~2,2s são gastos NO TTS** (Kokoro + ffmpeg), não no LLM. O gargalo da "primeira
palavra" migrou do modelo para a síntese de voz.

Isso é confirmado pelos `timings` do próprio `/ask_stream`: numa resposta de 1 frase,
`tts_ms = 2470ms` (e o `llm_ms` de 3107 é só o timestamp do 1º áudio, não geração real).

---

## 1. Tabela de gargalos (ordenada por impacto)

| # | Gargalo | Evidência | Causa raiz | Correção | Ganho estimado | Esforço/Risco |
|---|---|---|---|---|---|---|
| 1 | **TTS Kokoro lento (CPU)** | `synth=1198–1410ms` + `ffmpeg=176–225ms` medido dentro do container | Kokoro-82m roda em torch/CPU no VPS; síntese de 1 frase > 1s | Trocar default para **edge-tts** (medido TTFB **477ms**, MP3 direto, sem ffmpeg) | **first_audio −1.5 a −2.2s** | Baixo (já é o fallback) |
| 2 | **TTS só entrega áudio após a frase INTEIRA** | `tts_to_mp3_bytes()` retorna o buffer completo; `/ask_stream` emite 1 chunk por frase (linha 916–927) | Não há streaming de áudio intra-frase | Streamar o áudio da frase em sub-chunks (edge-tts já entrega em pedaços; Piper idem) | **first_audio ≈ TTFB (~300–500ms)** | Médio |
| 3 | **Whisper não é pré-aquecido** | `_warmup` (linha 724) só aquece TTS; `_get_whisper_model()` só na 1ª transcrição | Lazy load do Whisper paga ~1–3s na 1ª pergunta falada | Aquecer `_get_whisper_model()` no startup | **−1 a −3s na 1ª fala** | Trivial |
| 4 | **`httpx.AsyncClient` por request** | `AsyncClient(...)` nas linhas 245, 297, 345, 465 | Sem pool/keep-alive → TCP+TLS+DNS a cada chamada | Cliente global com `limits`/`keepalive` | **−50 a −150ms/req** | Trivial |
| 5 | **Kokoro cold start** | `import kokoro=11.2s`, `pipeline load=4.0s` | torch + download do modelo no 1º uso | Já mitigado pelo warm-up de TTS, mas o import é pesado no boot | (evita travar 1ª req) | Baixo |
| 6 | **Modelo LLM default** | deepseek TTFT **553–942ms**; llama-3.1-8b **218–532ms**; gemini-2.5-flash **470–516ms** | deepseek-v4.1-flash tem TTFT maior | Avaliar `google/gemini-2.5-flash` como default | **first_token −100 a −300ms** | Médio (qualidade/tools) |
| 7 | **TTS sequencial entre frases** | loop consumer serial (linha 907–930) | Synthesiza frase N+1 só após enviar N | Paralelizar 2–3 sínteses (6 cores / edge é I/O cloud) | **−300 a −800ms** em respostas multi-frase | Médio |
| 8 | **`max_tokens` não limitado** | sem cap no payload (linha 331/297) | Modelo pode gerar texto longo → mais frases → resposta arrastada | Cap ~80–120 tokens p/ voz | Respostas mais curtas e ágeis | Trivial |
| 9 | **base64 nos chunks de áudio** | linhas 864, 924 (encode); app decode | +33% de bytes e CPU de encode/decode | Framing binário (length-prefix) | −10 a −40ms + menos CPU móvel | Médio |
| 10 | **MediaPlayer novo + `prepare()` por chunk** | `VoiceService.playAudio` (linha 407) | Cria/prepare bloqueante a cada frase → gap entre frases | ExoPlayer com playlist gapless | Silêncios entre frases menores | Médio |
| 11 | **uvicorn sem tuning** | CMD sem `--workers`/`--loop` | single process; TTS/STT em threads competem por CPU | `--loop uvloop`, revisar workers | Margem sob concorrência | Baixo |

---

## 2. Quick wins (ganho grande, risco baixo)

1. **Trocar TTS default para edge-tts** → first_audio de ~2,8s para ~0,9–1,2s. *Maior ganho isolado.*
2. **Pré-aquecer o Whisper no startup** → elimina o freeze de 1–3s na primeira pergunta falada.
3. **Cliente httpx global (keep-alive)** → −50 a −150ms por request.
4. **Cap de `max_tokens` (~100)** → respostas faladas mais curtas, percepção de agilidade.
5. **Testar `google/gemini-2.5-flash` como modelo default** → −100 a −300ms no 1º token, com boa qualidade de ferramentas.

**Ganho combinado estimado dos quick wins: first_audio cai de ~2,8s para ~0,8–1,0s.**

---

## 3. Mudanças estruturais (maior impacto, maior esforço)

### 3.1 Streaming de TTS intra-frase (o "santo graal")
Hoje o app só toca depois de a frase inteira ser sintetizada. Se o servidor enviar
o áudio em sub-chunks (primeiros ~200ms de fala assim que saem do sintetizador),
o **first_audio passa a ser o TTFB (~300–500ms)** — quase instantâneo.
- edge-tts já entrega áudio em pedaços (stream nativo).
- Piper também faz streaming e é leve em CPU (alternativa self-hosted ao Kokoro).
- Requer mudar o protocolo NDJSON para permitir múltiplos chunks por frase.

### 3.2 Pipeline paralelo de TTS
Sintetizar a frase N+1 **enquanto** a N é enviada/tocada. Com 6 cores e TTS cloud
(edge), dá para 2–3 sínteses em paralelo. Ganho grande em respostas longas.

### 3.3 Pular o ffmpeg
O Kokoro gera WAV → transcoda pra MP3. O edge-tts já devolve MP3. Elimina o
processo ffmpeg (~200ms) por frase.

### 3.4 Considerar Piper (self-hosted, leve)
Se o custo/privacidade do edge-tts (cloud) incomodar: Piper roda em CPU, é bem mais
leve que Kokoro/torch, e faz streaming. Meio-termo entre latência e self-hosting.

---

## 4. O que NÃO vale a pena / é latência inevitável

- **Latência do OpenRouter** (geração LLM): ~500–700ms de TTFT é o piso da rede + modelo.
  Só trocando de modelo/provider dá para baixar.
- **STT → LLM é inerentemente sequencial** (o LLM precisa do texto). Não dá para
  paralelizar as duas etapas.
- **Upload do áudio** até o servidor: depende da rede móvel.

---

## 5. Resumo executivo (5–8 maiores oportunidades)

1. **TTS Kokoro é o novo gargalo** (~1,4–1,6s/frase). Trocar para **edge-tts** (TTFB 477ms) → **−1,5 a −2,2s** no first_audio.
2. **Streaming intra-frase de TTS** → first_audio ≈ 300–500ms (quase instantâneo).
3. **Pré-aquecer o Whisper** → elimina freeze de 1–3s na primeira fala.
4. **Cliente HTTP com keep-alive** → −50 a −150ms por requisição.
5. **Modelo default mais rápido** (gemini-2.5-flash / llama-3.1-8b) → −100 a −300ms no 1º token.
6. **TTS paralelo entre frases** → −300 a −800ms em respostas longas.
7. **Cap de max_tokens** → respostas faladas mais curtas e ágeis.
8. **Eliminar ffmpeg + base64** → menos overhead por frase (dezenas de ms).

**Meta realista:** com os quick wins, **first_audio de ~2,8s → ~0,9s**. Com streaming
intra-frase, **~0,4–0,5s**.