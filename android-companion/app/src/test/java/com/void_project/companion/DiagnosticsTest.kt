package com.void_project.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.ConnectException
import java.net.SocketTimeoutException
import java.net.UnknownHostException
import java.security.cert.CertificateException
import javax.net.ssl.SSLException
import javax.net.ssl.SSLHandshakeException

class DiagnosticsTest {
    private val digest = ByteArray(32) { it.toByte() }          // stand-in SHA-256
    private val pinColon = formatFingerprint(digest)             // 00:01:02:...
    private val pinPlain = pinColon.replace(":", "").lowercase()

    // ---- fingerprint normalization / pinning strictness ------------------

    @Test fun `colon upper, plain lower and whitespace-padded pins are the same pin`() {
        assertEquals(pinPlain, normalizeFingerprint(pinColon))
        assertEquals(pinPlain, normalizeFingerprint(pinPlain))
        assertEquals(pinPlain, normalizeFingerprint("  ${pinColon.replace(":", " ")}\n"))
        assertTrue(fingerprintMatches(pinColon, digest))
        assertTrue(fingerprintMatches(pinPlain, digest))
        assertTrue(fingerprintMatches("  $pinColon \n", digest))
    }

    @Test fun `normalization never lets a different certificate match`() {
        val other = digest.copyOf().also { it[31] = (it[31] + 1).toByte() }
        assertFalse(fingerprintMatches(pinColon, other))
        // a truncated / padded / non-hex pin is rejected outright, never matched
        assertNull(normalizeFingerprint(pinPlain.dropLast(2)))
        assertNull(normalizeFingerprint(pinPlain + "00"))
        assertNull(normalizeFingerprint("zz".repeat(32)))
        assertNull(normalizeFingerprint(""))
        assertFalse(fingerprintMatches("", digest))
        assertFalse(fingerprintMatches(pinPlain.dropLast(2), digest))
    }

    // ---- failure classification (cases B, C, D from the field report) ----

    @Test fun `android connect timeout is named TIMEOUT with the endpoint`() {
        // Exact shape Android's libcore produces when nothing answers (stale
        // IP, gateway stopped, firewall drop).
        val e = SocketTimeoutException(
            "failed to connect to /192.0.2.10 (port 8765) from /:: (port 41234) after 8000ms")
        val msg = describeFailure(e, "192.0.2.10", 8765)
        assertTrue(msg, msg.startsWith("TIMEOUT"))
        assertTrue(msg, msg.contains("192.0.2.10:8765"))
        assertTrue(msg, msg.contains("SocketTimeoutException"))
    }

    @Test fun `connection refused is named REFUSED and says the host is reachable`() {
        val e = ConnectException("failed to connect to /10.0.0.5 (port 8765): " +
            "isConnected failed: ECONNREFUSED (Connection refused)")
        val msg = describeFailure(e, "10.0.0.5", 8765)
        assertTrue(msg, msg.startsWith("REFUSED"))
        assertTrue(msg, msg.contains("reachable"))
    }

    @Test fun `no route is named UNREACHABLE`() {
        for (code in listOf("EHOSTUNREACH (No route to host)", "ENETUNREACH (Network is unreachable)")) {
            val e = ConnectException("failed to connect to /10.0.0.5 (port 8765): isConnected failed: $code")
            assertTrue(describeFailure(e, "10.0.0.5", 8765).startsWith("UNREACHABLE"))
        }
    }

    @Test fun `unknown host is named as a resolution problem`() {
        val msg = describeFailure(UnknownHostException("laptop.local"), "laptop.local", 8765)
        assertTrue(msg, msg.startsWith("Cannot resolve host"))
    }

    @Test fun `pinned certificate mismatch is a TLS mismatch not a network error`() {
        val e = SSLHandshakeException("Chain validation failed").also {
            it.initCause(CertificateException(
                "Certificate fingerprint mismatch - refusing to connect. expected=AA actual=BB"))
        }
        val msg = describeFailure(e, "192.0.2.10", 8765)
        assertTrue(msg, msg.startsWith("TLS: certificate fingerprint MISMATCH"))
        assertTrue(msg, msg.contains("expected=AA actual=BB"))
    }

    @Test fun `a TCP-level failure is never reported as a fingerprint failure`() {
        val tcp = listOf<Throwable>(
            SocketTimeoutException("failed to connect to /192.0.2.10 (port 8765) after 8000ms"),
            ConnectException("ECONNREFUSED (Connection refused)"),
            ConnectException("EHOSTUNREACH (No route to host)"),
            UnknownHostException("x"),
        )
        for (e in tcp) {
            val msg = describeFailure(e, "h", 1)
            assertFalse(msg, msg.contains("fingerprint", ignoreCase = true))
            assertFalse(msg, msg.startsWith("TLS"))
        }
    }

    @Test fun `other TLS failures are labelled TLS with their class`() {
        val msg = describeFailure(SSLException("Connection closed by peer"), "h", 8765)
        assertTrue(msg, msg.startsWith("TLS failure"))
        assertTrue(msg, msg.contains("SSLException"))
    }

    @Test fun `unknown exceptions keep their class name and message`() {
        val msg = describeFailure(IllegalStateException("boom"), "h", 8765)
        assertTrue(msg, msg.contains("IllegalStateException"))
        assertTrue(msg, msg.contains("boom"))
        assertNotNull(msg)
    }

    // ---- display vs saved endpoint (stale-config detector) ---------------

    @Test fun `screen matching the saved endpoint is not flagged`() {
        assertFalse(endpointDiffers("192.0.2.10", 8765, pinColon, "192.0.2.10", 8765, pinPlain))
    }

    @Test fun `an IP typed on screen but not saved is flagged`() {
        assertTrue(endpointDiffers("192.0.2.99", 8765, pinColon, "192.0.2.10", 8765, pinColon))
    }

    @Test fun `changed port or fingerprint is flagged, formatting differences are not`() {
        assertTrue(endpointDiffers("h", 9000, pinColon, "h", 8765, pinColon))
        val other = pinPlain.dropLast(2) + "ff"
        assertTrue(endpointDiffers("h", 8765, other, "h", 8765, pinColon))
        assertFalse(endpointDiffers(" h ", 8765, pinPlain.uppercase(), "h", 8765, pinColon))
    }

    @Test fun `nothing saved counts as differing`() {
        assertTrue(endpointDiffers("h", 8765, pinColon, null, 8765, null))
    }
}
