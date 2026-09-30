package com.nanobot.voice

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/** Restarts the assistant automatically after the device boots. */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        val action = intent.action
        if (action == Intent.ACTION_BOOT_COMPLETED || action == "android.intent.action.QUICKBOOT_POWERON") {
            if (Prefs.enabled(context)) {
                Log.i(VoiceService.TAG, "Boot recebido — iniciando assistente")
                try {
                    VoiceService.start(context.applicationContext)
                } catch (t: Throwable) {
                    Log.e(VoiceService.TAG, "Falha ao iniciar no boot", t)
                }
                try {
                    // Alarmes nao sobrevivem ao reboot: reagendar + sync.
                    ReminderReceiver.scheduleSync(context.applicationContext)
                    Reminders.syncAsync(context.applicationContext)
                } catch (t: Throwable) {
                    Log.e(VoiceService.TAG, "Falha ao reagendar lembretes", t)
                }
            }
        }
    }
}
