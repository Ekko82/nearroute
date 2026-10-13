"""Offline checks for scoring, switch policy, and Mihomo group discovery."""

import math
import unittest

from optimizer import choose_candidate, flatten_candidates, summarize_samples


class SampleSummaryTests(unittest.TestCase):
    def test_median_nearest_rank_p90_and_weighted_jitter(self):
        stats = summarize_samples("node", [100, 200, 300, 400, 500])
        self.assertEqual(stats["name"], "node")
        self.assertEqual(stats["median_ms"], 300)
        self.assertEqual(stats["jitter_ms"], 200)
        self.assertEqual(stats["success_rate"], 1)
        self.assertEqual(stats["score_ms"], 400)
        self.assertTrue(stats["eligible"])
        self.assertIn("samples", stats)
        self.assertIsInstance(stats["reason"], str)

    def test_even_median_and_unsorted_measurements(self):
        stats = summarize_samples("node", [40, 10, 30, 20])
        self.assertEqual(stats["median_ms"], 25)
        self.assertEqual(stats["jitter_ms"], 15)
        self.assertEqual(stats["score_ms"], 32.5)

    def test_p90_uses_nearest_rank_instead_of_interpolation(self):
        stats = summarize_samples("node", list(range(10, 110, 10)))
        self.assertEqual(stats["median_ms"], 55)
        self.assertEqual(stats["jitter_ms"], 35)
        self.assertEqual(stats["score_ms"], 72.5)

    def test_eighty_percent_success_is_eligible_with_failure_penalty(self):
        stats = summarize_samples("node", [100, None, 100, 100, 100])
        self.assertTrue(stats["eligible"])
        self.assertAlmostEqual(stats["success_rate"], 0.8)
        self.assertEqual(stats["median_ms"], 100)
        self.assertEqual(stats["jitter_ms"], 0)
        self.assertAlmostEqual(stats["score_ms"], 500)

    def test_success_rate_below_eighty_percent_is_ineligible(self):
        stats = summarize_samples("node", [100] * 7 + [None, None])
        self.assertFalse(stats["eligible"])
        self.assertAlmostEqual(stats["success_rate"], 7 / 9)
        self.assertTrue(stats["reason"])

    def test_too_few_measurements_are_ineligible(self):
        stats = summarize_samples("node", [10, 20])
        self.assertFalse(stats["eligible"])
        self.assertEqual(stats["success_rate"], 1)
        self.assertTrue(stats["reason"])

    def test_custom_minimum_is_honored(self):
        stats = summarize_samples("node", [10, 20, 30], min_samples=4)
        self.assertFalse(stats["eligible"])

    def test_failures_zero_negative_and_nonfinite_values_are_rejected(self):
        samples = [10, 20, 30, None, 0, -1, math.nan, math.inf, -math.inf]
        stats = summarize_samples("node", samples)
        self.assertEqual(stats["median_ms"], 20)
        self.assertEqual(stats["jitter_ms"], 10)
        self.assertAlmostEqual(stats["success_rate"], 3 / 9)
        self.assertAlmostEqual(stats["score_ms"], 25 + (1 - 3 / 9) * 2000)
        self.assertFalse(stats["eligible"])

    def test_all_failures_have_no_median_or_score(self):
        stats = summarize_samples("node", [None, 0, -5, math.nan, math.inf])
        self.assertIsNone(stats["median_ms"])
        self.assertIsNone(stats["score_ms"])
        self.assertEqual(stats["success_rate"], 0)
        self.assertFalse(stats["eligible"])
        self.assertTrue(stats["reason"])

    def test_empty_samples_are_ineligible_without_division_by_zero(self):
        stats = summarize_samples("node", [])
        self.assertIsNone(stats["median_ms"])
        self.assertIsNone(stats["score_ms"])
        self.assertFalse(stats["eligible"])

    def test_high_variance_loses_to_a_slightly_slower_stable_node(self):
        unstable = summarize_samples("unstable", [100, 100, 100, 100, 1000])
        stable = summarize_samples("stable", [200] * 5)
        self.assertGreater(unstable["score_ms"], stable["score_ms"])


