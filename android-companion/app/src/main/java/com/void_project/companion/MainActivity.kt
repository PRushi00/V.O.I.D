package com.void_project.companion

/**
 * V.O.I.D companion - minimal Android proof-of-concept client for the V1
 * Device Gateway (see void/device/ on the laptop side).
 *
 * WHAT THIS IS: the smallest thing that proves a phone can pair with, and
 * send one authorized request to, the V.O.I.D runtime on a laptop over a
 * phone hotspot - not a polished app. One screen, three actions: pair, get
 * status, launch an app on the laptop. No third-party dependency: only
 * standard Android/Java APIs (javax.net.ssl, javax.crypto, org.json),
 * deliberately, so the whole client is a single file that is easy to read
 * end-to-end and easy to audit.
 *
 * NOT BUILT OR RUN BY THE AI THAT WROTE THIS: this sandbox has no Android
 * SDK, no emulator, and no attached device. This file has been reviewed by
 * hand against the Android/Java standard library documentation, and its
 * request-signing logic exactly mirrors void/device/auth.py's HMAC-SHA256
 * scheme (which IS covered by real, passing, automated tests on the laptop
 * side - see tests/test_device_auth.py, tests/test_device_gateway.py). See
 * android-companion/README.md for exactly what could and could not be
 * validated, and the manual steps to validate this file for real in Android
 * Studio against a running `python -m void device serve`.
 *
 * SECURITY MODEL (matches the laptop side exactly):
 *  - TLS, but the server certificate is SELF-SIGNED (there is no CA for a
 *    LAN/hotspot IP) - so instead of CA validation, this client PINS the
 *    server's SHA-256 fingerprint, typed in by the user during pairing (read
 *    off the laptop's `device pair-start` output). A wrong/absent fingerprint
 *    match aborts the connection. This is a deliberate substitution for CA
 *    validation, not a disabled check.
 *  - After pairing, every request is additionally signed with HMAC-SHA256
 *    using the per-device shared secret issued at pairing time - proves
 *    which device sent this exact request body, independent of TLS.
 *  - The shared secret is stored in this app's private SharedPreferences
 *    (MODE_PRIVATE, sandboxed by Android to this app's UID). KNOWN V1
 *    LIMITATION (see README "Deferred to V2"): hardening this to
 *    Android-Keystore-backed encryption (e.g. EncryptedSharedPreferences)
 *    was deliberately deferred rather than shipped untested.
 *
 * DEVICE IDENTITY IS NOT THE NETWORK ADDRESS: pairing (the token flow) and
 * connecting (host/port/fingerprint) are deliberately separate actions here.
 * `device_id` + the shared secret are the persistent trust - once pairing
 * succeeds they survive app restarts and are never touched again except by
 * a fresh Pair or an explicit Forget Pairing. `host`/`port` are just where
 * to send bytes right now: the laptop's hotspot-assigned IP can and does
 * change (a new DHCP lease, a reconnect), and updating "Update Connection"
 * to point at wherever the laptop is NOW never requires - or performs - a
 * new pairing exchange. The certificate fingerprint is verified on every
 * connection regardless of which of the two actions last changed it.
 */

import android.app.Activity
import android.os.Bundle
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import android.widget.Toast
import org.json.JSONObject
import java.security.MessageDigest
import java.security.SecureRandom
import java.security.cert.X509Certificate
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec
import javax.net.ssl.HttpsURLConnection
import javax.net.ssl.SSLContext
import javax.net.ssl.SSLSession
import javax.net.ssl.TrustManager
import javax.net.ssl.X509TrustManager
import kotlin.concurrent.thread

private const val PREFS_NAME = "void_companion"
private const val PROTOCOL_VERSION = 1

/** Trusts exactly one certificate: whichever one hashes to the fingerprint
 * the user entered at pairing time. Everything else is rejected - this is
 * the client half of void/device/cert.py's fingerprint-pinning design, not
 * a "trust everything" TrustManager. */
private class PinnedTrustManager(private val expectedFingerprint: String) : X509TrustManager {
    override fun checkClientTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        throw java.security.cert.CertificateException("Client certificates are not used.")
    }

    override fun checkServerTrusted(chain: Array<out X509Certificate>?, authType: String?) {
        val cert = chain?.firstOrNull()
            ?: throw java.security.cert.CertificateException("No certificate presented.")
        val digest = MessageDigest.getInstance("SHA-256").digest(cert.encoded)
        val actual = digest.joinToString(":") { String.format("%02X", it) }
        if (!actual.equals(expectedFingerprint.trim(), ignoreCase = true)) {
            throw java.security.cert.CertificateException(
                "Certificate fingerprint mismatch - refusing to connect. " +
                "expected=$expectedFingerprint actual=$actual")
        }
    }

    override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf()
}

private fun pinnedSslContext(fingerprint: String): SSLContext {
    val context = SSLContext.getInstance("TLS")
    context.init(null, arrayOf<TrustManager>(PinnedTrustManager(fingerprint)), SecureRandom())
    return context
}

