plugins {
    alias(libs.plugins.kotlinMultiplatform) apply false
    alias(libs.plugins.composeMultiplatform) apply false
    alias(libs.plugins.composeCompiler) apply false
    alias(libs.plugins.kotlinxSerialization) apply false
    alias(libs.plugins.androidApplication) apply false
    alias(libs.plugins.androidMultiplatformLibrary) apply false
    alias(libs.plugins.mokoResources) apply false
    alias(libs.plugins.detekt)
}

detekt {
    buildUponDefaultConfig = true
    allRules = false
    config.setFrom(files("$rootDir/config/detekt/detekt.yml"))
    baseline = file("$rootDir/config/detekt/baseline.xml")
    source.setFrom(
        files(
            "composeApp/src",
            "miclient/src",
            "androidApp/src",
        ),
    )
    parallel = true
}

tasks.withType<dev.detekt.gradle.Detekt>().configureEach {
    jvmTarget = "17"
    reports {
        html.required.set(true)
        checkstyle.required.set(false)
        sarif.required.set(false)
        markdown.required.set(false)
    }
}
