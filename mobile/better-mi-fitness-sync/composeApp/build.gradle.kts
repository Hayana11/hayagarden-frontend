import org.jetbrains.kotlin.gradle.dsl.JvmTarget

plugins {
    alias(libs.plugins.kotlinMultiplatform)
    alias(libs.plugins.composeMultiplatform)
    alias(libs.plugins.composeCompiler)
    alias(libs.plugins.kotlinxSerialization)
    alias(libs.plugins.androidMultiplatformLibrary)
    alias(libs.plugins.mokoResources)
}

kotlin {
    // Android target via the official Android-KMP library plugin.
    android {
        namespace = "com.bettermifitness.sync.shared"
        // API 37 preview platform is android-37.0 (see androidApp note).
        compileSdk {
            version = release(37) {
                minorApiLevel = 0
            }
        }
        minSdk = 28
        compilerOptions {
            jvmTarget.set(JvmTarget.JVM_17)
        }
        // Required so composeResources pack into the Android library for the app module.
        androidResources {
            enable = true
        }
    }

    // iosX64 dropped: Compose Multiplatform 1.11+ no longer ships iosX64 artifacts.
    listOf(
        iosArm64(),
        iosSimulatorArm64()
    ).forEach { iosTarget ->
        iosTarget.binaries.framework {
            baseName = "ComposeApp"
            isStatic = true
            // Avoid "Cannot infer a bundle ID … use -Xbinary=bundleId="
            binaryOption("bundleId", "com.bettermifitness.sync.ComposeApp")
            // Export moko APIs (StringDesc.localized(), etc.) to SwiftUI.
            export(libs.moko.resources)
        }
    }

    // Silence expect/actual class Beta warnings (KT-61573) across all targets.
    targets.configureEach {
        compilations.configureEach {
            compileTaskProvider.configure {
                compilerOptions {
                    freeCompilerArgs.add("-Xexpect-actual-classes")
                }
            }
        }
    }

    sourceSets {
        commonMain.dependencies {
            // Explicit Compose coordinates (compose.* aliases deprecated since CMP 1.10)
            implementation(libs.compose.runtime)
            implementation(libs.compose.foundation)
            implementation(libs.compose.ui)
            implementation(libs.compose.material3)
            implementation(libs.compose.components.resources)
            implementation(libs.jetbrains.navigation.compose)
            implementation(project(":miclient"))

            // Shared i18n SSOT (system locale on Android + iOS)
            api(libs.moko.resources)
            api(libs.moko.resources.compose)

            implementation(libs.ktor.client.core)
            implementation(libs.ktor.client.content.negotiation)
            implementation(libs.ktor.serialization.kotlinx.json)
            implementation(libs.ktor.client.logging)
            implementation(libs.ktor.client.auth)

            implementation(libs.kotlinx.serialization.json)
            implementation(libs.kotlinx.coroutines.core)
            implementation(libs.kotlinx.datetime)

            implementation(libs.koin.core)
            implementation(libs.koin.compose)
            implementation(libs.koin.compose.viewmodel)
            implementation(libs.koin.compose.viewmodel.navigation)

            implementation(libs.datastore.preferences.core)
            implementation(libs.coil.compose)
            implementation(libs.coil.network.ktor3)

            implementation(libs.androidx.lifecycle.viewmodel)
            implementation(libs.androidx.lifecycle.runtime.compose)
        }

        androidMain.dependencies {
            implementation(libs.ktor.client.okhttp)
            implementation(libs.androidx.activity.compose)
            implementation(libs.health.connect)
            implementation(libs.androidx.work.runtime)
            implementation(libs.androidx.lifecycle.process)
        }

        iosMain.dependencies {
            implementation(libs.ktor.client.darwin)
        }

        commonTest.dependencies {
            implementation(kotlin("test"))
            implementation(libs.kotlinx.coroutines.test)
        }
    }
}

// moko-resources: one strings catalog for Compose + SwiftUI (system language).
multiplatformResources {
    resourcesPackage.set("com.bettermifitness.sync")
    resourcesClassName.set("MR")
    iosBaseLocalizationRegion.set("en")
    iosMinimalDeploymentTarget.set("16.0")
}
