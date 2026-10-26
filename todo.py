"""Todo list: tasks in their own SQLite file, the app's only write path.

TODO_DB never points inside NUTRITION_DIR; that folder belongs to log_entry.py.
"""
import calendar
import datetime as dt
import os
import re
import sqlite3
from collections import Counter, defaultdict
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import quote

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
  created_at TEXT NOT NULL,
  pos        INTEGER,                    -- order within a day: the last task added or moved there comes last
  day_order  INTEGER                     -- placed by drag and drop: this slot in its day, above unplaced tasks
);
CREATE TABLE IF NOT EXISTS project (name TEXT PRIMARY KEY);  -- kept even when no task uses it
CREATE TABLE IF NOT EXISTS label (name TEXT PRIMARY KEY, color TEXT NOT NULL);  -- color: one of PALETTE
CREATE TABLE IF NOT EXISTS task_label (task_id INTEGER NOT NULL, label TEXT NOT NULL, PRIMARY KEY (task_id, label))"""
# Label colors: the theme's own color variables (static/style.css .lc-*), so every theme has them.
PALETTE = ("p", "c", "f", "kcal", "accent", "warn")
NEXT_POS = "(SELECT COALESCE(MAX(COALESCE(pos, id)), 0) + 1 FROM task)"
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
        con.executescript(SCHEMA)
        have = {row["name"] for row in con.execute("PRAGMA table_info(task)")}
        for column in ("pos", "day_order"):  # files made before these columns
            if column not in have:
                con.execute(f"ALTER TABLE task ADD COLUMN {column} INTEGER")
        # files made before the project table: every name a task uses becomes a project
        con.execute("INSERT OR IGNORE INTO project (name) SELECT DISTINCT project FROM task WHERE project IS NOT NULL")


def _task(row) -> dict:
    t = dict(row)
    for k in ("due", "done_on", "prev_due"):
        t[k] = dt.date.fromisoformat(t[k]) if t[k] else None
    return t


def _params(t: dict) -> dict:
    return {**t, "due": t["due"].isoformat() if t["due"] else None}


def _with_labels(con, ts: list[dict]) -> list[dict]:
    """Each task's labels, as (name, color) sorted by name."""
    labels = defaultdict(list)
    for row in con.execute("SELECT tl.task_id, l.name, l.color FROM task_label tl JOIN label l ON l.name = tl.label"
                           " ORDER BY l.name"):
        labels[row[0]].append((row[1], row[2]))
    for t in ts:
        t["labels"] = labels[t["id"]]
    return ts


def tasks(path: Path, today: dt.date) -> list[dict]:
    """Open tasks plus anything ticked today; older finished tasks are never shown."""
    with _db(path) as con:
        rows = con.execute("SELECT * FROM task WHERE done_on IS NULL OR repeat IS NOT NULL OR done_on = ?",
                           (today.isoformat(),)).fetchall()
        return _with_labels(con, [_task(r) for r in rows])


def get(path: Path, task_id: int) -> dict | None:
    with _db(path) as con:
        row = con.execute("SELECT * FROM task WHERE id = ?", (task_id,)).fetchone()
        return _with_labels(con, [_task(row)])[0] if row else None


def _keep_project(con, name: str | None) -> None:
    """A project exists from its first use until it is deleted."""
    if name:
        con.execute("INSERT OR IGNORE INTO project (name) VALUES (?)", (name,))


def default_color(name: str) -> str:
    """A new label's color, from its name, so the quick-add preview can show it before the label exists."""
    return PALETTE[sum(name.encode()) % len(PALETTE)]


def _set_labels(con, task_id: int, names: list[str]) -> None:
    """The task's labels become exactly `names`; a label exists from its first use until it is deleted."""
    con.execute("DELETE FROM task_label WHERE task_id = ?", (task_id,))
    for name in names:
        con.execute("INSERT OR IGNORE INTO label (name, color) VALUES (?, ?)", (name, default_color(name)))
        con.execute("INSERT OR IGNORE INTO task_label (task_id, label) VALUES (?, ?)", (task_id, name))


