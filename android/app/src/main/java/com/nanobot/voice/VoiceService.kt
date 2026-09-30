package com.nanobot.voice

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioRecord
import android.media.MediaPlayer
import android.media.MediaRecorder
import android.media.ToneGenerator
import android.os.Build
import android.os.IBinder
import android.util.Log
import androidx.core.app.NotificationCompat
import org.json.JSONObject
import java.io.File
import kotlin.math.abs
import kotlin.math.sqrt
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.launch

/**
 * Foreground service that keeps the assistant alive in the background.
 *
 * Lifecycle:
 *  1. A wake-word engine listens continuously (sherpa-onnx KWS).
 *  2. On detection (or on an explicit "Testar" trigger) the engine pauses,
 *     the utterance is recorded until silence, uploaded to the Nanobot backend
 *     and the returned audio is played back.
 *  3. The engine resumes and the loop repeats.
 */
sealed class VoiceReq {
    object Wake : VoiceReq()
    data class Reminder(val id: String, val text: String) : VoiceReq()
}

class VoiceService : Service() {

    /** One recorded utterance: the bytes to upload plus their container format
     *  ("opus", "aac" or "wav"). [speechEndEpochMs] is the wall-clock instant the
     *  VAD decided the user had stopped talking — the anchor for every latency
     *  measurement downstream (request sent, first audio received, first audio
     *  actually played). 0 when unknown. */
    private data class RecordedAudio(
        val bytes: ByteArray,
        val format: String,
        val speechEndEpochMs: Long = 0L
    )

    companion object {
        const val TAG = "VoiceService"
        const val CHANNEL_ID = "nanobot_voice_channel"
        const val NOTIF_ID = 42

        const val ACTION_START = "com.nanobot.voice.START"
        const val ACTION_STOP = "com.nanobot.voice.STOP"
        const val ACTION_TRIGGER = "com.nanobot.voice.TRIGGER"
        const val ACTION_REMINDER = "com.nanobot.voice.REMINDER"

        fun start(ctx: Context) {
            val i = Intent(ctx, VoiceService::class.java).setAction(ACTION_START)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }

        fun stop(ctx: Context) {
            ctx.stopService(Intent(ctx, VoiceService::class.java))
        }

        fun trigger(ctx: Context) {
            val i = Intent(ctx, VoiceService::class.java).setAction(ACTION_TRIGGER)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }

        /** Dispara a fala de um lembrete (e a escuta da resposta) no servico. */
        fun speakReminder(ctx: Context, id: String, text: String) {
            val i = Intent(ctx, VoiceService::class.java)
                .setAction(ACTION_REMINDER)
                .putExtra("id", id)
                .putExtra("text", text)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                ctx.startForegroundService(i)
            } else {
                ctx.startService(i)
            }
        }
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val requests = Channel<VoiceReq>(Channel.UNLIMITED)

    @Volatile
    private var active = false

