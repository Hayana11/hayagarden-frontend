package com.bettermifitness.sync.data

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject

class MiRegionDiscoveryTimestampTest {
    private val discovery = MiRegionDiscovery()
    private val json = Json { ignoreUnknownKeys = true }

    @Test
    fun maxTimestamp_readsNestedDataList() {
        val element = buildJsonObject {
            put(
                "result",
                buildJsonObject {
                    put(
                        "data_list",
                        buildJsonArray {
                            add(
                                buildJsonObject {
                                    put("key", JsonPrimitive("heart_rate"))
                                    put("time", JsonPrimitive(1_700_000_000L))
                                },
                            )
                            add(
                                buildJsonObject {
                                    put("key", JsonPrimitive("steps"))
                                    put("time", JsonPrimitive(1_700_000_500L))
                                },
                            )
                        },
                    )
                },
            )
        }
        assertEquals(1_700_000_500L, discovery.maxTimestamp(element))
    }

    @Test
    fun maxTimestamp_empty() {
        val element = buildJsonObject {
            put("result", buildJsonObject { })
        }
        assertNull(discovery.maxTimestamp(element))
    }
}
