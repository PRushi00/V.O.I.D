// Root build file: only declares which plugin versions are available to
// subprojects (":app"); it applies nothing itself. Versions chosen for a
// known-compatible, JDK 17-based command-line toolchain (see README.md's
// "Toolchain" section for why).
plugins {
    id("com.android.application") version "8.5.2" apply false
    id("org.jetbrains.kotlin.android") version "1.9.24" apply false
}