    private var wakeEngine: WakeWordEngine? = null
    private var notificationManager: NotificationManager? = null
    private val notifBuilder: NotificationCompat.Builder by lazy { buildNotificationChannelAndBuilder() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        ReminderReceiver.scheduleSync(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                stopSelf()
                return START_NOT_STICKY
            }
            ACTION_TRIGGER -> {
                ensureForeground("Processando pedido…")
                if (!active) startLoop()
                requests.trySend(VoiceReq.Wake)
                return START_STICKY
            }
            ACTION_REMINDER -> {
                val rid = intent.getStringExtra("id") ?: ""
                val rtext = intent.getStringExtra("text") ?: ""
                ensureForeground("Lembrete: " + rtext.take(50))
                if (!active) startLoop()
                requests.trySend(VoiceReq.Reminder(rid, rtext))
                return START_STICKY
            }
            else -> {
                ensureForeground(getString(R.string.notif_text))
                if (!active) startLoop()
            }
        }
        return START_STICKY
    }

    private fun ensureForeground(text: String) {
        notifBuilder.setContentText(text)
        startForeground(NOTIF_ID, notifBuilder.build())
    }

    private fun updateNotification(text: String) {
        notifBuilder.setContentText(text)
        try {
            notificationManager?.notify(NOTIF_ID, notifBuilder.build())
        } catch (_: Throwable) {
        }
    }

    private fun startLoop() {
        active = true
        scope.launch { runLoop() }
    }

    private suspend fun runLoop() {
        val ctx = applicationContext

        // Drop any stale trigger left over from an earlier cycle (e.g. the user
        // tapped "Testar" twice) so we never start recording without a fresh
        // request.
        while (requests.tryReceive().isSuccess) {
            // drain
        }

        // (Re)create the engine each cycle so a changed wake word / config applies.
        while (active) {
            val wakeWord = Prefs.wakeWord(ctx)
            val engine = SherpaWakeWordEngine(ctx, wakeWord)
            wakeEngine = engine
            engine.onWakeWord = { requests.trySend(VoiceReq.Wake) }
            engine.start()
            if (engine.isRunning) {
                updateNotification("Ouvindo… diga \"$wakeWord\"")
            } else {
                updateNotification("Modo push-to-talk (KWS indisponível). Use \"Testar\".")
            }

            // Wait for a wake word or a manual trigger. Drop anything stale first
            // so a queued trigger cannot immediately re-trigger recording.
            while (requests.tryReceive().isSuccess) {
                // drain
            }
            val req = requests.receive()

            engine.stop()
            if (!active) break

            when (req) {
                is VoiceReq.Wake -> handleRequest(ctx)
                is VoiceReq.Reminder -> handleReminder(ctx, req.id, req.text)
            }
            // Pos-conversa: um sync barato pega lembretes criados agora.
            Reminders.syncAsync(ctx)
        }
    }

    /**
     * Records one utterance, sends it to the backend, plays the answer and then
     * optionally keeps the conversation going: while the follow-up window is
     * enabled the assistant keeps listening after its reply, so the user does
     * not have to repeat the wake word every single time.
     *
     * Two STT strategies are supported (see [Prefs.sttMode]):
     *  - "streaming": transcribe on-device while the user speaks and send only
     *    the text (fast path, no audio upload);
     *  - "audio": record and upload the utterance for server-side Whisper.
     * The streaming path is used only when the device actually has an on-device
     * recognizer; otherwise we silently fall back to the audio path.
     */
    private suspend fun handleRequest(ctx: Context) {
        if (Prefs.sttMode(ctx) == Prefs.STT_MODE_STREAMING && StreamingStt.isAvailable(ctx)) {
            handleRequestStreaming(ctx)
        } else {
            handleRequestAudio(ctx)
        }
    }

    /** Audio-upload path: record the utterance, ship it, let the server run STT. */
    private suspend fun handleRequestAudio(ctx: Context) {
        val rec0 = System.currentTimeMillis()
        var rec: RecordedAudio = recordForRequest(waitMillis = 4000, countdown = false)
            ?: run {
                updateNotification("Não ouvi nada. Tente novamente.")
                return
            }
        var recordMs = System.currentTimeMillis() - rec0

        while (active) {
            val ok = askAndPlay(ctx, rec, recordMs)
            if (!active) return
            // A failed turn (server down, timeout) ends the conversation instead
            // of silently re-opening the follow-up window.
            if (!ok) return

            val followUpSeconds = Prefs.followUpSeconds(ctx)
            if (followUpSeconds <= 0) return

            // Nothing said in the follow-up window -> the conversation is over,
            // go back to listening for the wake word.
            val rec1 = System.currentTimeMillis()
            rec = recordForRequest(waitMillis = followUpSeconds * 1000, countdown = true)
                ?: return
            recordMs = System.currentTimeMillis() - rec1
        }
    }

    /** On-device streaming path: transcribe while speaking, send only the text. */
    private suspend fun handleRequestStreaming(ctx: Context) {
        var turn = streamingTurn(ctx, waitMillis = 4000, countdown = false)
            ?: run {
                updateNotification("Não ouvi nada. Tente novamente.")
                return
            }

        while (active) {
            val ok = askAndPlayText(ctx, turn)
            if (!active) return
            if (!ok) return

            val followUpSeconds = Prefs.followUpSeconds(ctx)
            if (followUpSeconds <= 0) return

            turn = streamingTurn(ctx, waitMillis = followUpSeconds * 1000, countdown = true)
                ?: return
        }
    }

    /** One on-device transcription: the text plus the latency anchors. */
    private data class StreamedTurn(
        val text: String,
        /** Wall-clock instant the final transcript arrived (= end of speech). */
        val speechEndEpochMs: Long,
        /** ms from the start of listening to the first partial result, if any. */
        val firstPartialMs: Long?,
        /** Wall-clock instant we started listening. */
        val listenStartEpochMs: Long
    )

    /**
     * Listens for one utterance with the on-device recognizer and returns the
     * final transcript, or null when nothing was said / the wait window expired.
     * A real recognition failure propagates as an exception so the caller can
     * decide whether to fall back.
     */
    private suspend fun streamingTurn(
        ctx: Context,
        waitMillis: Int,
        countdown: Boolean
    ): StreamedTurn? {
        updateNotification(if (countdown) "Ouvindo… pode continuar falando" else "Ouvindo você…")
        if (!countdown) beep()

        val stt = StreamingStt(ctx, preferOffline = true)
        val listenStart = System.currentTimeMillis()
        var firstPartialMs: Long? = null
        val text = try {
            stt.start()
            // The recognizer ends the turn on its own trailing-silence detection;
            // the timeout is only a safety net (wait window + a generous cap for
            // a long monologue).
            stt.listen(timeoutMs = waitMillis.toLong() + 30_000) { partial ->
                if (firstPartialMs == null) {
                    firstPartialMs = System.currentTimeMillis() - listenStart
                }
                updateNotification(partial.take(80))
            }
        } catch (t: Throwable) {
            Log.e(TAG, "STT streaming falhou", t)
            null
        } finally {
            stt.release()
        }

        if (text.isNullOrBlank()) return null
        return StreamedTurn(text, System.currentTimeMillis(), firstPartialMs, listenStart)
    }

    /**
     * Entrega um lembrete em voz: fala o texto (TTS do servidor), avisa o
     * servidor que disparou e FICA TE OUVINDO por uma janela curta para o
     * usuario responder ("adiar 10 minutos", "ok obrigado"), conversando
     * normalmente em seguida. O texto do lembrete vai como contexto para o LLM.
     */
    private suspend fun handleReminder(ctx: Context, id: String, text: String) {
        updateNotification("Lembrete: " + text.take(50))
        val spoken = "Lembrete: $text"
        val audio = try {
            NanobotClient(Prefs.serverUrl(ctx), Prefs.token(ctx), clientId = Prefs.clientId(ctx))
                .tts(spoken, Prefs.voice(ctx))
        } catch (t: Throwable) {
            ByteArray(0)
        }
        if (audio.isNotEmpty()) {
            playAudio(audio)
        } else {
            Log.w(TAG, "TTS do lembrete falhou; notificando como fallback")
            ReminderReceiver.notifyPopup(ctx, spoken)
        }
        Reminders.markFiredAsync(ctx, id)

        // Espera uma resposta do usuario (mesma janela do follow-up).
        val listenMs = Prefs.followUpSeconds(ctx) * 1000
        if (listenMs <= 0) return
        val reminderContext =
            "Voce acabou de avisar o usuario: \"" + text + "\". Ele respondeu agora."

        if (Prefs.sttMode(ctx) == Prefs.STT_MODE_STREAMING && StreamingStt.isAvailable(ctx)) {
            var turn = streamingTurn(ctx, listenMs, countdown = true) ?: return
            var ctxNote = reminderContext
            while (active) {
                val ok = askAndPlayText(ctx, turn, ctxNote)
                ctxNote = ""
                if (!ok) return
                val f = Prefs.followUpSeconds(ctx) * 1000
                if (f <= 0) return
                turn = streamingTurn(ctx, f, countdown = true) ?: return
            }
        } else {
            var rec = recordForRequest(listenMs, countdown = true) ?: return
            var ctxNote = reminderContext
            var recordMs = listenMs.toLong()
            while (active) {
                val ok = askAndPlay(ctx, rec, recordMs, ctxNote)
                ctxNote = ""
                if (!ok) return
                val f = Prefs.followUpSeconds(ctx) * 1000
                if (f <= 0) return
                val t0 = System.currentTimeMillis()
                rec = recordForRequest(f, countdown = true) ?: return
                recordMs = System.currentTimeMillis() - t0
            }
        }
    }

    /** Sends [rec] to the backend and streams the spoken answer. */
    private suspend fun askAndPlay(ctx: Context, rec: RecordedAudio, recordMs: Long,
                                  context: String = ""): Boolean {
        return runTurn(
            ctx = ctx,
            anchorEpochMs = rec.speechEndEpochMs,
            recordMs = recordMs,
            audioFormat = rec.format,
            audioBytes = rec.bytes.size,
            sttMode = Prefs.STT_MODE_AUDIO,
            firstPartialMs = null,
            send = { onText, onAudioChunk, onTurnId, onPlay ->
                NanobotClient(
                    Prefs.serverUrl(ctx), Prefs.token(ctx), clientId = Prefs.clientId(ctx)
                ).askStream(
                    rec.bytes, Prefs.voice(ctx), Prefs.llmModel(ctx),
                    onText, onAudioChunk, onTurnId, onPlay, rec.format,
                    context = context.ifBlank { null }
                )
            }
        )
    }

    /** Sends the on-device transcript to the backend and streams the answer. */
    private suspend fun askAndPlayText(ctx: Context, turn: StreamedTurn,
                                       context: String = ""): Boolean {
        return runTurn(
            ctx = ctx,
            anchorEpochMs = turn.speechEndEpochMs,
            recordMs = turn.speechEndEpochMs - turn.listenStartEpochMs,
            audioFormat = "text",
            audioBytes = 0,
            sttMode = Prefs.STT_MODE_STREAMING,
            firstPartialMs = turn.firstPartialMs,
            send = { onText, onAudioChunk, onTurnId, onPlay ->
                NanobotClient(
                    Prefs.serverUrl(ctx), Prefs.token(ctx), clientId = Prefs.clientId(ctx)
                ).askStreamText(
                    turn.text, Prefs.voice(ctx), Prefs.llmModel(ctx),
                    onText, onAudioChunk, onTurnId, onPlay,
                    context = context.ifBlank { null }
                )
            }
        )
    }

    /**
     * Runs one turn: streams the reply audio, plays it chunk by chunk and
     * reports client-side telemetry. [send] performs the actual request and
     * returns the stream metrics; it is the only part that differs between the
     * audio and text STT paths.
     */
    private suspend fun runTurn(
        ctx: Context,
        anchorEpochMs: Long,
        recordMs: Long,
        audioFormat: String,
        audioBytes: Int,
        sttMode: String,
        firstPartialMs: Long?,
        send: (
            onText: (String) -> Unit,
            onAudioChunk: (ByteArray) -> Unit,
            onTurnId: (String) -> Unit,
            onPlay: (String) -> Unit
        ) -> StreamMetrics
    ): Boolean {
        updateNotification("Pensando…")

        // Audio files the server asked us to play in full (e.g. a book excerpt
        // generated by a tool). Collected thread-safely from the stream reader
        // and played *after* the spoken answer finishes, then the normal
        // conversation flow resumes.
        val playPaths = java.util.Collections.synchronizedList(mutableListOf<String>())

        // --- streaming playback -------------------------------------------
        // The stream reader (IO) pushes each audio chunk into this channel and
        // never waits for playback. A dedicated worker consumes the channel and
        // plays the chunks *sequentially* (one MediaPlayer per chunk), starting
        // with the very first one as soon as it arrives.
        val audioChunks = Channel<ByteArray>(Channel.UNLIMITED)
        var startedPlaying = false

        // Client-side playback timings.
        var firstChunkPlayMs: Long? = null
        var totalPlayMs: Long? = null
        val playWindowStart = System.currentTimeMillis()
        // Exact wall-clock instants for the latency breakdown, all anchored to
        // the moment the user stopped speaking (anchorEpochMs).
        var firstAudioReceivedEpochMs = 0L
        var firstAudioPlayedEpochMs = 0L

        val playerJob = scope.launch {
            for (chunk in audioChunks) {
                val chunkStart = System.currentTimeMillis()
                try {
                    playAudio(chunk) {
                        // Fired the instant MediaPlayer.start() returns, i.e. the
                        // first sample is actually going to the speaker.
                        if (firstAudioPlayedEpochMs == 0L) {
                            firstAudioPlayedEpochMs = System.currentTimeMillis()
                        }
                    }
                } catch (t: Throwable) {
                    Log.e(TAG, "Erro ao tocar áudio", t)
                } finally {
                    if (firstChunkPlayMs == null) {
                        // ms from the start of the turn until playback of the
                        // first chunk actually began (includes network + prep).
                        firstChunkPlayMs = chunkStart - playWindowStart
                    }
                    totalPlayMs = System.currentTimeMillis() - playWindowStart
                }
            }
        }

        var metrics: StreamMetrics? = null
        var turnId: String? = null
        var failed = false
        // Wall-clock instant we hand the request to the HTTP client. The gap
        // between this and anchorEpochMs is the local "turnaround" cost
        // (encoder.finish + multipart build + socket write).
        val requestSentEpochMs = System.currentTimeMillis()
        try {
            metrics = send(
                { text ->
                    Log.i(TAG, "Resposta: $text")
                    updateNotification(text.take(80))
                },
                { bytes ->
                    if (firstAudioReceivedEpochMs == 0L) {
                        firstAudioReceivedEpochMs = System.currentTimeMillis()
                    }
                    if (!startedPlaying) {
                        startedPlaying = true
                        updateNotification("Respondendo…")
                    }
                    // trySend on an UNLIMITED channel never blocks the reader.
                    audioChunks.trySend(bytes)
                },
                { id -> turnId = id },
                { path -> playPaths.add(path) }
            )
        } catch (t: Throwable) {
            failed = true
            Log.e(TAG, "Erro no backend", t)
            updateNotification("Erro ao falar com o servidor: ${t.message}")
        } finally {
            // Signal end-of-stream so the player worker drains and exits.
            audioChunks.close()
            playerJob.join()
        }

        // --- play any audio the server enqueued (book excerpt, etc.) --------
        // Runs after the spoken answer is fully played. playAudio() blocks until
        // the file finishes, so the whole excerpt is heard before we return to
        // the follow-up / wake-word loop.
        if (playPaths.isNotEmpty()) {
            val client = NanobotClient(
                Prefs.serverUrl(ctx), Prefs.token(ctx), clientId = Prefs.clientId(ctx)
            )
            for (path in playPaths) {
                if (!active) break
                try {
                    updateNotification("Tocando áudio…")
                    val bytes = client.fetchAudio(path)
                    if (bytes.isNotEmpty()) {
                        playAudio(bytes)
                    } else {
                        Log.w(TAG, "Áudio enfileirado vazio/indisponível: $path")
                    }
                } catch (t: Throwable) {
                    Log.e(TAG, "Erro ao tocar áudio enfileirado ($path)", t)
                }
            }
        }

        // --- client-side telemetry (best-effort, never blocks the voice) ---
        try {
            val m = metrics
            val audioDurationMs = when {
                audioFormat == "text" -> null
                audioFormat == "wav" -> {
                    // WAV header is 44 bytes; 16 kHz mono 16-bit => 32 bytes/ms.
                    val pcm = (audioBytes - 44).coerceAtLeast(0)
                    if (pcm > 0) pcm / 32.0 else null
                }
                else -> {
                    // Compressed: estimate from the codec bitrate (Opus 24k / AAC 24k).
                    if (audioBytes > 0) audioBytes * 8.0 / 24.0 else null
                }
            }
            val payload = JSONObject().apply {
                turnId?.let { put("turn_id", it) }
                    ?: m?.turnId?.let { put("turn_id", it) }
                put("stt_mode", sttMode)
                put("record_ms", recordMs)
                put("audio_format", audioFormat)
                put("audio_bytes", audioBytes)
                put("audio_duration_ms", audioDurationMs)
                put("request_to_first_audio_ms", m?.firstAudioMs ?: JSONObject.NULL)
                put("first_chunk_play_ms", firstChunkPlayMs ?: JSONObject.NULL)
                put("total_play_ms", totalPlayMs ?: JSONObject.NULL)
                put("chunks", m?.chunks ?: 0)
                // --- exact latency breakdown, anchored to end-of-speech ---
                // All values are ms elapsed since the recognizer/VAD decided the
                // user had stopped talking. This is the number the user feels.
                if (anchorEpochMs > 0) {
                    put("speech_end_epoch_ms", anchorEpochMs)
                    put("request_sent_ms", requestSentEpochMs - anchorEpochMs)
                    if (firstAudioReceivedEpochMs > 0) {
                        put("first_audio_received_ms", firstAudioReceivedEpochMs - anchorEpochMs)
                    }
                    if (firstAudioPlayedEpochMs > 0) {
                        put("first_audio_played_ms", firstAudioPlayedEpochMs - anchorEpochMs)
                    }
                }
                // Proof of the streaming win: how early the first partial landed
                // relative to the start of listening (negative = before the user
                // finished speaking).
                firstPartialMs?.let { put("first_partial_ms", it) }
                put("app_version", BuildConfig.VERSION_NAME)
                put("device_model", Build.MODEL)
                put("android_sdk", Build.VERSION.SDK_INT)
                put("network", networkType(ctx))
            }
            NanobotClient(Prefs.serverUrl(ctx), Prefs.token(ctx)).report(
                Prefs.clientId(ctx), Prefs.token(ctx), payload
            )
        } catch (t: Throwable) {
            Log.w(TAG, "Telemetria ignorada: ${t.message}")
        }

        // A failed turn must NOT silently fall back into the follow-up window:
        // that is what made the app look like it was "listening forever" after
        // the server died. Returning false tells the caller to stop the loop and
        // go back to the wake word.
        return !failed
    }

    /** Records an utterance; returns null when nothing (loud enough) was said. */
    private fun recordForRequest(waitMillis: Int, countdown: Boolean): RecordedAudio? {
        updateNotification(if (countdown) "Ouvindo… pode continuar falando" else "Ouvindo você…")
        if (!countdown) beep()
        val rec = try {
            recordUtterance(
                waitMillis,
                if (countdown) { s ->
                    updateNotification(getString(R.string.notif_follow_up, s))
                } else null,
                // Only the follow-up (post-answer) window needs a louder start
                // so the speaker tail can't kick off another turn. Normal turns
                // stay sensitive, and there is NO hard cap on how long you may
                // speak — recording ends only after real trailing silence.
                speechThreshold = if (countdown) 700.0 else 350.0,
                minSpeechFrames = if (countdown) 5 else 3,
                // Drain ~0.8s of mic input before listening so the speaker tail
                // (and the beep) can't be mistaken for the start of a new turn.
                // Normal turns start immediately — the wake word is the gate.
                flushMillis = if (countdown) 800 else 0
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Falha na gravação", t)
            null
        }
        return rec?.takeIf { it.bytes.size >= 3200 }
    }


    /** Best-effort connectivity label for telemetry: "wifi", "mobile" or "none". */
    private fun networkType(ctx: Context): String {
        return try {
            val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE)
                as? android.net.ConnectivityManager ?: return "unknown"
            val caps = cm.activeNetwork?.let { cm.getNetworkCapabilities(it) }
                ?: return "none"
            when {
                caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI) -> "wifi"
                caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_CELLULAR) -> "mobile"
                caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_ETHERNET) -> "ethernet"
                else -> "other"
            }
        } catch (_: Throwable) {
            "unknown"
        }
    }

    /**
     * Records the user's utterance (16 kHz mono PCM) until ~0.9s of trailing
     * silence is detected. There is no short cap on the length of a turn — a
     * long monologue is captured in full (a 60s safety ceiling only guards
     * against a stuck stream).
     *
     * @param waitMillis how long to wait for speech to start. After a reply the
     *   caller passes the follow-up window so the conversation can continue
     *   without repeating the wake word.
     * @param onCountdown optional callback invoked once per second with the
     *   remaining seconds, used to update the notification while we wait.
     * @param speechThreshold RMS above which a frame counts as speech.
     * @param minSpeechFrames consecutive loud frames needed to start a turn.
     * @param flushMillis leading milliseconds of mic input to discard before
     *   listening, so the speaker tail / beep cannot start a new turn.
     */
    private fun recordUtterance(
        waitMillis: Int = 4000,
        onCountdown: ((Int) -> Unit)? = null,
        speechThreshold: Double = 350.0,
        minSpeechFrames: Int = 3,
        flushMillis: Int = 0
    ): RecordedAudio {
        val sampleRate = SAMPLE_RATE
        val minBuf = AudioRecord.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        ).coerceAtLeast(sampleRate / 5 * 2)

        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            sampleRate,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            minBuf * 2
        )
        check(rec.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord não inicializou" }

        val frame = ShortArray(sampleRate / 50) // 20 ms
        val collected = ArrayList<Short>(sampleRate * 8)
        // Total capture is bounded only by a generous safety ceiling (60 s), so
        // a long monologue is never cut off mid-sentence. Recording ends when
        // ~0.9 s of trailing silence is detected after speech started.
        val maxTotalFrames = 60 * 50         // hard safety ceiling: 60 s
        val maxSilentFramesAfterSpeech = 900 / 20 // ~0.9 s of real silence ends the turn
        val maxWaitFrames = (waitMillis / 20).coerceAtLeast(20) // waiting for speech
        // Low enough to catch a quiet phone mic (MIUI often records very faint
        // audio). The follow-up window passes a higher value so the speaker tail
        // does not start a turn. Note: speech that starts soft but continues
        // (e.g. after a pause) is still captured — once speechStarted is true we
        // collect continuously and only a full silence window ends the turn.
        val speechRmsThreshold = speechThreshold

        var speechStarted = false
        var loudFrames = 0
        var silentFrames = 0
        var waitedFrames = 0
        var frames = 0
        // Wall-clock instant the trailing-silence window closed (i.e. the moment
        // we decided the user stopped speaking). This is the anchor for all
        // downstream latency numbers.
        var speechEndEpochMs = 0L

        rec.startRecording()
        // Encode in parallel with recording so the compressed audio is ready the
        // moment the VAD ends the turn (no post-recording encode on the critical
        // path). Falls back to raw PCM when no encoder is available.
        val encoder = LiveAudioEncoder(cacheDir, sampleRate).also { it.start() }
        try {
            // Drain whatever the mic buffered while we were not reading (i.e.
            // during the post-answer settle), so the speaker tail never lands at
            // the head of the recording.
            if (flushMillis > 0) {
                val flushFrames = (flushMillis / 20).coerceAtLeast(1)
                var drained = 0
                while (drained < flushFrames) {
                    if (rec.read(frame, 0, frame.size) > 0) drained++
                }
            }
            while (frames < maxTotalFrames) {
                val n = rec.read(frame, 0, frame.size)
                if (n <= 0) continue
                frames++

                var sumSq = 0.0
                for (i in 0 until n) {
                    val s = frame[i].toInt()
                    sumSq += (s * s).toDouble()
                }
                val rms = sqrt(sumSq / n)

                if (!speechStarted) {
                    waitedFrames++
                    if (waitedFrames % 50 == 0) {
                        val remaining = ((maxWaitFrames - waitedFrames) / 50).coerceAtLeast(0)
                        onCountdown?.invoke(remaining)
                    }
                    if (rms > speechRmsThreshold) {
                        // Require a few consecutive loud frames so a single click
                        // or pop does not count as speech.
                        loudFrames++
                        if (loudFrames >= minSpeechFrames) {
                            speechStarted = true
                        } else {
                            continue
                        }
                    } else if (waitedFrames > maxWaitFrames) {
                        // give up: nothing spoken
                        break
                    } else {
                        loudFrames = 0
                        continue
                    }
                }

                // once speech started, collect everything
                for (i in 0 until n) collected.add(frame[i])
                // Feed the live encoder in lock-step with the recording loop.
                if (encoder.isActive) encoder.feed(frame, n)

                if (speechStarted) {
                    if (rms > speechRmsThreshold) {
                        silentFrames = 0
                    } else {
                        silentFrames++
                        if (silentFrames > maxSilentFramesAfterSpeech) {
                            speechEndEpochMs = System.currentTimeMillis()
                            break
                        }
                    }
                }
            }
        } finally {
            try {
                rec.stop()
            } catch (_: Throwable) {
            }
            rec.release()
        }

        if (!speechStarted || collected.isEmpty()) {
            encoder.finish() // release codec/muxer even on an empty turn
            return RecordedAudio(ByteArray(0), "wav")
        }

        val samples = ShortArray(collected.size) { collected[it] }
        // Prefer the audio encoded live during recording; fall back to raw WAV.
        val encoded = encoder.finish()
        if (encoded.bytes.isNotEmpty() && encoded.format != "wav") {
            return RecordedAudio(encoded.bytes, encoded.format, speechEndEpochMs)
        }
        // trim a little leading/trailing silence
        return RecordedAudio(WavUtil.encode(samples, sampleRate), "wav", speechEndEpochMs)
    }

    private fun playAudio(bytes: ByteArray, onStarted: (() -> Unit)? = null) {
        val tmp = File(cacheDir, "reply_${System.currentTimeMillis()}_${(Math.random() * 1e6).toInt()}.ogg")
        tmp.writeBytes(bytes)
        val player = MediaPlayer()
        try {
            player.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_ASSISTANT)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            player.setDataSource(tmp.absolutePath)
            player.prepare()
            val done = Object()
            player.setOnCompletionListener {
                synchronized(done) { done.notifyAll() }
            }
            player.setOnErrorListener { _, _, _ ->
                synchronized(done) { done.notifyAll() }
                true
            }
            player.start()
            // start() returns once playback has begun; this is the closest we get
            // to "the first sample left the speaker".
            onStarted?.invoke()
            synchronized(done) {
                try {
                    done.wait(120_000)
                } catch (_: InterruptedException) {
                }
            }
        } finally {
            try {
                player.release()
            } catch (_: Throwable) {
            }
            tmp.delete()
        }
    }

    private fun beep() {
        try {
            val tg = ToneGenerator(AudioManager.STREAM_MUSIC, 60)
            tg.startTone(ToneGenerator.TONE_PROP_BEEP, 120)
            Thread {
                Thread.sleep(400)
                tg.release()
            }.start()
        } catch (_: Throwable) {
        }
    }

    private fun buildNotificationChannelAndBuilder(): NotificationCompat.Builder {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val chan = NotificationChannel(
                CHANNEL_ID,
                getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = getString(R.string.notif_channel_desc)
                setShowBadge(false)
            }
            notificationManager?.createNotificationChannel(chan)
        }
        val open = PendingIntent.getActivity(
            this,
            0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        val stopIntent = PendingIntent.getService(
            this,
            1,
            Intent(this, VoiceService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.notif_text))
            .setSmallIcon(android.R.drawable.ic_btn_speak_now)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(open)
            .addAction(android.R.drawable.ic_media_pause, "Desligar", stopIntent)
    }

    override fun onDestroy() {
        active = false
        try {
            wakeEngine?.release()
        } catch (_: Throwable) {
        }
        wakeEngine = null
        scope.cancel()
        super.onDestroy()
        Log.i(TAG, "Serviço encerrado")
    }
}
