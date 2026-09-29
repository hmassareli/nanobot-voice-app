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

    // Matches README.md ("Confira a URL do servidor e o token (já vêm
    // preenchidos)"), so a fresh install works without manual setup.
    const val DEFAULT_SERVER_URL = "https://nanobot-voice-api.lnyx9r.easypanel.host"
    const val DEFAULT_TOKEN = "nanobot-voice"
    const val DEFAULT_VOICE = "santa"
    const val DEFAULT_WAKE_WORD = "HEY NANOBOT"
    const val DEFAULT_LLM_MODEL = "gpt-4o-mini"

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
        "gpt-4o-mini",
        "gpt-4o",
        "llama3.1:8b",
        "qwen2.5:7b",
        "gemini-1.5-flash",
        "claude-3-5-sonnet"
    )

    fun get(ctx: Context) = ctx.getSharedPreferences(FILE, Context.MODE_PRIVATE)

    fun serverUrl(ctx: Context) = get(ctx).getString(KEY_SERVER_URL, DEFAULT_SERVER_URL) ?: DEFAULT_SERVER_URL
    fun token(ctx: Context) = get(ctx).getString(KEY_TOKEN, DEFAULT_TOKEN) ?: DEFAULT_TOKEN
    fun voice(ctx: Context) = get(ctx).getString(KEY_VOICE, DEFAULT_VOICE) ?: DEFAULT_VOICE
    fun wakeWord(ctx: Context) = get(ctx).getString(KEY_WAKE_WORD, DEFAULT_WAKE_WORD) ?: DEFAULT_WAKE_WORD
    fun llmModel(ctx: Context) = get(ctx).getString(KEY_LLM_MODEL, DEFAULT_LLM_MODEL) ?: DEFAULT_LLM_MODEL
    fun enabled(ctx: Context) = get(ctx).getBoolean(KEY_ENABLED, false)
}
