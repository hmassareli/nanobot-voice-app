package com.nanobot.voice

import android.Manifest
import android.app.Activity
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Color
import android.graphics.Typeface
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.widget.ArrayAdapter
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.Spinner
import android.widget.TextView
import android.widget.Toast
import androidx.core.app.ActivityCompat
import androidx.core.content.ContextCompat

/**
 * Settings screen for the Nanobot voice assistant.
 *
 * Everything is built programmatically (no XML layouts) so the whole UI lives in
 * one file. It lets the user configure the backend, pick a TTS voice, set the
 * wake word / LLM model, start or stop the foreground service, fire a manual
 * "speak now" trigger and open the battery-optimisation / autostart screens that
 * Chinese OEM ROMs (MIUI, etc.) hide by default.
 */
class MainActivity : Activity() {

    private lateinit var serverUrl: EditText
    private lateinit var token: EditText
    private lateinit var voiceSpinner: Spinner
    private lateinit var wakeWord: EditText
    private lateinit var llmModel: EditText
    private lateinit var status: TextView
    private lateinit var toggleButton: Button

    private val permRequestCode = 1001

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(buildUi())
        loadPrefs()
        requestRuntimePermissions()
    }

    override fun onResume() {
        super.onResume()
        refreshToggleLabel()
    }

    // ---------------------------------------------------------------- UI build

    private fun buildUi(): View {
        val pad = dp(20)
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            setBackgroundColor(Color.parseColor("#0B1020"))
        }

        root.addView(title("Nanobot Voice"))
        root.addView(subtitle("Assistente de voz com wake word"))

        serverUrl = field("URL do servidor", Prefs.DEFAULT_SERVER_URL, InputType.TYPE_TEXT_VARIATION_URI)
        root.addView(label("URL do servidor"))
        root.addView(serverUrl)

        token = field("Token", Prefs.DEFAULT_TOKEN, InputType.TYPE_CLASS_TEXT)
        root.addView(label("Token"))
        root.addView(token)

        root.addView(label("Voz TTS"))
        voiceSpinner = Spinner(this).apply {
            adapter = ArrayAdapter(
                this@MainActivity,
                android.R.layout.simple_spinner_dropdown_item,
                Prefs.VOICE_LABELS
            )
        }
        root.addView(voiceSpinner)

        wakeWord = field("Frase da wake word", Prefs.DEFAULT_WAKE_WORD, InputType.TYPE_CLASS_TEXT)
        root.addView(label("Frase da wake word"))
        root.addView(wakeWord)

        llmModel = field("Modelo LLM", Prefs.DEFAULT_LLM_MODEL, InputType.TYPE_CLASS_TEXT)
        root.addView(label("Modelo LLM"))
        root.addView(llmModel)

        root.addView(space(8))

        toggleButton = button("Ligar serviço") { toggleService() }
        root.addView(toggleButton)

        root.addView(button("Testar (falar agora)") { triggerNow() })
        root.addView(button("Pedir isenção de bateria") { requestBatteryExemption() })
        root.addView(button("Abrir Autostart") { openAutostart() })
        root.addView(button("Salvar") { savePrefs() })

        status = TextView(this).apply {
            setTextColor(Color.parseColor("#A5B4FC"))
            textSize = 13f
            setPadding(0, dp(16), 0, 0)
        }
        root.addView(status)

        val scroll = ScrollView(this).apply {
            addView(root, ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT)
            setBackgroundColor(Color.parseColor("#0B1020"))
        }
        return scroll
    }

    private fun title(t: String) = TextView(this).apply {
        text = t
        setTextColor(Color.WHITE)
        textSize = 24f
        setTypeface(typeface, Typeface.BOLD)
    }

    private fun subtitle(t: String) = TextView(this).apply {
        text = t
        setTextColor(Color.parseColor("#94A3B8"))
        textSize = 14f
        setPadding(0, dp(4), 0, dp(16))
    }

    private fun label(t: String) = TextView(this).apply {
        text = t
        setTextColor(Color.parseColor("#CBD5E1"))
        textSize = 13f
        setPadding(0, dp(12), 0, dp(4))
    }

    private fun field(hint: String, def: String, inputType: Int) = EditText(this).apply {
        this.hint = hint
        setText(def)
        this.inputType = inputType
        setTextColor(Color.WHITE)
        setHintTextColor(Color.parseColor("#64748B"))
        setBackgroundColor(Color.parseColor("#1E293B"))
        setPadding(dp(12), dp(12), dp(12), dp(12))
    }

    private fun button(text: String, onClick: () -> Unit) = Button(this).apply {
        this.text = text
        setOnClickListener { onClick() }
        val lp = LinearLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT,
            ViewGroup.LayoutParams.WRAP_CONTENT
        )
        lp.topMargin = dp(8)
        layoutParams = lp
    }

    private fun space(h: Int) = View(this).apply {
        layoutParams = LinearLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT, dp(h))
    }

    private fun dp(v: Int): Int = (v * resources.displayMetrics.density).toInt()

    // ------------------------------------------------------------- preferences

    private fun loadPrefs() {
        serverUrl.setText(Prefs.serverUrl(this))
        token.setText(Prefs.token(this))
        wakeWord.setText(Prefs.wakeWord(this))
        llmModel.setText(Prefs.llmModel(this))
        val idx = Prefs.VOICE_KEYS.indexOf(Prefs.voice(this))
        if (idx >= 0) voiceSpinner.setSelection(idx)
    }

    private fun savePrefs() {
        val voiceKey = Prefs.VOICE_KEYS.getOrElse(voiceSpinner.selectedItemPosition) { Prefs.DEFAULT_VOICE }
        Prefs.get(this).edit()
            .putString(Prefs.KEY_SERVER_URL, serverUrl.text.toString().trim())
            .putString(Prefs.KEY_TOKEN, token.text.toString().trim())
            .putString(Prefs.KEY_VOICE, voiceKey)
            .putString(Prefs.KEY_WAKE_WORD, wakeWord.text.toString().trim())
            .putString(Prefs.KEY_LLM_MODEL, llmModel.text.toString().trim())
            .apply()
        toast("Configurações salvas")
        setStatus("Configurações salvas.")
    }

    // ---------------------------------------------------------------- service

    private fun toggleService() {
        if (Prefs.enabled(this)) {
            VoiceService.stop(this)
            Prefs.get(this).edit().putBoolean(Prefs.KEY_ENABLED, false).apply()
            setStatus("Serviço desligado.")
        } else {
            savePrefs()
            Prefs.get(this).edit().putBoolean(Prefs.KEY_ENABLED, true).apply()
            try {
                VoiceService.start(this)
                setStatus("Serviço ligado — ouvindo a wake word.")
            } catch (t: Throwable) {
                setStatus("Falha ao ligar: ${t.message}")
            }
        }
        refreshToggleLabel()
    }

    private fun refreshToggleLabel() {
        toggleButton.text = if (Prefs.enabled(this)) "Desligar serviço" else "Ligar serviço"
    }

    private fun triggerNow() {
        savePrefs()
        try {
            VoiceService.trigger(this)
            setStatus("Fale agora…")
        } catch (t: Throwable) {
            setStatus("Falha no teste: ${t.message}")
        }
    }

    // ------------------------------------------------------------- permissions

    private fun requestRuntimePermissions() {
        val needed = mutableListOf<String>()
        if (ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO)
            != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.RECORD_AUDIO)
        }
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU &&
            ContextCompat.checkSelfPermission(this, Manifest.permission.POST_NOTIFICATIONS)
            != PackageManager.PERMISSION_GRANTED
        ) {
            needed.add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (needed.isNotEmpty()) {
            ActivityCompat.requestPermissions(this, needed.toTypedArray(), permRequestCode)
        }
    }

    override fun onRequestPermissionsResult(
        requestCode: Int,
        permissions: Array<out String>,
        grantResults: IntArray
    ) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        if (requestCode == permRequestCode) {
            val micOk = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
                PackageManager.PERMISSION_GRANTED
            setStatus(if (micOk) "Permissões concedidas." else "Permissão de microfone negada.")
        }
    }

    // ------------------------------------------------------- battery / autostart

    private fun requestBatteryExemption() {
        try {
            val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
            if (pm.isIgnoringBatteryOptimizations(packageName)) {
                toast("Isenção de bateria já concedida")
                return
            }
            @Suppress("BatteryLife")
            val i = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
                data = Uri.parse("package:$packageName")
            }
            startActivity(i)
        } catch (t: Throwable) {
            try {
                startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
            } catch (t2: Throwable) {
                toast("Não foi possível abrir as configurações de bateria")
            }
        }
    }

    private fun openAutostart() {
        // Common MIUI / OEM autostart activities, tried in order.
        val candidates = listOf(
            "com.miui.securitycenter" to "com.miui.permcenter.autostart.AutoStartManagementActivity",
            "com.letv.android.letvsafe" to "com.letv.android.letvsafe.AutobootManageActivity",
            "com.huawei.systemmanager" to "com.huawei.systemmanager.startupmgr.ui.StartupNormalAppListActivity",
            "com.coloros.safecenter" to "com.coloros.safecenter.permission.startup.StartupAppListActivity",
            "com.vivo.permissionmanager" to "com.vivo.permissionmanager.activity.BgStartUpManagerActivity",
            "com.samsung.android.lool" to "com.samsung.android.sm.ui.battery.BatteryActivity"
        )
        for ((pkg, cls) in candidates) {
            try {
                startActivity(Intent().apply {
                    setClassName(pkg, cls)
                    addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                })
                return
            } catch (_: Throwable) {
                // try next
            }
        }
        // Fallback: open the app's own details page.
        try {
            startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS).apply {
                data = Uri.parse("package:$packageName")
            })
        } catch (_: Throwable) {
            toast("Não foi possível abrir o autostart neste aparelho")
        }
    }

    // ----------------------------------------------------------------- helpers

    private fun setStatus(s: String) {
        status.text = s
    }

    private fun toast(s: String) {
        Toast.makeText(this, s, Toast.LENGTH_SHORT).show()
    }
}
