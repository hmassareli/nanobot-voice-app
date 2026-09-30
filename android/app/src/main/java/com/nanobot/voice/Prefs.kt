package com.nanobot.voice

import android.content.Context
import android.provider.Settings
import java.util.UUID

/**
 * Central definition of the persisted configuration plus the pick-lists used by
 * the settings screen. Everything is stored in a single SharedPreferences file.
 */
object Prefs {
    const val FILE = "nanobot_voice_prefs"

    const val KEY_SERVER_URL = "server_url"
    const val KEY_TOKEN = "token"
    const val KEY_VOICE = "voice"
    const val KEY_WAKE_WORD = "wake_word"
    const val KEY_LLM_MODEL = "llm_model"
    const val KEY_ENABLED = "service_enabled"
    const val KEY_FOLLOW_UP = "follow_up_seconds"
    const val KEY_STT_MODE = "stt_mode"
    const val KEY_REMINDER_MODE = "reminder_mode"
    const val KEY_HOME_SSID = "home_ssid"

    /** Como entregar lembretes: voz em casa, sempre voz, só popup ou off. */
    const val REMINDER_AUTO = "auto"
    const val REMINDER_VOICE = "voice"
    const val REMINDER_POPUP = "popup"
    const val REMINDER_OFF = "off"
    const val DEFAULT_REMINDER_MODE = REMINDER_AUTO

    val REMINDER_MODE_LABELS = listOf(
        "Voz quando em casa (padr\u00e3o)",
        "Sempre por voz",
        "S\u00f3 notifica\u00e7\u00e3o",
        "Desligado"
    )
    val REMINDER_MODE_VALUES = listOf(
        REMINDER_AUTO, REMINDER_VOICE, REMINDER_POPUP, REMINDER_OFF)

    /** Persisted stable id sent to the backend as the `X-Client-Id` header. */
    const val KEY_CLIENT_ID = "client_id"

    // Matches README.md ("Confira a URL do servidor e o token (já vêm
    // preenchidos)"), so a fresh install works without manual setup.
    const val DEFAULT_SERVER_URL = "https://nanobot-voice-api.lnyx9r.easypanel.host"
    const val DEFAULT_TOKEN = "nanobot-voice"
    const val DEFAULT_VOICE = "santa"
    const val DEFAULT_WAKE_WORD = "HEY NANOBOT"
    const val DEFAULT_LLM_MODEL = "deepseek/deepseek-v4.1-flash"

    /**
     * Seconds the assistant keeps listening for a follow-up question right
     * after answering, so a back-and-forth conversation does not require
     * repeating the wake word every time. 0 disables the follow-up window.
     */
    const val DEFAULT_FOLLOW_UP_SECONDS = 8

    /**
     * Speech-to-text strategy:
     *  - "streaming": transcribe on-device while the user speaks (SpeechRecognizer)
     *    and send only the text to the server. Removes the ~1-2s STT leg.
     *  - "audio": record the utterance and upload it; the server runs Whisper.
     *    Kept as a fallback for devices without an on-device recognizer.
     */
    const val STT_MODE_STREAMING = "streaming"
    const val STT_MODE_AUDIO = "audio"
    const val DEFAULT_STT_MODE = STT_MODE_STREAMING

    /** Pick-list for the "reconhecimento de fala" spinner: label -> mode. */
    val STT_MODE_LABELS = listOf(
        "No aparelho (rápido, streaming)",
        "No servidor (Whisper, upload de áudio)"
    )
    val STT_MODE_VALUES = listOf(STT_MODE_STREAMING, STT_MODE_AUDIO)

    /** Pick-list for the "seguir ouvindo" spinner: label -> seconds. */
    val FOLLOW_UP_LABELS = listOf(
        "Desligado (repetir wake word)",
        "5 segundos",
        "8 segundos",
        "12 segundos",
        "20 segundos"
    )
    val FOLLOW_UP_VALUES = listOf(0, 5, 8, 12, 20)

