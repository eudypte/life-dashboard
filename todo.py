"""Todo list: tasks in their own SQLite file, the app's only write path.

TODO_DB never points inside NUTRITION_DIR; that folder belongs to log_entry.py.
"""
import calendar
import datetime as dt
import os
import re
import sqlite3
from collections import Counter
from contextlib import contextmanager
from pathlib import Path

DEFAULT_DB = "~/.local/share/life-dashboard/todo.sqlite"
SCHEMA = """CREATE TABLE IF NOT EXISTS task (
  id         INTEGER PRIMARY KEY,
  title      TEXT NOT NULL,
  project    TEXT,                       -- NULL = inbox
  priority   INTEGER NOT NULL DEFAULT 4, -- 1 highest, 4 = none
  due        TEXT,                       -- ISO date
  due_time   TEXT,                       -- HH:MM
  repeat     TEXT,                       -- a RULE_RE rule, e.g. "every week"
  done_on    TEXT,                       -- ISO date of the last tick
  prev_due   TEXT,                       -- due before the last tick of a repeat, for undo
  created_at TEXT NOT NULL
)"""
# The edit form's choices; quick add can also make "every 3 days" and the like.
RULES = ("every day", "every weekday", "every week", "every 2 weeks", "every month", "every year")
RULE_RE = re.compile(r"every (day|weekday|week|month|year)|every [1-9]\d? (days|weeks|months)")
TIME_RE = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")  # date.weekday() order
MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
WD = r"(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|thu(?:rs(?:day)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)"
MN = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?"
      r"|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")


# ---------- storage ----------

def db_path(nutrition_dir: Path) -> Path:
    path = Path(os.environ.get("TODO_DB", DEFAULT_DB)).expanduser().resolve()
    if path.is_relative_to(nutrition_dir.expanduser().resolve()):
        raise RuntimeError(f"TODO_DB {path} is inside NUTRITION_DIR; tasks never go there")
    return path


@contextmanager
def _db(path: Path):
    """One connection per call; `with con` commits on success and rolls back on error."""
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    try:
        with con:
            yield con
    finally:
        con.close()


