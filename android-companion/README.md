# V.O.I.D companion (Android) - V1 proof-of-concept

**Status: source only, not built or run.** This sandbox has no Android SDK,
no emulator, and no attached phone, so this could not be compiled, installed,
or tested here. What follows is exactly what to do to validate it for real.

## What it is

One file, one screen: [`MainActivity.kt`](app/src/main/java/com/void_project/companion/MainActivity.kt).
No third-party dependency - only `javax.net.ssl`, `javax.crypto`, and
`org.json`, all part of the Android/Java standard library. It:

1. Pairs with a running `python -m void device serve` gateway (address, port,
   pairing token, and certificate fingerprint typed in from the laptop's
   `python -m void device pair-start` output).
2. Stores the resulting `device_id` + shared secret in this app's private
   `SharedPreferences`.
3. Sends one of two signed requests: `get_status`, or `launch_app` (opens
   Notepad on the laptop) - proving the full pipe: pin -> pair -> sign ->
   authorize -> execute -> respond.

## How to build and run it for real

1. Create a new Android Studio project ("Empty Views Activity", Kotlin,
   minSdk 24+ is plenty).
2. Replace the generated `MainActivity.kt` with this file's contents (update
   the package name to match, or set your project's package to
   `com.void_project.companion`).
3. In `AndroidManifest.xml`, add network permission (required to make any
   HTTPS request) and allow cleartext-adjacent local traffic isn't needed
   since this is real TLS:
   ```xml
   <uses-permission android:name="android.permission.INTERNET" />
   ```
4. Build and install onto a real phone (an emulator cannot join a real
   Wi-Fi hotspot the way this topology needs).

## Manual end-to-end validation procedure

1. On the phone: Settings -> turn on Wi-Fi hotspot.
2. On the laptop: connect its Wi-Fi to the phone's hotspot.
3. On the laptop, in the V.O.I.D repo:
   ```
   python -m void device serve
   ```
   leave it running; note the printed certificate fingerprint.
4. In another terminal on the laptop:
   ```
   python -m void device pair-start --name "My Phone" --minutes 5
   ```
   note the token, port, and fingerprint (address: use `ipconfig` to find
   the laptop's IP on the hotspot-facing adapter if the printed guess looks
   wrong).
5. On the phone, in the companion app: enter the laptop's address, port,
   fingerprint, and pairing token; tap **Pair**. Expect "Paired. device_id=...".
6. Tap **Get Status**. Expect a JSON response with `"ok": true` and the
   V.O.I.D version/name.
7. On the laptop:
   ```
   python -m void device grant <device_id> launch_app
   ```
8. Tap **Launch Notepad on Laptop**. Expect Notepad to open on the laptop,
   and `"ok": true` in the app.
9. Disconnect the phone's Wi-Fi from the laptop, reconnect, repeat step 6 -
   confirms reconnect works without re-pairing.
10. On the laptop:
    ```
    python -m void device forget <device_id>
    ```
    Tap **Get Status** again - expect an `unknown_device` error, proving
    revocation takes effect immediately.

## What was actually validated instead (this sandbox)

The exact same wire protocol, TLS fingerprint pinning, HMAC signing, pairing,
capability authorization, and RiskGate/KillSwitch integration are exercised
by real TLS sockets on `127.0.0.1` in `tests/test_device_gateway.py` - a
genuine socket/TLS round trip, just not over a real Wi-Fi hotspot or real
Android hardware. See the final engineering report for the precise scope of
what is and is not independently verified.

## Deferred to V2 (deliberately, not oversights)

- **Shared-secret storage hardening.** This proof-of-concept stores the
  shared secret in plain `SharedPreferences` (sandboxed by Android to this
  app's UID, but not additionally encrypted). Migrating to
  `androidx.security.crypto.EncryptedSharedPreferences` (Android
  Keystore-backed) is the natural next step, deferred here because it adds a
  dependency and encryption code path that could not be built or tested in
  this environment - shipping untested security-critical code would be worse
  than being explicit about the gap.
- Multiple paired laptops, richer capabilities, mDNS/local-network discovery
  instead of manual address entry, and any Android-side server/listener are
  all out of scope for V1 (see the main repository's final report).