    /** Selectable voices. The value sent to the server is the voice *key*
     *  (see VOICE_KEYS / the server's /voices endpoint); the label is what the
     *  user sees in the spinner. */
    val VOICE_LABELS = listOf(
        "Santa (masculina, calma)",
        "Dora (feminina)",
        "Alex (masculina)",
        "Antonio (edge, grátis)",
        "Francisca (edge, grátis)",
        "Thalita (edge, grátis)"
    )
    val VOICE_KEYS = listOf(
        "santa",
        "dora",
        "alex",
        "antonio",
        "francisca",
        "thalita"
    )

    /** Back-compat alias used elsewhere; kept in sync with VOICE_LABELS. */
    val VOICES = VOICE_LABELS

    val LLM_SUGGESTIONS = listOf(
        "deepseek/deepseek-v4.1-flash",
        "openai/gpt-4o-mini",
        "openai/gpt-4o",
        "google/gemini-2.5-flash",
        "anthropic/claude-sonnet-4",
        "meta-llama/llama-3.1-8b-instruct",
        "qwen/qwen-2.5-7b-instruct"
    )

    fun get(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun serverUrl(ctx: Context) = get(ctx).getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
    fun token(ctx: Context) = get(ctx).getString(KEY_TOKEN, DEFAULT_TOKEN) ?: DEFAULT_TOKEN
    fun voice(ctx: Context) = get(ctx).getString(KEY_VOICE, DEFAULT_VOICE) ?: DEFAULT_VOICE
    fun wakeWord(ctx: Context) = get(ctx).getString(KEY_WAKE_WORD, DEFAULT_WAKE_WORD) ?: DEFAULT_WAKE_WORD
    fun llmModel(ctx: Context) = get(ctx).getString(KEY_LLM_MODEL, DEFAULT_LLM_MODEL) ?: DEFAULT_LLM_MODEL
    fun enabled(ctx: Context) = get(ctx).getBoolean(KEY_ENABLED, false)

    fun reminderMode(ctx: Context): String =
        get(ctx).getString(KEY_REMINDER_MODE, DEFAULT_REMINDER_MODE)
            ?: DEFAULT_REMINDER_MODE

    /** SSID do Wi-Fi de casa; quando bate, lembretes sao falados em voz. */
    fun homeSsid(ctx: Context): String =
        get(ctx).getString(KEY_HOME_SSID, "") ?: ""

    /** Seconds of follow-up listening after a reply (0 = off). */
    fun followUpSeconds(ctx: Context): Int = get(ctx)
        .getInt(KEY_FOLLOW_UP, DEFAULT_FOLLOW_UP_SECONDS)
        .coerceAtLeast(0)

    /** STT strategy: "streaming" (on-device) or "audio" (server Whisper). */
    fun sttMode(ctx: Context): String =
        get(ctx).getString(KEY_STT_MODE, DEFAULT_STT_MODE) ?: DEFAULT_STT_MODE

    /**
     * Stable, device-unique client id used for telemetry (the backend groups
     * turns by it). We persist a random UUID on first use so it survives
     * reboots/reinstalls only if the prefs file survives; if the prefs are
     * missing (fresh install, cleared data) we fall back to the Android
     * `ANDROID_ID`, which is stable per app signature + user. Never throws.
     */
    fun clientId(ctx: Context): String {
        try {
            val prefs = get(ctx)
            prefs.getString(KEY_CLIENT_ID, null)?.takeIf { it.isNotBlank() }?.let { return it }
            val id = try {
                Settings.Secure.getString(ctx.contentResolver, Settings.Secure.ANDROID_ID)
                    ?.takeIf { it.isNotBlank() }
            } catch (_: Throwable) {
                null
            } ?: UUID.randomUUID().toString()
            prefs.edit().putString(KEY_CLIENT_ID, id).apply()
            return id
        } catch (_: Throwable) {
            // Absolutely last resort: a per-process random id.
            return UUID.randomUUID().toString()
        }
    }
}
