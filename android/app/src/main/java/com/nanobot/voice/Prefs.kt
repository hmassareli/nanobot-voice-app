package com.nanobot.voice

import android.content.Context

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

    /** Seconds of follow-up listening after a reply (0 = off). */
    fun followUpSeconds(ctx: Context): Int = get(ctx)
        .getInt(KEY_FOLLOW_UP, DEFAULT_FOLLOW_UP_SECONDS)
        .coerceAtLeast(0)
}