def init(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with _db(path) as con:
        con.execute(SCHEMA)


def _task(row) -> dict:
    t = dict(row)
    for k in ("due", "done_on", "prev_due"):
        t[k] = dt.date.fromisoformat(t[k]) if t[k] else None
    return t


def _params(t: dict) -> dict:
    return {**t, "due": t["due"].isoformat() if t["due"] else None}


def tasks(path: Path, today: dt.date) -> list[dict]:
    """Open tasks plus anything ticked today; older finished tasks are never shown."""
    with _db(path) as con:
        rows = con.execute("SELECT * FROM task WHERE done_on IS NULL OR repeat IS NOT NULL OR done_on = ?",
                           (today.isoformat(),)).fetchall()
    return [_task(r) for r in rows]


def get(path: Path, task_id: int) -> dict | None:
    with _db(path) as con:
        row = con.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
    return _task(row) if row else None


def add(path: Path, t: dict, now: dt.datetime) -> int:
    with _db(path) as con:
        return con.execute(
            "INSERT INTO task (title, project, priority, due, due_time, repeat, created_at)"
            " VALUES (:title, :project, :priority, :due, :due_time, :repeat, :created_at)",
            {**_params(t), "created_at": now.isoformat(timespec="seconds")}).lastrowid


def update(path: Path, task_id: int, t: dict) -> None:
    with _db(path) as con:
        con.execute("UPDATE task SET title = :title, project = :project, priority = :priority, due = :due,"
                    " due_time = :due_time, repeat = :repeat, prev_due = NULL WHERE id = :id",
                    {**_params(t), "id": task_id})


def delete(path: Path, task_id: int) -> None:
    with _db(path) as con:
        con.execute("DELETE FROM task WHERE id = ?", (task_id,))


def tick(path: Path, task_id: int, today: dt.date) -> None:
    """A repeating task never closes: it moves to its next date and remembers the old one."""
    with _db(path) as con:
        row = con.execute("SELECT repeat, due FROM task WHERE id = ?", (task_id,)).fetchone()
        if row and row["repeat"]:
            due = dt.date.fromisoformat(row["due"]) if row["due"] else None
            con.execute("UPDATE task SET prev_due = due, due = ?, done_on = ? WHERE id = ?",
                        (next_due(row["repeat"], due, today).isoformat(), today.isoformat(), task_id))
        elif row:
            con.execute("UPDATE task SET done_on = ? WHERE id = ?", (today.isoformat(), task_id))


def move(path: Path, task_id: int, due: dt.date | None) -> None:
    """New due date, keeping time and repeat; no date also drops the time.
    A moved task can no longer undo its last tick."""
    with _db(path) as con:
        if due:
            con.execute("UPDATE task SET due = ?, prev_due = NULL WHERE id = ?", (due.isoformat(), task_id))
        else:
            con.execute("UPDATE task SET due = NULL, due_time = NULL, prev_due = NULL WHERE id = ?", (task_id,))


def untick(path: Path, task_id: int) -> None:
    with _db(path) as con:
        con.execute("UPDATE task SET due = COALESCE(prev_due, due), prev_due = NULL, done_on = NULL"
                    " WHERE id = ?", (task_id,))


# ---------- dates and repeats ----------

def next_weekday(name: str, today: dt.date, include_today: bool = False) -> dt.date:
    ahead = (WEEKDAYS.index(name[:3].lower()) - today.weekday()) % 7
    return today + dt.timedelta(days=ahead or (0 if include_today else 7))


def on_date(today: dt.date, month: int, day: int) -> dt.date | None:
    """Month/day with no year: this year, or next year if that date has passed. None if invalid."""
    for year in (today.year, today.year + 1):
        try:
            d = dt.date(year, month, day)
        except ValueError:
            return None
        if d >= today:
            return d
    return None


def iso_date(s: str) -> dt.date | None:
    try:
        return dt.date.fromisoformat(s)
    except ValueError:
        return None


def add_months(d: dt.date, n: int) -> dt.date:
    """Same day n months on; the 31st becomes the last day of a shorter month."""
    # ponytail: the clamp sticks (Jan 31 -> Feb 28 -> Mar 28); store an anchor day if that matters
    year, month = divmod(d.month - 1 + n, 12)
    year += d.year
    return dt.date(year, month + 1, min(d.day, calendar.monthrange(year, month + 1)[1]))


def date_options(today: dt.date) -> list[tuple[str, str, dt.date | None]]:
    """The date menu, as (form value, label, date)."""
    weekend = today if today.weekday() >= 5 else next_weekday("sat", today)
    return [("tomorrow", "tomorrow", today + dt.timedelta(days=1)), ("weekend", "this weekend", weekend),
            ("next-week", "next week", next_weekday("mon", today)), ("none", "no date", None)]


def first_due(rule: str, today: dt.date) -> dt.date:
    """Due date of a new repeating task that was given no date."""
    return next_weekday("mon", today) if rule == "every weekday" and today.weekday() > 4 else today


def _step(rule: str, d: dt.date) -> dt.date:
    if rule == "every weekday":
        d += dt.timedelta(days=1)
        while d.weekday() > 4:
            d += dt.timedelta(days=1)
        return d
    n, unit = re.fullmatch(r"every (?:(\d+) )?(day|week|month|year)s?", rule).groups()
    n = int(n or 1)
    if unit == "day":
        return d + dt.timedelta(days=n)
    if unit == "week":
        return d + dt.timedelta(weeks=n)
    return add_months(d, n * (12 if unit == "year" else 1))


def next_due(rule: str, due: dt.date | None, today: dt.date) -> dt.date:
    """Step from the due date until past today, so a late tick never leaves missed copies behind."""
    d = _step(rule, due or today)
    while d <= today:
        d = _step(rule, d)
    return d


# ---------- quick add ----------

def project_name(s: str) -> str | None:
    return "-".join(re.findall(r"[\w-]+", s.lower())) or None


def parse(text: str, today: dt.date, due: dt.date | None = None, project: str | None = None) -> dict:
    """Quick-add text to task fields. Tokens are whole words; whatever is left is the title.
    `due` and `project` are the defaults of the page it was typed on."""
    s = f" {text} "

    def take(pattern, value):
        """Cut the first match out of s if value(match) is not None, and return that value."""
        nonlocal s
        m = re.search(pattern, s, re.I)
        v = value(m) if m else None
        if v is not None:
            s = s[:m.start()] + " " + s[m.end():]
        return v

    month = lambda name: MONTHS.index(name[:3].lower()) + 1  # noqa: E731
    t = {"due": None}
    t["repeat"] = (take(r"\severy\s+(day|weekday|week|month|year)\s", lambda m: "every " + m[1].lower())
                   or take(r"\severy\s+([1-9]\d?)\s+(day|week|month)s?\s",
                           lambda m: f"every {m[1]} {m[2].lower()}s" if m[1] != "1" else f"every {m[2].lower()}")
                   or take(r"\s(daily|weekly|monthly|yearly)\s",
                           lambda m: "every " + {"daily": "day", "weekly": "week", "monthly": "month",
                                                 "yearly": "year"}[m[1].lower()]))
    if not t["repeat"] and (day := take(rf"\severy\s+{WD}\s", lambda m: m[1])):
        t["repeat"], t["due"] = "every week", next_weekday(day, today, include_today=True)
    t["due"] = (take(r"\s(today|tomorrow)\s", lambda m: today + dt.timedelta(days=m[1].lower() == "tomorrow"))
                or take(r"\sin\s+(\d{1,3})\s+days?\s", lambda m: today + dt.timedelta(days=int(m[1])))
                or take(r"\snext\s+week\s", lambda m: next_weekday("mon", today))
                or take(r"\s(\d{4}-\d\d-\d\d)\s", lambda m: iso_date(m[1]))
                or take(rf"\s{MN}\s+(\d{{1,2}})\s", lambda m: on_date(today, month(m[1]), int(m[2])))
                or take(rf"\s(\d{{1,2}})\s+{MN}\s", lambda m: on_date(today, month(m[2]), int(m[1])))
                or take(r"\s(\d{1,2})/(\d{1,2})\s", lambda m: on_date(today, int(m[1]), int(m[2])))
                or take(rf"\s{WD}\s", lambda m: next_weekday(m[1], today))
                or t["due"])
    t["due_time"] = (take(r"\s(\d{1,2})(?::([0-5]\d))?\s?(am|pm)\s",
                          lambda m: f"{int(m[1]) % 12 + 12 * (m[3].lower() == 'pm'):02}:{m[2] or '00'}"
                          if 1 <= int(m[1]) <= 12 else None)
                     or take(r"\s([01]?\d|2[0-3]):([0-5]\d)\s", lambda m: f"{int(m[1]):02}:{m[2]}"))
    t["priority"] = take(r"\sp([1-4])\s", lambda m: int(m[1])) or 4
    t["project"] = take(r"\s#([\w-]+)\s", lambda m: m[1].lower()) or project
    t["title"] = " ".join(s.split())
    if t["repeat"] and not t["due"]:
        t["due"] = first_due(t["repeat"], today)
    t["due"] = t["due"] or (today if t["due_time"] else due)
    return t


def from_form(f: dict, today: dt.date) -> dict | None:
    """The edit form's fields, checked. None if there is no title."""
    title = " ".join(f.get("title", "").split())
    if not title:
        return None
    due = iso_date(f.get("due", ""))
    repeat = f.get("repeat", "") if RULE_RE.fullmatch(f.get("repeat", "")) else None
    return {
        "title": title, "project": project_name(f.get("project", "")),
        "priority": int(f["priority"]) if f.get("priority") in ("1", "2", "3", "4") else 4,
        "due": due or (first_due(repeat, today) if repeat else None),
        "due_time": f.get("due_time") if TIME_RE.fullmatch(f.get("due_time", "")) else None,
        "repeat": repeat,
    }


# ---------- views ----------

def is_open(t: dict) -> bool:
    return not t["done_on"] or bool(t["repeat"])


def _sort_key(t: dict):
    return t["due"] or dt.date.max, t["due_time"] or "99", t["priority"], t["id"]


def _group(label, rows, mode="date", cls="", done=False, keep=False, note=""):
    """mode: what the meta column shows for the due date ("time" when the group already is that day)."""
    return {"label": label, "rows": rows, "mode": mode, "cls": cls, "done": done, "keep": keep, "note": note}


def groups(ts: list[dict], today: dt.date, page: str, project: str | None = None) -> list[dict]:
    """Groups for one page: "today", "upcoming", "inbox" or "project". Empty groups are hidden unless keep."""
    open_ = sorted(filter(is_open, ts), key=_sort_key)
    done = [t for t in ts if t["done_on"] == today]
    overdue = [t for t in open_ if t["due"] and t["due"] < today]
    if page == "today":
        return [_group("overdue", overdue, cls="warn"),
                _group("today", [t for t in open_ if t["due"] == today], "time"),
                _group("done", done, done=True)]
    if page == "upcoming":
        days = [today + dt.timedelta(days=i) for i in range(7)]
        return ([_group("overdue", overdue, cls="warn")]
                + [_group(d.strftime("%a %d"), [t for t in open_ if t["due"] == d], "time", cls="day", keep=True,
                          note={0: "today", 1: "tomorrow"}.get(i, "")) for i, d in enumerate(days)]
                + [_group("later", [t for t in open_ if t["due"] and t["due"] > days[-1]])])
    member = (lambda t: t["project"] == project) if page == "project" else (lambda t: not t["project"])
    return [_group("overdue", [t for t in overdue if member(t)], cls="warn"),
            _group("scheduled", [t for t in open_ if t["due"] and t["due"] >= today and member(t)]),
            _group("no date", [t for t in open_ if not t["due"] and member(t)]),
            _group("done", [t for t in done if member(t)], done=True)]


def counts(ts: list[dict], today: dt.date) -> dict:
    """Nav numbers: open tasks in the inbox, due today or earlier, and per project."""
    open_ = [t for t in ts if is_open(t)]
    return {"inbox": sum(not t["project"] for t in open_),
            "today": sum(bool(t["due"]) and t["due"] <= today for t in open_),
            "projects": sorted(Counter(t["project"] for t in open_ if t["project"]).items())}


def day_label(d: dt.date, today: dt.date, long: bool = False) -> str:
    """today, tomorrow, Thu 22 within a week (Thu Oct 22 when long), else Oct 31; plus the year if not this one."""
    if d == today:
        return "today"
    if d == today + dt.timedelta(days=1):
        return "tomorrow"
    if long:
        s = f"{d:%a %b} {d.day}"
    elif today < d <= today + dt.timedelta(days=6):
        return d.strftime("%a %d")
    else:
        s = f"{d:%b} {d.day}"
    return s if d.year == today.year else f"{s} {d.year}"


def repeat_label(rule: str) -> str:
    return {"every day": "daily", "every weekday": "weekdays", "every week": "weekly",
            "every month": "monthly", "every year": "yearly"}.get(rule, rule)


def when(t: dict, group: dict, today: dt.date) -> tuple[str, str] | None:
    """The clickable date of an open task, as (text, css class); None in the done group.
    Where the group already says the day, it shows the time, or the day faintly."""
    if group["done"]:
        return None
    if not t["due"]:
        return "+ date", "faint"
    if group["mode"] == "time":
        return (t["due_time"], "") if t["due_time"] else (day_label(t["due"], today), "faint")
    text = day_label(t["due"], today) + (f" {t['due_time']}" if t["due_time"] else "")
    return text, "warn" if t["due"] < today else ""


def meta(t: dict, group: dict, today: dt.date, show_project: bool) -> list[tuple[str, str]]:
    """The rest of a task row after its date, as (text, css class) parts."""
    parts = []
    if group["done"] and t["repeat"]:
        parts.append((f"↻ next {day_label(t['due'], today)}", ""))
    elif t["repeat"]:
        parts.append(("↻ " + repeat_label(t["repeat"]), ""))
    if t["project"] and show_project:
        parts.append(("#" + t["project"], ""))
    return parts
