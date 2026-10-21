"""Checks for the todo parser, repeats and storage. Run: .venv/bin/python -m unittest"""
import datetime as dt
import os
import tempfile
import unittest
from pathlib import Path

import todo

T = dt.date(2026, 10, 20)  # a Tuesday


def d(month, day, year=2026):
    return dt.date(year, month, day)


class ParseTest(unittest.TestCase):
    def p(self, text, **defaults):
        return todo.parse(text, T, **defaults)

    def test_tokens_come_out_and_the_rest_is_the_title(self):
        t = self.p("submit lab report thu 5pm p1 #School")
        self.assertEqual((t["title"], t["due"], t["due_time"], t["priority"], t["project"], t["repeat"]),
                         ("submit lab report", d(10, 22), "17:00", 1, "school", None))

    def test_dates(self):
        cases = {"today": T, "tomorrow": d(10, 21), "in 3 days": d(10, 23), "next week": d(10, 26),
                 "oct 25": d(10, 25), "25 oct": d(10, 25), "10/25": d(10, 25), "2026-12-01": d(12, 1),
                 "fri": d(10, 23), "friday": d(10, 23), "tue": d(10, 27),  # never today
                 "oct 18": d(10, 18, 2027), "jan 5": d(1, 5, 2027)}  # passed this year: next year
        for text, due in cases.items():
            with self.subTest(text):
                t = self.p("x " + text)
                self.assertEqual((t["title"], t["due"]), ("x", due))

    def test_times(self):
        for text, time in {"5pm": "17:00", "9:30am": "09:30", "12am": "00:00", "17:00": "17:00", "7 pm": "19:00"}.items():
            with self.subTest(text):
                t = self.p("x tomorrow " + text)
                self.assertEqual((t["due"], t["due_time"]), (d(10, 21), time))
        self.assertEqual(self.p("x 5pm")["due"], T)  # a time alone means today

    def test_repeats(self):
        cases = {"every day": ("every day", T), "daily": ("every day", T), "every week": ("every week", T),
                 "every 2 weeks": ("every 2 weeks", T), "every 1 week": ("every week", T),
                 "every month": ("every month", T), "every weekday": ("every weekday", T),
                 "every sat": ("every week", d(10, 24)), "every tuesday": ("every week", T),
                 "every week oct 31": ("every week", d(10, 31))}
        for text, (rule, due) in cases.items():
            with self.subTest(text):
                t = self.p("x " + text)
                self.assertEqual((t["title"], t["repeat"], t["due"]), ("x", rule, due))

    def test_only_whole_words_and_valid_dates(self):
        for text in ("buy sunscreen", "fix monitor", "call mon5", "feb 30 deadline", "p5 thing", "x 13/45"):
            with self.subTest(text):
                t = self.p(text)
                self.assertEqual((t["title"], t["due"], t["priority"]), (text, None, 4))

    def test_page_defaults(self):
        self.assertEqual(self.p("x", due=T)["due"], T)
        self.assertEqual(self.p("x tomorrow", due=T)["due"], d(10, 21))
        self.assertEqual(self.p("x", project="home")["project"], "home")
        self.assertEqual(self.p("x #code", project="home")["project"], "code")

    def test_edit_form_is_checked(self):
        f = todo.from_form({"title": "  pay  rent ", "due": "", "due_time": "25:00", "priority": "9",
                            "project": "#Side Project", "repeat": "every month"}, T)
        self.assertEqual(f, {"title": "pay rent", "project": "side-project", "priority": 4, "due": T,
                             "due_time": None, "repeat": "every month"})
        self.assertIsNone(todo.from_form({"title": " "}, T))
        self.assertIsNone(todo.from_form({"title": "x", "repeat": "every ; drop"}, T)["repeat"])


