package com.mifitness.miclient.gps

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class SportGpsBinaryTest {

    @Test
    fun parseHeader_andPoints_classicLayout() {
        // Header: time, tz, version=1, sportType=22, reserved 0, dataValid=0x07 (time+lon+lat)
        val time = 1_784_279_686
        val header = byteArrayOf(
            (time and 0xFF).toByte(),
            ((time ushr 8) and 0xFF).toByte(),
            ((time ushr 16) and 0xFF).toByte(),
            ((time ushr 24) and 0xFF).toByte(),
            28, // tz
            1, // version → validLen 1
            22, // sport
            0, // reserved
            0xE0.toByte(), // dataValid MSB-first: fields 0,1,2 (time/lon/lat)
        )
        // One record: time, lon float LE, lat float LE
        val pointTime = time + 10
        val lonBits = 106.8f.toBits()
        val latBits = (-6.2f).toBits()
        val record = byteArrayOf(
            (pointTime and 0xFF).toByte(),
            ((pointTime ushr 8) and 0xFF).toByte(),
            ((pointTime ushr 16) and 0xFF).toByte(),
            ((pointTime ushr 24) and 0xFF).toByte(),
            (lonBits and 0xFF).toByte(),
            ((lonBits ushr 8) and 0xFF).toByte(),
            ((lonBits ushr 16) and 0xFF).toByte(),
            ((lonBits ushr 24) and 0xFF).toByte(),
            (latBits and 0xFF).toByte(),
            ((latBits ushr 8) and 0xFF).toByte(),
            ((latBits ushr 16) and 0xFF).toByte(),
            ((latBits ushr 24) and 0xFF).toByte(),
        )
        val blob = header + record
        val h = SportGpsBinary.parseHeader(blob)
        assertNotNull(h)
        assertEquals(1_784_279_686L, h.timeSec)
        assertEquals(1, h.version)
        assertEquals(22, h.sportType)

        val points = SportGpsBinary.parsePoints(blob)
        assertEquals(1, points.size)
        assertTrue(points[0].longitude in 106.7..106.9)
        assertTrue(points[0].latitude in -6.3..-6.1)
    }

    @Test
    fun parseLiveCapturedFixture_ifPresent() {
        // Optional: research/gps/fixtures/live_*_gps.bin from Phase 0 capture (gitignored).
        val candidates = listOf(
            java.io.File("research/gps/fixtures"),
            java.io.File("../research/gps/fixtures"),
        )
        val dir = candidates.firstOrNull { it.isDirectory } ?: return
        val bins = dir.listFiles { f -> f.name.startsWith("live_") && f.name.endsWith("_gps.bin") }
            ?: return
        if (bins.isEmpty()) return
        val data = bins.maxBy { it.lastModified() }.readBytes()
        val header = SportGpsBinary.parseHeader(data)
        assertNotNull(header)
        val points = SportGpsBinary.parsePoints(data)
        assertTrue(points.size > 100, "expected dense track, got ${points.size}")
        assertTrue(points.any { it.latitude in -90.0..90.0 && it.longitude in -180.0..180.0 })
    }
}
