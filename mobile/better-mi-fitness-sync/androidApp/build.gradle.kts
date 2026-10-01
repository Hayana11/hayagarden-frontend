plugins {
    alias(libs.plugins.androidApplication)
    alias(libs.plugins.composeCompiler)
}

// Tag-driven releases: -PversionName=1.2.3 -PversionCode=1002003
// or env VERSION_NAME / VERSION_CODE. Local defaults mirror v1.0.4 release metadata.
fun appVersionProp(name: String, envKey: String, default: String): String =
    (findProperty(name) as String?)?.takeIf { it.isNotBlank() }
        ?: System.getenv(envKey)?.takeIf { it.isNotBlank() }
        ?: default

val appVersionName: String = appVersionProp("versionName", "VERSION_NAME", "1.0.5")
val appVersionCode: Int = appVersionProp("versionCode", "VERSION_CODE", "1000005").toInt()

android {
    namespace = "com.bettermifitness.sync"
    // API 37 is a preview platform published as android-37.0; minorApiLevel
    // points AGP at platforms/android-37.0 instead of android-37.
    compileSdk {
        version = release(37) {
            minorApiLevel = 0
        }
    }

    defaultConfig {
        applicationId = "com.bettermifitness.sync"
        minSdk = 28
        targetSdk = 36
        versionCode = appVersionCode
        versionName = appVersionName
    }

    buildFeatures {
        compose = true
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    // Release variant for tagged GitHub releases; PR builds stay on debug.
    // Signed with the debug key (no keystore secret needed); the release
    // build type still applies the real release flags (non-debuggable)
    // so public downloads never ship a debuggable APK.
    buildTypes {
        getByName("release") {
            isMinifyEnabled = false
            signingConfig = signingConfigs.getByName("debug")
        }
    }

    // Keep all moko locales in the APK/AAB (system language can switch without redownload).
    bundle {
        language {
            enableSplit = false
        }
    }
}

// AGP 9 built-in Kotlin: configure JVM target without kotlin-android plugin.
tasks.withType<org.jetbrains.kotlin.gradle.tasks.KotlinCompile>().configureEach {
    compilerOptions {
        jvmTarget.set(org.jetbrains.kotlin.gradle.dsl.JvmTarget.JVM_17)
    }
}

dependencies {
    implementation(project(":composeApp"))
    implementation(libs.androidx.activity.compose)
    implementation(libs.health.connect)
}
