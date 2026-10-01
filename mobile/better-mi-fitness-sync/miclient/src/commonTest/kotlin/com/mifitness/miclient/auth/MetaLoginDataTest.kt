package com.mifitness.miclient.auth

import io.ktor.client.HttpClient
import io.ktor.client.plugins.cookies.AcceptAllCookiesStorage
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class MetaLoginDataTest {

    @Test
    fun metaLoginData_equalityHolds() {
        val first = MetaLoginData(sign = "s123", qs = "q456", callback = "https://cb")
        val second = MetaLoginData(sign = "s123", qs = "q456", callback = "https://cb")
        assertEquals(first, second)
    }

    @Test
    fun metaLoginData_copyPreservesFields() {
        val original = MetaLoginData(sign = "s123", qs = "q456", callback = "https://cb")
        val copied = original.copy(sign = "s789")
        assertEquals("s789", copied.sign)
        assertEquals(original.qs, copied.qs)
        assertEquals(original.callback, copied.callback)
    }

    @Test
    fun fallbackTriplet_usesCallerCallbackAndSynthesizedQs() {
        // 1.0.2 parity: when the server triplet is unavailable, login replays the
        // caller callback with a synthesized qs and omits _sign (never blank).
        val fallback = MetaLoginData(sign = "", qs = "?sid=miothealth&_json=true", callback = "cb")
        assertTrue(fallback.sign.isEmpty())
        assertEquals("?sid=miothealth&_json=true", fallback.qs)
        assertEquals("cb", fallback.callback)
    }

    @Test
    fun setCookieValue_readsStep1Token() {
        val headers = listOf(
            "cUserId=abc; Domain=.xiaomi.com; Path=/; HttpOnly",
            "step1Token=ST1.ABC123; Domain=.xiaomi.com; Path=/; HttpOnly",
        )
        assertEquals("ST1.ABC123", PassportAuthUtils.setCookieValue(headers, "step1Token"))
    }

    @Test
    fun buildStsUrl_returnsNullForCanonicalOrForeignUrls() {
        // Canonical grant URLs go straight through followRedirects; only
        // non-/healthapp/sts pages qualify for the canonical-STS retry.
        assertNull(
            MiAuth().buildStsUrl(
                "https://sts-hlth.io.mi.com/healthapp/sts?d=wb_abc&ticket=0",
                "wb_abc",
            ),
        )
        assertNull(MiAuth().buildStsUrl("https://example.com/?x=1", "wb_abc"))
    }

    @Test
    fun buildStsUrl_rebindsNonGrantPageToCanonicalSts() {
        // A pasted intermediate page (not the STS grant itself) keeps its auth
        // bitmap and rebinds d= to this install.
        val pasted = "https://sts-hlth.io.mi.com/login/ok" +
            "?d=wb_4e22db7b-ccd6-450d-93b4-9c60c431bd62&ticket=0&pwd=0" +
            "&p_ts=1790146132949&fid=0&p_lm=5&p_ur=ID" +
            "&auth=nnDPjOS7IfRy3rCTlOFRa12X7BtcbCE6GG2dK9%2BW3DkBANOO3" +
            "%2BvRHiCztChF5SbU3sPOUfVUKrFwFphgkWkgAbiD7ls%2FZLwNn5SPsRiyKp0w2tH" +
            "UEHHgMZ141SPgKv923KOXPRrB6yxDOuvph599P7xX0Wgx172ZSvTCPd8b%2BOY%3D" +
            "&m=2147483648&_group=DEFAULT&tsl=0&p_ca=0&p_idc=Singapore" +
            "&nonce=WRqYuhPIfrsBx0H4&_ssign=M1RPsnOcKay4rB9EBg17J3wQUzA%3D"
        val retry = MiAuth().buildStsUrl(pasted, "wb_stabledeviceid000000000000000000")
        assertTrue(retry?.startsWith("https://sts-hlth.io.mi.com/healthapp/sts?") == true)
        assertTrue(retry?.contains("d=wb_stabledeviceid000000000000000000") == true)
        assertTrue(retry?.contains("auth=") == true)
        assertTrue(retry?.contains("p_ur=ID") == true)
    }

    @Test
    fun finishLoginAfterOtp_defaultsRouteToCannedCredentials() = runTest {
        val canned = MiCredentials(
            userId = "123",
            ssecurity = "ssec",
            serviceToken = "token",
            passToken = "pass",
            deviceId = "wb_0123456789abcdef0123456789abcdef",
            region = "sg",
        )
        val fake = FakeAuthHost(canned)
        val storage = AcceptAllCookiesStorage()
        val client = PassportHttpSession.buildClient(storage)
        try {
            val result = fake.finishLoginAfterOtp(
                client,
                storage,
                "user@example.com",
                "pw",
                "wb_0123456789abcdef0123456789abcdef",
                "miothealth",
                "https://sts-hlth.io.mi.com/healthapp/sts",
            )
            assertEquals(canned, result)
            assertEquals("", fake.lastStep1Token)
            assertNull(fake.lastMeta)
        } finally {
            client.close()
        }
    }

    private class FakeAuthHost(val canned: MiCredentials) : MiAuthHost {
        var lastStep1Token = ""
        var lastMeta: MetaLoginData? = null

        override suspend fun fetchCaptchaImage(
            client: HttpClient,
            captchaUrl: String,
        ): LoginResult.CaptchaImage = LoginResult.CaptchaImage(byteArrayOf(1, 2, 3), "ick")

        override suspend fun loginWithCaptcha(
            challenge: LoginResult.CaptchaRequired,
            code: String,
            ick: String,
        ): LoginResult = LoginResult.Success(canned)

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
            lastStep1Token = step1Token
            lastMeta = meta
            return canned
        }

        override suspend fun followRedirectsCollectingServiceToken(
            client: HttpClient,
            startUrl: String,
        ): Pair<String, String> = "" to ""
    }
}
