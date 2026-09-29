package com.nanobot.voice

import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.util.Log
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.withTimeoutOrNull

/**
 * On-device streaming speech-to-text built on [SpeechRecognizer].
 *
 * Unlike the audio-upload path (record the whole utterance, ship it to the
 * server, wait for Whisper), this transcribes *while the user speaks*: partial
 * results arrive continuously and the final transcript is ready the instant the
 * user stops talking, so the server only ever receives text. That removes the
 * ~1-2s STT leg from the critical path.
 *
 * [SpeechRecognizer] must be created and driven from the main thread, so every
 * call is marshalled onto the main looper; callbacks are delivered on the main
 * thread too. [listen] bridges those callbacks into a suspendable call.
 */
class StreamingStt(
    private val context: Context,
    private val preferOffline: Boolean = true,
    private val languageTag: String = "pt-BR"
) {
    private sealed interface Event {
        data class Partial(val text: String) : Event
        data class Final(val text: String) : Event
        data class Failure(val code: Int, val message: String) : Event
    }

    companion object {
        const val TAG = "StreamingStt"

        /** True when the device has a usable on-device recognizer. */
        fun isAvailable(ctx: Context): Boolean = try {
            SpeechRecognizer.isRecognitionAvailable(ctx)
        } catch (_: Throwable) {
            false
        }

        /**
         * Errors that simply mean "the user said nothing" (not a real failure).
         * These must NOT trigger the audio fallback — the user just stayed
         * silent, exactly like an empty VAD window in the audio path.
         */
        fun isNoSpeech(code: Int): Boolean =
            code == SpeechRecognizer.ERROR_NO_MATCH ||
                code == SpeechRecognizer.ERROR_SPEECH_TIMEOUT
    }

    private val main = Handler(Looper.getMainLooper())
    private var recognizer: SpeechRecognizer? = null
    private val events = Channel<Event>(Channel.UNLIMITED)

    @Volatile
    private var released = false

    /** Starts listening. Results are consumed by [listen]. */
    fun start() {
        main.post {
            if (released) return@post
            try {
                val rec = SpeechRecognizer.createSpeechRecognizer(context)
                recognizer = rec
                rec.setRecognitionListener(object : RecognitionListener {
                    override fun onReadyForSpeech(params: Bundle?) {}

                    override fun onBeginningOfSpeech() {}

                    override fun onRmsChanged(rmsdB: Float) {}

                    override fun onBufferReceived(buffer: ByteArray?) {}

                    override fun onEndOfSpeech() {}

                    override fun onPartialResults(partialResults: Bundle?) {
                        val text = firstResult(partialResults)
                        if (text.isNotBlank()) events.trySend(Event.Partial(text))
                    }

                    override fun onResults(results: Bundle?) {
                        val text = firstResult(results)
                        events.trySend(Event.Final(text))
                    }

                    override fun onError(error: Int) {
                        events.trySend(Event.Failure(error, errorName(error)))
                    }

                    override fun onEvent(eventType: Int, params: Bundle?) {}
                })

                val intent = Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(
                        RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                        RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
                    )
                    putExtra(RecognizerIntent.EXTRA_LANGUAGE, languageTag)
                    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                    putExtra(RecognizerIntent.EXTRA_MAX_RESULTS, 1)
                    // Ask the platform to keep the audio on-device when a
                    // language pack is installed. The platform may still use the
                    // cloud if no offline pack exists — that is a device policy
                    // we cannot override, but the flag is the strongest hint.
                    putExtra(RecognizerIntent.EXTRA_PREFER_OFFLINE, preferOffline)
                }
                rec.startListening(intent)
            } catch (t: Throwable) {
                Log.e(TAG, "Falha ao iniciar o reconhecedor", t)
                events.trySend(Event.Failure(-1, t.message ?: "erro ao iniciar"))
            }
        }
    }

    /**
     * Suspends until the recognizer produces a final transcript, an error, or
     * [timeoutMs] elapses. Returns the final text (possibly blank when the user
     * said nothing) or null on timeout / no-speech. [onPartial] is invoked for
     * every interim result so the UI can show live feedback.
     *
     * @throws java.io.IOException on a real recognition failure (so the caller
     *   can fall back to the audio path).
     */
    suspend fun listen(
        timeoutMs: Long = 20000,
        onPartial: ((String) -> Unit)? = null
    ): String? = withTimeoutOrNull(timeoutMs) {
        for (e in events) {
            when (e) {
                is Event.Partial -> onPartial?.invoke(e.text)
                is Event.Final -> return@withTimeoutOrNull e.text
                is Event.Failure -> {
                    if (isNoSpeech(e.code)) return@withTimeoutOrNull null
                    throw java.io.IOException("STT ${e.code}: ${e.message}")
                }
            }
        }
        null
    }

    /** Asks the recognizer to stop and deliver whatever it has. */
    fun stop() {
        main.post {
            try {
                recognizer?.stopListening()
            } catch (_: Throwable) {
            }
        }
    }

    /** Destroys the recognizer. Safe to call more than once. */
    fun release() {
        released = true
        main.post {
            try {
                recognizer?.destroy()
            } catch (_: Throwable) {
            }
            recognizer = null
            events.close()
        }
    }

    private fun firstResult(bundle: Bundle?): String {
        val list = bundle?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
        return list?.firstOrNull()?.trim().orEmpty()
    }

    private fun errorName(code: Int): String = when (code) {
        SpeechRecognizer.ERROR_AUDIO -> "audio"
        SpeechRecognizer.ERROR_CLIENT -> "client"
        SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS -> "permissões"
        SpeechRecognizer.ERROR_NETWORK -> "rede"
        SpeechRecognizer.ERROR_NETWORK_TIMEOUT -> "timeout de rede"
        SpeechRecognizer.ERROR_NO_MATCH -> "sem correspondência"
        SpeechRecognizer.ERROR_RECOGNIZER_BUSY -> "reconhecedor ocupado"
        SpeechRecognizer.ERROR_SERVER -> "servidor"
        SpeechRecognizer.ERROR_SPEECH_TIMEOUT -> "sem fala"
        else -> "código $code"
    }
}
