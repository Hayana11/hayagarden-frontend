package com.mifitness.miclient.auth

import io.ktor.client.HttpClient
import io.ktor.client.plugins.cookies.AcceptAllCookiesStorage
import io.ktor.client.request.forms.submitForm
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.statement.bodyAsText
import io.ktor.client.statement.readRawBytes
import io.ktor.http.Cookie
import io.ktor.http.Parameters
import io.ktor.http.Url
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/** Result of a login attempt. */
sealed class LoginResult {
    data class Success(val credentials: MiCredentials) : LoginResult()

    /** Captcha image bytes plus the `ick` session token from its response headers. */
    data class CaptchaImage(val bytes: ByteArray, val ick: String) {
        override fun equals(other: Any?): Boolean {
            if (this === other) return true
            if (other !is CaptchaImage) return false
            return bytes.contentEquals(other.bytes) && ick == other.ick
        }

        override fun hashCode(): Int = 31 * bytes.contentHashCode() + ick.hashCode()
    }

    /**
     * Mi requires a typed picture captcha. Call [fetchImage], show the bytes,
     * then [submitCode]. Holds the live HTTP session so the `ick` binding is kept.
     */
    data class CaptchaRequired(
        private val host: MiAuthHost,
        internal val client: HttpClient,
        internal val cookieStorage: AcceptAllCookiesStorage,
        val email: String,
        internal val password: String,
        internal val sid: String,
        internal val callback: String,
        internal val meta: MetaLoginData,
        val captchaUrl: String,
        val captchaType: String,
        val deviceId: String,
    ) : LoginResult() {

        val isPictureCaptcha: Boolean get() = PassportAuthUtils.isPictureCaptchaType(captchaType)

        /** Downloads the image on this challenge's client; `ick` stays bound to it. */
        suspend fun fetchImage(): CaptchaImage = host.fetchCaptchaImage(client, captchaUrl)

        /** Retries password login with the typed code on the same session. */
        suspend fun submitCode(code: String, ick: String): LoginResult =
            host.loginWithCaptcha(this, code.trim(), ick)
    }