def add(path: Path, t: dict, now: dt.datetime) -> int:
    with _db(path) as con:
        _keep_project(con, t["project"])
        task_id = con.execute(
            "INSERT INTO task (title, project, priority, due, due_time, repeat, created_at, pos)"
            f" VALUES (:title, :project, :priority, :due, :due_time, :repeat, :created_at, {NEXT_POS})",
            {**_params(t), "created_at": now.isoformat(timespec="seconds")}).lastrowid
        _set_labels(con, task_id, t.get("labels", []))
        return task_id


def update(path: Path, task_id: int, t: dict) -> None:
    with _db(path) as con:
        _keep_project(con, t["project"])
        # a new date drops the place the task was dragged to in its old day
        con.execute("UPDATE task SET title = :title, project = :project, priority = :priority, due = :due,"
                    " due_time = :due_time, repeat = :repeat, prev_due = NULL,"
                    " day_order = CASE WHEN due IS :due THEN day_order END WHERE id = :id",
                    {**_params(t), "id": task_id})
        _set_labels(con, task_id, t.get("labels", []))


def delete(path: Path, task_id: int) -> None:
    with _db(path) as con:
        con.execute("DELETE FROM task WHERE id = ?", (task_id,))
        con.execute("DELETE FROM task_label WHERE task_id = ?", (task_id,))


def tick(path: Path, task_id: int, today: dt.date) -> None:
    """A repeating task never closes: it moves to its next date and remembers the old one."""
    with _db(path) as con:
        row = con.execute("SELECT repeat, due FROM task WHERE id = ?", (task_id,)).fetchone()
        if row and row["repeat"]:
            due = dt.date.fromisoformat(row["due"]) if row["due"] else None
            con.execute("UPDATE task SET prev_due = due, due = ?, done_on = ?, day_order = NULL WHERE id = ?",
                        (next_due(row["repeat"], due, today).isoformat(), today.isoformat(), task_id))
        elif row:
            con.execute("UPDATE task SET done_on = ? WHERE id = ?", (today.isoformat(), task_id))


def move(path: Path, task_id: int, due: dt.date | None) -> None:
    """New due date (the date menu), keeping time and repeat; no date also drops the time. In its new day the task
    goes after the others of the same time and priority. A moved task can no longer undo its last tick."""
    with _db(path) as con:
        if due:
            con.execute(f"UPDATE task SET due = ?, prev_due = NULL, pos = {NEXT_POS}, day_order = NULL WHERE id = ?",
                        (due.isoformat(), task_id))
        else:
            con.execute(f"UPDATE task SET due = NULL, due_time = NULL, prev_due = NULL, pos = {NEXT_POS},"
                        " day_order = NULL WHERE id = ?", (task_id,))


def set_priority(path: Path, task_id: int, priority: int) -> None:
    with _db(path) as con:
        con.execute("UPDATE task SET priority = ? WHERE id = ?", (priority, task_id))


def set_project(path: Path, task_id: int, project: str | None) -> None:
    """None moves the task to the inbox."""
    with _db(path) as con:
        _keep_project(con, project)
        con.execute("UPDATE task SET project = ? WHERE id = ?", (project, task_id))


def drop(path: Path, task_id: int, due: dt.date, order: list[int]) -> None:
    """Dragged on the Upcoming board onto `due`, in the slot `order` gives: that day's task ids, top to bottom.
    The day keeps that order, regardless of time and priority; tasks added or moved there later go below it."""
    if task_id not in order:
        order = [*order, task_id]
    with _db(path) as con:
        con.execute("UPDATE task SET due = ?, prev_due = NULL WHERE id = ?", (due.isoformat(), task_id))
        con.executemany("UPDATE task SET day_order = ? WHERE id = ? AND due = ?",
                        [(i, tid, due.isoformat()) for i, tid in enumerate(order)])


# ---------- projects ----------

def projects(path: Path) -> list[str]:
    with _db(path) as con:
        return [row["name"] for row in con.execute("SELECT name FROM project ORDER BY name")]


def rename_project(path: Path, old: str, new: str) -> None:
    """Renames everywhere. Renaming onto an existing project merges the two."""
    with _db(path) as con:
        _keep_project(con, new)
        con.execute("UPDATE task SET project = ? WHERE project = ?", (new, old))
        con.execute("DELETE FROM project WHERE name = ? AND name != ?", (old, new))


