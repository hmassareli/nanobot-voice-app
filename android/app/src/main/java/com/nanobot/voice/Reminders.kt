package com.nanobot.voice

import android.app.AlarmManager
import android.app.PendingIntent
import android.content.Context
import android.content.Intent
import android.util.Log
import org.json.JSONArray
import org.json.JSONObject
import java.io.File

/** Um lembrete agendado (cache local do que o servidor tem pendente). */
data class Reminder(
    val id: String,
    val text: String,
    val fireAtMs: Long,
    val delivery: String
)

/**
 * Cache local + agendamento dos lembretes.
 *
 * Estratégia de sync barata (sem WebSocket): o app busca a lista pendente no
 * servidor (a cada hora, no boot e depois de cada conversa) e agenda cada item
 * no AlarmManager. O disparo é 100% local — o AlarmManager acorda o app na
 * hora exata mesmo em Doze — então não há custo de conexão permanente.
 */
object Reminders {
    private const val TAG = "Reminders"
    private const val FILE = "reminders_local.json"

    fun load(ctx: Context): List<Reminder> {
        return try {
            parseArray(JSONArray(File(ctx.filesDir, FILE).readText()))
        } catch (t: Throwable) {
            emptyList()
        }
    }

    fun save(ctx: Context, items: List<Reminder>) {
        try {
            val arr = JSONArray()
            items.forEach { r ->
                arr.put(JSONObject().apply {
                    put("id", r.id)
                    put("texto", r.text)
                    put("fire_at_ms", r.fireAtMs)
                    put("delivery", r.delivery)
                })
            }
            File(ctx.filesDir, FILE).writeText(arr.toString())
        } catch (t: Throwable) {
            Log.w(TAG, "falha ao salvar cache local: ${t.message}")
        }
    }

    /** Busca os pendentes no servidor, guarda local e reagenda os alarmes. */
    fun sync(ctx: Context, client: NanobotClient): List<Reminder> {
        val remote = try {
            parseArray(client.fetchReminders())
        } catch (t: Throwable) {
            Log.w(TAG, "sync falhou (${t.message}); mantendo cache local")
            return load(ctx)
        }
        save(ctx, remote)
        scheduleAll(ctx)
        Log.i(TAG, "sync ok: ${remote.size} lembrete(s) pendente(s)")
        return remote
    }

    /** Sync em background, nunca lança exceção para o chamador. */
    fun syncAsync(ctx: Context) {
        val app = ctx.applicationContext
        Thread {
            try {
                sync(app, NanobotClient(
                    Prefs.serverUrl(app), Prefs.token(app), clientId = Prefs.clientId(app)))
            } catch (t: Throwable) {
                Log.w(TAG, "syncAsync: ${t.message}")
            }
        }.apply { isDaemon = true }.start()
    }

    /** Marca como disparado (cache local + fire-and-forget pro servidor). */
    fun markFiredAsync(ctx: Context, id: String) {
        val app = ctx.applicationContext
        save(app, load(app).filter { it.id != id })
        Thread {
            try {
                NanobotClient(Prefs.serverUrl(app), Prefs.token(app), clientId = Prefs.clientId(app))
                    .markReminderFired(id)
            } catch (_: Throwable) {
            }
        }.apply { isDaemon = true }.start()
    }

    private fun parseArray(arr: JSONArray): List<Reminder> {
        return (0 until arr.length()).mapNotNull { i ->
            val o = arr.optJSONObject(i) ?: return@mapNotNull null
            Reminder(
                id = o.optString("id"),
                text = o.optString("texto").ifBlank { o.optString("text") },
                fireAtMs = o.optLong("fire_at_ms"),
                delivery = o.optString("delivery", "auto")
            )
        }.filter { it.id.isNotBlank() && it.fireAtMs > 0 }
    }

    /** (Re)agenda no AlarmManager todos os lembretes futuros do cache. */
    fun scheduleAll(ctx: Context) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val now = System.currentTimeMillis()
        load(ctx).filter { it.fireAtMs > now }.forEach { r ->
            val pi = pendingFor(ctx, r)
            try {
                // setAlarmClock: o mais confiável (ignora Doze, mostra o ícone
                // de alarme). Exige SCHEDULE_EXACT_ALARM.
                am.setAlarmClock(AlarmManager.AlarmClockInfo(r.fireAtMs, pi), pi)
            } catch (sec: SecurityException) {
                try {
                    am.setExactAndAllowWhileIdle(AlarmManager.RTC_WAKEUP, r.fireAtMs, pi)
                } catch (t: Throwable) {
                    Log.w(TAG, "alarme inexato para ${r.id}: ${t.message}")
                    am.set(AlarmManager.RTC_WAKEUP, r.fireAtMs, pi)
                }
            }
        }
    }

    fun cancelAll(ctx: Context) {
        val am = ctx.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        load(ctx).forEach { r -> am.cancel(pendingFor(ctx, r)) }
    }

    private fun pendingFor(ctx: Context, r: Reminder): PendingIntent {
        val i = Intent(ctx, ReminderReceiver::class.java)
            .setAction(ReminderReceiver.ACTION_REMINDER)
            .putExtra("id", r.id)
            .putExtra("text", r.text)
            .putExtra("delivery", r.delivery)
        return PendingIntent.getBroadcast(
            ctx, 7100 + (r.id.hashCode() and 0x0FFF), i,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
    }
}
