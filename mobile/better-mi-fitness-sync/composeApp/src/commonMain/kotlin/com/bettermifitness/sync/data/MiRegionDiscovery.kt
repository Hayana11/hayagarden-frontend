package com.bettermifitness.sync.data

import com.mifitness.miclient.api.MiDataClient
import com.mifitness.miclient.auth.MiCredentials
import com.mifitness.miclient.auth.MiRegion
import kotlin.time.Duration.Companion.seconds
import kotlinx.coroutines.async
import kotlinx.coroutines.awaitAll
import kotlinx.coroutines.coroutineScope
import kotlinx.coroutines.withTimeout
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * Probes all Mi health regions and picks the shard with the newest fitness samples.
 */
class MiRegionDiscovery {
    suspend fun discover(credentials: MiCredentials): MiRegion.DiscoveryResult {
        val loginHint = credentials.region.takeIf { it.isNotBlank() }
        val probes = coroutineScope {
            MiRegion.ALL_REGIONS.map { region ->
                async { probeRegion(credentials, region) }
            }.awaitAll()
        }
        return MiRegion.pickWinner(probes, loginHint)
    }

    private suspend fun probeRegion(
        credentials: MiCredentials,
        region: String,
    ): MiRegion.RegionProbe {
        val code = MiRegion.normalizeCode(region)
        val host = MiRegion.healthHost(code)
        val client = MiDataClient(credentials.copy(region = code))
        return try {
            withTimeout(PROBE_TIMEOUT) {
                val params = PROBE_KEYS.map { key ->
                    mapOf("key" to key, "limit" to 1)
                }
                val element = client.post(
                    path = "/app/v1/data/get_latest_fitness_data",
                    payload = mapOf("params" to params),
                    host = host,
                )
                val latest = maxTimestamp(element)
                MiRegion.RegionProbe(
                    region = code,
                    reachable = true,
                    latestEpochSec = latest,
                )
            }
        } catch (_: Exception) {
            MiRegion.RegionProbe(region = code, reachable = false, latestEpochSec = null)
        } finally {
            client.close()
        }
    }

    /** Max unix seconds across data_list entries (and nested lists). */
    internal fun maxTimestamp(element: JsonElement): Long? {
        return try {
            val times = mutableListOf<Long>()
            collectTimes(element, times)
            times.maxOrNull()?.takeIf { it > 0L }
        } catch (_: Exception) {
            null
        }
    }

    private fun collectTimes(element: JsonElement, out: MutableList<Long>) {
        when {
            element is kotlinx.serialization.json.JsonObject -> {
                element["time"]?.jsonPrimitive?.longOrNull?.let { out += it }
                element.values.forEach { collectTimes(it, out) }
            }
            element is kotlinx.serialization.json.JsonArray -> {
                element.forEach { collectTimes(it, out) }
            }
        }
    }

    companion object {
        private val PROBE_TIMEOUT = 12.seconds
        private val PROBE_KEYS = listOf("heart_rate", "steps", "sleep")
    }
}
