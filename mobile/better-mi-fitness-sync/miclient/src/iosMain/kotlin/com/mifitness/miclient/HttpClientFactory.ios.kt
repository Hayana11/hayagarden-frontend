package com.mifitness.miclient

import io.ktor.client.HttpClient
import io.ktor.client.engine.darwin.Darwin
import io.ktor.client.plugins.HttpTimeout

actual fun createPlatformHttpClient(): HttpClient = HttpClient(Darwin) {
    install(HttpTimeout) {
        connectTimeoutMillis = 15_000
        requestTimeoutMillis = 60_000
        socketTimeoutMillis = 30_000
    }
    engine {
        configureRequest {
            setAllowsCellularAccess(true)
            setTimeoutInterval(30.0)
        }
    }
}