class RepeatTest(unittest.TestCase):
    def test_next_due(self):
        cases = [("every day", T, d(10, 21)), ("every week", T, d(10, 27)),
                 ("every week", d(10, 10), d(10, 24)),  # overdue: skip to the first date after today
                 ("every month", d(10, 31), d(11, 30)), ("every year", d(2, 29, 2024), d(2, 28, 2027)),
                 ("every weekday", d(10, 23), d(10, 26)), ("every 2 weeks", T, d(11, 3)),
                 ("every 3 days", None, d(10, 23))]
        for rule, due, nxt in cases:
            with self.subTest(rule=rule, due=due):
                self.assertEqual(todo.next_due(rule, due, T), nxt)


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "sub" / "todo.sqlite"
        todo.init(self.db)

    def tearDown(self):
        self.tmp.cleanup()

    def add(self, text):
        return todo.add(self.db, todo.parse(text, T), dt.datetime(2026, 10, 20, 9))

    def test_tick_and_untick(self):
        a, r = self.add("call dentist"), self.add("groceries every week #home")
        todo.tick(self.db, a, T)
        todo.tick(self.db, r, T)
        ts = todo.tasks(self.db, T)
        self.assertEqual({t["id"]: (t["due"], t["done_on"]) for t in ts}, {a: (None, T), r: (d(10, 27), T)})
        g = {x["label"]: [t["id"] for t in x["rows"]] for x in todo.groups(ts, T, "today")}
        self.assertEqual((g["today"], g["done"]), ([], [a, r]))
        self.assertEqual(todo.counts(ts, T), {"inbox": 0, "today": 0, "projects": [("home", 1)]})
        # tomorrow only the repeat is left; the finished task is gone
        self.assertEqual([t["id"] for t in todo.tasks(self.db, d(10, 21))], [r])
        todo.untick(self.db, r)
        todo.untick(self.db, a)
        self.assertEqual({t["id"]: (t["due"], t["done_on"]) for t in todo.tasks(self.db, T)},
                         {a: (None, None), r: (T, None)})

    def test_move_keeps_time_and_repeat(self):
        r = self.add("standup 9am every weekday")
        todo.tick(self.db, r, T)
        todo.move(self.db, r, d(10, 30))
        t = todo.get(self.db, r)
        self.assertEqual((t["due"], t["due_time"], t["repeat"], t["prev_due"]), (d(10, 30), "09:00", "every weekday", None))

    def test_update_and_delete(self):
        a = self.add("x tomorrow")
        todo.update(self.db, a, todo.from_form({"title": "y", "project": "code"}, T))
        t = todo.get(self.db, a)
        self.assertEqual((t["title"], t["due"], t["project"]), ("y", None, "code"))
        todo.delete(self.db, a)
        self.assertIsNone(todo.get(self.db, a))

    def test_never_inside_nutrition_dir(self):
        old = os.environ.get("TODO_DB")
        try:
            os.environ["TODO_DB"] = self.tmp.name + "/nutrition/todo.sqlite"
            with self.assertRaises(RuntimeError):
                todo.db_path(Path(self.tmp.name) / "nutrition")
            self.assertEqual(todo.db_path(Path(self.tmp.name) / "other").name, "todo.sqlite")
        finally:
            os.environ.pop("TODO_DB") if old is None else os.environ.update(TODO_DB=old)


class ViewTest(unittest.TestCase):
    def task(self, i, due=None, project=None, repeat=None, done_on=None, due_time=None):
        return {"id": i, "title": f"t{i}", "due": due, "due_time": due_time, "project": project,
                "priority": 4, "repeat": repeat, "done_on": done_on, "prev_due": None}

    def test_upcoming_has_every_day_of_the_week(self):
        ts = [self.task(1, d(10, 18)), self.task(2, d(10, 22)), self.task(3, d(11, 2)), self.task(4)]
        g = todo.groups(ts, T, "upcoming")
        self.assertEqual([x["label"] for x in g],
                         ["overdue", "Tue 20", "Wed 21", "Thu 22", "Fri 23", "Sat 24", "Sun 25", "Mon 26", "later"])
        self.assertEqual([[t["id"] for t in x["rows"]] for x in g], [[1], [], [], [2], [], [], [], [], [3]])

    def test_row_date_and_meta(self):
        day_group, date_group = {"done": False, "mode": "time"}, {"done": False, "mode": "date"}
        done_group = {"done": True, "mode": "date"}
        t = self.task(1, d(10, 18), "home", "every week", due_time="09:00")
        self.assertEqual(todo.when(t, date_group, T), ("Oct 18 09:00", "warn"))
        self.assertEqual(todo.when(t, day_group, T), ("09:00", ""))
        self.assertEqual(todo.when(self.task(2, T), day_group, T), ("today", "faint"))
        self.assertEqual(todo.when(self.task(3), date_group, T), ("+ date", "faint"))
        self.assertEqual(todo.when(self.task(4, d(10, 23)), date_group, T), ("Fri 23", ""))
        self.assertIsNone(todo.when(t, done_group, T))
        self.assertEqual(todo.meta(t, date_group, T, True), [("↻ weekly", ""), ("#home", "")])
        self.assertEqual(todo.meta(t, done_group, T, False), [("↻ next Oct 18", "")])
        self.assertEqual(todo.day_label(d(1, 5, 2027), T, long=True), "Tue Jan 5 2027")


if __name__ == "__main__":
    unittest.main()
