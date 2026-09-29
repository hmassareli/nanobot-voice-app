package com.nanobot.voice

import android.media.MediaCodec
import android.media.MediaCodecInfo
import android.media.MediaCodecList
import android.media.MediaFormat
import android.media.MediaMuxer
import android.os.Build
import android.util.Log
import java.io.File
import java.nio.ByteBuffer
import java.nio.ByteOrder

/**
 * Encodes microphone PCM to a compressed container **while the user is still
 * speaking**, so that when the VAD detects end-of-speech the audio is already
 * compressed and can be uploaded immediately — no encode step on the critical
 * path (the old approach converted the whole WAV after recording, adding
 * ~200 ms and shipping ~125 KB instead of ~10-17 KB).
 *
 * Preferred codec is Opus (audio/opus, OGG container). Some devices — notably
 * the Redmi Note 7 on Android 10 — do not expose an Opus encoder, so we fall
 * back to AAC (audio/mp4a-latm, MP4/M4A container), which is universally
 * available. If neither exists we report [format] = "wav" and the caller keeps
 * the raw PCM path.
 *
 * Usage:
 *   val enc = LiveAudioEncoder(cacheDir, sampleRate = 16000)
 *   enc.start()
 *   ... enc.feed(frame, n) ...   // called from the recording loop, per 20 ms
 *   val out = enc.finish()       // EncodedAudio(bytes, format)
 */
