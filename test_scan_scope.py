"""Offline checks for nearby-region scan scope and Japan-weighted retests."""

import math
import unittest

from optimizer import filter_scan_candidates, node_region, select_shortlist


class NodeRegionTests(unittest.TestCase):
    def test_common_region_names_flags_and_independent_iso_codes(self):
        cases = {
            "JP": ["日本 01", "东京 02", "大阪 03", "🇯🇵 04", "JP 05", "jp-06", "Japan 07", "TOKYO 08", "Osaka 09"],
            "TW": ["台湾 01", "臺灣 02", "台北 03", "🇹🇼 04", "TW 05", "tw-06", "Taiwan 07", "Taipei 08"],
            "SG": ["新加坡 01", "🇸🇬 02", "SG 03", "sg-04", "Singapore 05"],
            "HK": ["香港 01", "🇭🇰 02", "HK 03", "hk-04", "Hong Kong 05", "HongKong 06"],
        }
        for expected, names in cases.items():
            for name in names:
                with self.subTest(name=name):
                    self.assertEqual(node_region(name), expected)

    def test_iso_substrings_do_not_count_as_regions(self):
        for name in ("JPX", "SGP", "TWX", "HKX", "XJP", "superSG", "someTWword"):
            with self.subTest(name=name):
                self.assertIsNone(node_region(name))

    def test_actual_provider_taiwan_name_with_neutral_china_flag(self):
        name = "1.0x 🇨🇳 台湾 TW - 1"
        self.assertEqual(node_region(name), "TW")
        self.assertEqual(filter_scan_candidates([name], ["TW"]), [name])

    def test_first_supported_location_marker_controls_multi_region_names(self):
        cases = {
            "JP-TW route": "JP",
            "TW-JP route": "TW",
            "SG / JP": "SG",
            "HK-日本": "HK",
            "台湾→新加坡": "TW",
            "新加坡→香港": "SG",
            "🇯🇵 🇹🇼": "JP",
            "🇭🇰 日本": "HK",
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(node_region(name), expected)

    def test_first_far_region_marker_prevents_nearby_destination_false_match(self):
        for name in ("US-JP route", "🇺🇸 🇯🇵", "美国-日本", "US to Singapore", "美国转台湾"):
            with self.subTest(name=name):
                self.assertEqual(node_region(name), "US")
                self.assertEqual(filter_scan_candidates([name], ["JP", "TW", "SG", "HK"]), [])

    def test_other_country_flags_before_nearby_text_are_not_neutral(self):
        for name, expected in (("🇲🇾 Japan route", "MY"), ("🇬🇧 台湾 route", "GB"),
                               ("🇦🇺 SG route", "AU"), ("🇰🇷 日本 route", "KR")):
            with self.subTest(name=name):
                self.assertEqual(node_region(name), expected)
                self.assertEqual(filter_scan_candidates([name], ["JP", "TW", "SG", "HK"]), [])

    def test_nearby_first_marker_is_retained_when_later_destination_is_far(self):
        self.assertEqual(node_region("JP-US route"), "JP")
        self.assertEqual(node_region("日本→美国"), "JP")

    def test_unknown_names_are_not_guessed_to_be_nearby(self):
        for name in ("", "premium 01", "自动选择", "DIRECT", "unlabelled"):
            with self.subTest(name=name):
                self.assertIsNone(node_region(name))

    def test_regions_outside_default_scope_are_recognized(self):
        for name, expected in (("美国 01", "US"), ("US 02", "US"), ("🇺🇸 03", "US"),
                               ("UK 01", "GB"), ("英国 01", "GB"), ("Malaysia 01", "MY"),
                               ("韩国 01", "KR"), ("🇿🇦 01", "ZA")):
            with self.subTest(name=name):
                self.assertEqual(node_region(name), expected)


class ScanScopeTests(unittest.TestCase):
    def test_only_recognized_allowed_regions_are_returned_in_original_order(self):
        names = ["US 01", "TW 01", "unknown", "日本 01", "香港 01", "Singapore 01", "JPX"]
        before = names.copy()
        self.assertEqual(
            filter_scan_candidates(names, ["JP", "TW", "SG"]),
            ["TW 01", "日本 01", "Singapore 01"],
        )
        self.assertEqual(names, before)

    def test_hong_kong_is_included_only_when_allowed(self):
        names = ["香港 01", "HK 02", "JP 01"]
        self.assertEqual(filter_scan_candidates(names, ["JP"]), ["JP 01"])
        self.assertEqual(filter_scan_candidates(names, ["HK"]), ["香港 01", "HK 02"])

    def test_far_to_near_route_and_unknown_name_are_excluded(self):
        self.assertEqual(
            filter_scan_candidates(["US-JP route", "美国-日本", "unknown", "日本 01"], ["JP"]),
            ["日本 01"],
        )

    def test_empty_scope_and_empty_candidates_have_no_matches(self):
        self.assertEqual(filter_scan_candidates(["JP 01"], []), [])
        self.assertEqual(filter_scan_candidates([], ["JP", "TW", "SG", "HK"]), [])


class ShortlistTests(unittest.TestCase):
    @staticmethod
    def measurements(japan=10, others=10):
        fresh = {f"JP {index:02d}": 100 + index for index in range(1, japan + 1)}
        fresh.update({f"TW {index:02d}": index for index in range(1, others + 1)})
        return fresh

    def test_default_twelve_reserves_eight_japan_and_four_other_nodes(self):
        fresh = self.measurements()
        before = fresh.copy()
        selected = select_shortlist(fresh, None, 12)
        expected = {f"JP {i:02d}" for i in range(1, 9)} | {f"TW {i:02d}" for i in range(1, 5)}
        self.assertEqual(set(selected), expected)
        self.assertEqual(len(selected), 12)
        self.assertEqual(fresh, before)

    def test_six_node_limit_reserves_four_japan_and_two_other_nodes(self):
        selected = select_shortlist(self.measurements(), None, 6)
        expected = {f"JP {i:02d}" for i in range(1, 5)} | {"TW 01", "TW 02"}
        self.assertEqual(set(selected), expected)
        self.assertEqual(len(selected), 6)

    def test_missing_japan_quota_is_filled_with_fastest_remaining_others(self):
        selected = select_shortlist(self.measurements(japan=2, others=20), None, 12)
        expected = {"JP 01", "JP 02"} | {f"TW {i:02d}" for i in range(1, 11)}
        self.assertEqual(set(selected), expected)
        self.assertEqual(len(selected), 12)

    def test_missing_other_quota_is_filled_with_fastest_remaining_japan(self):
        selected = select_shortlist(self.measurements(japan=20, others=2), None, 12)
        expected = {f"JP {i:02d}" for i in range(1, 11)} | {"TW 01", "TW 02"}
        self.assertEqual(set(selected), expected)
        self.assertEqual(len(selected), 12)

    def test_no_japan_nodes_uses_fastest_other_nodes(self):
        selected = select_shortlist(self.measurements(japan=0, others=10), None, 6)
        self.assertEqual(set(selected), {f"TW {i:02d}" for i in range(1, 7)})

    def test_no_other_nodes_can_use_all_available_japan_nodes(self):
        selected = select_shortlist(self.measurements(japan=4, others=0), None, 12)
        self.assertEqual(set(selected), {f"JP {i:02d}" for i in range(1, 5)})
        self.assertEqual(len(selected), 4)

    def test_disabled_japan_preference_selects_overall_fastest_measurements(self):
        selected = select_shortlist(self.measurements(), None, 6, prefer_japan=False)
        self.assertEqual(set(selected), {f"TW {i:02d}" for i in range(1, 7)})

    def test_failed_and_nonfinite_initial_measurements_do_not_fill_quota(self):
        fresh = {
            "JP 01": None, "JP 02": 0, "JP 03": -1, "JP 04": math.nan,
            "JP 05": math.inf, "JP 06": 150,
            "TW 01": 100, "TW 02": 120, "HK 01": 200,
        }
        selected = select_shortlist(fresh, None, 6)
        self.assertEqual(set(selected), {"JP 06", "TW 01", "TW 02", "HK 01"})
        self.assertEqual(len(selected), 4)

    def test_current_outside_fresh_is_never_added_even_if_it_is_japanese(self):
        fresh = self.measurements(japan=3, others=3)
        selected = select_shortlist(fresh, "日本 unscanned", 6)
        self.assertNotIn("日本 unscanned", selected)
        self.assertTrue(set(selected).issubset(fresh))

    def test_slow_current_inside_scan_scope_is_retained_for_confirmation(self):
        fresh = self.measurements()
        fresh["HK current"] = 5000
        selected = select_shortlist(fresh, "HK current", 12)
        self.assertIn("HK current", selected)
        self.assertEqual(len(selected), len(set(selected)))
        self.assertTrue(set(selected).issubset(fresh))

    def test_failed_current_inside_scan_scope_can_be_retested(self):
        fresh = self.measurements(japan=3, others=3)
        fresh["SG current"] = None
        selected = select_shortlist(fresh, "SG current", 6)
        self.assertIn("SG current", selected)
        self.assertEqual(len(selected), len(set(selected)))

    def test_current_already_in_shortlist_is_not_duplicated(self):
        selected = select_shortlist(self.measurements(), "JP 01", 12)
        self.assertEqual(selected.count("JP 01"), 1)
        self.assertEqual(len(selected), 12)

    def test_empty_initial_measurements_do_not_add_an_unscanned_current(self):
        self.assertEqual(select_shortlist({}, "JP current", 12), [])


if __name__ == "__main__":
    unittest.main()
