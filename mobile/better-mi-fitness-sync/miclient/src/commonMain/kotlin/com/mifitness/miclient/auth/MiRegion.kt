package com.mifitness.miclient.auth

/**
 * Mi health cloud regions (machine codes) and discovery winner selection.
 */
object MiRegion {
    const val DEFAULT = "sg"

    /** All shards we probe for data. */
    val ALL_REGIONS: List<String> = listOf("cn", "sg", "us", "de", "ru", "i2")

    data class RegionInfo(val code: String, val label: String)

    val REGION_INFO: List<RegionInfo> = listOf(
        RegionInfo("cn", "China"),
        RegionInfo("sg", "Singapore"),
        RegionInfo("us", "United States"),
        RegionInfo("de", "Europe"),
        RegionInfo("ru", "Russia"),
        RegionInfo("i2", "India"),
    )

    enum class DiscoverySource {
        /** Highest latest-data timestamp among reachable regions. */
        FromData,
        /** No timestamps; used login STS region. */
        FromLogin,
        /** No login signal; used default. */
        Default,
    }

    /**
     * Result of probing one region.
     * @param reachable true if the host answered successfully (may have zero data)
     * @param latestEpochSec max sample time, or null if none
     */
    data class RegionProbe(
        val region: String,
        val reachable: Boolean,
        val latestEpochSec: Long? = null,
    )

    data class DiscoveryResult(
        val region: String,
        val source: DiscoverySource,
        val latestEpochSec: Long? = null,
        val probes: List<RegionProbe> = emptyList(),
    ) {
        val host: String get() = healthHost(region)
        val label: String get() = labelFor(region)
    }

    fun normalizeCode(code: String?): String {
        val c = code?.trim()?.lowercase().orEmpty()
        if (c.isEmpty()) return DEFAULT
        if (c in ALL_REGIONS) return c
        return PassportAuthUtils.resolveRegion(c).let { resolved ->
            if (resolved in ALL_REGIONS) resolved else DEFAULT
        }
    }

    fun healthHost(region: String): String {
        val r = normalizeCode(region)
        return if (r == "cn") "hlth.io.mi.com" else "$r.hlth.io.mi.com"
    }

    fun labelFor(region: String): String =
        REGION_INFO.firstOrNull { it.code == normalizeCode(region) }?.label
            ?: normalizeCode(region)

    /**
     * Pure winner selection from probe results.
     * @param loginRegion STS-derived region hint
     */
    fun pickWinner(
        probes: List<RegionProbe>,
        loginRegion: String?,
    ): DiscoveryResult {
        val login = loginRegion?.takeIf { it.isNotBlank() }?.let { normalizeCode(it) }
        val reachable = probes.filter { it.reachable }

        if (reachable.isEmpty()) {
            val fallback = login ?: DEFAULT
            return DiscoveryResult(
                region = fallback,
                source = if (login != null) DiscoverySource.FromLogin else DiscoverySource.Default,
                latestEpochSec = null,
                probes = probes,
            )
        }

        val withData = reachable.filter { (it.latestEpochSec ?: 0L) > 0L }
        if (withData.isNotEmpty()) {
            val maxTs = withData.maxOf { it.latestEpochSec!! }
            val tied = withData.filter { it.latestEpochSec == maxTs }.map { normalizeCode(it.region) }
            val winner = selectAmong(tied, login)
            return DiscoveryResult(
                region = winner,
                source = DiscoverySource.FromData,
                latestEpochSec = maxTs,
                probes = probes,
            )
        }

        // Reachable but empty: prefer login if that host is reachable, else first reachable
        val loginReachable = login != null && reachable.any { normalizeCode(it.region) == login }
        val winner = if (loginReachable) {
            login!!
        } else {
            selectAmong(reachable.map { normalizeCode(it.region) }, login)
        }
        return DiscoveryResult(
            region = winner,
            source = if (login != null) DiscoverySource.FromLogin else DiscoverySource.Default,
            latestEpochSec = null,
            probes = probes,
        )
    }

    private fun selectAmong(candidates: List<String>, login: String?): String {
        val set = candidates.map { normalizeCode(it) }.distinct()
        if (login != null && login in set) return login
        for (preferred in listOf("cn", "sg", "us", "de", "ru", "i2")) {
            if (preferred in set) return preferred
        }
        return set.firstOrNull() ?: DEFAULT
    }

    fun settingsDetail(resultRegion: String, source: DiscoverySource?, latestEpochSec: Long?): String {
        val host = healthHost(resultRegion)
        val name = labelFor(resultRegion)
        val sourceText = when (source) {
            DiscoverySource.FromData -> "from your data"
            DiscoverySource.FromLogin -> "from login"
            DiscoverySource.Default -> "default"
            null -> "saved"
        }
        return "$name · $host · $sourceText"
    }
}