def label_colors(path: Path) -> dict[str, str]:
    with _db(path) as con:
        return dict(con.execute("SELECT name, color FROM label ORDER BY name").fetchall())


def rename_label(path: Path, old: str, new: str) -> None:
    """Renames everywhere, keeping the color. Renaming onto an existing label merges the two."""
    with _db(path) as con:
        row = con.execute("SELECT color FROM label WHERE name = ?", (old,)).fetchone()
        con.execute("INSERT OR IGNORE INTO label (name, color) VALUES (?, ?)", (new, row[0] if row else default_color(new)))
        con.execute("UPDATE OR IGNORE task_label SET label = ? WHERE label = ?", (new, old))
        con.execute("DELETE FROM task_label WHERE label = ? AND label != ?", (old, new))  # already had both
        con.execute("DELETE FROM label WHERE name = ? AND name != ?", (old, new))


def color_label(path: Path, name: str, color: str) -> None:
    if color in PALETTE:
        with _db(path) as con:
            con.execute("UPDATE label SET color = ? WHERE name = ?", (color, name))


def delete_label(path: Path, name: str) -> None:
    """The label comes off its tasks; the tasks stay."""
    with _db(path) as con:
        con.execute("DELETE FROM task_label WHERE label = ?", (name,))
        con.execute("DELETE FROM label WHERE name = ?", (name,))


