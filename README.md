# nanobot Voice — Assistente de voz sempre-ligado

App Android que transforma um celular antigo (Redmi Note 7) numa "Alexa pessoal"
ligada ao nanobot. Fica ouvindo em background esperando a wake word; quando você
fala, ele grava, manda pro servidor, e toca a resposta em voz.

## Arquitetura

```
[Redmi Note 7]                          [VPS / EasyPanel]
  wake word (offline, sherpa-onnx)         nanobot-voice-api
  grava áudio  ──── POST /ask ───────────►  STT (Groq/Whisper)
  toca resposta ◄─── {text, audio} ───────  LLM (OpenRouter)
                                            TTS (Kokoro self-hosted / edge-tts)
```

O celular só captura e toca. Todo o trabalho pesado roda no servidor.

## Servidor (já no ar)

- URL: `https://nanobot-voice-api.lnyx9r.easypanel.host`
- Token: `nanobot-voice`
- Endpoints: `GET /health`, `POST /ask` (multipart `audio` ou JSON `{"text":...}`)

## Instalar o APK no Redmi Note 7

1. Copie `app-debug.apk` para o celular (WhatsApp, cabo USB ou Google Drive).
2. Abra o arquivo e permita "instalar de fontes desconhecidas" se pedir.
3. Abra o app **nanobot Voice**.

## Configurar (dentro do app)

1. Confira a **URL do servidor** e o **token** (já vêm preenchidos).
2. Escolha a **voz** no seletor — as vozes são geradas no servidor:
   - **Santa** (masculina, calma) — Kokoro, padrão
   - **Dora** (feminina) — Kokoro
   - **Alex** (masculina) — Kokoro
   - **Antonio / Francisca / Thalita** — edge-tts (grátis)
3. Defina a **frase da wake word** (ex: "ei nanobot").
4. Toque em **Salvar**.
5. Toque em **Ligar serviço** → aceite a permissão de microfone.
6. Toque em **Pedir isenção de bateria** → permita.
7. Toque em **Abrir Autostart** → ative o autostart do MIUI.

> A voz escolhida é enviada junto de cada pergunta (`voice=<key>`) e sintetizada
> no servidor. Trocar a voz **não** exige reinstalar o app.

## Configuração obrigatória do MIUI (senão o Android mata o app)

No Redmi Note 7 (MIUI 12), faça uma vez:

1. **Configurações → Apps → nanobot Voice → Autostart**: ATIVAR.
2. **Configurações → Apps → nanobot Voice → Economia de bateria**: "Sem restrições".
3. **Configurações → Bateria → Economia de energia**: desligar para este app.
4. **Recentes**: abra o app, toque no ícone de cadeado para **travar** o app
   (impede que seja fechado ao limpar os recentes).
5. **Configurações → Apps → nanobot Voice → Outras permissões**: permitir
   "Iniciar em segundo plano" / "Exibir janelas pop-up".

## Uso

- Diga a wake word ("ei nanobot") e depois o comando.
- Ou use o botão **Testar (falar agora)** no app.

## Delay esperado

Ciclo completo (você para de falar → ouve a resposta): **~2,5 a 6s**.
O maior custo é a rede (Brasil→França) + o LLM. É usável, mas não é instantâneo
como Alexa/Siri.

## Estrutura do projeto

```
nanobot-voice-app/
├── android/          # app Kotlin (Gradle)
│   └── app/src/main/java/com/nanobot/voice/
│       ├── MainActivity.kt      # tela de configurações
│       ├── VoiceService.kt      # foreground service sempre-ligado
│       ├── WakeWordEngine.kt    # sherpa-onnx KWS (offline)
│       ├── NanobotClient.kt     # HTTP para o servidor
│       ├── BootReceiver.kt      # religa no boot
│       ├── Prefs.kt             # SharedPreferences
│       └── WavUtil.kt
└── server/           # backend FastAPI
    ├── app.py
    ├── Dockerfile
    └── deploy.py
```

## Pendências / melhorias futuras

- Wake word: o modelo KWS embutido é genérico; treinar/ajustar a frase exata
  melhora a detecção.
- Streaming de STT (transcrever enquanto fala) reduziria ~1-2s.
- Cache de TTS para frases comuns.
