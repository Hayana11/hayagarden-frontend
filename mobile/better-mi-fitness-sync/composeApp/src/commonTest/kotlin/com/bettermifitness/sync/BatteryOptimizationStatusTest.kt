package com.bettermifitness.sync

import kotlin.test.Test
import kotlin.test.assertEquals

class BatteryOptimizationStatusTest {
    @Test
    fun batteryOptimizationReadFailure_degradesToUnavailable() {
        assertEquals(
            BatteryOptimizationStatus.UNAVAILABLE,
            safeBatteryOptimizationStatus { error("PowerManager unavailable") },
        )
    }

    @Test
    fun unsupportedPlatform_isUnavailable() {
        assertEquals(
            BatteryOptimizationStatus.UNAVAILABLE,
            safeBatteryOptimizationStatus { BatteryOptimizationStatus.UNAVAILABLE },
        )
    }
}
