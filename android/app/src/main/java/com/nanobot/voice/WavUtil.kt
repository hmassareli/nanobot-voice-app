package com.nanobot.voice

import java.io.ByteArrayOutputStream

/** Minimal helper to wrap raw 16-bit PCM samples into a WAV container. */
object WavUtil {

    fun encode(samples: ShortArray, sampleRate: Int): ByteArray {
        val dataLen = samples.size * 2
        val out = ByteArrayOutputStream(44 + dataLen)
        fun writeStr(s: String) = out.write(s.toByteArray(Charsets.US_ASCII))
        fun writeInt(v: Int) {
            out.write(v and 0xff)
            out.write((v shr 8) and 0xff)
            out.write((v shr 16) and 0xff)
            out.write((v shr 24) and 0xff)
        }
        fun writeShort(v: Int) {
            out.write(v and 0xff)
            out.write((v shr 8) and 0xff)
        }
        writeStr("RIFF")
        writeInt(36 + dataLen)
        writeStr("WAVE")
        writeStr("fmt ")
        writeInt(16)              // subchunk1 size
        writeShort(1)             // PCM
        writeShort(1)             // mono
        writeInt(sampleRate)
        writeInt(sampleRate * 2)  // byte rate
        writeShort(2)             // block align
        writeShort(16)            // bits per sample
        writeStr("data")
        writeInt(dataLen)
        for (s in samples) {
            out.write(s.toInt() and 0xff)
            out.write((s.toInt() shr 8) and 0xff)
        }
        return out.toByteArray()
    }
}
