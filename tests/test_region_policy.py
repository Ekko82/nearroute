"""Offline checks for the user's Japan-first node preference."""

import copy
import unittest

from optimizer import apply_region_policy, choose_candidate, is_japan_node, summarize_samples


def row(name, median=200, *, score=None, rate=1.0, samples=5, eligible=True):
    return {
        "name": name,
        "median_ms": median,
        "jitter_ms": 0 if median is not None else None,
        "success_rate": rate,
        "samples": samples,
        "score_ms": median + (1 - rate) * 2000 if score is None and median is not None else score,
        "eligible": eligible,
        "reason": "known-stable" if eligible else "known-ineligible",
    }


class JapanNameTests(unittest.TestCase):
    def test_recognizes_japanese_names_and_case_insensitive_locations(self):
        for name in (
            "日本 01", "精品日本节点", "🇯🇵 02", "JP", "jp-03", "节点 JP 04",
            "Japan 05", "japan 06", "TOKYO 07", "Osaka 08", "东京 09", "大阪 10",
        ):
            with self.subTest(name=name):
                self.assertTrue(is_japan_node(name))

    def test_jp_inside_another_word_is_not_a_japan_match(self):
        for name in ("JPX", "JPX-01", "XJP", "SJPT", "superJP", "Hong Kong 01", "美国 02", ""):
            with self.subTest(name=name):
                self.assertFalse(is_japan_node(name))


