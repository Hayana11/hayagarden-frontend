package com.mifitness.miclient.auth

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull

class MiRegionTest {

    @Test
    fun pickWinner_prefersNewestData() {
        val result = MiRegion.pickWinner(
            probes = listOf(
                MiRegion.RegionProbe("sg", reachable = true, latestEpochSec = 100),
                MiRegion.RegionProbe("cn", reachable = true, latestEpochSec = 500),
                MiRegion.RegionProbe("us", reachable = true, latestEpochSec = 200),
            ),
            loginRegion = "sg",
        )
        assertEquals("cn", result.region)
        assertEquals(MiRegion.DiscoverySource.FromData, result.source)
        assertEquals(500L, result.latestEpochSec)
    }

    @Test
    fun pickWinner_tieBreaksToLoginRegion() {
        val result = MiRegion.pickWinner(
            probes = listOf(
                MiRegion.RegionProbe("sg", reachable = true, latestEpochSec = 100),
                MiRegion.RegionProbe("us", reachable = true, latestEpochSec = 100),
            ),
            loginRegion = "us",
        )
        assertEquals("us", result.region)
        assertEquals(MiRegion.DiscoverySource.FromData, result.source)
    }

    @Test
    fun pickWinner_allEmpty_usesLoginIfReachable() {
        val result = MiRegion.pickWinner(
            probes = listOf(
                MiRegion.RegionProbe("sg", reachable = true, latestEpochSec = null),
                MiRegion.RegionProbe("cn", reachable = true, latestEpochSec = 0),
            ),
            loginRegion = "cn",
        )
        assertEquals("cn", result.region)
        assertEquals(MiRegion.DiscoverySource.FromLogin, result.source)
        assertNull(result.latestEpochSec)
    }

    @Test
    fun pickWinner_allFail_fallsBackToLoginOrDefault() {
        val withLogin = MiRegion.pickWinner(
            probes = listOf(
                MiRegion.RegionProbe("sg", reachable = false),
                MiRegion.RegionProbe("cn", reachable = false),
            ),
            loginRegion = "de",
        )
        assertEquals("de", withLogin.region)
        assertEquals(MiRegion.DiscoverySource.FromLogin, withLogin.source)

        val noLogin = MiRegion.pickWinner(
            probes = listOf(MiRegion.RegionProbe("sg", reachable = false)),
            loginRegion = null,
        )
        assertEquals("sg", noLogin.region)
        assertEquals(MiRegion.DiscoverySource.Default, noLogin.source)
    }

    @Test
    fun healthHost_cnAndRegional() {
        assertEquals("hlth.io.mi.com", MiRegion.healthHost("cn"))
        assertEquals("sg.hlth.io.mi.com", MiRegion.healthHost("sg"))
    }

    @Test
    fun normalizeCode_mapsCountryAndUnknown() {
        assertEquals("cn", MiRegion.normalizeCode("CN"))
        assertEquals("sg", MiRegion.normalizeCode("ID"))
        assertEquals("sg", MiRegion.normalizeCode(null))
    }
}
