package com.mifitness.miclient.fds

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertContentEquals
import kotlin.test.assertTrue

class FdsKeysTest {

    @Test
    fun genDataTypeByte_sportGps_proto22() {
        // dataType=1, sportType=22, fileType=2 → (1<<7)+(22<<2)+2 = 128+88+2 = 218
        assertEquals(
            218,
            FdsKeys.genDataTypeByte(
                dataType = FdsKeys.DATA_TYPE_SPORT,
                sportType = 22,
                fileType = FdsKeys.FILE_TYPE_GPS,
            ),
        )
    }

    @Test
    fun gpsKeyBytes_matchesOutdoorRunningFixture() {
        // research fixture outdoor_running_1784279686.json
        val time = 1_784_279_686L
        val tz = 28
        val proto = 22
        val bytes = FdsKeys.gpsDataIdKeyBytes(time, tz, proto)
        assertEquals(6, bytes.size)
        // little-endian time 1784279686 = 0x6a59f286
        assertEquals(0x86.toByte(), bytes[0])
        assertEquals(0xF2.toByte(), bytes[1])
        assertEquals(0x59.toByte(), bytes[2])
        assertEquals(0x6A.toByte(), bytes[3])
        assertEquals(28.toByte(), bytes[4])
        assertEquals(0xDA.toByte(), bytes[5]) // 218
    }

    @Test
    fun suffix_isStable() {
        val sid = "xiaomiwear_app"
        val suffix = FdsKeys.suffixForGps(sid, 1_784_279_686L, 28, 22)
        assertTrue(suffix.contains("_"))
        val parts = suffix.split("_")
        assertEquals(2, parts.size)
        // URL-safe base64, no padding
        assertTrue(parts[0].none { it == '=' || it == '+' || it == '/' })
        assertTrue(parts[1].none { it == '=' || it == '+' || it == '/' })
        val server = FdsKeys.serverKey(suffix, 1_784_279_686L)
        assertTrue(server.endsWith("_1784279686"))
    }
}

class FdsAesTest {
    @Test
    fun roundTrip_aesCbcPkcs7() {
        val key = ByteArray(16) { (it * 17).toByte() }
        val plain = "mi-fitness-fds-gps-test-payload".encodeToByteArray()
        val cipher = FdsAes.encrypt(key, plain)
        val out = FdsAes.decrypt(key, cipher)
        assertContentEquals(plain, out)
    }

    @Test
    fun objectKey_urlSafeBase64() {
        val raw = ByteArray(16) { it.toByte() }
        val encoded = FdsKeys.encodeUrlSafeNoPad(raw)
        assertContentEquals(raw, FdsAes.decodeObjectKey(encoded))
    }
}