class SwitchPolicyTests(unittest.TestCase):
    @staticmethod
    def node(name, delay, samples=3):
        return summarize_samples(name, [delay] * samples)

    def choose(self, stats, current="current", last_switch_at=0, now=1000, **kwargs):
        result = choose_candidate(stats, current, last_switch_at, now, **kwargs)
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        self.assertIsInstance(result[1], str)
        return result[0]

    def test_switch_when_both_improvement_thresholds_are_met(self):
        stats = [self.node("current", 500), self.node("backup", 350)]
        self.assertEqual(self.choose(stats), "backup")

    def test_exact_sixty_ms_and_fifteen_percent_boundaries_allow_switch(self):
        stats = [self.node("current", 400), self.node("backup", 340)]
        self.assertEqual(self.choose(stats), "backup")

    def test_relative_improvement_alone_does_not_allow_switch(self):
        stats = [self.node("current", 200), self.node("backup", 165)]
        self.assertIsNone(self.choose(stats))

    def test_absolute_improvement_alone_does_not_allow_switch(self):
        stats = [self.node("current", 1000), self.node("backup", 930)]
        self.assertIsNone(self.choose(stats))

    def test_cooldown_blocks_an_otherwise_qualified_switch(self):
        stats = [self.node("current", 500), self.node("backup", 350)]
        self.assertIsNone(self.choose(stats, last_switch_at=999, now=1000))

    def test_exact_cooldown_boundary_allows_switch(self):
        stats = [self.node("current", 500), self.node("backup", 350)]
        self.assertEqual(self.choose(stats, last_switch_at=1000, now=1600), "backup")

    def test_explicit_current_failure_bypasses_cooldown(self):
        stats = [self.node("current", 500), self.node("backup", 350)]
        self.assertEqual(
            self.choose(stats, last_switch_at=999, now=1000, current_failed=True),
            "backup",
        )

    def test_missing_current_measurement_bypasses_cooldown(self):
        self.assertEqual(
            self.choose([self.node("backup", 250)], last_switch_at=999), "backup"
        )

    def test_ineligible_current_bypasses_cooldown(self):
        stats = [
            summarize_samples("current", [None, None, None]),
            self.node("backup", 250),
        ]
        self.assertEqual(self.choose(stats, last_switch_at=999), "backup")

    def test_no_eligible_candidate_means_no_switch_even_after_failure(self):
        stats = [
            summarize_samples("current", [None] * 3),
            summarize_samples("backup", [10, None, None]),
        ]
        self.assertIsNone(self.choose(stats, current_failed=True))

    def test_best_ineligible_node_is_never_selected(self):
        stats = [
            self.node("current", 1000),
            summarize_samples("unproven", [1, 1]),
            self.node("backup", 700),
        ]
        self.assertEqual(self.choose(stats), "backup")

    def test_already_on_best_node_means_no_switch(self):
        stats = [self.node("current", 200), self.node("backup", 300)]
        self.assertIsNone(self.choose(stats))

    def test_empty_statistics_do_not_switch(self):
        self.assertIsNone(self.choose([]))

    def test_failure_penalty_affects_selection(self):
        stats = [
            self.node("current", 1000),
            summarize_samples("lossy", [100] * 4 + [None]),
            self.node("stable", 400),
        ]
        self.assertEqual(self.choose(stats), "stable")

    def test_custom_switch_thresholds_and_cooldown_are_honored(self):
        stats = [self.node("current", 200), self.node("backup", 165)]
        self.assertEqual(
            self.choose(
                stats,
                last_switch_at=990,
                min_improvement_ms=30,
                min_improvement_ratio=0.1,
                cooldown_seconds=10,
            ),
            "backup",
        )


class CandidateDiscoveryTests(unittest.TestCase):
    def test_nested_main_group_collects_leaf_nodes_in_order_once(self):
        proxies = {
            "GLOBAL": {"type": "Selector", "all": ["Main", "DIRECT"]},
            "Main": {"type": "Selector", "all": ["Auto", "Fallback", "Hong Kong"]},
            "Auto": {"type": "URLTest", "all": ["Hong Kong", "Japan"]},
            "Fallback": {"type": "Fallback", "all": ["Japan", "Singapore"]},
            "Hong Kong": {"type": "Shadowsocks"},
            "Japan": {"type": "Trojan"},
            "Singapore": {"type": "Hysteria2"},
            "DIRECT": {"type": "Direct"},
        }
        self.assertEqual(
            flatten_candidates(proxies, "GLOBAL"), ["Hong Kong", "Japan", "Singapore"]
        )

    def test_cycle_does_not_recurse_forever_or_hide_reachable_leaf(self):
        proxies = {
            "Main": {"type": "Selector", "all": ["Loop", "node"]},
            "Loop": {"type": "URLTest", "all": ["Main", "node"]},
            "node": {"type": "VMess"},
        }
        self.assertEqual(flatten_candidates(proxies, "Main"), ["node"])

    def test_special_direct_reject_pass_and_compatible_types_are_excluded(self):
        excluded_types = ["Direct", "Reject", "RejectDrop", "REJECT-DROP", "Pass", "Compatible"]
        proxies = {
            "Main": {
                "type": "Selector",
                "all": [f"special-{i}" for i in range(len(excluded_types))] + ["node"],
            },
            "node": {"type": "WireGuard"},
        }
        proxies.update(
            {f"special-{i}": {"type": kind} for i, kind in enumerate(excluded_types)}
        )
        self.assertEqual(flatten_candidates(proxies, "Main"), ["node"])

    def test_missing_references_are_ignored(self):
        proxies = {
            "Main": {"type": "Selector", "all": ["missing", "node"]},
            "node": {"type": "Snell"},
        }
        self.assertEqual(flatten_candidates(proxies, "Main"), ["node"])

    def test_unknown_group_has_no_candidates(self):
        self.assertEqual(flatten_candidates({}, "missing"), [])

    def test_empty_and_self_referential_groups_have_no_leaf_candidates(self):
        for members in ([], ["Main"]):
            with self.subTest(members=members):
                self.assertEqual(
                    flatten_candidates({"Main": {"type": "Selector", "all": members}}, "Main"),
                    [],
                )


if __name__ == "__main__":
    unittest.main()
