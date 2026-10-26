"""Checks for media.csv parsing and the media page's log in media.py. Run: .venv/bin/python -m unittest"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import data
import media
import test_auth  # the app on a throwaway data folder, and call() to send it requests

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

    def test_dragged_titles_keep_their_place(self):
        ts = titles("2026-10-11,,Bear,show,3,4,,,,\n2026-10-18,,Dune,movie,,,,1:00:00,,\n"
                    "2026-10-21,,Frieren,anime,,19,,,,\n2026-10-22,,Severance,show,2,7,,,,\n")
        ts = sorted(ts.values(), key=lambda t: t["at"], reverse=True)
        self.assertEqual([t["title"] for t in media.arrange(ts, {})], ["Severance", "Frieren", "Dune", "Bear"])
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "todo.sqlite"
            media.init(db)
            media.set_order(db, ["Bear", "frieren", "Bear"])
            self.assertEqual(media.placed(db), {"bear": 0, "frieren": 1})
            # never dragged: on top, last watched first; dragged: in their places, whenever they were watched
            self.assertEqual([t["title"] for t in media.arrange(ts, media.placed(db))], ["Severance", "Dune", "Bear", "Frieren"])
            media.set_order(db, ["Dune", "Severance"])  # a new drag replaces the whole order
            self.assertEqual(media.placed(db), {"dune": 0, "severance": 1})
            media.init(db)  # a restart keeps it
            self.assertEqual(media.placed(db), {"dune": 0, "severance": 1})

    def test_movie(self):
        ts = titles("2026-10-18,,Dune,movie,,,,1:12:40,166,\n2026-10-18,,Clip,,,,,0:04:00,,\n2026-10-18,,Show,show,1,1,,,,\n")
        self.assertEqual([media.is_movie(ts[k]) for k in ("Dune", "Clip", "Show")], [True, True, False])
        self.assertEqual((media.clock(166 * 60), media.clock(4360)), ("2:46", "1:12:40"))

    def test_quick_add(self):
        cases = {
            "severance s2e8": ("severance", 2, 8, "", ""), "Frieren  E20": ("Frieren", None, 20, "", ""),
            "dune 1:12:40": ("dune", None, None, "1:12:40", ""), "the bear s3 e5 done": ("the bear", 3, 5, "", "done"),
            "the penguin drop": ("the penguin", None, None, "", "dropped"), "blade runner 2049": ("blade runner 2049", None, None, "", ""),
            "the bear s3e8 5:45": ("the bear", 3, 8, "5:45", ""), "frieren e20 12:30 done": ("frieren", None, 20, "12:30", "done"),
            "andor s1e2 1:02:03 drop": ("andor", 1, 2, "1:02:03", "dropped"), "done": ("done", None, None, "", ""),  # a bare word is a title
        }
        for q, want in cases.items():
            p = media.parse(q)
            self.assertEqual((p["title"], p["season"], p["episode"], p["position"], p["status"], p["error"]), (*want, None), q)

    def test_quick_add_says_why_it_logged_nothing(self):
        for q, error in {"s3e8 5:45": "title", "5:45": "title", "  ": "title", "the bear s3e8 5:75": "time",
                         "dune 1:61:00": "time"}.items():
            self.assertEqual(media.parse(q)["error"], error, q)
            self.assertIn("nothing logged", media.ERRORS[error])

    def test_paused_partway_through_an_episode(self):
        t = titles("2026-10-11,,Bear,show,3,7,10,,,\n2026-10-12,,Bear,,3,8,,5:45,,\n")["Bear"]
        self.assertTrue(media.paused(t))
        self.assertFalse(media.is_movie(t))
        self.assertEqual((t["episode"], t["position"]), (8, "5:45"))
        s3 = media.seasons(t)[-1]
        self.assertEqual((s3["watched"], [st for _, st in s3["eps"]]), (7, ["w"] * 7 + ["ip", "n", "n"]))
        self.assertEqual(media.step(t), (3, 8))  # +1 finishes the paused episode
        finished = titles("2026-10-11,,Bear,show,3,8,10,5:45,,\n2026-10-12,,Bear,,3,8,,,,\n")["Bear"]
        self.assertFalse(media.paused(finished))  # the episode without a position: done
        self.assertEqual(media.seasons(finished)[-1]["eps"][7:9], [(8, "w"), (9, "nx")])
        later = titles("2026-10-11,,Bear,show,3,8,10,5:45,,\n2026-10-12,,Bear,,,,,20:10,,\n")["Bear"]
        self.assertEqual((later["episode"], later["position"]), (8, "20:10"))  # a time alone moves within the episode
        next_season = titles("2026-10-11,,Bear,show,3,8,10,5:45,,\n2026-10-12,,Bear,,4,,,,,\n")["Bear"]
        self.assertFalse(media.paused(next_season))
        done = titles("2026-10-11,,Bear,show,3,10,10,5:45,,\n2026-10-12,,Bear,,,,,,,done\n")["Bear"]
        self.assertFalse(media.paused(done))

    def test_ago(self):
        self.assertEqual([media.ago(D - dt.timedelta(days=n), D) for n in (0, 1, 2, 6, 7)],
                         ["today", "yesterday", "Wed 21", "Sat 17", "Oct 16"])
        self.assertEqual(media.ago(dt.date(2025, 12, 31), D), "Dec 31 2025")


class MediaPageTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"LIFE_DASHBOARD_TOKEN": test_auth.TOKEN})
        patch.start()
        self.addCleanup(patch.stop)
        self.app, self.call = test_auth.app, test_auth.call
        tmp = tempfile.TemporaryDirectory()  # its own TODO_DB: test_auth's folder is gone once its tests ran
        self.addCleanup(tmp.cleanup)
        db = mock.patch.object(self.app, "TODO_DB", Path(tmp.name) / "todo.sqlite")
        db.start()
        self.addCleanup(db.stop)
        self.app.todo.init(self.app.TODO_DB)
        self.app.media.init(self.app.TODO_DB)
        self.jar = self.call("POST", "/login", {"token": test_auth.TOKEN, "next": "/media"})[1]["set-cookie"].split(";")[0]

    def finished_row(self, title: str) -> str:
        body = self.call("GET", "/media", cookie=self.jar)[2]
        finished = body[body.index("[ Finished ]"):]
        return finished[finished.index(title):finished.index("</tr>", finished.index(title))]

    def test_drop_from_the_menu(self):
        for q in ("Menu Dropped s1e2 5:45", "Menu Done s1e3"):
            self.call("POST", "/media/log", {"q": q}, cookie=self.jar)
        body = self.call("GET", "/media", cookie=self.jar)[2]
        self.assertIn('id="ctx"', body)
        self.assertIn('<button class="item warn" name="do" value="drop">drop</button>', body)
        self.assertEqual(self.call("POST", "/media/step", {"title": "Menu Dropped", "do": "drop"}, cookie=self.jar)[0], 303)
        self.call("POST", "/media/step", {"title": "Menu Done", "do": "done"}, cookie=self.jar)
        ts = {t["title"]: t for t in self.app.media_titles()}
        self.assertEqual((ts["Menu Dropped"]["status"], ts["Menu Done"]["status"]), ("dropped", "done"))
        self.assertIn('Menu Dropped <span class="warn">dropped</span></td>', self.finished_row("Menu Dropped"))
        self.assertNotIn("dropped", self.finished_row("Menu Done"))
        self.assertNotIn("5:45", self.finished_row("Menu Dropped"))  # no "at" column: finished means the whole title
        self.assertNotIn("<th>at</th>", body)

    def test_signed_out_cannot_drop(self):
        self.call("POST", "/media/log", {"q": "Kept s1e1"}, cookie=self.jar)
        self.assertNotIn('id="ctx"', self.call("GET", "/media")[2])
        self.assertEqual(self.call("POST", "/media/step", {"title": "Kept", "do": "drop"}, sec_fetch_mode="cors")[0], 403)
        self.assertEqual(self.call("POST", "/media/step", {"title": "Kept", "do": "drop"})[0], 303)  # a plain form: to the login box
        self.assertEqual(next(t for t in self.app.media_titles() if t["title"] == "Kept")["status"], "watching")


if __name__ == "__main__":
    unittest.main()
