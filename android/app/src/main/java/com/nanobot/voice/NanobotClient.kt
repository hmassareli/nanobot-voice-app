package com.nanobot.voice

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.IOException
import java.util.concurrent.TimeUnit

data class AskResult(
    val text: String,
    val audio: ByteArray,
    val turnId: String? = null
)

/**
 * Client-side measurements collected while streaming a reply. These complement
 * the server-side timings (which only know the backend's own clock) with what
 * the phone actually experienced: how long until the *first* audio chunk landed
 * and how much audio we received in total.
 *
 * @param turnId          server-assigned turn id (from the "done" event), if any.
 * @param firstAudioMs    ms from sending the request to the FIRST audio chunk.
 * @param chunks          number of audio chunks received.
 * @param audioBytes      total bytes of (encoded) audio received.
 * @param requestStartedMs wall-clock (SystemClock/currentTimeMillis) when the request was sent.
 */
data class StreamMetrics(
    val turnId: String? = null,
    val firstAudioMs: Long? = null,
    val chunks: Int = 0,
    val audioBytes: Long = 0,
    val requestStartedMs: Long = 0
)

/**
 * Thin HTTP client for the Nanobot backend.
 *
 * POST {serverUrl}/ask  (multipart/form-data)
 *   header: Authorization: Bearer <token>
 *   header: X-Client-Id: <stable device id>
 *   field "audio": WAV file (16 kHz mono PCM)
 *   field "voice": TTS voice id
 *   optional field "llm": model id
 * Response JSON: {"text": "...", "audio_base64": "...", "turn_id": "..."}  (base64 of an audio file)
 *
 * POST {serverUrl}/ask_stream  (multipart/form-data, same fields)
 * Response: NDJSON (application/x-ndjson), one JSON object per line:
 *   {"type":"text","text":"<full answer>"}
 *   {"type":"audio","data":"<base64 ogg/opus of one sentence>","index":i,"text":"..."}
 *   {"type":"done","timings":{...},"turn_id":"..."}
 *   {"type":"error","message":"..."}
 *
 * POST {serverUrl}/report  (application/json) — fire-and-forget client telemetry.
 */