private fun hmacSha256Hex(secret: String, body: ByteArray): String {
    val mac = Mac.getInstance("HmacSHA256")
    mac.init(SecretKeySpec(secret.toByteArray(Charsets.UTF_8), "HmacSHA256"))
    return mac.doFinal(body).joinToString("") { String.format("%02x", it) }
}

/** One POST, over the pinned-TLS connection, hostname verification disabled
 * (there is no DNS name for a LAN IP - the fingerprint pin is what actually
 * authenticates the server here, exactly as documented in
 * void/device/cert.py). Takes the ALREADY-SERIALIZED body bytes (rather than
 * re-serializing a JSONObject here) so the caller signs the exact same bytes
 * that go on the wire - no risk of the signature covering a different
 * serialization than what was actually sent. Runs on a background thread;
 * never on the UI thread. */
private fun postJsonBytes(host: String, port: Int, path: String, fingerprint: String,
                          raw: ByteArray, signature: String? = null): JSONObject {
    val url = java.net.URL("https://$host:$port$path")
    val conn = url.openConnection() as HttpsURLConnection
    conn.sslSocketFactory = pinnedSslContext(fingerprint).socketFactory
    conn.hostnameVerifier = javax.net.ssl.HostnameVerifier { _: String?, _: SSLSession? -> true }
    conn.requestMethod = "POST"
    conn.doOutput = true
    conn.setRequestProperty("Content-Type", "application/json")
    if (signature != null) {
        conn.setRequestProperty("X-Void-Signature", signature)
    }
    conn.outputStream.use { it.write(raw) }
    val stream = if (conn.responseCode in 200..299) conn.inputStream else conn.errorStream
    val text = stream.bufferedReader().use { it.readText() }
    return JSONObject(text)
}

class MainActivity : Activity() {

    private lateinit var hostField: EditText
    private lateinit var portField: EditText
    private lateinit var fingerprintField: EditText
    private lateinit var tokenField: EditText
    private lateinit var nameField: EditText
    private lateinit var statusView: TextView
    private lateinit var pairButton: Button

    // Guards against a real-world race: the pairing token is single-use
    // server-side (see void/device/pairing.py), so a SECOND concurrent Pair
    // request - a double-tap, or a retry after a slow/flaky hotspot
    // connection makes the first tap feel unresponsive - would burn the
    // token before the first request's response is even shown, making a
    // correctly-typed, still-valid token look like it failed. This does not
    // change (or need to change) the server's single-use semantics; it just
    // stops the client from accidentally submitting the same token twice.
    private val pairingInFlight = java.util.concurrent.atomic.AtomicBoolean(false)

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val layout = LinearLayout(this)
        layout.orientation = LinearLayout.VERTICAL
        val pad = (16 * resources.displayMetrics.density).toInt()
        layout.setPadding(pad, pad, pad, pad)

        val prefs = getSharedPreferences(PREFS_NAME, MODE_PRIVATE)

        // Pre-fill from whatever is already persisted, so a restarted app
        // shows the device's actual current configuration instead of blank
        // fields that quietly hide an already-paired, already-connectable
        // state. The pairing token is the one field NEVER pre-filled or
        // persisted - it is a one-time bootstrap value, not something kept
        // around after use.
        hostField = labeledField(layout, "Laptop address (from `device pair-start`)",
            prefs.getString("host", "") ?: "")
        portField = labeledField(layout, "Port", prefs.getInt("port", 8765).toString())
        fingerprintField = labeledField(layout, "Certificate fingerprint",
            prefs.getString("fingerprint", "") ?: "")
        tokenField = labeledField(layout, "Pairing token (only needed to (re)pair)")
        nameField = labeledField(layout, "This device's name",
            prefs.getString("device_name", "") ?: "My Phone")

        val updateConnectionButton = Button(this)
        updateConnectionButton.text = "Update Connection"
        updateConnectionButton.setOnClickListener { updateConnection(prefs) }
        layout.addView(updateConnectionButton)

        pairButton = Button(this)
        pairButton.text = "Pair"
        pairButton.setOnClickListener { pair(prefs) }
        layout.addView(pairButton)

        val statusButton = Button(this)
        statusButton.text = "Get Status"
        statusButton.setOnClickListener { call(prefs, "get_status", JSONObject()) }
        layout.addView(statusButton)

        val launchButton = Button(this)
        launchButton.text = "Launch Notepad on Laptop"
        launchButton.setOnClickListener {
            call(prefs, "launch_app", JSONObject().put("name", "notepad"))
        }
        layout.addView(launchButton)

        val forgetButton = Button(this)
        forgetButton.text = "Forget Pairing"
        forgetButton.setOnClickListener { forgetPairing(prefs) }
        layout.addView(forgetButton)

        statusView = TextView(this)
        statusView.text = pairingStatusText(prefs)
        layout.addView(statusView)

