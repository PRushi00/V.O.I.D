package com.void_project.companion

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Regression tests for the field failure "Launch Notepad worked, USB was
 * unplugged, then everything said pairing failed": the phone's saved
 * device_id + shared_secret disappeared (laptop registry was intact), the app
 * then asked for a new pairing, and the gateway correctly answered
 * no_window/invalid_pairing_token.
 */
class PairingStateTest {

    // ---- ConfirmGate: one stray tap must not destroy the credentials ------

    @Test fun `a single tap never proceeds`() {
        val t = 0L
        val gate = ConfirmGate(5_000) { t }
        assertFalse(gate.tap())
    }

    @Test fun `second tap within the window proceeds`() {
        var t = 0L
        val gate = ConfirmGate(5_000) { t }
        assertFalse(gate.tap())
        t = 4_999
        assertTrue(gate.tap())
    }

    @Test fun `second tap after the window only re-arms`() {
        var t = 0L
        val gate = ConfirmGate(5_000) { t }
        assertFalse(gate.tap())
        t = 5_001
        assertFalse(gate.tap())      // too late: re-armed, not confirmed
        t = 6_000
        assertTrue(gate.tap())       // a fresh confirmation within the new window works
    }

    @Test fun `confirming consumes the arm, so the next tap starts over`() {
        val t = 0L
        val gate = ConfirmGate(5_000) { t }
        gate.tap()
        assertTrue(gate.tap())
        assertFalse(gate.tap())
    }

    @Test fun `any other action disarms it`() {
        var t = 0L
        val gate = ConfirmGate(5_000) { t }
        gate.tap()
        gate.disarm()                // e.g. the user tapped Launch Notepad
        t = 100
        assertFalse(gate.tap())
    }

    @Test fun `a clock that goes backwards never confirms`() {
        var t = 10_000L
        val gate = ConfirmGate(5_000) { t }
        gate.tap()
        t = 9_000
        assertFalse(gate.tap())
    }

    // ---- messages tell the user what actually happened --------------------

    @Test fun `missing credentials with a saved endpoint is not blamed on the network`() {
        val msg = notPairedMessage(hasSavedEndpoint = true)
        assertTrue(msg, msg.contains("NOT a network problem"))
        assertTrue(msg, msg.contains("device pair-start"))
    }

    @Test fun `never-configured phone asks for the full pairing inputs`() {
        val msg = notPairedMessage(hasSavedEndpoint = false)
        assertTrue(msg, msg.contains("fingerprint"))
        assertTrue(msg, msg.contains("pair-start"))
    }

    @Test fun `rejected pairing token says the laptop was reached and how to fix it`() {
        val msg = describePairingRejection("invalid_pairing_token")
        assertTrue(msg, msg.contains("pair-start"))
        assertTrue(msg, msg.contains("TLS is fine"))
        assertTrue(msg, msg.contains("no pairing window is open"))
    }

    @Test fun `other pairing errors keep their code`() {
        assertEquals("Pairing refused by the laptop (rate_limited).",
            describePairingRejection("rate_limited"))
        assertTrue(describePairingRejection(null).contains("unknown error"))
    }

    // ---- static guard: credentials are cleared in exactly one place -------

    /** The app must never lose valid credentials to a timeout, refusal, TLS
     * failure, USB/network change or capability denial. The strongest cheap
     * guarantee is structural: exactly one place removes them, and it is the
     * (confirmed) forgetPairing(). This fails if anyone adds another. */
    @Test fun `device_id and shared_secret are removed only inside forgetPairing`() {
        val src = File("src/main/java/com/void_project/companion/MainActivity.kt").readText()
        val removals = Regex("""\.remove\("(device_id|shared_secret)"\)""").findAll(src).toList()
        assertEquals(2, removals.size)            // the pair, in one place
        val fnStart = src.indexOf("private fun forgetPairing")
        val fnEnd = src.indexOf("private fun call(")
        assertTrue(fnStart in 0 until fnEnd)
        assertTrue(removals.all { it.range.first in fnStart until fnEnd })
        assertFalse(src.contains(".clear()"))     // no blanket prefs wipe
        assertFalse(src.contains("deleteSharedPreferences"))
        // and forgetPairing is gated by the confirmation
        assertTrue(src.substring(fnStart, fnEnd).contains("forgetGate.tap()"))
    }

    @Test fun `no lifecycle or connectivity hooks that could touch saved state`() {
        val src = File("src/main/java/com/void_project/companion/MainActivity.kt").readText()
        for (hook in listOf("onPause", "onStop", "onDestroy", "BroadcastReceiver",
                            "ConnectivityManager", "UsbManager")) {
            assertFalse("unexpected $hook", src.contains(hook))
        }
    }
}
