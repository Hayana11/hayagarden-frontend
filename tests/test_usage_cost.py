import unittest

from relay.usage_cost import estimate_quota, quota_to_usd


class UsageCostTests(unittest.TestCase):
    def test_tree_claude_opus_round_with_cache_read(self):
        quota = estimate_quota(
            input_tokens=24370,
            output_tokens=1607,
            cache_read=32965,
            model_ratio=2.5,
            completion_ratio=5,
            cache_ratio=0.1,
            group_ratio=2.2,
        )
        self.assertEqual(quota, 196358)
        self.assertAlmostEqual(quota_to_usd(quota), 0.3927, places=4)

    def test_tree_claude_opus_round_with_cache_create(self):
        quota = estimate_quota(
            input_tokens=24819,
            output_tokens=1965,
            cache_creation=32965,
            model_ratio=2.5,
            completion_ratio=5,
            cache_ratio=0.1,
            cache_creation_ratio=1.25,
            group_ratio=2.2,
        )
        self.assertEqual(quota, 417176)
        self.assertAlmostEqual(quota_to_usd(quota), 0.8344, places=4)

    def test_per_request_pricing(self):
        quota = estimate_quota(
            input_tokens=0,
            output_tokens=0,
            model_ratio=1.0,
            completion_ratio=1.0,
            group_ratio=2.2,
            per_request_price=0.2,
        )
        self.assertEqual(quota, 220000)
        self.assertAlmostEqual(quota_to_usd(quota), 0.44, places=4)


if __name__ == "__main__":
    unittest.main()