        setContentView(layout)
    }

    /** Local-only description of whatever is currently persisted - never a
     * network call. Shown at startup so restarting the app doesn't look
     * indistinguishable from never having paired at all. */
    private fun pairingStatusText(prefs: android.content.SharedPreferences): String {
        val deviceId = prefs.getString("device_id", null)
        return if (deviceId != null) "Paired (device_id=$deviceId)."
        else "Not paired yet - enter a pairing token and tap Pair."
    }

    private fun labeledField(parent: LinearLayout, hint: String, default: String = ""): EditText {
        val label = TextView(this)
        label.text = hint
        parent.addView(label)
        val field = EditText(this)
        field.setText(default)
        field.hint = hint
        parent.addView(field)
        return field
    }

    private fun show(message: String) {
        runOnUiThread {
            statusView.text = message
            Toast.makeText(this, message, Toast.LENGTH_SHORT).show()
        }
    }

    private fun pair(prefs: android.content.SharedPreferences) {
        if (!pairingInFlight.compareAndSet(false, true)) {
            show("Already pairing - please wait for that request to finish.")
            return
        }
        runOnUiThread { pairButton.isEnabled = false }

        val host = hostField.text.toString().trim()
        val port = portField.text.toString().trim().toIntOrNull() ?: 8765
        val fingerprint = fingerprintField.text.toString().trim()
        val token = tokenField.text.toString().trim()
        val name = nameField.text.toString().trim().ifEmpty { "My Phone" }

        thread {
            try {
                val body = JSONObject()
                    .put("protocol", PROTOCOL_VERSION)
                    .put("token", token)
                    .put("name", name)
                val raw = body.toString().toByteArray(Charsets.UTF_8)
                val response = postJsonBytes(host, port, "/void/v1/pair", fingerprint, raw)
                if (response.optBoolean("ok", false)) {
                    val result = response.getJSONObject("result")
                    prefs.edit()
                        .putString("host", host)
                        .putInt("port", port)
                        .putString("fingerprint", fingerprint)
                        .putString("device_name", name)
                        .putString("device_id", result.getString("device_id"))
                        .putString("shared_secret", result.getString("shared_secret"))
                        .apply()
                    show("Paired. device_id=${result.getString("device_id")}")
                } else {
                    show("Pairing failed: ${response.optJSONObject("error")}")
                }
            } catch (e: Exception) {
                show("Pairing error: ${e.message}")
            } finally {
                pairingInFlight.set(false)
                runOnUiThread { pairButton.isEnabled = true }
            }
        }
    }

    /** Updates ONLY the connection endpoint (host/port/fingerprint) from the
     * current field values - never touches device_id/shared_secret and
     * never talks to the network. This is how a laptop IP change (a new
     * hotspot DHCP lease, a reconnect) is handled: point the SAME paired
     * identity at wherever the laptop is now, without a new pairing token.
     * Certificate fingerprint verification stays mandatory either way - if
     * the laptop's certificate ever genuinely changes, this is also where
     * the new fingerprint gets entered. */
    private fun updateConnection(prefs: android.content.SharedPreferences) {
        val host = hostField.text.toString().trim()
        val port = portField.text.toString().trim().toIntOrNull() ?: 8765
        val fingerprint = fingerprintField.text.toString().trim()
        if (host.isEmpty() || fingerprint.isEmpty()) {
            show("Enter both an address and a certificate fingerprint first.")
            return
        }
        prefs.edit()
            .putString("host", host)
            .putInt("port", port)
            .putString("fingerprint", fingerprint)
            .apply()
        show("Connection updated: $host:$port")
    }

    /** Client-side reset: clears the persistent trust (device_id + shared
     * secret) so this app can no longer authenticate as the device it used
     * to be, mirroring the laptop's own `device forget`. Does not contact
     * the network - the laptop-side registry entry is a separate thing and
     * is removed independently via `device forget <device_id>`. Connection
     * fields (host/port/fingerprint/name) are left alone since they are not
     * secrets and re-typing them for the next Pair is just friction. */
    private fun forgetPairing(prefs: android.content.SharedPreferences) {
        prefs.edit()
            .remove("device_id")
            .remove("shared_secret")
            .apply()
        show("Pairing forgotten on this phone. Run 'device forget <device_id>' " +
            "on the laptop too if you want to fully revoke it there.")
    }

    private fun call(prefs: android.content.SharedPreferences, operation: String,
                     parameters: JSONObject) {
        val host = prefs.getString("host", null)
        val port = prefs.getInt("port", 8765)
        val fingerprint = prefs.getString("fingerprint", null)
        val deviceId = prefs.getString("device_id", null)
        val secret = prefs.getString("shared_secret", null)
        if (host == null || fingerprint == null || deviceId == null || secret == null) {
            show("Not paired yet - tap Pair first.")
            return
        }

        thread {
            try {
                val requestId = java.util.UUID.randomUUID().toString()
                val body = JSONObject()
                    .put("protocol", PROTOCOL_VERSION)
                    .put("request_id", requestId)
                    .put("device_id", deviceId)
                    .put("operation", operation)
                    .put("parameters", parameters)
                    .put("timestamp", System.currentTimeMillis() / 1000.0)
                val raw = body.toString().toByteArray(Charsets.UTF_8)
                val signature = hmacSha256Hex(secret, raw)
                val response = postJsonBytes(host, port, "/void/v1/request", fingerprint, raw, signature)
                show(response.toString())
            } catch (e: Exception) {
                show("Request error: ${e.message}")
            }
        }
    }
}
