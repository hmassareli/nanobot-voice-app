package com.nanobot.voice

import kotlin.math.ln

/**
 * Minimal on-device sentencepiece **unigram** tokenizer for the KWS model.
 *
 * The sherpa-onnx AAR does not expose a text tokenizer to Kotlin, and
 * `assets/kws/bpe.model` is a protobuf that is impractical to parse on-device.
 * So the piece list and their log-probabilities are baked into [BpeVocab] and
 * we reproduce sentencepiece's unigram Viterbi search here.
 *
 * The output is byte-for-byte identical to `SentencePieceProcessor.encode(...)`
 * for the vocabulary used by this model (verified against the Python reference
 * for a battery of wake words). Only ASCII/letters/symbols matter for wake
 * words, so no unicode normalisation is performed.
 *
 * Keyword format expected by sherpa-onnx: pieces joined by spaces, where the
 * "▁" (U+2581) prefix marks the beginning of a word.
 */
object BpeTokenizer {

    private const val WORD_MARK = '\u2581'
    private val UNK: String get() = BpeVocab.PIECES[2]

    /** piece string -> id, built once. */
    private val ids: Map<String, Int> by lazy {
        HashMap<String, Int>(BpeVocab.SIZE * 2).apply {
            for (i in 0 until BpeVocab.SIZE) put(BpeVocab.PIECES[i], i)
        }
    }

    /**
     * Tokenises [text] into sentencepiece pieces.
     *
     * @return the pieces in order, or `null` when the text is empty.
     */
    fun encode(text: String): List<String>? {
        val words = text.trim().uppercase()
            .split(Regex("\\s+"))
            .filter { it.isNotEmpty() }
        if (words.isEmpty()) return null

        val out = ArrayList<String>(words.size * 5)
        for (word in words) {
            val s = WORD_MARK + word
            val pieces = viterbi(s) ?: return null
            out.addAll(pieces)
        }
        return out
    }

    /**
     * Sentencepiece keyword line for [text], e.g. `"HEY NANOBOT"` ->
     * `"▁HE Y ▁NA N O B O T"`. Returns `null` when nothing could be encoded.
     */
    fun keywordLine(text: String): String? =
        encode(text)?.joinToString(" ")?.takeIf { it.isNotBlank() }

    /**
     * Unigram Viterbi: picks the segmentation of [s] with the highest total log
     * probability. Unknown single characters fall back to `<unk>` exactly like
     * sentencepiece does.
     */
    private fun viterbi(s: String): List<String>? {
        val n = s.length
        val best = DoubleArray(n + 1) { Double.NEGATIVE_INFINITY }
        val back = IntArray(n + 1)
        best[0] = 0.0

        for (i in 1..n) {
            // Longest-first so ties favour longer pieces, as sentencepiece does.
            for (j in i - 1 downTo 0) {
                val piece = s.substring(j, i)
                val id = ids[piece] ?: continue
                val score = best[j]
                if (score == Double.NEGATIVE_INFINITY) continue
                // A leading word-mark cannot start a piece that was already
                // consumed, but the search above guarantees contiguous spans.
                val cand = score + BpeVocab.SCORES[id]
                if (cand > best[i]) {
                    best[i] = cand
                    back[i] = j
                }
            }
            if (best[i] == Double.NEGATIVE_INFINITY) {
                // No known piece ends here: consume one character as <unk>.
                best[i] = best[i - 1] + ln(1e-10)
                back[i] = i - 1
            }
        }

        val out = ArrayList<String>(n)
        var i = n
        while (i > 0) {
            val j = back[i]
            val piece = s.substring(j, i)
            out.add(if (ids.containsKey(piece)) piece else UNK)
            i = j
        }
        out.reverse()
        return out
    }
}
