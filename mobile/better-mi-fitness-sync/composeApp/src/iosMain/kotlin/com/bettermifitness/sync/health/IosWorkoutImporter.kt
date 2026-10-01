package com.bettermifitness.sync.health

import com.bettermifitness.sync.data.api.WorkoutRoutePoint
import com.bettermifitness.sync.data.api.WorkoutSession
import kotlinx.cinterop.ExperimentalForeignApi
import platform.CoreLocation.CLLocation
import platform.CoreLocation.CLLocationCoordinate2DMake
import platform.Foundation.NSDate
import platform.Foundation.NSLog
import platform.Foundation.dateWithTimeIntervalSince1970
import platform.HealthKit.HKHealthStore
import platform.HealthKit.HKQuantity
import platform.HealthKit.HKQuantitySample
import platform.HealthKit.HKSample
import platform.HealthKit.HKUnit
import platform.HealthKit.HKWorkout
import platform.HealthKit.HKWorkoutBuilder
import platform.HealthKit.HKWorkoutConfiguration
import platform.HealthKit.HKWorkoutRouteBuilder
import platform.HealthKit.HKWorkoutSessionLocationTypeIndoor
import platform.HealthKit.HKWorkoutSessionLocationTypeOutdoor
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException
import kotlin.coroutines.suspendCoroutine

/**
 * Imports a single Mi workout into HealthKit (SRP: orchestration only).
 * Sample construction is delegated to [IosWorkoutSampleFactory].
 */
