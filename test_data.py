"""Checks for the parsing rules in data.py. Run: .venv/bin/python -m unittest"""
import datetime as dt
import json
import os
import tempfile
import unittest
from pathlib import Path

import data

HEADER = ("id,time,food,source,servings,serving_size,calories,fat_g,sat_fat_g,trans_fat_g,"
          "cholesterol_mg,sodium_mg,carbs_g,fiber_g,sugar_g,added_sugar_g,protein_g,source_url,notes\n")
D = dt.date(2026, 10, 13)


class DataTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.cfg = data.Config(self.dir)
        data._cache.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, text):
        (self.dir / name).write_text(text, encoding="utf-8")

    def test_empty_cells_are_unknown_and_totals_count_coverage(self):
        self.write("2026-10-13.csv", HEADER
                   + "1,08:10,protein shake,preset,1,,485,,,,,,,,,,72,,preset: protein shake\n"
                   + "2,12:42,Sandwich,lookup,1,1 sandwich,490,22,,,,1430,46,,,,28,https://x,standard build\n")
        rows = data.day_rows(self.cfg, D)
        self.assertIsNone(rows[0]["fat_g"])
        t = data.totals(rows)
        self.assertEqual((t["calories"], t["protein_g"], t["fat_g"]), (975, 100, 22))
        self.assertEqual((t["fat_g_known"], t["protein_g_known"], t["items"]), (1, 2, 2))

    def test_partial_last_line_is_skipped(self):
        # A read mid-append: "713" is half written as "71" with no trailing LF.
        self.write("2026-10-13.csv", HEADER + "1,16:59,A,manual,1,,100,,,,,,,,,,,,\n2,17:00,B,manual,1,,71")
        rows = data.day_rows(self.cfg, D)
        self.assertEqual([r["food"] for r in rows], ["A"])

    def test_header_only_and_missing_file_mean_not_logged(self):
        self.write("2026-10-13.csv", HEADER)
        hist = data.history(self.cfg, D, 2)
        self.assertEqual([d["logged"] for d in hist], [False, False])
        self.assertIsNone(data.average(hist, "calories"))

    def test_average_skips_unlogged_days(self):
        self.write("2026-10-11.csv", HEADER + "1,08:00,A,manual,1,,1000,,,,,,,,,,,,\n")
        self.write("2026-10-13.csv", HEADER + "1,08:00,B,manual,1,,2000,,,,,,,,,,,,\n")
        self.assertEqual(data.average(data.history(self.cfg, D, 3), "calories"), 1500)

    def test_columns_by_name_and_unknown_columns_ignored(self):
        self.write("2026-10-13.csv", "food,calories,id,extra\nApple,95,1,whatever\n")
        row = data.day_rows(self.cfg, D)[0]
        self.assertEqual((row["food"], row["calories"], row["protein_g"]), ("Apple", 95, None))

    def test_macro_mismatch_flag(self):
        base = {"protein_g": 10, "carbs_g": 10, "fat_g": 10}  # 4*10 + 4*10 + 9*10 = 170
        self.assertFalse(data.macro_mismatch({**base, "calories": 180}))
        self.assertTrue(data.macro_mismatch({**base, "calories": 250}))
        self.assertFalse(data.macro_mismatch({**base, "calories": 30}))  # under 40 kcal
        self.assertFalse(data.macro_mismatch({**base, "fat_g": None, "calories": 900}))

    def test_cache_rereads_on_change(self):
        self.write("2026-10-13.csv", HEADER + "1,08:00,A,manual,1,,100,,,,,,,,,,,,\n")
        self.assertEqual(len(data.day_rows(self.cfg, D)), 1)
        self.write("2026-10-13.csv", HEADER + "1,08:00,A,manual,1,,100,,,,,,,,,,,,\n"
                   "2,09:00,B,manual,1,,200,,,,,,,,,,,,\n")
        self.assertEqual(len(data.day_rows(self.cfg, D)), 2)

    def test_weights_sorted_and_stats_match_weight_show(self):
        self.write("weight.csv", "id,date,time,weight,unit,weight_kg,source,notes\n"
                   "1,2026-10-06,07:10,66.2,kg,66.20,manual,\n"
                   "2,2026-10-13,18:40,144,lb,65.32,manual,after gym\n"
                   "3,2026-10-13,07:05,65,kg,65.00,photo,\n")  # backdated: appended last
        ws = data.weights(self.cfg)
        self.assertEqual([w["id"] for w in ws], [1, 3, 2])
        week = data.weights_in(ws, D, 7)
        self.assertEqual([w["id"] for w in week], [3, 2])  # Oct 6 is outside 7 days ending Oct 13
        s = data.weight_stats(week)
        self.assertAlmostEqual(s["mean"], 65.16)
        self.assertAlmostEqual(s["change"], 0.32)
        self.assertAlmostEqual(data.to_unit(45.359237, "lb"), 100)

    def test_presets_one_serving_totals(self):
        self.write("presets.json", json.dumps({"version": 1, "presets": {"usual breakfast": {
            "name": "usual breakfast", "items": [
                {"food": "egg", "servings": 2.0, "calories": 72.0, "protein_g": 6.3},
                {"food": "bread", "servings": 1.0, "calories": 110.0, "fiber_g": 5.0}]}}}))
        per = data.presets(self.cfg)[0]["per"]
        self.assertEqual(per["calories"], 254)
        self.assertAlmostEqual(per["protein_g"], 12.6)
        self.assertEqual(per["protein_g_known"], 1)
        self.assertIsNone(per["fat_g"])

    def test_missing_presets_and_conflicts(self):
        self.assertEqual(data.presets(self.cfg), [])
        self.write("2026-10-13.sync-conflict-20261013-120000-ABC.csv", HEADER)
        self.write(".2026-10-13.csv.ab12cd.tmp", "junk")
        self.assertEqual(data.conflicts(self.cfg), ["2026-10-13.sync-conflict-20261013-120000-ABC.csv"])

    def test_logical_today_respects_day_start_hour(self):
        cfg = data.Config(self.dir, day_start_hour=4)
        self.assertEqual(data.logical_today(cfg, dt.datetime(2026, 10, 14, 1, 30)), D)
        self.assertEqual(data.logical_today(cfg, dt.datetime(2026, 10, 14, 4, 0)), D + dt.timedelta(1))

    def test_config_env_overrides_settings(self):
        settings = self.dir / "settings.json"
        settings.write_text(json.dumps({"env": {"NUTRITION_DIR": "~/Sync/nutrition",
                                                "NUTRITION_WEIGHT_UNIT": "lb"}}))
        old = dict(os.environ)
        try:
            os.environ.update(LIFE_DASHBOARD_SETTINGS=str(settings), NUTRITION_DAY_START_HOUR="4")
            os.environ.pop("NUTRITION_DIR", None)
            cfg = data.load_config()
        finally:
            os.environ.clear()
            os.environ.update(old)
        self.assertEqual(cfg, data.Config(Path("~/Sync/nutrition").expanduser(), 4, "lb"))


if __name__ == "__main__":
    unittest.main()
