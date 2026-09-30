package com.nanobot.voice

import android.app.AlarmManager
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.wifi.WifiManager
import android.os.Build
import android.util.Log
import androidx.core.app.NotificationCompat

/**
 * Recebe o disparo do AlarmManager (lembrete) e o sync periódico.
 *
 * Entrega do lembrete:
 *  - modo "voice"  -> sempre fala (via VoiceService, que depois fica te ouvindo);
 *  - modo "popup"  -> só notificação;
 *  - modo "off"    -> descarta;
 *  - modo "auto"   -> fala se estiver em casa (SSID do Wi-Fi = configurado),
 *                     senão notificação.
 */
class ReminderReceiver : BroadcastReceiver() {

    companion object {
        const val TAG = "ReminderReceiver"
        const val ACTION_REMINDER = "com.nanobot.voice.REMINDER"
        const val ACTION_SYNC = "com.nanobot.voice.SYNC"
        const val CHANNEL_ID = "nanobot_reminders_channel"
        const val NOTIF_ID = 9000
        private const val SYNC_REQ = 7200

        /** Sync periódico dos lembretes (a cada ~1h). Reagendar no boot. */
        fun scheduleSync(ctx: Context) {
            try {
                val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
                val pi = PendingIntent.getBroadcast(
                    ctx, SYNC_REQ,
                    Intent(ctx, ReminderReceiver::class.java).setAction(ACTION_SYNC),
                    PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
                am.setInexactRepeating(
                    AlarmManager.RTC_WAKEUP,
                    System.currentTimeMillis() + 15 * 60_000L,
                    AlarmManager.INTERVAL_HOUR, pi)
            } catch (t: Throwable) {
                Log.w(TAG, "scheduleSync: ${t.message}")
            }
        }

        /** "Popup": notificação alta com o texto do lembrete. */
        fun notifyPopup(ctx: Context, text: String) {
            try {
                val nm = ctx.getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
                if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                    nm.createNotificationChannel(NotificationChannel(
                        CHANNEL_ID, "Lembretes", NotificationManager.IMPORTANCE_HIGH))
                }
                val open = PendingIntent.getActivity(
                    ctx, 0, Intent(ctx, MainActivity::class.java),
                    PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
                nm.notify(NOTIF_ID, NotificationCompat.Builder(ctx, CHANNEL_ID)
                    .setSmallIcon(android.R.drawable.ic_popup_reminder)
                    .setContentTitle("Lembrete")
                    .setContentText(text)
                    .setStyle(NotificationCompat.BigTextStyle().bigText(text))
                    .setAutoCancel(true)
                    .setContentIntent(open)
                    .build())
            } catch (t: Throwable) {
                Log.e(TAG, "notifyPopup: ${t.message}")
            }
        }

        /** Decide se o lembrete deve ser falado em voz alta. */
        fun shouldSpeak(ctx: Context, deliveryHint: String): Boolean {
            if (!Prefs.enabled(ctx)) return false
            return when (Prefs.reminderMode(ctx)) {
                Prefs.REMINDER_VOICE -> true
                Prefs.REMINDER_POPUP, Prefs.REMINDER_OFF -> false
                else -> when ((deliveryHint.ifBlank { "auto" }).lowercase()) {
                    "voice" -> true
                    "popup" -> false
                    // Padrao: fala SEMPRE em voz alta (antes dependia do Wi-Fi de
                    // casa, o que fazia o lembrete virar so notificacao silenciosa
                    // quando o usuario estava em dado movel ou outro Wi-Fi).
                    else -> true
                }
            }
        }

        /** Em casa = SSID do Wi-Fi bate com o configurado (fallback: é Wi-Fi). */
        fun isAtHome(ctx: Context): Boolean {
            val home = Prefs.homeSsid(ctx)
            val ssid = currentSsid(ctx)
            if (home.isNotBlank() && ssid.isNotBlank()) {
                return ssid.equals(home, ignoreCase = true)
            }
            return networkIsWifi(ctx)
        }

        private fun currentSsid(ctx: Context): String {
            return try {
                val wm = ctx.applicationContext
                    .getSystemService(Context.WIFI_SERVICE) as WifiManager
                @Suppress("DEPRECATION")
                (wm.connectionInfo?.ssid ?: "").replace("\"", "").trim()
            } catch (_: Throwable) {
                ""
            }
        }

        private fun networkIsWifi(ctx: Context): Boolean {
            return try {
                val cm = ctx.getSystemService(Context.CONNECTIVITY_SERVICE)
                    as android.net.ConnectivityManager
                val caps = cm.activeNetwork?.let { cm.getNetworkCapabilities(it) }
                    ?: return false
                caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_WIFI)
            } catch (_: Throwable) {
                false
            }
        }
    }

    override fun onReceive(ctx: Context, intent: Intent) {
        when (intent.action) {
            ACTION_SYNC -> Reminders.syncAsync(ctx.applicationContext)
            ACTION_REMINDER -> {
                val id = intent.getStringExtra("id") ?: return
                val text = intent.getStringExtra("text") ?: return
                val delivery = intent.getStringExtra("delivery") ?: "auto"
                if (Prefs.reminderMode(ctx) == Prefs.REMINDER_OFF) {
                    Log.i(TAG, "lembrete $id descartado (modo off)")
                    Reminders.markFiredAsync(ctx.applicationContext, id)
                    return
                }
                Log.i(TAG, "lembrete $id delivery=$delivery")
                if (shouldSpeak(ctx, delivery)) {
                    VoiceService.speakReminder(ctx.applicationContext, id, text)
                } else {
                    notifyPopup(ctx, text)
                    Reminders.markFiredAsync(ctx.applicationContext, id)
                }
            }
        }
    }
}
