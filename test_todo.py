"""Checks for the todo parser, repeats and storage. Run: .venv/bin/python -m unittest"""
import datetime as dt
import os
import sqlite3
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

    def test_token_spans_for_highlighting(self):
        text = "submit lab report thu 5pm p1 #School every week"
        t = self.p(text)
        self.assertEqual([(text[a:b], kind) for a, b, kind in t["tokens"]],
                         [("thu", "date"), ("5pm", "time"), ("p1", "p1"), ("#School", "project"), ("every week", "repeat")])
        self.assertEqual(self.p("x  in 3 days  9:30 am")["tokens"], [(3, 12, "date"), (14, 21, "time")])

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
        self.assertEqual(self.p("x 4pm", due=d(10, 25))["due"], d(10, 25))  # a time on an Upcoming day stays on that day
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
        self.assertEqual(todo.counts(ts, T, todo.projects(self.db)), {"inbox": 0, "today": 0, "projects": [("home", 1)]})
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
        todo.move(self.db, r, None)  # no date also drops the time
        t = todo.get(self.db, r)
        self.assertEqual((t["due"], t["due_time"], t["repeat"]), (None, None, "every weekday"))

    def test_a_moved_task_goes_last_in_its_new_day(self):
        a, b = self.add("a tomorrow"), self.add("b tomorrow")
        c = self.add("c today")  # newest id, but moved last
        todo.move(self.db, a, d(10, 22))
        todo.move(self.db, c, d(10, 22))
        todo.move(self.db, b, d(10, 22))
        day = todo.groups(todo.tasks(self.db, T), T, "upcoming")[3]
        self.assertEqual([t["title"] for t in day["rows"]], ["a", "c", "b"])

    def test_drag_and_drop_order(self):
        early, p1 = self.add("standup thu 9am"), self.add("urgent thing thu p1")
        a, b = self.add("a today p1"), self.add("b today 8am")
        day = lambda: [t["title"] for t in todo.groups(todo.tasks(self.db, T), T, "upcoming")[3]["rows"]]
        self.assertEqual(day(), ["standup", "urgent thing"])  # time, then priority
        todo.drop(self.db, a, d(10, 22), [early, p1, a])  # dropped at the bottom
        todo.drop(self.db, b, d(10, 22), [early, p1, a, b])  # an earlier time still stays where it was dropped
        self.assertEqual(day(), ["standup", "urgent thing", "a", "b"])
        todo.drop(self.db, b, d(10, 22), [b, early, p1, a])  # reordered within the day
        self.assertEqual(day(), ["b", "standup", "urgent thing", "a"])
        later = self.add("later thu 7am p1")  # added later: below the placed ones, whatever its time
        self.assertEqual(day()[-1], "later")
        todo.update(self.db, b, {**todo.get(self.db, b), "title": "b2"})  # an edit on the same day keeps the slot
        self.assertEqual(day()[0], "b2")
        todo.move(self.db, b, d(10, 23))  # the date menu clears it
        self.assertIsNone(todo.get(self.db, b)["day_order"])
        self.assertNotIn("b2", day())

    def test_projects_persist_rename_and_delete(self):
        a, b = self.add("a #home"), self.add("b #home")
        todo.tick(self.db, a, T)
        todo.tick(self.db, b, T)
        self.assertEqual(todo.projects(self.db), ["home"])  # still there with no open task
        self.assertEqual(todo.counts(todo.tasks(self.db, d(10, 21)), T, todo.projects(self.db))["projects"], [("home", 0)])
        c = self.add("c #code")
        todo.rename_project(self.db, "home", "house")
        self.assertEqual((todo.projects(self.db), todo.get(self.db, a)["project"]), (["code", "house"], "house"))
        todo.rename_project(self.db, "code", "house")  # onto an existing name: merged
        self.assertEqual((todo.projects(self.db), todo.get(self.db, c)["project"]), (["house"], "house"))
        todo.delete_project(self.db, "house")  # tasks move to the inbox, never deleted
        self.assertEqual((todo.projects(self.db), todo.get(self.db, c)["project"]), ([], None))

    def test_old_files_gain_the_new_columns(self):
        old = Path(self.tmp.name) / "old.sqlite"
        con = sqlite3.connect(old)
        con.execute("CREATE TABLE task (id INTEGER PRIMARY KEY, title TEXT NOT NULL, project TEXT,"  # before pos
                    " priority INTEGER NOT NULL DEFAULT 4, due TEXT, due_time TEXT, repeat TEXT, done_on TEXT,"
                    " prev_due TEXT, created_at TEXT NOT NULL)")
        con.execute("INSERT INTO task (title, created_at) VALUES ('x', '2026-10-01')")
        con.commit()
        con.close()
        con = sqlite3.connect(old)
        con.execute("UPDATE task SET project = 'home'")
        con.commit()
        con.close()
        todo.init(old)
        self.assertEqual([t["title"] for t in todo.tasks(old, T)], ["x"])
        self.assertEqual(todo.projects(old), ["home"])  # project names copied into the new table
        todo.move(old, 1, T)
        todo.drop(old, 1, T, [1])
        self.assertEqual((todo.get(old, 1)["pos"], todo.get(old, 1)["day_order"]), (2, 0))

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


