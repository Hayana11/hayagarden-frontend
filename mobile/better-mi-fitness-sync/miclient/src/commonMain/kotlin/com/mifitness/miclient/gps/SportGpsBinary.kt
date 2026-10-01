package com.mifitness.miclient.gps

/**
 * One GPS track point from Mi sport GPS binary (`SportGpsParser` native layout).
 *
 * Field ids: 0 time, 1 lon, 2 lat, 3 accuracy, 4 speed, 5 gpsSource, 6 altitude, 7 hdop.
 */
data class GpsPoint(
    val timeSec: Long,
    val longitude: Double,
    val latitude: Double,
    val accuracy: Float? = null,
    val speedMps: Float? = null,
    val altitude: Float? = null,
    val hdop: Float? = null,
    val gpsSource: Int? = null,
)

/**
 * Minimal native GPS binary reader for Phase 0 / product foundation.
 *
 * Layout after FDS decrypt (sport fileType=2):
 * - 7-byte server data id: int32 time LE, tz, version, sportType
 * - 1 reserved byte (0)
 * - N-byte dataValid bitmap (N from validity table; often 1–2)
 * - body: loop records of enabled fields
 *
 * For version ≥ 4 the APK uses TGC packing — this parser focuses on classic
 * one-dimen records (version < 4). Higher versions fall back to a best-effort
 * scan once dataValid is known.
 */
object SportGpsBinary {
    private const val SERVER_SPORT_ID_LEN = 7

    data class Header(
        val timeSec: Long,
        val tzIn15Min: Int,
        val version: Int,
        val sportType: Int,
        val dataValid: ByteArray,
        val bodyOffset: Int,
    )

    fun parseHeader(data: ByteArray): Header? {
        if (data.size < SERVER_SPORT_ID_LEN + 1) return null
        val timeSec = u32le(data, 0)
        val tz = data[4].toInt()
        val version = data[5].toInt() and 0xFF
        val sportType = data[6].toInt() and 0xFF
        // reserved byte at offset 7
        val validLen = gpsValidLen(version)
        if (validLen < 0) return null
        val validOffset = SERVER_SPORT_ID_LEN + 1
        if (data.size < validOffset + validLen) return null
        val dataValid = data.copyOfRange(validOffset, validOffset + validLen)
        return Header(
            timeSec = timeSec,
            tzIn15Min = tz,
            version = version,
            sportType = sportType,
            dataValid = dataValid,
            bodyOffset = validOffset + validLen,
        )
    }

    /**
     * Parse points when the binary uses classic one-dimen packing (version < 4).
     * Returns empty list if layout is unsupported (caller may try schema path later).
     */
    fun parsePoints(data: ByteArray): List<GpsPoint> {
        val header = parseHeader(data) ?: return emptyList()
        val body = data.copyOfRange(header.bodyOffset, data.size)
        val valid = decodeValidBits(header.dataValid, fieldCount = 8)
        // Field sizes from SportGpsParser.OneDimenDataType:
        // time4, lon4f, lat4f, acc4f, speed2, source0, alt4f, hdop4f
        val sizes = intArrayOf(4, 4, 4, 4, 2, 0, 4, 4)
        val recordSize = (0 until 8).sumOf { if (valid[it]) sizes[it] else 0 }
        if (recordSize <= 0) return emptyList()

        // Optional leading count (TGC / some versions use int count first).
        // Classic parseRecordData does not always have a count; try both.
        return parseLoop(body, start = 0, valid, sizes, recordSize)
            .ifEmpty {
                if (body.size >= 4) {
                    val count = u32le(body, 0).toInt()
                    if (count in 1..200_000 && 4 + count * recordSize <= body.size) {
                        parseLoop(body, start = 4, valid, sizes, recordSize, maxRecords = count)
                    } else {
                        emptyList()
                    }
                } else {
                    emptyList()
                }
            }
    }

    private fun parseLoop(
        body: ByteArray,
        start: Int,
        valid: BooleanArray,
        sizes: IntArray,
        recordSize: Int,
        maxRecords: Int = Int.MAX_VALUE,
    ): List<GpsPoint> {
        val points = ArrayList<GpsPoint>()
        var off = start
        var n = 0
        while (off + recordSize <= body.size && n < maxRecords) {
            var p = off
            var time = 0L
            var lon = 0f
            var lat = 0f
            var acc: Float? = null
            var speed: Float? = null
            var src: Int? = null
            var alt: Float? = null
            var hdop: Float? = null
            for (field in 0 until 8) {
                if (!valid[field]) continue
                when (field) {
                    0 -> {
                        time = u32le(body, p); p += 4
                    }
                    1 -> {
                        lon = floatLe(body, p); p += 4
                    }
                    2 -> {
                        lat = floatLe(body, p); p += 4
                    }
                    3 -> {
                        acc = floatLe(body, p); p += 4
                    }
                    4 -> {
                        val raw = u16le(body, p)
                        // APK: speed from high bits of lon-related packing in some paths;
                        // field 4 is 2-byte raw. Treat as u16 / 100 when plausible.
                        speed = raw / 100f
                        p += 2
                    }
                    5 -> { /* 0 bytes when present as flag only */ }
                    6 -> {
                        alt = floatLe(body, p); p += 4
                    }
                    7 -> {
                        hdop = floatLe(body, p); p += 4
                    }
                }
            }
            if (lat in -90f..90f && lon in -180f..180f && (lat != 0f || lon != 0f)) {
                points += GpsPoint(
                    timeSec = time,
                    longitude = lon.toDouble(),
                    latitude = lat.toDouble(),
                    accuracy = acc,
                    speedMps = speed,
                    altitude = alt,
                    hdop = hdop,
                    gpsSource = src,
                )
            }
            off += recordSize
            n++
        }
        return points
    }

    /** Matches `FitnessDataValidity.getSportGpsValidityLen`. */
    fun gpsValidLen(version: Int): Int = when (version) {
        1, 2, 3, 4 -> 1
        5 -> 2
        else -> -1
    }

    /**
     * Matches OneDimenDataParser.parseDataValid bit packing:
     * field i uses bit `(7 - (i % 8))` of byte `i / 8` (MSB first within each byte).
     */
    private fun decodeValidBits(dataValid: ByteArray, fieldCount: Int): BooleanArray {
        val out = BooleanArray(fieldCount)
        for (i in 0 until fieldCount) {
            val byteIndex = i / 8
            val bitInByte = i % 8
            if (byteIndex >= dataValid.size) break
            val mask = 1 shl (7 - bitInByte)
            out[i] = (dataValid[byteIndex].toInt() and mask) != 0
        }
        // If all false (unexpected packing), assume time+lon+lat present.
        if (out.none { it }) {
            out[0] = true; out[1] = true; out[2] = true
        }
        return out
    }

    private fun u32le(b: ByteArray, o: Int): Long {
        return (b[o].toLong() and 0xFF) or
            ((b[o + 1].toLong() and 0xFF) shl 8) or
            ((b[o + 2].toLong() and 0xFF) shl 16) or
            ((b[o + 3].toLong() and 0xFF) shl 24)
    }

    private fun u16le(b: ByteArray, o: Int): Int =
        (b[o].toInt() and 0xFF) or ((b[o + 1].toInt() and 0xFF) shl 8)

    private fun floatLe(b: ByteArray, o: Int): Float {
        val bits = (b[o].toInt() and 0xFF) or
            ((b[o + 1].toInt() and 0xFF) shl 8) or
            ((b[o + 2].toInt() and 0xFF) shl 16) or
            ((b[o + 3].toInt() and 0xFF) shl 24)
        return Float.fromBits(bits)
    }
}
