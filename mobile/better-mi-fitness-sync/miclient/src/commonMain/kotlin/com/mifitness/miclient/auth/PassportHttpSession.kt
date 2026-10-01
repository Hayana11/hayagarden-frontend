package com.mifitness.miclient.auth

import com.mifitness.miclient.createPlatformHttpClient
import io.ktor.client.HttpClient
import io.ktor.client.plugins.cookies.AcceptAllCookiesStorage
import io.ktor.client.plugins.cookies.HttpCookies
import io.ktor.http.Cookie
import io.ktor.http.CookieEncoding
import io.ktor.http.Url

/**
 * Builds a cookie-aware Ktor client for Xiaomi passport and seeds deviceId.
 *
 * The official app presents the passport device id as both `deviceId` and
 * `PassportDeviceId` cookies (Reqable: `Cookie: PassportDeviceId=3E88…` on
 * health calls, `d=3E88…` on STS). Seed both so every passport leg carries it.
 */
object PassportHttpSession {
    fun buildClient(cookieStorage: AcceptAllCookiesStorage): HttpClient {
        return createPlatformHttpClient().config {
            followRedirects = false
            install(HttpCookies) {
                storage = cookieStorage
            }
        }
    }

    suspend fun seedDeviceIdCookie(storage: AcceptAllCookiesStorage, deviceId: String) {
        if (deviceId.isEmpty()) return
        for (host in listOf("https://account.xiaomi.com/", "https://sts-hlth.io.mi.com/")) {
            for (name in listOf("deviceId", "PassportDeviceId")) {
                storage.addCookie(
                    Url(host),
                    Cookie(
                        name = name,
                        value = deviceId,
                        domain = host.removePrefix("https://").removeSuffix("/").let {
                            if (it.contains("xiaomi")) ".xiaomi.com" else it
                        },
                        path = "/",
                    ),
                )
            }
        }
    }

    /** Seeds the captcha `ick` token so the retry posts on the same session. */
    suspend fun seedIckCookie(storage: AcceptAllCookiesStorage, ick: String) {
        if (ick.isEmpty()) return
        // The ick value carries base64 padding (+/=) that must survive verbatim;
        // Ktor re-encodes cookie values, so store the raw value exactly once.
        storage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(
                name = "ick",
                value = ick,
                encoding = CookieEncoding.RAW,
                domain = ".xiaomi.com",
                path = "/",
            ),
        )
    }
}
