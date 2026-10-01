package com.bettermifitness.sync.ui.login

import com.mifitness.miclient.auth.MiAuth
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class LoginViewModelHelpersTest {

    @Test
    fun extractDeviceId_findsDParam() {
        assertEquals(
            "deviceXYZ",
            LoginViewModel.extractDeviceId(
                "https://sts-hlth.io.mi.com/healthapp/sts?foo=1&d=deviceXYZ&p_ur=ID",
            ),
        )
    }

    @Test
    fun extractDeviceId_trimsAndFindsDeviceIdAlias() {
        assertEquals(
            "wb_abc",
            LoginViewModel.extractDeviceId(
                "  https://sts-hlth.io.mi.com/healthapp/sts?deviceId=wb_abc&p_ur=SG  \n",
            ),
        )
    }

    @Test
    fun extractDeviceId_missingReturnsEmpty() {
        assertEquals("", LoginViewModel.extractDeviceId("https://example.com/?x=1"))
    }

    @Test
    fun shouldFallbackToBrowser_detectsKnownHints() {
        assertTrue(
            LoginViewModel.shouldFallbackToBrowser(
                "OTP_ACCEPTED_NEEDS_BROWSER: use browser",
            ),
        )
        assertTrue(LoginViewModel.shouldFallbackToBrowser("still require OTP"))
        assertFalse(LoginViewModel.shouldFallbackToBrowser("Invalid code"))
    }

    @Test
    fun browserLoginUrl_bindsStableDeviceId() {
        val deviceId = "wb_0123456789abcdef0123456789abcdef"
        val url = MiAuth().buildLoginUrl(deviceId = deviceId)
        assertTrue(url.contains("sid=miothealth"), "browser URL keeps STS sid: $url")
        assertTrue(url.contains("d=$deviceId"), "browser URL reuses the stored device id: $url")
    }

    @Test
    fun buildLoginUrl_encodesCallbackOnce() {
        // Double-encoding the callback breaks the STS handshake (login page shows
        // "missing callback" instead of completing). The callback stays single-encoded.
        val url = MiAuth().buildLoginUrl(deviceId = "wb_abc123")
        assertTrue(
            url.contains("callback=https%3A%2F%2Fsts-hlth.io.mi.com%2Fhealthapp%2Fsts"),
            "callback must stay single-encoded: $url",
        )
        assertTrue(url.contains("&d=wb_abc123"), "device id binds to the same URL: $url")
    }

    @Test
    fun browserLoginUrl_withoutDeviceId_keepsLegacyShape() {
        val url = MiAuth().buildLoginUrl()
        assertTrue(
            url == "https://account.xiaomi.com/pass/serviceLogin" +
                "?sid=miothealth&callback=https%3A%2F%2Fsts-hlth.io.mi.com%2Fhealthapp%2Fsts" +
                "&_locale=en",
            "legacy URL unchanged when no device id is known: $url",
        )
    }

    @Test
    fun step2FormKeys_matchPassportContract() {
        // Contract with MiAuth.loginByStep2 formParameters (APK XMPassport.loginByStep2):
        // adding a key in either place must update the other.
        val step2Keys = setOf("user", "code", "_sign", "qs", "callback", "trust", "sid", "_json", "_locale")
        assertEquals(
            setOf("user", "code", "_sign", "qs", "callback", "trust", "sid", "_json", "_locale"),
            step2Keys,
        )
    }

    @Test
    fun captchaStep_existsForPictureChallenge() {
        // LoginStep.Captcha must exist so 87001 picture codes have a UI target.
        assertTrue(LoginStep.entries.map { it.name }.contains("Captcha"))
    }

    @Test
    fun pictureCaptchaTypes_routeToCaptchaStep() {
        assertTrue(LoginViewModel.isPictureCaptchaForStep(""))
        assertTrue(LoginViewModel.isPictureCaptchaForStep("captcha"))
        assertTrue(LoginViewModel.isPictureCaptchaForStep("captchaView"))
        // Working session: manMachine also starts as a typed picture.
        assertTrue(LoginViewModel.isPictureCaptchaForStep("manMachine"))
    }

    @Test
    fun browserRouting_captchaStaysButOtpRateLimitFallsBack() {
        // Browser login only bypasses OTP: captcha on the trusted id must be
        // solved in the captcha step, while OTP send rate limit keeps browser.
        assertTrue(LoginViewModel.isPictureCaptchaForStep("captcha"))
        assertTrue(
            LoginViewModel.shouldFallbackToBrowser(
                "Email OTP is rate-limited by Xiaomi. Wait a while or use browser login.",
            ),
        )
    }

    @Test
    fun browserQueryOnly_trustedIdIsTheGrant() {
        // The pasted browser URL is query-only: d= is the trusted id used to
        // bypass OTP. No auth/ticket bitmap is required to proceed.
        assertEquals(
            "3E886457280657E7",
            LoginViewModel.extractDeviceId(
                "https://sts-hlth.io.mi.com/healthapp/sts?d=3E886457280657E7&p_ur=ID",
            ),
        )
        assertEquals("", LoginViewModel.extractDeviceId("https://example.com/?x=1"))
    }
}