class DateOptionsTest(unittest.TestCase):
    def test_weekend_is_today_on_a_weekend(self):
        cases = {T: (d(10, 21), d(10, 24), d(10, 26)),            # Tue
                 d(10, 23): (d(10, 24), d(10, 24), d(10, 26)),    # Fri: tomorrow is the weekend
                 d(10, 24): (d(10, 25), d(10, 24), d(10, 26)),    # Sat
                 d(10, 25): (d(10, 26), d(10, 25), d(10, 26)),    # Sun
                 d(10, 26): (d(10, 27), d(10, 31), d(11, 2))}     # Mon: next week is a week away
        for today, dates in cases.items():
            with self.subTest(today=today):
                opts = todo.date_options(today)
                self.assertEqual(opts[0], ("today", "today", today))
                self.assertEqual(tuple(x[2] for x in opts[1:4]), dates)
                self.assertEqual(opts[3][2], todo.parse("x next week", today)["due"])
                self.assertEqual(opts[4], ("none", "no date", None))


class ViewTest(unittest.TestCase):
    def task(self, i, due=None, project=None, repeat=None, done_on=None, due_time=None):
        return {"id": i, "title": f"t{i}", "due": due, "due_time": due_time, "project": project,
                "priority": 4, "repeat": repeat, "done_on": done_on, "prev_due": None}

    def test_upcoming_has_every_day_of_the_week(self):
        ts = [self.task(1, d(10, 18)), self.task(2, d(10, 22)), self.task(3, d(11, 2)), self.task(4)]
        g = todo.groups(ts, T, "upcoming")
        self.assertEqual([(x["label"], x["note"]) for x in g],
                         [("Overdue", ""), ("Oct 20", "Today"), ("Oct 21", "Tomorrow"), ("Oct 22", "Thursday"),
                          ("Oct 23", "Friday"), ("Oct 24", "Saturday"), ("Oct 25", "Sunday"), ("Oct 26", "Monday")])
        self.assertEqual([[t["id"] for t in x["rows"]] for x in g], [[1], [], [], [2], [], [], [], []])
        later = todo.groups(ts, T, "upcoming", first=d(11, 2))  # a later week: no overdue group
        self.assertEqual([x["day"] for x in later][:2], [d(11, 2), d(11, 3)])
        self.assertEqual(later[0]["rows"][0]["id"], 3)

    def test_week_view(self):
        w = todo.week_view(T)  # Tue: this week is Mon 19 to Sun 25, listed from today
        self.assertEqual((w["start"], w["first"], w["prev"], w["next"], w["title"]),
                         (d(10, 19), T, None, d(10, 26), "October 2026"))
        w = todo.week_view(T, d(10, 29))  # any day picks its Monday
        self.assertEqual((w["start"], w["first"], w["prev"], w["title"]), (d(10, 26), d(10, 26), d(10, 19), "Oct - Nov 2026"))
        self.assertEqual(todo.week_view(T, d(9, 1))["start"], d(10, 19))  # never before this week
        self.assertEqual(todo.week_view(T, d(12, 30))["title"], "Dec 2026 - Jan 2027")

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
        self.assertEqual(todo.meta(t, date_group, T, True), [("↻ weekly", "", ""), ("#home", "", "/todo/project/home")])
        self.assertEqual(todo.meta(t, done_group, T, False), [("↻ next Oct 18", "", "")])
        card = {"done": False, "mode": "time", "board": True}  # Upcoming board cards name the inbox too
        self.assertEqual(todo.meta(self.task(5, T), card, T, True), [("inbox", "", "/todo/inbox")])
        self.assertEqual(todo.meta(self.task(6, T), date_group, T, True), [])
        self.assertEqual(todo.day_label(d(1, 5, 2027), T, long=True), "Tue Jan 5 2027")


if __name__ == "__main__":
    unittest.main()