    /**
     * Mi requires OTP. Call [sendOtp], then [verifyOtp].
     * Holds the live HTTP session so verification trust is not lost.
     */
    data class OtpRequired(
        private val host: MiAuthHost,
        internal val client: HttpClient,
        internal val cookieStorage: AcceptAllCookiesStorage,
        val email: String,
        internal val password: String,
        internal val sid: String,
        internal val callback: String,
        val notificationUrl: String,
        val maskedTarget: String,
        val deviceId: String,
    ) : LoginResult() {

        private val json = PassportAuthUtils.json
        private var verifyPageUrl: String = ""
        private val userAgent = PassportAuthUtils.DEFAULT_USER_AGENT
        private var sessionWarmed = false

        // Passport trust-binding material captured on verifyEmail success.
        internal var lastStep1Token: String = ""
            private set
        internal var lastMeta: MetaLoginData? = null
            private set

        suspend fun sendOtp() {
            if (notificationUrl.isEmpty()) {
                throw MiAuthException("No verification session from Xiaomi — try browser login.")
            }
            warmVerificationSession()

            val endpoint =
                "https://account.xiaomi.com/identity/auth/sendEmailTicket?_dc=${currentTimeMs()}"
            val response = client.submitForm(
                url = endpoint,
                formParameters = Parameters.build {
                    append("retry", "0")
                    append("icode", "")
                    append("_json", "true")
                },
            ) {
                header("User-Agent", userAgent)
                header("Referer", verifyPageUrl.ifEmpty { notificationUrl })
                header("X-Requested-With", "XMLHttpRequest")
            }
            val body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
            val obj = json.parseToJsonElement(body).jsonObject
            val code = obj["code"]?.jsonPrimitive?.content?.toIntOrNull()
                ?: obj["code"]?.jsonPrimitive?.int
                ?: -1
            if (code != 0) {
                val desc = obj["desc"]?.jsonPrimitive?.content
                    ?: obj["description"]?.jsonPrimitive?.content
                    ?: "unknown"
                throw MiAuthException(PassportAuthUtils.friendlySendOtpError(code, desc))
            }
        }

        /**
         * Verifies the OTP, marks this device as trusted, completes identity redirects,
         * then re-auths on the **same** cookie jar + deviceId to mint serviceToken.
         */
        suspend fun verifyOtp(code: String): MiCredentials {
            warmVerificationSession()

            val endpoint =
                "https://account.xiaomi.com/identity/auth/verifyEmail?_dc=${currentTimeMs()}"
            val response = client.submitForm(
                url = endpoint,
                formParameters = Parameters.build {
                    append("_flag", "8")
                    append("ticket", code.trim())
                    append("trust", "true")
                    append("_json", "true")
                },
            ) {
                header("User-Agent", userAgent)
                header("Referer", verifyPageUrl.ifEmpty { notificationUrl })
                header("X-Requested-With", "XMLHttpRequest")
            }
            val body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
            val obj = json.parseToJsonElement(body).jsonObject
            val verifyCode = obj["code"]?.jsonPrimitive?.content?.toIntOrNull()
                ?: obj["code"]?.jsonPrimitive?.int
                ?: -1
            if (verifyCode != 0) {
                val desc = obj["desc"]?.jsonPrimitive?.content
                    ?: obj["description"]?.jsonPrimitive?.content
                    ?: "verification failed"
                throw MiAuthException("Verification failed (code $verifyCode): $desc")
            }

            val location = obj["location"]?.jsonPrimitive?.content
            if (!location.isNullOrEmpty()) {
                try {
                    host.followRedirectsCollectingServiceToken(
                        client,
                        PassportAuthUtils.absUrl(location),
                    )
                } catch (_: Exception) {
                    // Some locations are SPA pages, not STS — still continue.
                }
            } else {
                try {
                    val context = PassportAuthUtils.parseContext(notificationUrl)
                    if (context.isNotEmpty()) {
                        client.get(
                            "https://account.xiaomi.com/identity/list?sid=$sid&supportedMask=0&_locale=en_US&context=$context",
                        ) {
                            header("User-Agent", userAgent)
                            header("Referer", notificationUrl)
                        }
                    }
                } catch (_: Exception) {
                }
            }

            val inlineUserId = obj["userId"]?.jsonPrimitive?.content
            val inlinePass = obj["passToken"]?.jsonPrimitive?.content
            // Passport device trust binding: verifyEmail hands back step1Token
            // (response cookie/header) plus an optional fresh _sign/qs/callback triplet.
            val setCookies = (response.headers.getAll("Set-Cookie") ?: emptyList()) +
                (response.headers.getAll("set-cookie") ?: emptyList())
            lastStep1Token = PassportAuthUtils.setCookieValue(setCookies, "step1Token")
                ?: response.headers["step1Token"].orEmpty()
            val freshSign = obj["_sign"]?.jsonPrimitive?.content.orEmpty()
            val freshQs = obj["qs"]?.jsonPrimitive?.content.orEmpty()
            val freshCallback = obj["callback"]?.jsonPrimitive?.content.orEmpty()
            lastMeta = if (freshSign.isNotEmpty() && freshQs.isNotEmpty() && freshCallback.isNotEmpty()) {
                MetaLoginData(sign = freshSign, qs = freshQs, callback = freshCallback)
            } else {
                null
            }
            if (!inlineUserId.isNullOrEmpty() && !inlinePass.isNullOrEmpty()) {
                cookieStorage.addCookie(
                    Url("https://account.xiaomi.com/"),
                    Cookie(name = "userId", value = inlineUserId, domain = ".xiaomi.com", path = "/"),
                )
                cookieStorage.addCookie(
                    Url("https://account.xiaomi.com/"),
                    Cookie(name = "passToken", value = inlinePass, domain = ".xiaomi.com", path = "/"),
                )
            }

            return host.finishLoginAfterOtp(
                client = client,
                cookieStorage = cookieStorage,
                email = email,
                password = password,
                deviceId = deviceId,
                sid = sid,
                callback = callback,
                step1Token = lastStep1Token,
                meta = lastMeta,
                step2code = code.trim(),
                userId = inlineUserId.orEmpty(),
            )
        }

        private suspend fun warmVerificationSession() {
            if (notificationUrl.isEmpty()) return
            if (sessionWarmed) return
            sessionWarmed = true

            try {
                client.get(notificationUrl) { header("User-Agent", userAgent) }
            } catch (_: Exception) {
            }

            val context = PassportAuthUtils.parseContext(notificationUrl)
            if (context.isNotEmpty()) {
                val listUrl =
                    "https://account.xiaomi.com/identity/list?sid=$sid&supportedMask=0&_locale=en_US&context=$context"
                try {
                    client.get(listUrl) {
                        header("User-Agent", userAgent)
                        header("Referer", notificationUrl)
                    }
                } catch (_: Exception) {
                }
            }

            val maskUrl = "https://account.xiaomi.com/identity/auth/verifyEmail?_flag=8&_json=true"
            try {
                client.get(maskUrl) {
                    header("User-Agent", userAgent)
                    header("Referer", notificationUrl)
                }
            } catch (_: Exception) {
            }

            verifyPageUrl = notificationUrl
                .replace("/identity/authStart", "/identity/verifyEmail")
                .replace("/fe/service/identity/authStart", "/fe/service/identity/verifyEmail")
        }

        private fun currentTimeMs(): Long =
            com.mifitness.miclient.crypto.currentUnixSeconds() * 1000
    }
}

/**
 * Host used by OTP challenge to finish login without depending on the full [MiAuth] type.
 */
interface MiAuthHost {
    suspend fun fetchCaptchaImage(client: HttpClient, captchaUrl: String): LoginResult.CaptchaImage

    suspend fun loginWithCaptcha(
        challenge: LoginResult.CaptchaRequired,
        code: String,
        ick: String,
    ): LoginResult

    suspend fun finishLoginAfterOtp(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        email: String,
        password: String,
        deviceId: String,
        sid: String,
        callback: String,
        step1Token: String = "",
        meta: MetaLoginData? = null,
        step2code: String = "",
        userId: String = "",
    ): MiCredentials

    suspend fun followRedirectsCollectingServiceToken(
        client: HttpClient,
        startUrl: String,
    ): Pair<String, String>
}

/**
 * Passport / STS failures.
 * [kind] lets the app decide re-login vs verification vs retry without parsing messages.
 */
class MiAuthException(
    message: String,
    val kind: Kind = Kind.Generic,
    val notificationUrl: String? = null,
    val businessCode: Int? = null,
) : Exception(message) {
    enum class Kind {
        Generic,
        /** passToken rejected / missing — full sign-in required. */
        InvalidCredential,
        /** Xiaomi securityStatus / notification challenge. */
        NeedsVerification,
        MissingDeviceId,
        MissingPassToken,
        StsFailed,
    }
}
