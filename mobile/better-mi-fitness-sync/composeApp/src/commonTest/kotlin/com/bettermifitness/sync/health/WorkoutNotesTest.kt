package com.bettermifitness.sync.health

import com.bettermifitness.sync.data.api.WorkoutSession
import kotlin.test.Test
import kotlin.test.assertContains
import kotlin.test.assertNotNull
import kotlin.test.assertTrue

class WorkoutNotesTest {

    @Test
    fun build_includesPaceCadenceStride() {
        val notes = WorkoutNotes.build(
            WorkoutSession(
                startTime = 1_700_000_000L,
                endTime = 1_700_003_600L,
                activityType = "outdoor_running",
                avgPaceSecPerKm = 600.0,
                avgCadenceSpm = 160.0,
                avgStrideCm = 110.0,
            ),
        )
        val text = assertNotNull(notes)
        assertContains(text, "avg pace 10:00/km")
        assertContains(text, "cadence 160 spm")
        assertContains(text, "stride 110 cm")
    }

    @Test
    fun build_cyclingUsesRpm() {
        val notes = WorkoutNotes.build(
            WorkoutSession(
                startTime = 1L,
                endTime = 100L,
                activityType = "outdoor_riding",
                avgCadenceSpm = 85.0,
            ),
        )
        val text = assertNotNull(notes)
        assertTrue(text.contains("rpm"))
    }

    @Test
    fun formatPace_padsSeconds() {
        assertTrue(WorkoutNotes.formatPace(65.0) == "1:05")
    }
}
