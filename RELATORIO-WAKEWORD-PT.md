# Wake word em português — "Opa amigo"

**Data:** 29/09/2026
**Objetivo:** detectar a wake word **"Opa amigo"** (português) on-device no app Android.

## Problema

O app usa **sherpa-onnx KWS** com o modelo `kws-zipformer-gigaspeech-3.3M`
(treinado **só em inglês**). Testes comprovados:

| Frase | Detecta? |
|---|---|
| HEY NANOBOT / HEY NANO / HEY BOT / ALEXA | ✅ sim |
| OPA AMIGO / EI NANOBOT / OI NANOBOT / NANO | ❌ não (nenhum threshold) |

Ou seja: o modelo não cobre fonemas do português. Não é bug de tokenização
(a tokenização BPE está correta) — é limitação do modelo.

## Solução recomendada: VOSK (KaldiRecognizer com gramática restrita)

**Veredito: DÁ.** O Vosk tem modelo de português e suporta *keyword spotting*
por gramática restrita — você passa a lista de frases permitidas e ele só
transcreve uma delas.

### Medição (modelo `vosk-model-small-pt-0.3`)

```
=== POSITIVOS (deveriam acender) ===
  [HIT ] wa_1790680203553_7f45d665.wav   -> 'opa amigo'   (áudio real do Henrique)
  [HIT ] wa_1790680199782_5be7d5a5.wav   -> 'opa amigo'   (áudio real do Henrique)
  [HIT ] tts_opa_amigo.wav               -> 'opa amigo'
  [HIT ] opa_amigo.wav                   -> 'opa amigo'
  [MISS] tts_opa.wav                     -> 'opa'         (só "opa", sem "amigo")
  [MISS] opa.wav                         -> 'opa'         (só "opa", sem "amigo")

=== NEGATIVOS (não deveriam acender) ===
  18/18 corretos — 0 falsos positivos
  (hey nanobot, alexa, ei/oi nanobot, nano, fala normal, etc.)

Recall positivos: 4/6  (100% dos que dizem "opa amigo" completo)
Falsos positivos: 0/18
```

As 2 falhas são áudios que dizem **só "opa"** — não são a wake word completa.
Para a frase-alvo "opa amigo", o recall é **100%** com **zero** falsos positivos.

### Custo / tamanho

| Item | Valor |
|---|---|
| Modelo `vosk-model-small-pt-0.3` | **52 MB** descompactado (Apache-2.0) |
| SDK Android `com.alphacephei:vosk-android:0.3.75` | AAR ~13 MB |
| Licença | Apache-2.0 (grátis, offline, sem API key) |
| Bateria | roda on-device, sem rede |

### Integração no app (passos)

1. **`build.gradle`**: adicionar `implementation("com.alphacephei:vosk-android:0.3.75")`.
2. **Assets**: colocar o modelo em `android/app/src/main/assets/vosk-model-pt/`
   (ou baixar no 1º uso para não inflar o APK em 52 MB).
3. **`WakeWordEngine.kt`**: criar `VoskWakeWordEngine` que:
   - carrega `Model(assetsPath)`;
   - cria `KaldiRecognizer(model, 16000, "[\"opa amigo\",\"[unk]\"]")`;
   - alimenta PCM 16 kHz do `AudioRecord` e checa se o texto contém "opa amigo".
4. **`Prefs.kt`**: manter a frase editável; mapear a frase → gramática Vosk.
5. Manter o sherpa-onnx como alternativa para wake words em inglês.

### Riscos / limitações

- **+52 MB** no APK (ou download no 1º uso).
- Gramática restrita é mais rígida: exige a frase exata (bom p/ evitar falsos
  positivos, mas não aceita variações fonéticas).
- Vosk é mais pesado em CPU que o sherpa KWS (mas roda tranquilo em celular).

## Alternativas avaliadas

- **Picovoice Porcupine**: suporta `pt`, mas o treino de wake word custom exige
  **AccessKey** e o free tier tem limites. Não testado (evitar custo).
- **Modelo KWS sherpa-onnx em português**: não existe modelo oficial PT para KWS
  (só ASR). Descartado.

## Próximo passo

Implementar `VoskWakeWordEngine` + baixar o modelo no 1º uso, mantendo o
sherpa-onnx para inglês. Testar no Redmi Note 7 com o áudio real do Henrique.