class NanobotClient(
    serverUrl: String,
    private val token: String,
    private val timeoutSeconds: Long = 120,
    private val clientId: String? = null
) {
    private val baseUrl: String = serverUrl.trimEnd('/')

    private val http = OkHttpClient.Builder()
        .connectTimeout(15, TimeUnit.SECONDS)
        .writeTimeout(timeoutSeconds, TimeUnit.SECONDS)
        .readTimeout(timeoutSeconds, TimeUnit.SECONDS)
        .build()

    @Throws(IOException::class)
    fun ask(wavBytes: ByteArray, voice: String, llmModel: String?): AskResult {
        val reqBuilder = Request.Builder()
            .url("$baseUrl/ask")
            .post(buildMultipart(wavBytes, voice, llmModel))
        if (token.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer $token")
        }
        if (!clientId.isNullOrBlank()) {
            reqBuilder.header("X-Client-Id", clientId)
        }

        http.newCall(reqBuilder.build()).execute().use { resp ->
            val bodyStr = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw IOException("HTTP ${resp.code}: ${bodyStr.take(300)}")
            }
            if (bodyStr.isBlank()) {
                return AskResult("", ByteArray(0))
            }
            val json = JSONObject(bodyStr)
            val text = json.optString("text", "")
            val turnId = json.optString("turn_id", "").takeIf { it.isNotBlank() }
            val b64 = json.optString("audio_base64", "")
            val audio = if (b64.isNotBlank()) {
                try {
                    android.util.Base64.decode(b64, android.util.Base64.DEFAULT)
                } catch (t: Throwable) {
                    Log.e("NanobotClient", "audio_base64 inválido", t)
                    ByteArray(0)
                }
            } else {
                ByteArray(0)
            }
            return AskResult(text, audio, turnId)
        }
    }

    /**
     * Streaming variant of [ask]. Posts the WAV to `$baseUrl/ask_stream` and reads
     * the NDJSON response line by line, invoking [onText] for the text event and
     * [onAudioChunk] for *each* audio chunk as soon as it arrives. This lets the
     * caller start playback before the whole answer is synthesized.
     *
     * Client-side telemetry is collected on the way:
     *   * [onTurnId] is invoked with the server `turn_id` as soon as the "done"
     *     event arrives (also returned in [StreamMetrics.turnId]);
     *   * [StreamMetrics.firstAudioMs] measures the request→first-chunk latency;
     *   * [StreamMetrics.chunks] / [StreamMetrics.audioBytes] count what arrived.
     *
     * If the server does not implement the streaming endpoint (HTTP 404), it
     * transparently falls back to the non-streaming [ask].
     */
    @Throws(IOException::class)
    fun askStream(
        wavBytes: ByteArray,
        voice: String,
        llmModel: String?,
        onText: (String) -> Unit,
        onAudioChunk: (ByteArray) -> Unit,
        onTurnId: ((String) -> Unit)? = null
    ): StreamMetrics {
        val t0 = System.currentTimeMillis()
        var firstAudioMs: Long? = null
        var chunks = 0
        var audioBytes = 0L
        var turnId: String? = null

        fun noteChunk(bytes: ByteArray) {
            if (firstAudioMs == null) firstAudioMs = System.currentTimeMillis() - t0
            chunks++
            audioBytes += bytes.size.toLong()
            onAudioChunk(bytes)
        }

        val reqBuilder = Request.Builder()
            .url("$baseUrl/ask_stream")
            .post(buildMultipart(wavBytes, voice, llmModel))
        if (token.isNotBlank()) {
            reqBuilder.header("Authorization", "Bearer $token")
        }
        if (!clientId.isNullOrBlank()) {
            reqBuilder.header("X-Client-Id", clientId)
        }

        http.newCall(reqBuilder.build()).execute().use { resp ->
            if (resp.code == 404) {
                // Old server without /ask_stream — fall back to the plain endpoint.
                Log.w("NanobotClient", "/ask_stream ausente (404) — usando /ask")
                val result = ask(wavBytes, voice, llmModel)
                if (result.text.isNotBlank()) onText(result.text)
                if (result.audio.isNotEmpty()) noteChunk(result.audio)
                result.turnId?.let { onTurnId?.invoke(it) }
                return StreamMetrics(result.turnId, firstAudioMs, chunks, audioBytes, t0)
            }
            if (!resp.isSuccessful) {
                val bodyStr = resp.body?.string().orEmpty()
                throw IOException("HTTP ${resp.code}: ${bodyStr.take(300)}")
            }

            val source = resp.body?.source() ?: throw IOException("resposta sem corpo")
            while (true) {
                val line = source.readUtf8Line() ?: break
                if (line.isBlank()) continue
                val evt = try {
                    JSONObject(line)
                } catch (t: Throwable) {
                    Log.w("NanobotClient", "linha NDJSON inválida: ${line.take(120)}")
                    continue
                }
                when (evt.optString("type")) {
                    "text" -> {
                        val text = evt.optString("text", "")
                        if (text.isNotBlank()) onText(text)
                    }
                    "audio" -> {
                        val b64 = evt.optString("data", "")
                        if (b64.isNotBlank()) {
                            val bytes = try {
                                android.util.Base64.decode(b64, android.util.Base64.DEFAULT)
                            } catch (t: Throwable) {
                                Log.e("NanobotClient", "chunk de áudio inválido", t)
                                ByteArray(0)
                            }
                            if (bytes.isNotEmpty()) noteChunk(bytes)
                        }
                    }
                    "done" -> {
                        val id = evt.optString("turn_id", "")
                        if (id.isNotBlank()) {
                            turnId = id
                            onTurnId?.invoke(id)
                        }
                        return StreamMetrics(turnId, firstAudioMs, chunks, audioBytes, t0)
                    }
                    "error" -> throw IOException(
                        evt.optString("message", "erro desconhecido do servidor")
                    )
                    else -> Log.w("NanobotClient", "evento desconhecido: ${line.take(120)}")
                }
            }
        }
        return StreamMetrics(turnId, firstAudioMs, chunks, audioBytes, t0)
    }

    /**
     * Fire-and-forget client telemetry. Posts [payload] (a JSON object) to
     * `$baseUrl/report` on a background thread. This method NEVER throws: any
     * failure (offline, timeout, bad response) is swallowed and logged, because
     * telemetry must never break the voice flow.
     */
    fun report(clientId: String, token: String, payload: JSONObject) {
        Thread {
            try {
                val body = payload.toString().toRequestBody("application/json".toMediaType())
                val req = Request.Builder()
                    .url("$baseUrl/report")
                    .post(body)
                val bearer = token.ifBlank { this.token }
                if (bearer.isNotBlank()) req.header("Authorization", "Bearer $bearer")
                if (clientId.isNotBlank()) req.header("X-Client-Id", clientId)
                http.newCall(req.build()).execute().use { resp ->
                    if (!resp.isSuccessful) {
                        Log.w("NanobotClient", "/report HTTP ${resp.code}")
                    }
                }
            } catch (t: Throwable) {
                Log.w("NanobotClient", "/report falhou (ignorado): ${t.message}")
            }
        }.apply { isDaemon = true }.start()
    }

    private fun buildMultipart(wavBytes: ByteArray, voice: String, llmModel: String?): MultipartBody {
        val audioPart = wavBytes.toRequestBody("audio/wav".toMediaType())
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("audio", "speech.wav", audioPart)
            .addFormDataPart("voice", voice)
        if (!llmModel.isNullOrBlank()) {
            builder.addFormDataPart("llm", llmModel)
        }
        return builder.build()
    }
}