def delete_project(path: Path, name: str) -> None:
    """The project goes; its tasks move to the inbox, never deleted with it."""
    with _db(path) as con:
        con.execute("UPDATE task SET project = NULL WHERE project = ?", (name,))
        con.execute("DELETE FROM project WHERE name = ?", (name,))


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
    return [("today", "today", today), ("tomorrow", "tomorrow", today + dt.timedelta(days=1)),
            ("weekend", "this weekend", weekend),
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
    """The occurrence after the ticked one (`due`), skipping missed ones up to today but never today itself:
    a task ticked late lands on today if that is one of its days, else on its next day. Ticked on time or
    early, it is simply the next occurrence."""
    d = _step(rule, due or today)
    while d < today:
        d = _step(rule, d)
    return d


# ---------- quick add ----------

def project_name(s: str) -> str | None:
    return "-".join(re.findall(r"[\w-]+", s.lower())) or None


def parse(text: str, today: dt.date, due: dt.date | None = None, project: str | None = None,
          label: str | None = None) -> dict:
    """Quick-add text to task fields. Tokens are whole words; whatever is left is the title.
    `due`, `project` and `label` are the defaults of the page it was typed on.
    "tokens" lists where each recognised token sits in `text`, as (start, end, kind), for highlighting."""
    s = f" {text} "
    tokens = []

    def take(pattern, value, kind=None):
        """Blank out the first match in s if value(match) is not None, and return that value.
        Blanking keeps every position, so the token spans stay true to `text`."""
        nonlocal s
        m = re.search(pattern, s, re.I)
        v = value(m) if m else None
        if v is not None:
            g = m.group(0)
            start, end = m.start() + len(g) - len(g.lstrip()), m.end() - len(g) + len(g.rstrip())
            tokens.append((start - 1, end - 1, kind or f"p{v}"))
            s = s[:m.start()] + " " * len(g) + s[m.end():]
        return v

    month = lambda name: MONTHS.index(name[:3].lower()) + 1  # noqa: E731
    t = {"due": None}
    t["repeat"] = (take(r"\severy\s+(day|weekday|week|month|year)\s", lambda m: "every " + m[1].lower(), "repeat")
                   or take(r"\severy\s+([1-9]\d?)\s+(day|week|month)s?\s",
                           lambda m: f"every {m[1]} {m[2].lower()}s" if m[1] != "1" else f"every {m[2].lower()}",
                           "repeat")
                   or take(r"\s(daily|weekly|monthly|yearly)\s",
                           lambda m: "every " + {"daily": "day", "weekly": "week", "monthly": "month",
                                                 "yearly": "year"}[m[1].lower()], "repeat"))
    if not t["repeat"] and (day := take(rf"\severy\s+{WD}\s", lambda m: m[1], "repeat")):
        t["repeat"], t["due"] = "every week", next_weekday(day, today, include_today=True)
    t["due"] = (take(r"\s(today|tomorrow)\s", lambda m: today + dt.timedelta(days=m[1].lower() == "tomorrow"), "date")
                or take(r"\sin\s+(\d{1,3})\s+days?\s", lambda m: today + dt.timedelta(days=int(m[1])), "date")
                or take(r"\snext\s+week\s", lambda m: next_weekday("mon", today), "date")
                or take(r"\s(\d{4}-\d\d-\d\d)\s", lambda m: iso_date(m[1]), "date")
                or take(rf"\s{MN}\s+(\d{{1,2}})\s", lambda m: on_date(today, month(m[1]), int(m[2])), "date")
                or take(rf"\s(\d{{1,2}})\s+{MN}\s", lambda m: on_date(today, month(m[2]), int(m[1])), "date")
                or take(r"\s(\d{1,2})/(\d{1,2})\s", lambda m: on_date(today, int(m[1]), int(m[2])), "date")
                or take(rf"\s{WD}\s", lambda m: next_weekday(m[1], today), "date")
                or t["due"])
    t["due_time"] = (take(r"\s(\d{1,2})(?::([0-5]\d))?\s?(am|pm)\s",
                          lambda m: f"{int(m[1]) % 12 + 12 * (m[3].lower() == 'pm'):02}:{m[2] or '00'}"
                          if 1 <= int(m[1]) <= 12 else None, "time")
                     or take(r"\s([01]?\d|2[0-3]):([0-5]\d)\s", lambda m: f"{int(m[1]):02}:{m[2]}", "time"))
    t["priority"] = take(r"\sp([1-4])\s", lambda m: int(m[1])) or 4  # kind p1..p4, for its color
    t["project"] = take(r"\s#([\w-]+)\s", lambda m: m[1].lower(), "project") or project
    t["labels"] = []
    while name := take(r"\s@([\w-]+)\s", lambda m: m[1].lower(), "label"):  # any number of @labels
        t["labels"] += [name] if name not in t["labels"] else []
    if label and label not in t["labels"]:
        t["labels"].append(label)
    t["title"] = " ".join(s.split())
    t["tokens"] = sorted(tokens)
    if t["repeat"] and not t["due"]:
        t["due"] = first_due(t["repeat"], today)
    t["due"] = t["due"] or due or (today if t["due_time"] else None)  # a time alone means the page's day, else today
    return t


def from_form(f: dict, today: dt.date) -> dict | None:
    """The edit form's fields, checked. None if there is no title."""
    title = " ".join(f.get("title", "").split())
    if not title:
        return None
    due = iso_date(f.get("due", ""))
    repeat = f.get("repeat", "") if RULE_RE.fullmatch(f.get("repeat", "")) else None
    labels = [project_name(x) for x in f.get("labels", "").split(",")]
    return {
        "title": title, "project": project_name(f.get("project", "")),
        "labels": list(dict.fromkeys(name for name in labels if name)),  # "@errand, phone" -> errand, phone
        "priority": int(f["priority"]) if f.get("priority") in ("1", "2", "3", "4") else 4,
        "due": due or (first_due(repeat, today) if repeat else None),
        "due_time": f.get("due_time") if TIME_RE.fullmatch(f.get("due_time", "")) else None,
        "repeat": repeat,
    }


# ---------- views ----------

def is_open(t: dict) -> bool:
    return not t["done_on"] or bool(t["repeat"])


def _sort_key(t: dict):
    """Within a day: tasks placed by drag and drop in their slots first; then the rest, timed tasks by time,
    then by priority, then in the order they were added or moved there."""
    return (t["due"] or dt.date.max, t.get("day_order") is None, t.get("day_order") or 0, t["due_time"] or "99",
            t["priority"], t.get("pos") or t["id"])


def _group(label, rows, mode="date", cls="", done=False, keep=False, note="", day=None, board=False):
    """mode: what the meta column shows for the due date ("time" when the group already is that day).
    day: the date of an Upcoming column. board: a column of the Upcoming board, whose cards can be
    dragged and say "inbox" for tasks with no project."""
    return {"label": label, "rows": rows, "mode": mode, "cls": cls, "done": done, "keep": keep, "note": note,
            "day": day, "board": board}


def week_view(today: dt.date, start: dt.date | None = None) -> dict:
    """Upcoming's header: the week shown (Monday first, never before this week) and its first listed day."""
    this = today - dt.timedelta(days=today.weekday())
    start = max(start - dt.timedelta(days=start.weekday()), this) if start else this
    end = start + dt.timedelta(days=6)
    if start.year != end.year:
        title = f"{start:%b %Y} - {end:%b %Y}"
    elif start.month != end.month:
        title = f"{start:%b} - {end:%b %Y}"
    else:
        title = f"{start:%B %Y}"
    return {"start": start, "first": max(start, today), "title": title,
            "prev": start - dt.timedelta(days=7) if start > this else None, "next": start + dt.timedelta(days=7)}


def groups(ts: list[dict], today: dt.date, page: str, name: str | None = None,
           first: dt.date | None = None) -> list[dict]:
    """Groups for one page: "today", "upcoming", "inbox", "project" or "label" (`name` says which one).
    Empty groups are hidden unless keep.
    Upcoming is a board: an overdue column, then one column per day for the 7 days from `first` (default today)."""
    open_ = sorted(filter(is_open, ts), key=_sort_key)
    done = [t for t in ts if t["done_on"] == today]
    overdue = [t for t in open_ if t["due"] and t["due"] < today]
    if page == "today":
        return [_group("overdue", overdue, cls="warn"),
                _group("today", [t for t in open_ if t["due"] == today], "time"),
                _group("done", done, done=True)]
    if page == "upcoming":
        first = first or today
        notes = {today: "Today", today + dt.timedelta(days=1): "Tomorrow"}
        days = [first + dt.timedelta(days=i) for i in range(7)]
        return ([_group("Overdue", overdue, cls="warn", board=True)] if first == today else []) + [
            _group(f"{d:%b} {d.day}", [t for t in open_ if t["due"] == d], "time", keep=True,
                   note=notes.get(d, f"{d:%A}"), day=d, board=True) for d in days]
    member = {"project": lambda t: t["project"] == name,
              "label": lambda t: name in [n for n, _ in t["labels"]]}.get(page, lambda t: not t["project"])
    return [_group("overdue", [t for t in overdue if member(t)], cls="warn"),
            _group("scheduled", [t for t in open_ if t["due"] and t["due"] >= today and member(t)]),
            _group("no date", [t for t in open_ if not t["due"] and member(t)]),
            _group("done", [t for t in done if member(t)], done=True)]


def counts(ts: list[dict], today: dt.date, names: list[str], labels: dict[str, str]) -> dict:
    """Nav numbers: open tasks in the inbox, due today or earlier, per project and per label
    (every project and label, even empty; labels as (name, count, color))."""
    open_ = [t for t in ts if is_open(t)]
    per = Counter(t["project"] for t in open_ if t["project"])
    per_label = Counter(n for t in open_ for n, _ in t["labels"])
    return {"inbox": sum(not t["project"] for t in open_),
            "today": sum(bool(t["due"]) and t["due"] <= today for t in open_),
            "projects": [(name, per[name]) for name in sorted(set(names) | set(per))],
            "labels": [(name, per_label[name], color) for name, color in sorted(labels.items())]}


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


def meta(t: dict, group: dict, today: dt.date, show_project: bool) -> list[tuple[str, str, str]]:
    """The rest of a task row after its date, as (text, css class, link) parts; the project links to its page."""
    parts = []
    if group["done"] and t["repeat"]:
        parts.append((f"↻ next {day_label(t['due'], today)}", "", ""))
    elif t["repeat"]:
        parts.append(("↻ " + repeat_label(t["repeat"]), "", ""))
    if show_project and t["project"]:
        parts.append(("#" + t["project"], "", "/todo/project/" + quote(t["project"], safe="")))
    elif show_project and group.get("board"):
        parts.append(("inbox", "", "/todo/inbox"))
    for name, color in t.get("labels") or ():
        parts.append(("@" + name, f"lab lc-{color}", "/todo/label/" + quote(name, safe="")))
    return parts
