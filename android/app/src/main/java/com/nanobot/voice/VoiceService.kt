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
class VoiceService : Service() {

    companion object {
        const val TAG = "VoiceService"
        const val CHANNEL_ID = "nanobot_voice_channel"
        const val NOTIF_ID = 42

        const val ACTION_START = "com.nanobot.voice.START"
        const val ACTION_STOP = "com.nanobot.voice.STOP"
        const val ACTION_TRIGGER = "com.nanobot.voice.TRIGGER"

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
    }

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.IO)
    private val requests = Channel<Unit>(Channel.UNLIMITED)

    @Volatile
    private var active = false

    private var wakeEngine: WakeWordEngine? = null
    private var notificationManager: NotificationManager? = null
    private val notifBuilder: NotificationCompat.Builder by lazy { buildNotificationChannelAndBuilder() }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        notificationManager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
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
                requests.trySend(Unit)
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
            engine.onWakeWord = { requests.trySend(Unit) }
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
            requests.receive()

            engine.stop()
            if (!active) break

            handleRequest(ctx)
        }
    }

    /**
     * Records one utterance, sends it to the backend, plays the answer and then
     * optionally keeps the conversation going: while the follow-up window is
     * enabled the assistant keeps listening after its reply, so the user does
     * not have to repeat the wake word every single time.
     */
    private suspend fun handleRequest(ctx: Context) {
        var wav: ByteArray = recordForRequest(waitMillis = 4000, countdown = false)
            ?: run {
                updateNotification("Não ouvi nada. Tente novamente.")
                return
            }

        while (active) {
            askAndPlay(ctx, wav)
            if (!active) return

            val followUpSeconds = Prefs.followUpSeconds(ctx)
            if (followUpSeconds <= 0) return

            // Nothing said in the follow-up window -> the conversation is over,
            // go back to listening for the wake word.
            wav = recordForRequest(waitMillis = followUpSeconds * 1000, countdown = true)
                ?: return
        }
    }

    /** Records an utterance; returns null when nothing (loud enough) was said. */
    private fun recordForRequest(waitMillis: Int, countdown: Boolean): ByteArray? {
        updateNotification(if (countdown) "Ouvindo… pode continuar falando" else "Ouvindo você…")
        if (!countdown) beep()
        val wav = try {
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
        return wav?.takeIf { it.size >= 3200 }
    }

    /** Sends [wav] to the backend and streams the spoken answer. */
    private suspend fun askAndPlay(ctx: Context, wav: ByteArray) {
        updateNotification("Pensando…")

        // --- streaming playback -------------------------------------------
        // The stream reader (IO) pushes each audio chunk into this channel and
        // never waits for playback. A dedicated worker consumes the channel and
        // plays the chunks *sequentially* (one MediaPlayer per chunk), starting
        // with the very first one as soon as it arrives.
        val audioChunks = Channel<ByteArray>(Channel.UNLIMITED)
        var startedPlaying = false

        val playerJob = scope.launch {
            for (chunk in audioChunks) {
                try {
                    playAudio(chunk)
                } catch (t: Throwable) {
                    Log.e(TAG, "Erro ao tocar áudio", t)
                }
            }
        }

        try {
            NanobotClient(Prefs.serverUrl(ctx), Prefs.token(ctx)).askStream(
                wav, Prefs.voice(ctx), Prefs.llmModel(ctx),
                onText = { text ->
                    Log.i(TAG, "Resposta: $text")
                    updateNotification(text.take(80))
                },
                onAudioChunk = { bytes ->
                    if (!startedPlaying) {
                        startedPlaying = true
                        updateNotification("Respondendo…")
                    }
                    // trySend on an UNLIMITED channel never blocks the reader.
                    audioChunks.trySend(bytes)
                }
            )
        } catch (t: Throwable) {
            Log.e(TAG, "Erro no backend", t)
            updateNotification("Erro ao falar com o servidor: ${t.message}")
        } finally {
            // Signal end-of-stream so the player worker drains and exits.
            audioChunks.close()
            playerJob.join()
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
    ): ByteArray {
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

        rec.startRecording()
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

                if (speechStarted) {
                    if (rms > speechRmsThreshold) {
                        silentFrames = 0
                    } else {
                        silentFrames++
                        if (silentFrames > maxSilentFramesAfterSpeech) break
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
            return ByteArray(0)
        }

        val samples = ShortArray(collected.size) { collected[it] }
        // trim a little leading/trailing silence
        return WavUtil.encode(samples, sampleRate)
    }

    private fun playAudio(bytes: ByteArray) {
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
