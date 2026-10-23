"""Checks for media.csv parsing and the media page's log in media.py. Run: .venv/bin/python -m unittest"""
import datetime as dt
import tempfile
import unittest
from pathlib import Path

import data
import media

HEADER = "date,time,title,kind,season,episode,episodes,position,runtime,status\n"
D = dt.date(2026, 10, 23)


def titles(text: str, log_rows=()) -> dict[str, dict]:
    return {t["title"]: t for t in media.titles(media.merged(data._parse_media(HEADER + text), list(log_rows)))}


class MediaTest(unittest.TestCase):
    def test_rows_skip_bad_lines_and_bad_positions(self):
        rows = data._parse_media(HEADER + "2026-10-20,21:00,Dune,movie,,,,1:12:40,166,\n"
                                 "not-a-date,21:00,X,,,,,,,\n2026-10-20,,,,,,,,,\n2026-10-20,21:00,Y,,x,,,,,\n"
                                 "2026-10-21,,Dune,,,,,1:75,,\n2026-10-22,,Frieren")  # partial last line
        self.assertEqual([(r["title"], r["position"], r["runtime"]) for r in rows], [("Dune", "1:12:40", 166), ("Dune", "", None)])
        self.assertEqual((data.seconds("1:12:40"), data.seconds("45:10"), data.seconds("1:2:3:4"), data.seconds("")),
                         (4360, 2710, None, None))

    def test_later_rows_win_and_blanks_keep_earlier(self):
        t = titles("2026-10-15,21:00,Severance,show,1,9,9,,,\n2026-10-16,21:00,severance,,2,,10,,,\n"
                   "2026-10-22,21:30,Severance,,,7,,,,\n")["Severance"]
        self.assertEqual((t["kind"], t["season"], t["episode"], t["episodes"], t["status"], t["last"]),
                         ("show", 2, 7, {1: 9, 2: 10}, "watching", dt.date(2026, 10, 22)))

    def test_a_new_season_starts_before_its_first_episode(self):
        t = titles("2026-10-15,,Bear,show,2,10,10,,,\n2026-10-16,,Bear,,3,,,,,\n")["Bear"]
        self.assertEqual((t["season"], t["episode"]), (3, None))
        self.assertEqual(media.seasons(t)[-1]["eps"], [(1, "nx")])

    def test_seasons_ticks(self):
        t = titles("2026-10-15,,S,show,1,9,9,,,\n2026-10-16,,S,,2,7,10,,,\n")["S"]
        s1, s2 = media.seasons(t)
        self.assertEqual((s1["season"], s1["watched"], s1["total"], {st for _, st in s1["eps"]}), (1, 9, 9, {"w"}))
        self.assertEqual([st for _, st in s2["eps"]], ["w"] * 7 + ["nx", "n", "n"])
        unknown = titles("2026-10-16,,A,anime,,19,,,,\n")["A"]  # no season, no length: watched plus the next one
        self.assertEqual([(s["season"], len(s["eps"]), s["eps"][-1]) for s in media.seasons(unknown)], [(None, 20, (20, "nx"))])
        done = titles("2026-10-16,,S,show,2,7,10,,,\n2026-10-17,,S,,,,,,,done\n")["S"]
        self.assertNotIn("nx", [st for _, st in media.seasons(done)[-1]["eps"]])
        gap = titles("2026-10-16,,G,show,3,2,,,,\n")["G"]  # earlier seasons of unknown length are not invented
        self.assertEqual([s["season"] for s in media.seasons(gap)], [3])

    def test_step(self):
        ts = titles("2026-10-16,,S,show,2,7,10,,,\n2026-10-16,,E,show,1,8,8,,,\n2026-10-16,,A,anime,,19,28,,,\n"
                    "2026-10-16,,N,,,,,,,\n")
        self.assertEqual([media.step(ts[k]) for k in "SEAN"], [(2, 8), (2, 1), (None, 20), (None, 1)])

    def test_page_log_merges_with_csv_in_time_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "todo.sqlite"
            media.init(db)
            media.log(db, {"date": D, "time": "09:00:00", "title": "Severance", "season": 2, "episode": 8, "status": "watching"})
            log_rows = media.page_rows(db)
        self.assertEqual(log_rows[0]["position"], "")  # NULL reads back as blank, like an empty cell
        ts = media.titles(media.merged(data._parse_media(HEADER + "2026-10-22,21:30,Severance,show,2,7,10,,,\n"
                                                                  "2026-10-23,21:00,Dune,movie,,,,0:30:00,,\n"), log_rows))
        self.assertEqual([(t["title"], t["episode"], t["kind"]) for t in ts], [("Dune", None, "movie"), ("Severance", 8, "show")])
        later = media.titles(media.merged(data._parse_media(HEADER + "2026-10-23,10:00,Severance,,2,9,,,,\n"), log_rows))
        self.assertEqual(later[0]["episode"], 9)  # a later media.csv row wins over the page's

    def test_movie(self):
        ts = titles("2026-10-18,,Dune,movie,,,,1:12:40,166,\n2026-10-18,,Clip,,,,,0:04:00,,\n2026-10-18,,Show,show,1,1,,,,\n")
        self.assertEqual([media.is_movie(ts[k]) for k in ("Dune", "Clip", "Show")], [True, True, False])
        self.assertEqual((media.clock(166 * 60), media.clock(4360)), ("2:46", "1:12:40"))

    def test_quick_add(self):
        cases = {
            "severance s2e8": ("severance", 2, 8, "", ""), "Frieren  E20": ("Frieren", None, 20, "", ""),
            "dune 1:12:40": ("dune", None, None, "1:12:40", ""), "the bear s3 e5 done": ("the bear", 3, 5, "", "done"),
            "the penguin drop": ("the penguin", None, None, "", "dropped"), "blade runner 2049": ("blade runner 2049", None, None, "", ""),
            "12:75": ("12:75", None, None, "", ""), "done": ("done", None, None, "", ""),  # a bare word is a title
        }
        for q, want in cases.items():
            p = media.parse(q)
            self.assertEqual((p["title"], p["season"], p["episode"], p["position"], p["status"]), want, q)

    def test_ago(self):
        self.assertEqual([media.ago(D - dt.timedelta(days=n), D) for n in (0, 1, 2, 6, 7)],
                         ["today", "yesterday", "Wed 21", "Sat 17", "Oct 16"])
        self.assertEqual(media.ago(dt.date(2025, 12, 31), D), "Dec 31 2025")


if __name__ == "__main__":
    unittest.main()