class RegionPolicyTests(unittest.TestCase):
    def apply(self, stats, **kwargs):
        before = copy.deepcopy(stats)
        result = apply_region_policy(stats, **kwargs)
        self.assertEqual(stats, before, "the policy must not mutate input rows")
        self.assertEqual([item["name"] for item in result], [item["name"] for item in stats])
        self.assertIsNot(result, stats)
        for original, copied in zip(stats, result):
            self.assertIsNot(original, copied)
        return {item["name"]: item for item in result}

    def test_normal_japan_is_preferred_even_when_other_region_is_far_faster(self):
        result = self.apply([row("日本 01", 300), row("Singapore", 50)])
        self.assertTrue(result["日本 01"]["eligible"])
        self.assertFalse(result["Singapore"]["eligible"])
        self.assertIn("日本", result["Singapore"]["reason"])
        self.assertIn("备用", result["Singapore"]["reason"])

    def test_fastest_healthy_japan_prevents_fallback_due_to_a_slow_japan(self):
        result = self.apply([row("日本 slow", 900), row("日本 good", 250), row("Singapore", 100)])
        self.assertTrue(result["日本 slow"]["eligible"])
        self.assertTrue(result["日本 good"]["eligible"])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_latency_and_ratio_equality_allow_other_regions(self):
        result = self.apply([row("日本 01", 500, score=500), row("Singapore", 300, score=300)])
        self.assertTrue(result["日本 01"]["eligible"])
        self.assertTrue(result["Singapore"]["eligible"])

    def test_latency_just_below_threshold_keeps_japan_preference(self):
        result = self.apply([row("日本 01", 499.999, score=500), row("Singapore", 100)])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_high_jitter_score_alone_does_not_trigger_latency_fallback(self):
        japan = row("日本 01", 200, score=1000)
        japan["jitter_ms"] = 1600
        result = self.apply([japan, row("Singapore", 200)])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_slow_japan_does_not_fallback_without_large_enough_score_improvement(self):
        result = self.apply([row("日本 01", 500, score=500), row("Singapore", 300.001, score=300.001)])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_fallback_opens_only_foreign_candidates_that_individually_qualify(self):
        result = self.apply([
            row("日本 01", 600, score=600),
            row("Singapore", 200, score=200),
            row("Hong Kong", 550, score=550),
            row("broken", None, eligible=False),
        ])
        self.assertTrue(result["Singapore"]["eligible"])
        self.assertFalse(result["Hong Kong"]["eligible"])
        self.assertFalse(result["broken"]["eligible"])

    def test_eligible_other_region_is_required_to_trigger_fallback(self):
        result = self.apply([
            row("日本 01", 500, score=500),
            row("unproven", 1, eligible=False),
            row("Singapore", 350, score=350),
        ])
        self.assertFalse(result["unproven"]["eligible"])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_unreliable_japan_can_fallback_at_five_samples_and_ninety_five_percent_other_success(self):
        result = self.apply([
            row("日本 01", 100, score=500, rate=0.8, samples=5),
            row("Singapore", 200, score=300, rate=0.95, samples=20),
        ])
        self.assertTrue(result["Singapore"]["eligible"])

    def test_exact_ninety_percent_japan_success_does_not_trigger_reliability_fallback(self):
        result = self.apply([
            row("日本 01", 100, score=300, rate=0.9, samples=10),
            row("Singapore", 50, score=50, rate=1),
        ])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_less_than_five_japan_samples_does_not_trigger_reliability_fallback(self):
        result = self.apply([
            row("日本 01", 100, score=500, rate=0.8, samples=4),
            row("Singapore", 100, score=100, rate=1),
        ])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_other_success_just_below_ninety_five_percent_does_not_trigger_reliability_fallback(self):
        result = self.apply([
            row("日本 01", 100, score=500, rate=0.8, samples=5),
            row("Singapore", 100, score=200, rate=0.94999, samples=20),
        ])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_reliability_fallback_also_requires_score_ratio(self):
        result = self.apply([
            row("日本 01", 100, score=500, rate=0.8, samples=5),
            row("Singapore", 250, score=300.001, rate=0.95, samples=20),
        ])
        self.assertFalse(result["Singapore"]["eligible"])

    def test_reliable_alternative_does_not_open_a_less_reliable_foreign_node(self):
        result = self.apply([
            row("日本 01", 100, score=500, rate=0.8, samples=5),
            row("Hong Kong", 50, score=250, rate=0.9, samples=10),
            row("Singapore", 200, score=300, rate=0.95, samples=20),
        ])
        self.assertTrue(result["Singapore"]["eligible"])
        self.assertFalse(result["Hong Kong"]["eligible"])

    def test_unreliable_japan_falls_back_to_healthy_hong_kong_instead_of_fast_lossy_singapore(self):
        stats = [
            summarize_samples("日本 current", [450] * 4 + [None]),
            summarize_samples("HK healthy", [500] * 5),
            summarize_samples("SG lossy", [10] * 4 + [None]),
        ]
        self.assertAlmostEqual(stats[0]["score_ms"], 850)
        self.assertAlmostEqual(stats[0]["success_rate"], 0.8)
        self.assertAlmostEqual(stats[1]["score_ms"], 500)
        self.assertAlmostEqual(stats[2]["score_ms"], 410)
        result = self.apply(stats)
        self.assertTrue(result["HK healthy"]["eligible"])
        self.assertFalse(result["SG lossy"]["eligible"])
        candidate, _ = choose_candidate(list(result.values()), "日本 current", 0, 1000)
        self.assertEqual(candidate, "HK healthy")

    def test_healthy_foreign_standby_cannot_return_to_japan_during_cooldown(self):
        result = self.apply([row("日本 preferred", 300), row("HK current", 100)])
        self.assertTrue(result["HK current"].get("region_standby"))
        candidate, _ = choose_candidate(
            list(result.values()), "HK current", 1000, 1100, cooldown_seconds=600
        )
        self.assertIsNone(candidate)

    def test_healthy_foreign_standby_returns_to_japan_after_cooldown_without_speed_improvement(self):
        result = self.apply([row("日本 preferred", 300), row("HK current", 100)])
        self.assertTrue(result["HK current"].get("region_standby"))
        candidate, _ = choose_candidate(
            list(result.values()), "HK current", 1000, 1600, cooldown_seconds=600,
            min_improvement_ms=60, min_improvement_ratio=0.15,
        )
        self.assertEqual(candidate, "日本 preferred")

    def test_missing_eligible_japan_allows_other_regions(self):
        result = self.apply([
            row("日本 unavailable", None, eligible=False),
            row("Singapore", 250),
        ])
        self.assertFalse(result["日本 unavailable"]["eligible"])
        self.assertTrue(result["Singapore"]["eligible"])

    def test_no_japan_rows_preserves_other_region_eligibility(self):
        result = self.apply([row("Singapore", 250), row("unavailable", None, eligible=False)])
        self.assertTrue(result["Singapore"]["eligible"])
        self.assertFalse(result["unavailable"]["eligible"])

    def test_every_node_unavailable_remains_unavailable(self):
        result = self.apply([row("日本", None, eligible=False), row("Singapore", None, eligible=False)])
        self.assertFalse(any(item["eligible"] for item in result.values()))

    def test_disabled_preference_preserves_reasons_and_eligibility(self):
        stats = [row("日本", 300), row("Singapore", 50), row("unavailable", None, eligible=False)]
        result = self.apply(stats, prefer_japan=False)
        for item in stats:
            self.assertEqual(result[item["name"]]["eligible"], item["eligible"])
            self.assertEqual(result[item["name"]]["reason"], item["reason"])

    def test_empty_list_is_supported(self):
        self.assertEqual(self.apply([]), {})

    def test_extra_fields_are_preserved_without_polluting_inputs(self):
        japan = row("日本", 300)
        japan.update(display_name="Tokyo premium", health={"http_status": 405}, direct_selectable=True)
        other = row("Singapore", 50)
        other.update(history=[100, 50, 80], direct_selectable=False)
        result = self.apply([japan, other])
        self.assertEqual(result["日本"]["display_name"], "Tokyo premium")
        self.assertEqual(result["日本"]["health"], {"http_status": 405})
        self.assertTrue(result["日本"]["direct_selectable"])
        self.assertEqual(result["Singapore"]["history"], [100, 50, 80])
        self.assertFalse(result["Singapore"]["direct_selectable"])

    def test_custom_poor_latency_threshold_is_honored(self):
        stats = [row("日本", 600, score=600), row("Singapore", 200, score=200)]
        result = self.apply(stats, poor_latency_ms=700)
        self.assertFalse(result["Singapore"]["eligible"])

    def test_custom_fallback_ratio_is_honored(self):
        stats = [row("日本", 600, score=600), row("Singapore", 400, score=400)]
        default = self.apply(stats)
        widened = self.apply(stats, fallback_ratio=0.7)
        self.assertFalse(default["Singapore"]["eligible"])
        self.assertTrue(widened["Singapore"]["eligible"])


if __name__ == "__main__":
    unittest.main()
