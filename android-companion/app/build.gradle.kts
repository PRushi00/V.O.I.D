plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.void_project.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "com.void_project.companion"
        minSdk = 24
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }
}

// Deliberately NO dependencies block: MainActivity.kt uses only the Android
// platform SDK (android.app.Activity, android.widget.*) and the Java
// standard library (javax.net.ssl, javax.crypto, org.json - org.json ships
// inside the Android platform itself, not as a Maven artifact). Adding
// AndroidX/appcompat/OkHttp/etc. here would contradict the companion's own
// "no third-party dependency" design and this task's "do not add
// dependencies merely for convenience" instruction.
