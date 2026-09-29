# Auditoria de Otimização e Latência — App de Voz Nanobot

**Data:** 2026-09-29 (revisado)
**Regra inviolável:** **NÃO reduzir a qualidade** — nem do LLM, nem do TTS.
Kokoro (`pm_santa`) fica. Modelo de LLM de qualidade fica. edge-tts **descartado**
(qualidade pior, decisão já tomada pelo Henrique).

---

## 0. Baseline medido (produção, hoje)

| Pergunta | first_token | first_audio | total |
|---|---|---|---|
| "Quanto é dois mais dois?" | 628ms | 3107ms | 3108ms |
| "Qual a capital da França?" | 679ms | 2816ms | 2816ms |
| "Me conte uma curiosidade sobre gatos." | 534ms | 4602ms | 8307ms |

**Diagnóstico:** o 1º token do LLM chega em ~0,6s, mas o 1º áudio só sai em ~2,8–3,1s.
O gargalo é o **TTS (Kokoro)**, não o LLM.

### Decomposição do TTS (medido dentro do container)

| Etapa | Tempo | Observação |
|---|---|---|
| Kokoro synth (frase curta) | **~500–640ms** | "Quatro." |
| Kokoro synth (frase média) | **~1,1–1,4s** | "Dois mais dois é igual a quatro." |
| ffmpeg WAV→MP3 | **~180–240ms** | por frase |
| **Total por frase** | **~1,3–1,6s** | |

---

## 1. O que NÃO fazer (fere a qualidade ou já foi decidido)

- ❌ **Trocar para edge-tts** — qualidade pior; Henrique já rejeitou.
- ❌ **Trocar o modelo de LLM por um "mais rápido"** — perde qualidade.
- ❌ **kokoro-onnx** — testado: **não é mais rápido** (1,4–1,7s, igual ou pior que torch).
- ❌ **Mexer nas threads do torch** — 4 threads ~igual a 6; 8 threads **muito pior** (3,5s).

---

## 2. Gargalos reais e correções que PRESERVAM a qualidade

| # | Gargalo | Evidência | Correção (sem perder qualidade) | Ganho | Risco |
|---|---|---|---|---|---|
| 1 | **ffmpeg desnecessário** | Kokoro gera WAV; Android toca WAV nativo | Enviar **WAV direto** (lossless!) e remover o ffmpeg | **−180 a −240ms/frase** + remove dependência | Baixo |
| 2 | **TTS só entrega após a frase inteira** | `/ask_stream` emite 1 chunk por frase (linha 916–927) | **Streaming intra-frase**: enviar o 1º segmento de áudio assim que o Kokoro o produz | **first_audio ≈ 0,6–0,9s** | Médio |
| 3 | **TTS sequencial entre frases** | loop consumer serial (linha 907–930) | **Sintetizar 2–3 frases em paralelo** (6 cores disponíveis) | **−300 a −800ms** em respostas longas | Médio |
| 4 | **Whisper não pré-aquecido** | `_warmup` (linha 724) só aquece TTS | Aquecer `_get_whisper_model()` no startup | **−1 a −3s na 1ª fala** | Trivial |
| 5 | **`httpx.AsyncClient` por request** | linhas 245, 297, 345, 465 | Cliente global com keep-alive | **−50 a −150ms/req** | Trivial |
| 6 | **base64 nos chunks** | linhas 864, 924 | Framing binário (length-prefix) | −10 a −40ms + menos CPU móvel | Médio |
| 7 | **MediaPlayer novo por chunk** | `VoiceService.playAudio` (linha 407) | ExoPlayer gapless | menos silêncio entre frases | Médio |

---

## 3. Quick wins (ganho grande, risco baixo, qualidade intacta)

1. **Remover o ffmpeg** → WAV direto (lossless). −200ms/frase e menos uma dependência.
2. **Pré-aquecer o Whisper** → elimina freeze de 1–3s na primeira fala.
3. **Cliente httpx global (keep-alive)** → −50 a −150ms por request.

**Ganho combinado estimado: first_audio de ~2,8s → ~2,3–2,5s** (sem tocar em qualidade).

---

## 4. Mudanças estruturais (o grande salto, qualidade intacta)

### 4.1 Streaming intra-frase de TTS (maior ganho)
Hoje o app só toca depois de a frase inteira ser sintetizada (~1,3–1,6s). Se o servidor
enviar o áudio em sub-chunks (primeiros ~200–300ms de fala assim que saem do Kokoro),
o **first_audio cai para ~0,6–0,9s** — sem mudar a voz nem a qualidade.

### 4.2 TTS paralelo entre frases
Sintetizar a frase N+1 **enquanto** a N é enviada. Com 6 cores, 2–3 sínteses em paralelo.
Ganho grande em respostas de 2+ frases.

### 4.3 Remover o ffmpeg (WAV direto)
O Kokoro já produz WAV; o Android toca WAV nativamente. Enviar WAV é **lossless**
(qualidade igual ou melhor que MP3) e elimina o processo ffmpeg (~200ms/frase).
*Trade-off:* WAV é maior (~118KB vs ~20KB por frase). Em Wi-Fi é irrelevante; em dados
móveis, avaliar. Alternativa: Opus (~11KB, qualidade transparente) — mas exige encoder.

---

## 5. Resumo executivo

1. **O gargalo é o TTS Kokoro** (~1,3–1,6s/frase), não o LLM (~0,6s).
2. **ffmpeg é desnecessário** — WAV direto economiza ~200ms/frase e é lossless.
3. **Streaming intra-frase** → first_audio ~0,6–0,9s (o maior salto).
4. **TTS paralelo entre frases** → −300 a −800ms em respostas longas.
5. **Pré-aquecer o Whisper** → −1 a −3s na primeira fala.
6. **HTTP keep-alive** → −50 a −150ms/req.

**Meta realista (qualidade 100% preservada):**
- Quick wins: first_audio ~2,8s → **~2,3–2,5s**
- + streaming intra-frase: **~0,6–0,9s**

**Nada disso troca a voz (Kokoro pm_santa) nem o modelo de LLM.**