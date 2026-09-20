package com.void_project.companion

/**
 * Pure, Android-free helpers (no android.* imports, so they run under a plain
 * JVM unit test - see app/src/test). None of these logs, stores or returns a
 * pairing token, shared secret or signature: the certificate fingerprint they
 * handle is public information (it is printed on the laptop's screen), and
 * host/port are connection config, not credentials.
 *
 * Why this exists: before, every network failure reached the screen as just
 * `e.message`, e.g. "failed to connect to /10.192.243.47:8765". That one
 * string is what Android produces for a connect TIMEOUT (nothing answered:
 * gateway not running, firewall drop, wrong address), for a REFUSED
 * connection (host up, nothing listening) and for "no route" alike - three
 * very different situations with three different fixes, indistinguishable
 * on the phone. Naming the failure class and the endpoint actually used
 * makes each of them recognizable without USB and without adb logcat.
 */

/** Strips separators/whitespace and lower-cases, so `17:40:F7...`, `1740f7...`
 * and a value with a stray space/newline from copy-paste all compare equal.
 * Returns null unless the result is exactly 64 hex digits (a SHA-256), so a
 * malformed entry is reported as MALFORMED rather than as a "mismatch". This
 * does not weaken pinning: the comparison is still all 32 bytes of the
 * digest, exactly. */
internal fun normalizeFingerprint(raw: String): String? {
    val hex = raw.filter { it.isLetterOrDigit() }.lowercase()
    return if (hex.length == 64 && hex.all { it in '0'..'9' || it in 'a'..'f' }) hex else null
}

internal fun fingerprintMatches(expectedRaw: String, actualDigest: ByteArray): Boolean {
    val expected = normalizeFingerprint(expectedRaw) ?: return false
    val actual = actualDigest.joinToString("") { String.format("%02x", it) }
    // Constant-time not required (the fingerprint is public), but lengths are
    // already both 64 here, so plain equality is exact.
    return expected == actual
}

/** Colon-separated uppercase form, the way the laptop prints it. */
internal fun formatFingerprint(digest: ByteArray): String =
    digest.joinToString(":") { String.format("%02X", it) }

/** True when what is typed on screen would send somewhere DIFFERENT from what
 * is saved. `call()` always uses the saved endpoint (only Pair / Update
 * Connection change it), so without this the screen can show one address
 * while requests go to another. */
internal fun endpointDiffers(
    fieldHost: String, fieldPort: Int, fieldFingerprint: String,
    savedHost: String?, savedPort: Int, savedFingerprint: String?,
): Boolean {
    if (savedHost == null || savedFingerprint == null) return true
    if (fieldHost.trim() != savedHost) return true
    if (fieldPort != savedPort) return true
    val a = normalizeFingerprint(fieldFingerprint)
    val b = normalizeFingerprint(savedFingerprint)
    return a != b
}

private fun causeChain(e: Throwable): Sequence<Throwable> =
    generateSequence(e) { it.cause?.takeIf { c -> c !== it } }.take(8)

/** One human-readable line naming WHAT failed and where, most specific class
 * first. Order matters: SocketTimeoutException and ConnectException are both
 * IOExceptions, and a fingerprint mismatch surfaces as an SSLHandshakeException
 * wrapping a CertificateException, so the checks go specific -> general. */
internal fun describeFailure(e: Throwable, host: String, port: Int): String {
    val target = "$host:$port"
    val chain = causeChain(e).toList()
    val name = e.javaClass.simpleName

    val cert = chain.firstOrNull { it is java.security.cert.CertificateException }
    if (cert != null && (cert.message ?: "").contains("fingerprint mismatch", ignoreCase = true)) {
        return "TLS: certificate fingerprint MISMATCH for $target - the certificate this " +
            "address presented is not the one pinned. ${cert.message}"
    }
    if (chain.any { it is javax.net.ssl.SSLException }) {
        return "TLS failure talking to $target ($name): ${e.message}"
    }
    if (chain.any { it is java.net.SocketTimeoutException }) {
        return "TIMEOUT: nothing answered at $target. Gateway not running, wrong address, " +
            "phone and laptop on different networks, or a firewall dropping the port ($name)."
    }
    if (chain.any { it is java.net.UnknownHostException }) {
        return "Cannot resolve host '$host' ($name) - enter the laptop's IPv4 address."
    }
    val connect = chain.firstOrNull { it is java.net.ConnectException }
    if (connect != null) {
        val msg = connect.message ?: ""
        return when {
            msg.contains("ECONNREFUSED", ignoreCase = true) ||
                msg.contains("refused", ignoreCase = true) ->
                "REFUSED: $target is reachable but nothing is listening on that port " +
                    "(gateway not running or wrong port) ($name)."
            msg.contains("EHOSTUNREACH", ignoreCase = true) ||
                msg.contains("ENETUNREACH", ignoreCase = true) ||
                msg.contains("unreachable", ignoreCase = true) ->
                "UNREACHABLE: no route to $target - different network, hotspot off, or " +
                    "the laptop's address changed ($name)."
            else -> "Could not connect to $target ($name): $msg"
        }
    }
    if (chain.any { it.javaClass.name == "org.json.JSONException" }) {
        return "Gateway at $target sent a response that is not valid JSON ($name) - " +
            "is something other than the V.O.I.D gateway on that port?"
    }
    return "Request to $target failed ($name): ${e.message}"
}