@OptIn(ExperimentalForeignApi::class)
class IosWorkoutImporter(
    private val healthStore: HKHealthStore,
    private val samples: IosWorkoutSampleFactory = IosWorkoutSampleFactory(healthStore),
    private val saveObjects: suspend (List<Any>) -> Unit,
) {

    suspend fun importAll(sessions: List<WorkoutSession>) {
        val clean = HealthDataNormalizer.normalizeWorkouts(sessions)
        for (session in clean) {
            try {
                importWithBuilder(session)
            } catch (e: Exception) {
                NSLog("BetterMi: workout builder failed (${session.startTime}): ${e.message}; legacy")
                try {
                    importLegacy(session)
                } catch (e2: Exception) {
                    NSLog("BetterMi: workout write failed (${session.startTime}): ${e2.message}")
                }
            }
        }
    }

    /**
     * Historical import via [HKWorkoutBuilder] so form metrics associate with the workout
     * (WWDC22 / Apple docs — statistics need builder-collected samples).
     */
    private suspend fun importWithBuilder(session: WorkoutSession) {
        val mapping = SportTypeMapper.map(session.activityType)
        val version = WorkoutFormSeries.contentVersion(session, mapping.title, "builder-v3-stride")
        val meta = samples.workoutMetadata(session, version)
        val start = epochDate(session.startTime)
        val end = epochDate(session.endTime)

        val config = HKWorkoutConfiguration()
        config.activityType = mapping.healthKitType.toULong()
        config.locationType = locationType(session.activityType)

        val builder = HKWorkoutBuilder(
            healthStore = healthStore,
            configuration = config,
            device = null,
        )

        beginCollection(builder, start)
        addMetadata(builder, meta)

        val core = samples.coreSamples(session, version)
        val form = samples.formSamples(session, version)
        addSamples(builder, core)
        var formAdded = 0
        form.groupBy { it.quantityType.identifier }.forEach { (typeId, list) ->
            try {
                addSamples(builder, list)
                formAdded += list.size
            } catch (e: Exception) {
                NSLog("BetterMi: skip form type $typeId: ${e.message}")
            }
        }
        if (form.isNotEmpty()) {
            NSLog(
                "BetterMi: workout ${session.startTime} builder core=${core.size} " +
                    "form=$formAdded/${form.size} stride=${session.strideMetersSeries.size}",
            )
        }

        endCollection(builder, end)
        val workout = finishWorkout(builder)
        if (session.route.size >= 2) {
            try {
                saveRoute(workout, session.route)
            } catch (e: Exception) {
                NSLog("BetterMi: route failed: ${e.message}")
            }
        }
        saveRecoverHr(session, version)
    }

    private suspend fun importLegacy(session: WorkoutSession) {
        val mapping = SportTypeMapper.map(session.activityType)
        val version = WorkoutFormSeries.contentVersion(session, mapping.title, "builder-v3-stride")
        val meta = samples.workoutMetadata(session, version)
        val kcal = HKUnit.unitFromString("kcal")
        val meters = HKUnit.unitFromString("m")
        val workout = HKWorkout.workoutWithActivityType(
            workoutActivityType = mapping.healthKitType.toULong(),
            startDate = epochDate(session.startTime),
            endDate = epochDate(session.endTime),
            workoutEvents = null,
            totalEnergyBurned = session.caloriesKcal?.let { HKQuantity.quantityWithUnit(kcal, it) },
            totalDistance = session.distanceMeters?.let { HKQuantity.quantityWithUnit(meters, it) },
            metadata = meta,
        )
        saveObjects(listOf(workout))
        if (session.route.size >= 2) {
            try {
                saveRoute(workout, session.route)
            } catch (_: Exception) { /* best-effort */ }
        }
        samples.allSamples(session, version).chunked(200).forEach { chunk ->
            try {
                saveObjects(chunk)
            } catch (_: Exception) { /* optional */ }
        }
        saveRecoverHr(session, version)
    }

    private suspend fun saveRecoverHr(session: WorkoutSession, version: Long) {
        val list = samples.recoverHeartRateSamples(session, version)
        if (list.isEmpty()) return
        try {
            saveObjects(list)
        } catch (_: Exception) { /* optional */ }
    }

    private suspend fun beginCollection(builder: HKWorkoutBuilder, start: NSDate) {
        suspendCoroutine { cont ->
            builder.beginCollectionWithStartDate(start) { ok, error ->
                if (ok) cont.resume(Unit)
                else cont.resumeWithException(
                    Exception(error?.localizedDescription ?: "beginCollection failed"),
                )
            }
        }
    }

    private suspend fun addMetadata(builder: HKWorkoutBuilder, meta: Map<Any?, Any?>) {
        suspendCoroutine { cont ->
            builder.addMetadata(meta) { ok, error ->
                if (ok) cont.resume(Unit)
                else cont.resumeWithException(
                    Exception(error?.localizedDescription ?: "addMetadata failed"),
                )
            }
        }
    }

    private suspend fun addSamples(builder: HKWorkoutBuilder, list: List<HKQuantitySample>) {
        if (list.isEmpty()) return
        list.chunked(100).forEach { chunk ->
            suspendCoroutine { cont ->
                @Suppress("UNCHECKED_CAST")
                builder.addSamples(chunk as List<HKSample>) { ok, error ->
                    if (ok) cont.resume(Unit)
                    else cont.resumeWithException(
                        Exception(error?.localizedDescription ?: "addSamples failed"),
                    )
                }
            }
        }
    }

    private suspend fun endCollection(builder: HKWorkoutBuilder, end: NSDate) {
        suspendCoroutine { cont ->
            builder.endCollectionWithEndDate(end) { ok, error ->
                if (ok) cont.resume(Unit)
                else cont.resumeWithException(
                    Exception(error?.localizedDescription ?: "endCollection failed"),
                )
            }
        }
    }

    private suspend fun finishWorkout(builder: HKWorkoutBuilder): HKWorkout =
        suspendCoroutine { cont ->
            builder.finishWorkoutWithCompletion { finished, error ->
                if (finished != null) cont.resume(finished)
                else cont.resumeWithException(
                    Exception(error?.localizedDescription ?: "finishWorkout failed"),
                )
            }
        }

    private suspend fun saveRoute(workout: HKWorkout, route: List<WorkoutRoutePoint>) {
        val locations = route.map { p ->
            val date = epochDate(p.timeSec)
            CLLocation(
                coordinate = CLLocationCoordinate2DMake(p.latitude, p.longitude),
                altitude = p.altitudeMeters ?: 0.0,
                horizontalAccuracy = p.horizontalAccuracyMeters ?: 10.0,
                verticalAccuracy = if (p.altitudeMeters != null) 10.0 else -1.0,
                timestamp = date,
            )
        }
        val routeBuilder = HKWorkoutRouteBuilder(healthStore = healthStore, device = null)
        suspendCoroutine { cont ->
            routeBuilder.insertRouteData(locations) { success, error ->
                if (!success) {
                    cont.resumeWithException(
                        Exception(error?.localizedDescription ?: "insertRouteData failed"),
                    )
                    return@insertRouteData
                }
                routeBuilder.finishRouteWithWorkout(workout, metadata = null) { _, finishError ->
                    if (finishError != null) {
                        cont.resumeWithException(Exception(finishError.localizedDescription))
                    } else {
                        cont.resume(Unit)
                    }
                }
            }
        }
    }

    private fun locationType(activityType: String): Long {
        val a = activityType.lowercase()
        return if (
            a.contains("indoor") || a.contains("treadmill") || a.contains("spinning")
        ) {
            HKWorkoutSessionLocationTypeIndoor
        } else {
            HKWorkoutSessionLocationTypeOutdoor
        }
    }

    private fun epochDate(sec: Long): NSDate =
        NSDate.dateWithTimeIntervalSince1970(sec.toDouble())
}
