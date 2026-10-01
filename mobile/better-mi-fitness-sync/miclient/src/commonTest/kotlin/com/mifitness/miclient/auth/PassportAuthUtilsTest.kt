package com.mifitness.miclient.auth

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue

class PassportAuthUtilsTest {

    @Test
    fun stripJsonPrefix_removesMiPrefix() {
        assertEquals(
            """{"code":0}""",
            PassportAuthUtils.stripJsonPrefix("""&&&START&&&{"code":0}"""),
        )
    }

    @Test
    fun absUrl_prefixesRelativePaths() {
        assertEquals(
            "https://account.xiaomi.com/identity/foo",
            PassportAuthUtils.absUrl("/identity/foo"),
        )
        assertEquals(
            "https://example.com/x",
            PassportAuthUtils.absUrl("https://example.com/x"),
        )
    }

    @Test
    fun resolveRegion_mapsKnownCountries() {
        assertEquals("cn", PassportAuthUtils.resolveRegion("CN"))
        assertEquals("us", PassportAuthUtils.resolveRegion("US"))
        assertEquals("de", PassportAuthUtils.resolveRegion("DE"))
        assertEquals("sg", PassportAuthUtils.resolveRegion("ID"))
    }

    @Test
    fun inferMaskedEmail_masksLocalPart() {
        assertEquals("tes***@example.com", PassportAuthUtils.inferMaskedEmail("test@example.com"))
        assertEquals("ab@x.com", PassportAuthUtils.inferMaskedEmail("ab@x.com"))
    }

    @Test
    fun friendlyLoginError_mapsKnownCodes() {
        assertTrue(PassportAuthUtils.friendlyLoginError(70016, "x").contains("password", ignoreCase = true))
        assertTrue(PassportAuthUtils.friendlyLoginError(70022, "x").contains("rate", ignoreCase = true))
    }

    @Test
    fun absCaptchaUrl_resolvesRelativeAgainstAccountDomain() {
        assertEquals(
            "https://account.xiaomi.com/pass/getCode?icodeType=login",
            PassportAuthUtils.absCaptchaUrl("/pass/getCode?icodeType=login"),
        )
        assertEquals("", PassportAuthUtils.absCaptchaUrl(""))
    }

    @Test
    fun isPictureCaptchaType_acceptsTypableCodesOnly() {
        assertTrue(PassportAuthUtils.isPictureCaptchaType(""))
        assertTrue(PassportAuthUtils.isPictureCaptchaType("captcha"))
        assertTrue(PassportAuthUtils.isPictureCaptchaType("captchaView"))
        // Working Reqable session: manMachine still serves a typed picture
        // (getCode image + captCode retry) before any behavioral step.
        assertTrue(PassportAuthUtils.isPictureCaptchaType("manMachine"))
    }

    @Test
    fun friendlyCaptchaError_mapsWrongCode() {
        assertTrue(PassportAuthUtils.friendlyCaptchaError(87001, "x").contains("picture", ignoreCase = true))
    }

    @Test
    fun generateDeviceId_matchesWbHexPattern() {
        val id = PassportAuthUtils.generateDeviceId()
        assertTrue(id.startsWith("wb_"))
        assertEquals(35, id.length) // wb_ + 32 hex
        assertTrue(id.drop(3).all { it in "0123456789abcdef" })
    }

    @Test
    fun parseContext_extractsQueryParam() {
        assertEquals(
            "abc",
            PassportAuthUtils.parseContext("https://account.xiaomi.com/x?context=abc&y=1"),
        )
    }

    @Test
    fun setCookieValue_readsRotatedPassToken() {
        val headers = listOf(
            "cUserId=abc; Domain=.xiaomi.com; Path=/; HttpOnly",
            "passToken=V1:ROTATED; Domain=.xiaomi.com; Path=/; Max-Age=31536000; HttpOnly",
        )
        assertEquals("V1:ROTATED", PassportAuthUtils.setCookieValue(headers, "passToken"))
        assertEquals("abc", PassportAuthUtils.setCookieValue(headers, "cUserId"))
    }

    @Test
    fun setCookieValue_ignoresDeletionAndForeignNames() {
        // `re-pass-token` must not be mistaken for `passToken`, and deletion
        // cookies must not overwrite a live credential with an empty value.
        assertEquals(
            null,
            PassportAuthUtils.setCookieValue(listOf("re-pass-token=ABC123; Path=/"), "passToken"),
        )
        assertEquals(
            null,
            PassportAuthUtils.setCookieValue(listOf("passToken=EXPIRED; Path=/"), "passToken"),
        )
        assertEquals(
            null,
            PassportAuthUtils.setCookieValue(listOf("passToken=x; Max-Age=0; Path=/"), "passToken"),
        )
    }

    @Test
    fun setCookieValue_isCaseInsensitiveOnName() {
        assertEquals(
            "tok",
            PassportAuthUtils.setCookieValue(listOf("PASSTOKEN=tok; Path=/"), "passToken"),
        )
    }

    @Test
    fun generateDeviceId_isStableShapeForTrustedDevice() {
        // Trust accumulates on one id, so every minted id must keep the wb_ + 32 hex shape.
        repeat(8) {
            val id = PassportAuthUtils.generateDeviceId()
            assertTrue(id.startsWith("wb_"), "device id must keep wb_ prefix, was $id")
            assertEquals(35, id.length)
            assertTrue(id.drop(3).all { it in "0123456789abcdef" }, "device id must be hex: $id")
        }
    }
}
