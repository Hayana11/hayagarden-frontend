package com.mifitness.miclient.auth

import com.mifitness.miclient.crypto.MiCloudSigner
import io.ktor.client.HttpClient
import io.ktor.client.plugins.cookies.AcceptAllCookiesStorage
import io.ktor.client.request.forms.submitForm
import io.ktor.client.request.get
import io.ktor.client.request.header
import io.ktor.client.statement.HttpResponse
import io.ktor.client.statement.bodyAsText
import io.ktor.client.statement.readRawBytes
import io.ktor.http.Cookie
import io.ktor.http.Parameters
import io.ktor.http.Url
import io.ktor.http.encodeURLParameter
import io.ktor.http.parseQueryString
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.long

/**
 * Server-issued meta-login triplet from serviceLogin 70016 rejection.
 */
data class MetaLoginData(
    val sign: String,
    val qs: String,
    val callback: String,
)

/**
 * Mi Account authentication façade (password, OTP handoff, STS browser callback).
 *
 * Session refresh follows the official passport path:
 * passToken cookies → `/pass/serviceLogin` → signed STS (`clientSign`) → new serviceToken.
 */
@Suppress("LargeClass")
class MiAuth(
    private val userAgent: String = PassportAuthUtils.DEFAULT_USER_AGENT,
) : MiAuthHost {
    private val json = PassportAuthUtils.json

    suspend fun login(
        email: String,
        password: String,
        deviceId: String = "",
        sid: String = PassportAuthUtils.DEFAULT_SID,
        callback: String = PassportAuthUtils.DEFAULT_STS_CALLBACK,
    ): LoginResult {
        val effectiveDeviceId = deviceId.ifEmpty { PassportAuthUtils.generateDeviceId() }
        val cookieStorage = AcceptAllCookiesStorage()
        val client = PassportHttpSession.buildClient(cookieStorage)
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, effectiveDeviceId)

        return try {
            passwordLoginStep(
                client = client,
                cookieStorage = cookieStorage,
                email = email,
                password = password,
                deviceId = effectiveDeviceId,
                sid = sid,
                callback = callback,
                closeClientOnSuccess = true,
            )
        } catch (e: Exception) {
            client.close()
            throw e
        }
    }

    /**
     * Completes browser login from the final STS redirect URL (paste from Safari/Chrome).
     * Follows redirects and collects serviceToken — does **not** re-run password/OTP login
     * (which would ask for verification again).
     */
    suspend fun completeFromCallbackUrl(
        stsCallbackUrl: String,
        sid: String = PassportAuthUtils.DEFAULT_SID,
    ): MiCredentials {
        val cleaned = stsCallbackUrl.trim().lines().firstOrNull { it.isNotBlank() }?.trim()
            ?: throw MiAuthException("Empty redirect URL")
        val parsedUrl = Url(cleaned)
        val params = parseQueryString(parsedUrl.encodedQuery)
        val deviceId = params["d"]
            ?: params["deviceId"]
            ?: throw MiAuthException(
                "Missing device id (d=…) in redirect URL — copy the full address bar URL",
                kind = MiAuthException.Kind.MissingDeviceId,
            )
        val regionFromUrl = params["p_ur"] ?: ""

        val cookieStorage = AcceptAllCookiesStorage()
        val client = PassportHttpSession.buildClient(cookieStorage)
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)

        try {
            val (serviceToken, regionFromRedirects) = followRedirectsCollectingServiceToken(
                client,
                cleaned,
            )
            if (serviceToken.isNotEmpty()) {
                return finishBrowserCredentials(
                    client = client,
                    cookieStorage = cookieStorage,
                    serviceToken = serviceToken,
                    regionFromRedirects = regionFromRedirects,
                    regionFromUrl = regionFromUrl,
                    deviceId = deviceId,
                    sid = sid,
                )
            }
            // The pasted page may be the "login ok" landing page rather than the
            // STS endpoint itself (e.g. pwd=0 bitmap URL or an intermediate page):
            // retry from the canonical STS URL so cookies still complete the grant.
            val stsRetry = buildStsUrl(cleaned, deviceId)
            if (stsRetry != null) {
                val (retryToken, retryRegion) = followRedirectsCollectingServiceToken(
                    client,
                    stsRetry,
                )
                if (retryToken.isNotEmpty()) {
                    return finishBrowserCredentials(
                        client = client,
                        cookieStorage = cookieStorage,
                        serviceToken = retryToken,
                        regionFromRedirects = retryRegion,
                        regionFromUrl = regionFromUrl,
                        deviceId = deviceId,
                        sid = sid,
                    )
                }
            }
            throw MiAuthException(
                "Could not get a session from that redirect URL. " +
                    "Open the login page again, finish sign-in, then paste the new full URL " +
                    "(it should start with https://sts-hlth.io.mi.com/).",
                kind = MiAuthException.Kind.StsFailed,
            )
        } finally {
            client.close()
        }
    }

    /**
     * Harvests userId/passToken/ssecurity for a browser STS grant. Matches the
     * 1.0.3 cookie-jar behavior the user confirmed working: reads the live jar,
     * then harvests ssecurity (which also picks up passToken rotation).
     */
    private suspend fun finishBrowserCredentials(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        serviceToken: String,
        regionFromRedirects: String,
        regionFromUrl: String,
        deviceId: String,
        sid: String,
    ): MiCredentials {
        val accountCookies = cookieStorage.get(Url("https://account.xiaomi.com/"))
        val stsCookies = cookieStorage.get(Url("https://sts-hlth.io.mi.com/"))
        fun cookie(name: String): String =
            stsCookies.firstOrNull { it.name == name }?.value
                ?: accountCookies.firstOrNull { it.name == name }?.value
                ?: ""

        val userId = cookie("userId")
        val passToken = cookie("passToken")
        val cUserId = cookie("cUserId")
        if (passToken.isBlank()) {
            throw MiAuthException(
                "Browser login did not yield a passToken, so the session cannot be refreshed later. " +
                    "Try password login or paste the URL immediately after Xiaomi shows “ok”.",
                kind = MiAuthException.Kind.MissingPassToken,
            )
        }
        val harvested = if (userId.isNotEmpty()) {
            harvestSsecurity(client, userId, passToken, deviceId, sid)
        } else {
            null
        }
        val ssecurity = harvested?.ssecurity.orEmpty()
        // That call can already rotate the passToken; store the newest one.
        val effectivePassToken = harvested?.rotatedPassToken?.takeIf { it.isNotBlank() } ?: passToken
        if (ssecurity.isEmpty() || userId.isEmpty()) {
            throw MiAuthException(
                "Got a service token but not full session details. " +
                    "Try browser login again and paste the URL as soon as the page says “ok”.",
                kind = MiAuthException.Kind.StsFailed,
            )
        }

        return MiCredentials(
            userId = userId,
            ssecurity = ssecurity,
            serviceToken = serviceToken,
            passToken = effectivePassToken,
            deviceId = deviceId,
            region = PassportAuthUtils.resolveRegion(
                regionFromUrl.ifBlank { regionFromRedirects },
            ),
            cUserId = cUserId,
        )
    }

    /**
     * Canonical STS retry URL for a pasted page that is not the STS grant itself.
     * Keeps the browser's auth bitmap (auth/_ssign/nonce) and rebinds d= so the
     * grant completes on the same device the browser just trusted.
     */
    internal fun buildStsUrl(cleaned: String, deviceId: String): String? {
        val parsed = try {
            Url(cleaned)
        } catch (_: Exception) {
            return null
        }
        if (!parsed.host.equals("sts-hlth.io.mi.com", ignoreCase = true)) return null
        if (parsed.encodedPath.trim('/') == "healthapp/sts") return null
        val params = parseQueryString(parsed.encodedQuery)
        val query = linkedMapOf<String, String>()
        query["d"] = deviceId.ifBlank { params["d"].orEmpty() }
        for (key in params.names()) {
            if (key == "d" || key == "deviceId") continue
            params[key]?.let { query[key] = it }
        }
        if (query["d"].isNullOrBlank()) query.remove("d")
        if (query.isEmpty()) return null
        val encoded = query.entries.joinToString("&") { (key, value) ->
            "${key.encodeURLParameter()}=${value.encodeURLParameter()}"
        }
        return "https://sts-hlth.io.mi.com/healthapp/sts?$encoded"
    }

    fun buildLoginUrl(
        sid: String = PassportAuthUtils.DEFAULT_SID,
        callback: String = PassportAuthUtils.DEFAULT_STS_CALLBACK,
        deviceId: String = "",
    ): String {
        val base =
            "https://account.xiaomi.com/pass/serviceLogin?sid=${sid.encodeURLParameter()}" +
                "&callback=${callback.encodeURLParameter()}&_locale=en"
        if (deviceId.isBlank()) return base
        return "$base&d=${deviceId.encodeURLParameter()}"
    }

    /**
     * Re-mint serviceToken (and ssecurity when needed) using a stored [passToken].
     * Mirrors APK force-refresh via [XMPassport.loginByPassToken] + signed STS.
     */
    suspend fun refreshWithPassToken(
        credentials: MiCredentials,
        sid: String = PassportAuthUtils.DEFAULT_SID,
        callback: String = PassportAuthUtils.DEFAULT_STS_CALLBACK,
    ): MiCredentials {
        if (credentials.passToken.isBlank()) {
            throw MiAuthException(
                "No passToken saved — sign in again",
                kind = MiAuthException.Kind.MissingPassToken,
            )
        }
        if (credentials.userId.isBlank()) {
            throw MiAuthException(
                "No userId saved — sign in again",
                kind = MiAuthException.Kind.InvalidCredential,
            )
        }
        if (credentials.deviceId.isBlank()) {
            throw MiAuthException(
                "No deviceId saved — sign in again (device identity is required for refresh)",
                kind = MiAuthException.Kind.MissingDeviceId,
            )
        }

        val deviceId = credentials.deviceId
        val cookieStorage = AcceptAllCookiesStorage()
        val client = PassportHttpSession.buildClient(cookieStorage)
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "userId", value = credentials.userId, domain = ".xiaomi.com", path = "/"),
        )
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "passToken", value = credentials.passToken, domain = ".xiaomi.com", path = "/"),
        )

        return try {
            val refreshed = loginWithPassTokenCookies(
                client = client,
                cookieStorage = cookieStorage,
                deviceId = deviceId,
                sid = sid,
                callback = callback,
                previousPassToken = credentials.passToken,
                previousCUserId = credentials.cUserId,
            )

            val region = refreshed.region.takeIf { it.isNotBlank() }
                ?: credentials.region.takeIf { it.isNotBlank() }
                ?: "sg"
            refreshed.copy(
                deviceId = deviceId,
                region = region,
            )
        } finally {
            client.close()
        }
    }

    private suspend fun passwordLoginStep(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        email: String,
        password: String,
        deviceId: String,
        sid: String,
        callback: String,
        closeClientOnSuccess: Boolean,
        captCode: String = "",
        captIck: String = "",
    ): LoginResult {
        // Server-issued triplet, with 1.0.2 fallback: fetchMetaLoginData throws
        // when Xiaomi answers without _sign/qs/callback (seen after OTP verify on
        // reused sessions). The old fetchSign+synthesized-qs shape still logs in.
        // Fallback needs the caller's callback (server triplet preferred when present).
        val meta = try {
            fetchMetaLoginData(client, cookieStorage, sid, deviceId)
        } catch (_: Exception) {
            fetchMetaLoginDataFallback(sid, callback)
        }
        val authResponse = postServiceLoginAuth2(client, email, password, sid, meta, captCode, captIck)

        return handleAuthResponse(
            client = client,
            cookieStorage = cookieStorage,
            email = email,
            password = password,
            deviceId = deviceId,
            sid = sid,
            callback = callback,
            meta = meta,
            authResponse = authResponse,
            closeClientOnSuccess = closeClientOnSuccess,
        )
    }

    /**
     * Shared auth-response routing for the first login and captcha retries.
     * Retries must call this directly with the challenge meta — never refetch
     * the triplet, which would orphan the captcha `ick` session binding.
     */
    private suspend fun handleAuthResponse(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        email: String,
        password: String,
        deviceId: String,
        sid: String,
        callback: String,
        meta: MetaLoginData,
        authResponse: JsonObject,
        closeClientOnSuccess: Boolean,
    ): LoginResult {
        val code = authResponse["code"]?.jsonPrimitive?.int ?: -1
        if (code != 0) {
            val desc = authResponse["desc"]?.jsonPrimitive?.content
                ?: authResponse["description"]?.jsonPrimitive?.content
                ?: "Login failed"
            return captchaChallengeOrThrow(
                code = code,
                desc = desc,
                authResponse = authResponse,
                hostClient = client,
                cookieStorage = cookieStorage,
                email = email,
                password = password,
                deviceId = deviceId,
                sid = sid,
                callback = callback,
                meta = meta,
            )
        }

        val securityStatus = authResponse["securityStatus"]?.jsonPrimitive?.int ?: 0
        val notification = authResponse["notificationUrl"]?.jsonPrimitive?.content ?: ""

        if (securityStatus != 0 || notification.isNotEmpty()) {
            return LoginResult.OtpRequired(
                host = this,
                client = client,
                cookieStorage = cookieStorage,
                email = email,
                password = password,
                sid = sid,
                callback = callback,
                notificationUrl = PassportAuthUtils.absUrl(notification),
                maskedTarget = PassportAuthUtils.inferMaskedEmail(email),
                deviceId = deviceId,
            )
        }

        val credentials = exchangeLocationForCredentials(
            client = client,
            cookieStorage = cookieStorage,
            authResponse = authResponse,
            deviceId = deviceId,
        )
        if (closeClientOnSuccess) client.close()
        return LoginResult.Success(credentials)
    }

    /**
     * APK captcha shape: `87001` carries `captchaUrl` + `type`, and `70016`
     * can carry the meta triplet plus an optional `captchaUrl`. Picture codes
     * become a user-solved challenge; anything else stays a typed error.
     */
    private fun captchaChallengeOrThrow(
        code: Int,
        desc: String,
        authResponse: JsonObject,
        hostClient: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        email: String,
        password: String,
        deviceId: String,
        sid: String,
        callback: String,
        meta: MetaLoginData,
    ): LoginResult {
        val captchaPath = authResponse["captchaUrl"]?.jsonPrimitive?.content.orEmpty()
            .takeIf { it.isNotBlank() && !it.equals("null", ignoreCase = true) }
        val captchaUrl = captchaPath?.let { PassportAuthUtils.absCaptchaUrl(it) }.orEmpty()
        if (captchaUrl.isNotBlank() && (code == 87001 || code == 70016)) {
            return LoginResult.CaptchaRequired(
                host = this,
                client = hostClient,
                cookieStorage = cookieStorage,
                email = email,
                password = password,
                sid = sid,
                callback = callback,
                meta = meta,
                captchaUrl = captchaUrl,
                captchaType = authResponse["type"]?.jsonPrimitive?.content.orEmpty(),
                deviceId = deviceId,
            )
        }
        throw MiAuthException(
            PassportAuthUtils.friendlyLoginError(code, desc),
            kind = MiAuthException.Kind.InvalidCredential,
            businessCode = code,
        )
    }

    override suspend fun fetchCaptchaImage(
        client: HttpClient,
        captchaUrl: String,
    ): LoginResult.CaptchaImage {
        // The working session shows the server rotates the picture on every
        // fetch: each getCode answers a new image AND a new ick. Callers must
        // fetch once per shown picture — never refetch after the user typed a
        // code — or the submit answers 87001 for a stale image.
        val url = PassportAuthUtils.absCaptchaUrl(captchaUrl)
        if (url.isBlank()) throw MiAuthException("Captcha image URL is missing")
        val response = client.get(url) {
            header("User-Agent", userAgent)
        }
        val bytes = response.readRawBytes()
        if (bytes.isEmpty()) throw MiAuthException("Captcha image download was empty — refresh and try again")
        val headers = (response.headers.getAll("Set-Cookie") ?: emptyList()) +
            (response.headers.getAll("set-cookie") ?: emptyList())
        val ick = PassportAuthUtils.setCookieValue(headers, "ick")
            ?: response.headers["ick"].orEmpty()
        if (ick.isBlank()) throw MiAuthException("Captcha session expired — refresh the picture and try again")
        return LoginResult.CaptchaImage(bytes = bytes, ick = ick)
    }

    override suspend fun loginWithCaptcha(
        challenge: LoginResult.CaptchaRequired,
        code: String,
        ick: String,
    ): LoginResult {
        if (code.isBlank()) throw MiAuthException("Type the code shown in the picture first")
        if (ick.isBlank()) throw MiAuthException("Captcha session expired — refresh the picture and try again")
        // Post the retry on the SAME session: reseed device id + ick into the
        // challenge jar, reuse the challenge meta. The working Reqable session
        // posts deviceId + ick cookies with captCode and the original triplet.
        PassportHttpSession.seedDeviceIdCookie(challenge.cookieStorage, challenge.deviceId)
        PassportHttpSession.seedIckCookie(challenge.cookieStorage, ick)
        // Same client, same cookies, same challenge meta: a fresh serviceLogin
        // triplet would mint a new session the ick does not belong to.
        val authResponse = postServiceLoginAuth2(
            client = challenge.client,
            email = challenge.email,
            password = challenge.password,
            sid = challenge.sid,
            meta = challenge.meta,
            captCode = code,
            captIck = ick,
        )
        return handleAuthResponse(
            client = challenge.client,
            cookieStorage = challenge.cookieStorage,
            email = challenge.email,
            password = challenge.password,
            deviceId = challenge.deviceId,
            sid = challenge.sid,
            callback = challenge.callback,
            meta = challenge.meta,
            authResponse = authResponse,
            closeClientOnSuccess = false,
        )
    }

    override suspend fun finishLoginAfterOtp(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        email: String,
        password: String,
        deviceId: String,
        sid: String,
        callback: String,
        step1Token: String,
        meta: MetaLoginData?,
        step2code: String,
        userId: String,
    ): MiCredentials {
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)

        // Passport device trust binding (APK XMPassport.loginByStep2): binds the
        // verified OTP to this deviceId. Additive; any failure falls through below.
        if (step1Token.isNotBlank() && meta != null) {
            try {
                val step2 = loginByStep2(
                    client = client,
                    cookieStorage = cookieStorage,
                    userId = userId.ifBlank { email },
                    code = step2code,
                    step1Token = step1Token,
                    meta = meta,
                    deviceId = deviceId,
                    sid = sid,
                )
                if (step2["code"]?.jsonPrimitive?.int == 0) {
                    val credentials = exchangeLocationForCredentials(
                        client = client,
                        cookieStorage = cookieStorage,
                        authResponse = step2,
                        deviceId = deviceId,
                    )
                    client.close()
                    return credentials
                }
            } catch (_: Exception) {
                // Fall through to existing behavior.
            }
        }

        val result = passwordLoginStep(
            client = client,
            cookieStorage = cookieStorage,
            email = email,
            password = password,
            deviceId = deviceId,
            sid = sid,
            callback = callback,
            closeClientOnSuccess = false,
        )
        when (result) {
            is LoginResult.Success -> {
                client.close()
                return result.credentials
            }
            is LoginResult.OtpRequired -> {
                // Same client reused; do not open a nested OTP challenge.
            }
            is LoginResult.CaptchaRequired -> {
                client.close()
                throw MiAuthException(
                    "Captcha is still required after verification — solve the picture and try again.",
                    kind = MiAuthException.Kind.InvalidCredential,
                )
            }
        }

        return try {
            val passTokenCreds = loginWithPassTokenCookies(
                client = client,
                cookieStorage = cookieStorage,
                deviceId = deviceId,
                sid = sid,
                callback = callback,
                previousPassToken = null,
                previousCUserId = "",
            )
            client.close()
            passTokenCreds
        } catch (e: MiAuthException) {
            client.close()
            throw MiAuthException(
                "OTP_ACCEPTED_NEEDS_BROWSER: Email code was accepted, but Xiaomi still " +
                    "won't finish app login for this session. Use browser login once to trust this device. " +
                    "(${e.message})",
                kind = MiAuthException.Kind.NeedsVerification,
            )
        }
    }

    /**
     * passToken cookie login + STS — APK [XMPassport.loginByPassToken] shape.
     * Throws [MiAuthException] with typed [MiAuthException.kind] instead of silent null.
     */
    private suspend fun loginWithPassTokenCookies(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        deviceId: String,
        sid: String,
        callback: String,
        previousPassToken: String?,
        previousCUserId: String,
    ): MiCredentials {
        val accountCookies = cookieStorage.get(Url("https://account.xiaomi.com/"))
        val userId = accountCookies.firstOrNull { it.name == "userId" }?.value?.takeIf { it.isNotBlank() }
        val passToken = accountCookies.firstOrNull { it.name == "passToken" }?.value?.takeIf { it.isNotBlank() }
        if (userId.isNullOrEmpty() || passToken.isNullOrEmpty()) {
            throw MiAuthException(
                "Missing userId/passToken cookies for session refresh",
                kind = MiAuthException.Kind.MissingPassToken,
            )
        }

        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "userId", value = userId, domain = ".xiaomi.com", path = "/"),
        )
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "passToken", value = passToken, domain = ".xiaomi.com", path = "/"),
        )

        val loginUrl =
            "https://account.xiaomi.com/pass/serviceLogin?sid=${sid.encodeURLParameter()}" +
                "&_json=true&callback=${callback.encodeURLParameter()}" +
                "&d=${deviceId.encodeURLParameter()}"
        val response = client.get(loginUrl) {
            header("User-Agent", userAgent)
        }
        val body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
        val obj = try {
            json.parseToJsonElement(body).jsonObject
        } catch (_: Exception) {
            throw MiAuthException(
                "Session refresh returned non-JSON from serviceLogin",
                kind = MiAuthException.Kind.StsFailed,
            )
        }
        val code = obj["code"]?.jsonPrimitive?.int ?: -1
        if (code != 0) {
            val desc = obj["desc"]?.jsonPrimitive?.content
                ?: obj["description"]?.jsonPrimitive?.content
                ?: "passToken login failed"
            // 70016 == XMPassport.RESULT_CODE_AUTHENTICATE_FAILED / ServerErrorCode.ERROR_PASSWORD.
            // The APK maps it to InvalidCredentialException on this path (processLoginContent),
            // and XMPassport.refreshPassToken rejects it too — there is no automatic recovery,
            // the passToken is dead. Only a fresh sign-in helps.
            throw MiAuthException(
                if (code == 70016) {
                    "Saved Mi session was rejected by Xiaomi (code 70016) — sign in again"
                } else {
                    PassportAuthUtils.friendlyLoginError(code, desc)
                },
                kind = MiAuthException.Kind.InvalidCredential,
                businessCode = code,
            )
        }
        val securityStatus = obj["securityStatus"]?.jsonPrimitive?.int ?: 0
        if (securityStatus != 0) {
            val notification = obj["notificationUrl"]?.jsonPrimitive?.content
            throw MiAuthException(
                "Xiaomi requires re-verification to renew the session (securityStatus=$securityStatus)",
                kind = MiAuthException.Kind.NeedsVerification,
                notificationUrl = notification?.let { PassportAuthUtils.absUrl(it) },
                businessCode = securityStatus,
            )
        }

        // Passport rotates passToken on every successful passToken login and hands the new
        // value back as a response cookie/header — NOT in the JSON body. The APK reads it via
        // `StringContent.getHeader("passToken")` (XMPassport.parseLoginResult, non-CA branch)
        // and persists it whenever it differs from the old one
        // (OwnAppXiaomiAccountAuthenticator.getAuthTokenBundle → addAccountOrUpdatePassToken).
        // Keeping the old token instead makes the session die once the server-side grace
        // window for the superseded token closes (~days), which looks like a random logout.
        //
        // Resolved before any further passport call so follow-up requests never present the
        // superseded token (which would rotate it again and orphan the value we just read).
        val oldPass = previousPassToken ?: passToken
        var effectivePass = responseCredential(response, "passToken")
            ?: PassportSts.preferRotatedPassToken(
                oldPassToken = oldPass,
                newPassToken = obj["passToken"]?.jsonPrimitive?.content,
                rePassTokenHeader = response.headers["re-pass-token"]
                    ?: response.headers["Re-Pass-Token"],
            )
        if (effectivePass != oldPass) {
            updatePassTokenCookie(cookieStorage, effectivePass)
        }

        var ssecurity = obj["ssecurity"]?.jsonPrimitive?.content.orEmpty()
        if (ssecurity.isEmpty()) {
            val harvested = harvestSsecurity(client, userId, effectivePass, deviceId, sid)
            ssecurity = harvested.ssecurity
            harvested.rotatedPassToken?.takeIf { it != effectivePass }?.let {
                effectivePass = it
                updatePassTokenCookie(cookieStorage, it)
            }
        }
        if (ssecurity.isEmpty()) {
            throw MiAuthException(
                "Session refresh missing ssecurity",
                kind = MiAuthException.Kind.StsFailed,
            )
        }

        val location = obj["location"]?.jsonPrimitive?.content
        val nonce = obj["nonce"]?.let { el ->
            try {
                el.jsonPrimitive.long.toString()
            } catch (_: Exception) {
                el.jsonPrimitive.content
            }
        }.orEmpty()

        val uid = PassportAuthUtils.jsonUserId(obj)
            .ifEmpty { responseCredential(response, "userId").orEmpty() }
            .ifEmpty { userId }
        val cUserId = responseCredential(response, "cUserId")
            ?: obj["cUserId"]?.jsonPrimitive?.content
            ?: obj["encryptedUserId"]?.jsonPrimitive?.content
            ?: previousCUserId

        val serviceToken: String
        val region: String
        if (!location.isNullOrEmpty()) {
            val pair = exchangeStsLocation(
                client = client,
                location = PassportAuthUtils.absUrl(location),
                ssecurity = ssecurity,
                nonce = nonce,
                sid = sid,
            )
            serviceToken = pair.first
            region = pair.second
        } else {
            serviceToken = cookieStorage.get(Url("https://sts-hlth.io.mi.com/"))
                .firstOrNull { it.name == "serviceToken" }?.value
                ?: cookieStorage.get(Url("https://account.xiaomi.com/"))
                    .firstOrNull { it.name == "serviceToken" }?.value
                ?: ""
            region = ""
        }
        if (serviceToken.isEmpty()) {
            throw MiAuthException(
                "Session refresh did not yield a serviceToken",
                kind = MiAuthException.Kind.StsFailed,
            )
        }

        return MiCredentials(
            userId = uid,
            ssecurity = ssecurity,
            serviceToken = serviceToken,
            passToken = effectivePass,
            deviceId = deviceId,
            region = PassportAuthUtils.resolveRegion(region),
            cUserId = cUserId,
        )
    }

    private suspend fun fetchMetaLoginData(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        sid: String,
        deviceId: String,
    ): MetaLoginData {
        // Device identity rides in cookies seeded by PassportHttpSession; no d= query here.
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)
        val url = "https://account.xiaomi.com/pass/serviceLogin" +
            "?sid=${sid.encodeURLParameter()}&_json=true"
        val response = client.get(url) {
            header("User-Agent", userAgent)
        }
        val body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
        val obj = try {
            json.parseToJsonElement(body).jsonObject
        } catch (_: Exception) {
            throw MiAuthException(
                "serviceLogin returned non-JSON meta login data",
                kind = MiAuthException.Kind.StsFailed,
            )
        }
        // Code 70016 here is the expected empty-passToken rejection carrying the triplet.
        val sign = obj["_sign"]?.jsonPrimitive?.content.orEmpty()
        val qs = obj["qs"]?.jsonPrimitive?.content.orEmpty()
        val callback = obj["callback"]?.jsonPrimitive?.content.orEmpty()
        if (sign.isBlank() || qs.isBlank() || callback.isBlank()) {
            throw MiAuthException(
                "serviceLogin meta login missing _sign/qs/callback (code " +
                    "${obj["code"]?.jsonPrimitive?.content ?: "?"})",
                kind = MiAuthException.Kind.StsFailed,
            )
        }
        return MetaLoginData(sign = sign, qs = qs, callback = callback)
    }

    /**
     * 1.0.2 fallback triplet: synthesized qs plus the caller callback, no _sign.
     * Used when the server-issued triplet is unavailable.
     */
    private fun fetchMetaLoginDataFallback(sid: String, callback: String): MetaLoginData {
        return MetaLoginData(sign = "", qs = "?sid=$sid&_json=true", callback = callback)
    }

    private suspend fun postServiceLoginAuth2(
        client: HttpClient,
        email: String,
        password: String,
        sid: String,
        meta: MetaLoginData,
        captCode: String = "",
        captIck: String = "",
    ): JsonObject {
        val hash = MiCloudSigner.hashPassword(password)
        // APK ByPassword: _sign/qs/callback are injected at request time from the
        // challenge meta, AFTER captCode is already in the params. A solved captcha
        // retry must therefore reuse the challenge meta, never a fresh triplet —
        // the fresh triplet belongs to a new session the ick does not match.
        val response = client.submitForm(
            url = "https://account.xiaomi.com/pass/serviceLoginAuth2",
            formParameters = Parameters.build {
                append("sid", sid)
                append("hash", hash)
                if (captCode.isNotBlank()) append("captCode", captCode)
                append("callback", meta.callback)
                append("qs", meta.qs)
                append("user", email)
                append("_json", "true")
                // Fallback triplet carries no _sign — omit instead of sending blank.
                if (meta.sign.isNotEmpty()) append("_sign", meta.sign)
                append("_locale", "en")
            },
        ) {
            header("User-Agent", userAgent)
        }
        val body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
        return json.parseToJsonElement(body).jsonObject
    }

    /**
     * Passport device trust binding (APK XMPassport.loginByStep2, URL_LOGIN_AUTH_STEP2).
     * Binds the verified OTP to this device via step1Token cookie + fresh triplet.
     */
    suspend fun loginByStep2(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        userId: String,
        code: String,
        step1Token: String,
        meta: MetaLoginData,
        deviceId: String,
        sid: String,
    ): JsonObject {
        PassportHttpSession.seedDeviceIdCookie(cookieStorage, deviceId)
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "step1Token", value = step1Token, domain = ".xiaomi.com", path = "/"),
        )
        val response = client.submitForm(
            url = "https://account.xiaomi.com/pass/loginStep2",
            formParameters = Parameters.build {
                append("user", userId)
                append("code", code)
                append("_sign", meta.sign)
                append("qs", meta.qs)
                append("callback", meta.callback)
                append("trust", "true")
                append("sid", sid)
                append("_json", "true")
                append("_locale", "en")
            },
        ) {
            header("User-Agent", userAgent)
        }
        val step2Body = PassportAuthUtils.stripJsonPrefix(response.bodyAsText())
        return json.parseToJsonElement(step2Body).jsonObject
    }

    private suspend fun exchangeLocationForCredentials(
        client: HttpClient,
        cookieStorage: AcceptAllCookiesStorage,
        authResponse: JsonObject,
        deviceId: String,
    ): MiCredentials {
        val location = authResponse["location"]?.jsonPrimitive?.content
            ?: throw MiAuthException(
                "Login succeeded but no location URL (can't get serviceToken)",
                kind = MiAuthException.Kind.StsFailed,
            )
        val userId = PassportAuthUtils.jsonUserId(authResponse)
        val ssecurity = authResponse["ssecurity"]?.jsonPrimitive?.content ?: ""
        // Xiaomi can issue the passToken as a Set-Cookie instead of the JSON body.
        val cookiePassToken = cookieStorage.get(Url("https://account.xiaomi.com/"))
            .firstOrNull { cookie -> cookie.name == "passToken" }?.value.orEmpty()
        val passToken = authResponse["passToken"]?.jsonPrimitive?.content
            ?.takeIf { it.isNotBlank() }
            ?: cookiePassToken.takeIf { it.isNotBlank() }.orEmpty()
        if (passToken.isBlank()) {
            throw MiAuthException(
                "Login succeeded but no passToken — session cannot be refreshed later",
                kind = MiAuthException.Kind.MissingPassToken,
            )
        }
        val cUserId = authResponse["cUserId"]?.jsonPrimitive?.content
            ?: authResponse["encryptedUserId"]?.jsonPrimitive?.content
            ?: cookieStorage.get(Url("https://account.xiaomi.com/"))
                .firstOrNull { cookie -> cookie.name == "cUserId" }?.value
                .takeIf { it?.isNotBlank() == true }
            ?: cookieStorage.get(Url("https://sts-hlth.io.mi.com/"))
                .firstOrNull { cookie -> cookie.name == "cUserId" }?.value
                .takeIf { it?.isNotBlank() == true }
            ?: ""
        val nonce = authResponse["nonce"]?.let { el ->
            try {
                el.jsonPrimitive.long.toString()
            } catch (_: Exception) {
                el.jsonPrimitive.content
            }
        }.orEmpty()

        val (serviceToken, region) = exchangeStsLocation(
            client = client,
            location = PassportAuthUtils.absUrl(location),
            ssecurity = ssecurity,
            nonce = nonce,
            sid = PassportAuthUtils.DEFAULT_SID,
        )
        if (serviceToken.isEmpty()) {
            throw MiAuthException(
                "STS did not set serviceToken — location follow failed",
                kind = MiAuthException.Kind.StsFailed,
            )
        }

        return MiCredentials(
            userId = userId,
            ssecurity = ssecurity,
            serviceToken = serviceToken,
            passToken = passToken,
            deviceId = deviceId,
            region = PassportAuthUtils.resolveRegion(region),
            cUserId = cUserId,
        )
    }

    /**
     * Prefer APK signed STS (`clientSign` + `_userIdNeedEncrypt`); fall back to bare redirect follow.
     */
    private suspend fun exchangeStsLocation(
        client: HttpClient,
        location: String,
        ssecurity: String,
        nonce: String,
        sid: String,
    ): Pair<String, String> {
        if (ssecurity.isNotEmpty() && nonce.isNotEmpty()) {
            val signed = PassportSts.signedLocationUrl(location, nonce, ssecurity)
            val signedResult = followRedirectsCollectingServiceToken(client, signed, sid)
            if (signedResult.first.isNotEmpty()) return signedResult
        }
        return followRedirectsCollectingServiceToken(client, location, sid)
    }

    override suspend fun followRedirectsCollectingServiceToken(
        client: HttpClient,
        startUrl: String,
    ): Pair<String, String> = followRedirectsCollectingServiceToken(client, startUrl, PassportAuthUtils.DEFAULT_SID)

    private suspend fun followRedirectsCollectingServiceToken(
        client: HttpClient,
        startUrl: String,
        sid: String,
    ): Pair<String, String> {
        var serviceToken = ""
        var region = ""
        var currentUrl = startUrl
        repeat(12) {
            val response = client.get(currentUrl) {
                header("User-Agent", userAgent)
            }
            val cookieHeaders = (response.headers.getAll("Set-Cookie") ?: emptyList()) +
                (response.headers.getAll("set-cookie") ?: emptyList())
            cookieHeaders.forEach { cookie ->
                PassportSts.extractServiceTokenFromCookieHeader(cookie, sid)?.let {
                    serviceToken = it
                }
            }
            // Some STS responses put tokens in plain headers (APK SimpleRequest headers map).
            val headerToken = response.headers["serviceToken"]
                ?: response.headers["${sid}_serviceToken"]
            if (!headerToken.isNullOrBlank()) serviceToken = headerToken

            // 1.0.3 parity: the STS bitmap page answers HTTP 200 "ok" with the grant
            // in Set-Cookie (no Location redirect to follow). Without this, pasted
            // pwd=0 URLs always fail with "Could not get a session".
            if (serviceToken.isNotEmpty()) {
                if (currentUrl.contains("p_ur=")) {
                    region = try {
                        parseQueryString(Url(currentUrl).encodedQuery)["p_ur"] ?: region
                    } catch (_: Exception) {
                        region
                    }
                }
                return serviceToken to region
            }

            if (currentUrl.contains("p_ur=")) {
                region = try {
                    parseQueryString(Url(currentUrl).encodedQuery)["p_ur"] ?: region
                } catch (_: Exception) {
                    region
                }
            }
            val redirectUrl = response.headers["Location"]
            if (redirectUrl.isNullOrEmpty() || response.status.value !in 300..399) {
                return@repeat
            }
            currentUrl = PassportAuthUtils.absUrl(redirectUrl)
        }
        return serviceToken to region
    }

    /**
     * Reads a credential that passport returns as a response **cookie or header**
     * (`passToken`, `userId`, `cUserId`).
     *
     * The APK merges `Set-Cookie` values into its response header map
     * (`SimpleRequest.parseCookies` → `HeaderContent.putCookies`) and then reads them with
     * `StringContent.getHeader(name)`; this reproduces that lookup.
     */
    private fun responseCredential(response: HttpResponse, name: String): String? {
        val setCookies = (response.headers.getAll("Set-Cookie") ?: emptyList()) +
            (response.headers.getAll("set-cookie") ?: emptyList())
        return PassportAuthUtils.setCookieValue(setCookies, name)
            ?: response.headers[name]?.takeIf { it.isNotBlank() }
    }

    /** Keeps the cookie jar on the freshly rotated passToken so later legs don't reuse a dead one. */
    private suspend fun updatePassTokenCookie(cookieStorage: AcceptAllCookiesStorage, passToken: String) {
        cookieStorage.addCookie(
            Url("https://account.xiaomi.com/"),
            Cookie(name = "passToken", value = passToken, domain = ".xiaomi.com", path = "/"),
        )
    }

    private data class SsecurityHarvest(
        val ssecurity: String,
        /** This extra serviceLogin can rotate passToken again — never drop that value. */
        val rotatedPassToken: String?,
    )

    private suspend fun harvestSsecurity(
        client: HttpClient,
        userId: String,
        passToken: String,
        deviceId: String,
        sid: String,
    ): SsecurityHarvest {
        val url = "https://account.xiaomi.com/pass/serviceLogin" +
            "?sid=${sid.encodeURLParameter()}&_json=true&d=${deviceId.encodeURLParameter()}"
        val response = client.get(url) {
            header("User-Agent", userAgent)
            header("Cookie", "userId=$userId; passToken=$passToken; deviceId=$deviceId")
        }
        val rotated = responseCredential(response, "passToken")
        val pragma = response.headers["Extension-Pragma"] ?: response.headers["extension-pragma"]
        if (pragma != null) {
            val ssec = PassportAuthUtils.parseJsonField(pragma, "ssecurity")
            if (ssec.isNotEmpty()) return SsecurityHarvest(ssec, rotated)
        }
        return SsecurityHarvest(
            PassportAuthUtils.parseJsonField(
                PassportAuthUtils.stripJsonPrefix(response.bodyAsText()),
                "ssecurity",
            ),
            rotated,
        )
    }

    companion object {
        const val DEFAULT_SID = PassportAuthUtils.DEFAULT_SID
        const val DEFAULT_STS_CALLBACK = PassportAuthUtils.DEFAULT_STS_CALLBACK
        const val DEFAULT_USER_AGENT = PassportAuthUtils.DEFAULT_USER_AGENT

        fun generateDeviceId(): String = PassportAuthUtils.generateDeviceId()
    }
}