class LiveAudioEncoder(
    private val cacheDir: File,
    private val sampleRate: Int = 16000,
    private val channels: Int = 1,
    private val bitRate: Int = 24000
) {
    companion object {
        private const val TAG = "LiveAudioEncoder"
        private const val TIMEOUT_US = 10_000L
    }

    data class EncodedAudio(val bytes: ByteArray, val format: String)

    private var codec: MediaCodec? = null
    private var muxer: MediaMuxer? = null
    private var trackIndex = -1
    private var muxerStarted = false
    private var outFile: File? = null
    private var presentationUs = 0L
    private var totalSamples = 0L
    private var eosQueued = false

    /** "opus", "aac" or "wav" (unsupported -> caller falls back to raw PCM). */
    var format: String = "wav"
        private set

    private val pcmBytes = ByteBuffer.allocate(0) // placeholder, real buffer per feed

    /** True when a real encoder was configured and [feed] should be called. */
    val isActive: Boolean get() = codec != null

    fun start(): Boolean {
        val (mime, container, ext) = pickCodec()
        if (mime == null) {
            Log.w(TAG, "Nenhum encoder Opus/AAC disponível — usando WAV cru")
            format = "wav"
            return false
        }
        return try {
            val fmt = MediaFormat.createAudioFormat(mime, sampleRate, channels).apply {
                setInteger(MediaFormat.KEY_BIT_RATE, bitRate)
                setInteger(MediaFormat.KEY_MAX_INPUT_SIZE, 16384)
                if (mime == MediaFormat.MIMETYPE_AUDIO_AAC) {
                    setInteger(
                        MediaFormat.KEY_AAC_PROFILE,
                        MediaCodecInfo.CodecProfileLevel.AACObjectLC
                    )
                }
            }
            val c = MediaCodec.createEncoderByType(mime)
            c.configure(fmt, null, null, MediaCodec.CONFIGURE_FLAG_ENCODE)
            c.start()
            codec = c

            val f = File(cacheDir, "utt_${System.currentTimeMillis()}.$ext")
            outFile = f
            muxer = MediaMuxer(f.absolutePath, container)
            format = if (mime == MediaFormat.MIMETYPE_AUDIO_OPUS) "opus" else "aac"
            Log.i(TAG, "Encoder ativo: $mime -> .$ext (format=$format)")
            true
        } catch (t: Throwable) {
            Log.w(TAG, "Falha ao iniciar encoder $mime: ${t.message}")
            releaseQuietly()
            format = "wav"
            false
        }
    }

    private fun pickCodec(): Triple<String?, Int, String> {
        val opus = MediaFormat.MIMETYPE_AUDIO_OPUS
        val aac = MediaFormat.MIMETYPE_AUDIO_AAC
        if (hasEncoder(opus)) {
            // MUXER_OUTPUT_OGG requires API 29; the app targets modern devices.
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
                return Triple(opus, MediaMuxer.OutputFormat.MUXER_OUTPUT_OGG, "ogg")
            }
        }
        if (hasEncoder(aac)) {
            return Triple(aac, MediaMuxer.OutputFormat.MUXER_OUTPUT_MPEG_4, "m4a")
        }
        return Triple(null, 0, "")
    }

    private fun hasEncoder(mime: String): Boolean {
        return try {
            val list = MediaCodecList(MediaCodecList.REGULAR_CODECS)
            list.codecInfos.any { info ->
                info.isEncoder && info.supportedTypes.any { it.equals(mime, ignoreCase = true) }
            }
        } catch (t: Throwable) {
            Log.w(TAG, "MediaCodecList falhou: ${t.message}")
            false
        }
    }

    /** Feed one PCM frame (16-bit mono). Safe to call only when [isActive]. */
    fun feed(samples: ShortArray, length: Int) {
        val c = codec ?: return
        if (length <= 0) return
        try {
            val inIndex = c.dequeueInputBuffer(TIMEOUT_US)
            if (inIndex >= 0) {
                val buf = c.getInputBuffer(inIndex) ?: return
                buf.clear()
                buf.order(ByteOrder.LITTLE_ENDIAN)
                val n = minOf(length, buf.remaining() / 2)
                for (i in 0 until n) buf.putShort(samples[i])
                val ptsUs = presentationUs
                presentationUs += (n.toLong() * 1_000_000L) / sampleRate
                totalSamples += n
                c.queueInputBuffer(inIndex, 0, n * 2, ptsUs, 0)
            }
            drain(false)
        } catch (t: Throwable) {
            Log.w(TAG, "feed falhou: ${t.message}")
        }
    }

    /** Signal end-of-stream, drain everything and return the encoded bytes. */
    fun finish(): EncodedAudio {
        val c = codec
        if (c == null) return EncodedAudio(ByteArray(0), "wav")
        try {
            if (!eosQueued) {
                val inIndex = c.dequeueInputBuffer(TIMEOUT_US * 10)
                if (inIndex >= 0) {
                    val buf = c.getInputBuffer(inIndex)
                    buf?.clear()
                    c.queueInputBuffer(inIndex, 0, 0, presentationUs, MediaCodec.BUFFER_FLAG_END_OF_STREAM)
                    eosQueued = true
                }
            }
            drain(true)
        } catch (t: Throwable) {
            Log.w(TAG, "finish falhou: ${t.message}")
        }
        val bytes = try {
            outFile?.takeIf { it.exists() }?.readBytes() ?: ByteArray(0)
        } catch (t: Throwable) {
            ByteArray(0)
        }
        val fmt = format
        releaseQuietly()
        return EncodedAudio(bytes, fmt)
    }

    private fun drain(untilEos: Boolean) {
        val c = codec ?: return
        val info = MediaCodec.BufferInfo()
        while (true) {
            val outIndex = c.dequeueOutputBuffer(info, if (untilEos) TIMEOUT_US * 10 else 0L)
            when {
                outIndex == MediaCodec.INFO_TRY_AGAIN_LATER -> {
                    if (!untilEos) return
                    // keep waiting for EOS
                }
                outIndex == MediaCodec.INFO_OUTPUT_FORMAT_CHANGED -> {
                    val m = muxer ?: return
                    if (!muxerStarted) {
                        trackIndex = m.addTrack(c.outputFormat)
                        m.start()
                        muxerStarted = true
                    }
                }
                outIndex >= 0 -> {
                    val buf = c.getOutputBuffer(outIndex)
                    val isConfig = (info.flags and MediaCodec.BUFFER_FLAG_CODEC_CONFIG) != 0
                    if (buf != null && info.size > 0 && !isConfig && muxerStarted) {
                        buf.position(info.offset)
                        buf.limit(info.offset + info.size)
                        muxer?.writeSampleData(trackIndex, buf, info)
                    }
                    c.releaseOutputBuffer(outIndex, false)
                    if ((info.flags and MediaCodec.BUFFER_FLAG_END_OF_STREAM) != 0) return
                }
            }
        }
    }

    private fun releaseQuietly() {
        try {
            if (muxerStarted) muxer?.stop()
        } catch (_: Throwable) {
        }
        try {
            muxer?.release()
        } catch (_: Throwable) {
        }
        try {
            codec?.stop()
        } catch (_: Throwable) {
        }
        try {
            codec?.release()
        } catch (_: Throwable) {
        }
        muxer = null
        codec = null
        muxerStarted = false
        trackIndex = -1
    }
}
