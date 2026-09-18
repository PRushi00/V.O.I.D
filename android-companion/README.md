# V.O.I.D companion (Android) - buildable V1

**Status: buildable.** This is now a real, minimal Gradle/Android project
around the same single-file client described below. It was built end-to-end
from the command line in this repo's own environment (JDK 17 + a
Gradle-wrapper-driven build + the Android command-line SDK tools - no
Android Studio); see "Toolchain" and "What was actually validated in this
task" below for exactly what was verified and how. It has **not** yet been
installed on or run against a real Android phone - see "Manual end-to-end
validation procedure" for the exact remaining steps.

## What it is

One file, one screen:
[`MainActivity.kt`](app/src/main/java/com/void_project/companion/MainActivity.kt).
No third-party dependency - only `javax.net.ssl`, `javax.crypto`, and
`org.json`, all part of the Android/Java standard library (`org.json` ships
inside the Android platform itself). It:

1. Pairs with a running `python -m void device serve` gateway (address, port,
   pairing token, and certificate fingerprint typed in from the laptop's
   `python -m void device pair-start` output).
2. Stores the resulting `device_id` + shared secret in this app's private
   `SharedPreferences`.
3. Sends one of two signed requests: `get_status`, or `launch_app` (opens
   Notepad on the laptop) - proving the full pipe: pin -> pair -> sign ->
   authorize -> execute -> respond.

The protocol implementation itself is unchanged from the original
proof-of-concept; this task only made it a buildable Android application
(project scaffolding, manifest, Gradle wrapper) around that existing code.

## Toolchain

| Component | Version | Why |
|---|---|---|
| JDK (for Gradle/AGP) | 17 (Temurin) | The system's installed JDK was 25, which is newer than any current stable Gradle/AGP combination supports running on. JDK 17 is the widely-documented, known-good JDK for AGP 8.x. Installed standalone to `C:\android-build-tools\jdk17\` - the system JDK 25 was left untouched. |
| Gradle | 8.7 | Matches AGP 8.5.x's minimum Gradle requirement; runs cleanly on JDK 17. Only the **wrapper** (checked into this project) is needed afterwards - a global Gradle install is not required to build. |
| Android Gradle Plugin | 8.5.2 | Current stable AGP compatible with Gradle 8.7 and compileSdk 34. |
| Kotlin Gradle plugin | 1.9.24 | Current stable, compatible with AGP 8.5.x. |
| compileSdk / targetSdk | 34 | Current stable Android API level. |
| minSdk | 24 | Comfortably covers real phones likely to be used for V1 testing; no API used here needs anything newer. |

A one-time Gradle 8.7 distribution and the Android SDK command-line tools
were downloaded to `C:\android-build-tools\` (outside this repository - pure
machine-local build tooling, not part of the project) to bootstrap the
Gradle wrapper and provide `sdkmanager`/`aapt`/`adb`. Everyday builds only
need the wrapper (`gradlew.bat`) plus `local.properties` pointing at wherever
the Android SDK ends up on a given machine.

## Building the APK

```powershell
cd android-companion
copy local.properties.sample local.properties
# edit local.properties: sdk.dir must point at your Android SDK

$env:JAVA_HOME = "<path to a JDK 17 installation>"
.\gradlew.bat assembleDebug
```

The debug APK is written to
`android-companion\app\build\outputs\apk\debug\app-debug.apk`.

`local.properties` is machine-specific and gitignored - never commit it (see
`local.properties.sample` for the format).

## Manual end-to-end validation procedure (real phone)

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
   the laptop's IP on the hotspot-facing adapter - it changes machine to
   machine and session to session, which is exactly why the app asks for it
   at pairing time instead of having anything baked in).
5. Install the APK (see "Installing on a real phone" below), then in the
   app: enter the laptop's address, port, fingerprint, and pairing token;
   tap **Pair**. Expect "Paired. device_id=...".
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

### Installing on a real phone

```powershell
$env:ANDROID_HOME = "C:\android-build-tools\android-sdk"
& "$env:ANDROID_HOME\platform-tools\adb.exe" devices     # confirm the phone shows up
                                                           # (enable USB debugging first)
& "$env:ANDROID_HOME\platform-tools\adb.exe" install -r `
    android-companion\app\build\outputs\apk\debug\app-debug.apk
```

### Firewall (laptop side)

The gateway listens on TCP port 8765 by default. If Windows Firewall blocks
the phone from reaching it over the hotspot connection, add the narrowest
rule that allows exactly that (run in an **elevated** PowerShell - this was
NOT run automatically, since it changes machine-wide firewall state):

```powershell
New-NetFirewallRule -DisplayName "V.O.I.D Device Gateway" `
    -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8765 `
    -Profile Private -Program "C:\V.O.I.D\.venv\Scripts\python.exe"
```

`-Profile Private` only (not Public/Domain) and `-Program` scoped to the
exact interpreter - not a blanket "allow this port from anywhere" rule.
Windows classifies a phone-hotspot connection as Private by default; verify
under Settings -> Network & Internet if unsure.

## What was actually validated in this task (no real phone available here)

- **Compiles and packages for real**: `gradlew assembleDebug` succeeded
  end-to-end from a clean checkout using only the command-line toolchain
  above (no Android Studio); `MainActivity.kt` needed no source changes.
  Output: `app/build/outputs/apk/debug/app-debug.apk` (~804 KB).
  `gradlew test` also ran cleanly (no test sources exist yet, so this
  confirms the test infrastructure works rather than exercising real cases).
- **APK contents verified statically** with `aapt dump badging` and
  `aapt dump xmltree AndroidManifest.xml` (both from build-tools 34.0.0):
  package `com.void_project.companion`, `versionName 0.1.0`,
  `minSdkVersion 24` / `targetSdkVersion 34`, launcher activity
  `com.void_project.companion.MainActivity` with `android:exported=true`,
  `android:allowBackup=false`, and **exactly one** permission
  (`android.permission.INTERNET` - no others). `unzip -l` on the APK showed
  only `classes*.dex`, the Kotlin stdlib's bundled metadata (an unavoidable
  consequence of the source being Kotlin, not an added dependency),
  `AndroidManifest.xml`, and a near-empty `resources.arsc` - no AndroidX, no
  other third-party code.
- **Wire protocol**: the exact same request/response shapes this app sends
  (pairing envelope, signed-request envelope, HMAC-SHA256 signature header,
  certificate-fingerprint format) are exercised by real TLS sockets in
  `tests/test_device_gateway.py` on the laptop side, and were previously
  validated end-to-end with a Python client mimicking this exact Kotlin
  logic (see the main repository's device-communication engineering report).
- **Not validated here**: installation on, or a live pairing/get_status/
  launch_app round trip against, a real Android phone - this sandbox has no
  attached device. See "Manual end-to-end validation procedure" above for
  the exact remaining steps.

## Deferred to V2 (deliberately, not oversights)

- **Shared-secret storage hardening.** This proof-of-concept stores the
  shared secret in plain `SharedPreferences` (sandboxed by Android to this
  app's UID, but not additionally encrypted). Migrating to
  `androidx.security.crypto.EncryptedSharedPreferences` (Android
  Keystore-backed) is the natural next step, deferred here because it adds a
  dependency and encryption code path that could not be tested against a
  real device in this environment - shipping untested security-critical
  code would be worse than being explicit about the gap.
- Multiple paired laptops, richer capabilities, mDNS/local-network discovery
  instead of manual address entry, and any Android-side server/listener are
  all out of scope for V1 (see the main repository's final report).
