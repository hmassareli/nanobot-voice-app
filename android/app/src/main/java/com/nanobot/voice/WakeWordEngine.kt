package com.nanobot.voice

import android.content.Context
import android.content.res.AssetManager
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Log
import com.k2fsa.sherpa.onnx.FeatureConfig
import com.k2fsa.sherpa.onnx.KeywordSpotter
import com.k2fsa.sherpa.onnx.KeywordSpotterConfig
import com.k2fsa.sherpa.onnx.OnlineModelConfig
import com.k2fsa.sherpa.onnx.OnlineStream
import com.k2fsa.sherpa.onnx.OnlineTransducerModelConfig
import kotlin.concurrent.thread

/**
 * Pluggable wake-word detector. The service only depends on this interface so
 * that alternative back-ends (Porcupine, cloud, a plain heuristic, ...) can be
 * dropped in without touching [VoiceService].
 */
interface WakeWordEngine {
    /** Called on a background thread when the wake word is detected. */
    var onWakeWord: (() -> Unit)?
    fun start()
    fun stop()
    fun release()
    val isRunning: Boolean
}

private const val TAG = "WakeWordEngine"
const val SAMPLE_RATE = 16000

/**
 * Real implementation backed by sherpa-onnx KeywordSpotting (KWS) with a
 * Zipformer transducer model bundled in `assets/kws`.
 *
 * If the native runtime cannot be initialised (e.g. unsupported ABI) the engine
 * degrades gracefully: [isRunning] stays false and the service keeps working
 * (push-to-talk via the "Testar" button still works).
 */
class SherpaWakeWordEngine(
    private val context: Context,
    private val wakeWord: String
) : WakeWordEngine {

    override var onWakeWord: (() -> Unit)? = null

    @Volatile
    override var isRunning: Boolean = false
        private set

    private var spotter: KeywordSpotter? = null
    private var stream: OnlineStream? = null
    private var record: AudioRecord? = null
    private var worker: Thread? = null

    @Volatile
    private var shouldStop = false

    override fun start() {
        if (isRunning) return
        shouldStop = false
        try {
            initSherpa()
            initRecorder()
        } catch (t: Throwable) {
            Log.e(TAG, "Não foi possível iniciar o KWS sherpa-onnx (usando modo passivo)", t)
            releaseQuietly()
            isRunning = false
            return
        }
        isRunning = true
        worker = thread(name = "kws-loop") { loop() }
        Log.i(TAG, "Wake word engine ativo para: $wakeWord")
    }

    private fun initSherpa() {
        val assets: AssetManager = context.assets
        val transducer = OnlineTransducerModelConfig(
            encoder = "kws/encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx",
            decoder = "kws/decoder-epoch-12-avg-2-chunk-16-left-64.onnx",
            joiner = "kws/joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
        )
        val modelConfig = OnlineModelConfig(
            transducer = transducer,
            tokens = "kws/tokens.txt",
            numThreads = 2,
            debug = false,
            provider = "cpu",
            modelType = "zipformer2",
            modelingUnit = "bpe",
            bpeVocab = "kws/bpe.model"
        )
        val featConfig = FeatureConfig(sampleRate = SAMPLE_RATE, featureDim = 80, dither = 0.0f)
        val config = KeywordSpotterConfig(
            featConfig = featConfig,
            modelConfig = modelConfig,
            maxActivePaths = 4,
            keywordsFile = "kws/keywords.txt",
            keywordsScore = 1.0f,
            keywordsThreshold = 0.25f,
            numTrailingBlanks = 1
        )
        val kws = KeywordSpotter(assetManager = assets, config = config)
        spotter = kws

        // The keyword the user typed must actually drive detection. sherpa needs
        // it tokenised with the model's BPE vocabulary (a raw word fails with
        // "Cannot find ID for token ..."), so tokenise on-device and pass the
        // line to createStream(). If that fails, fall back to the bundled
        // keywords.txt via an empty stream.
        val tokenised = BpeTokenizer.keywordLine(wakeWord)
        var st: OnlineStream? = null
        if (tokenised != null) {
            Log.i(TAG, "Wake word '$wakeWord' -> '$tokenised'")
            st = kws.createStream(tokenised)
        }
        if (st == null) {
            Log.w(TAG, "Tokenização falhou para '$wakeWord'; usando keywords.txt")
            st = kws.createStream()
        }
        stream = st
    }

    private fun initRecorder() {
        val minBuf = AudioRecord.getMinBufferSize(
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT
        ).coerceAtLeast(SAMPLE_RATE / 5 * 2)
        val rec = AudioRecord(
            MediaRecorder.AudioSource.VOICE_RECOGNITION,
            SAMPLE_RATE,
            AudioFormat.CHANNEL_IN_MONO,
            AudioFormat.ENCODING_PCM_16BIT,
            minBuf * 2
        )
        check(rec.state == AudioRecord.STATE_INITIALIZED) { "AudioRecord não inicializou" }
        record = rec
    }

    private fun loop() {
        val kws = spotter ?: return
        val st = stream ?: return
        val rec = record ?: return
        val buf = ShortArray(SAMPLE_RATE / 10) // 100 ms
        try {
            rec.startRecording()
        } catch (t: Throwable) {
            Log.e(TAG, "Falha ao iniciar gravação", t)
            return
        }
        while (!shouldStop) {
            val n = rec.read(buf, 0, buf.size)
            if (n <= 0) continue
            val samples = FloatArray(n) { buf[it] / 32768.0f }
            try {
                st.acceptWaveform(samples, SAMPLE_RATE)
                while (kws.isReady(st)) {
                    kws.decode(st)
                }
                val res = kws.getResult(st)
                if (res.keyword.isNotBlank()) {
                    Log.i(TAG, "Wake word detectada: ${res.keyword}")
                    kws.reset(st)
                    onWakeWord?.invoke()
                }
            } catch (t: Throwable) {
                Log.e(TAG, "Erro no decode KWS", t)
            }
        }
        try {
            rec.stop()
        } catch (_: Throwable) {
        }
    }

    override fun stop() {
        shouldStop = true
        worker?.join(1500)
        worker = null
        isRunning = false
    }

    override fun release() {
        stop()
        releaseQuietly()
    }

    private fun releaseQuietly() {
        try {
            stream?.release()
        } catch (_: Throwable) {
        }
        try {
            spotter?.release()
        } catch (_: Throwable) {
        }
        try {
            record?.release()
        } catch (_: Throwable) {
        }
        stream = null
        spotter = null
        record = null
    }
}
