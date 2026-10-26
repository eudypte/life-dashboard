"""Media: where each show, anime, movie or YouTube series is.

Two logs of the same row shape (data.media_row) are read as one, in time order: media.csv in
NUTRITION_DIR, appended outside the app, and this page's own log, a table in the todo SQLite file
(TODO_DB), so the app still never writes to NUTRITION_DIR.
"""
import datetime as dt
import re
from pathlib import Path

import data
from todo import _db

SCHEMA = """CREATE TABLE IF NOT EXISTS media_log (
  id       INTEGER PRIMARY KEY,
  date     TEXT NOT NULL,  -- ISO date, the logical day
  time     TEXT NOT NULL,  -- HH:MM:SS
  title    TEXT NOT NULL,
  kind     TEXT,
  season   INTEGER,
  episode  INTEGER,
  episodes INTEGER,
  position TEXT,
  runtime  INTEGER,
  status   TEXT
)"""
ORDER_SCHEMA = """CREATE TABLE IF NOT EXISTS media_order (
  key TEXT PRIMARY KEY,  -- the casefolded title
  pos INTEGER NOT NULL   -- its place in the list, set by dragging
)"""
COLUMNS = ("date", "time", "title", "kind", "season", "episode", "episodes", "position", "runtime", "status")
# quick add: "severance s2e8", "frieren e20", "dune 1:12:40", "the bear s3e8 5:45" (paused in that episode),
# any of them with "done" or "drop" last
STATUS_RE = re.compile(r"\s+(done|drop(?:ped)?)$", re.I)
POSITION_RE = re.compile(r"\s+(\d+:\d+(?::\d+)?)$")
EPISODE_RE = re.compile(r"\s+(?:s(\d{1,3})\s*e(\d{1,4})|e(\d{1,4}))$", re.I)
ERRORS = {  # why quick add logged nothing, shown under the box
    "title": "nothing logged: start with the title, e.g. the bear s3e8 5:45",
    "time": "nothing logged: a time must be m:ss or h:mm:ss, e.g. 5:45 or 1:12:40",
}


def init(path: Path) -> None:
    with _db(path) as con:
        con.execute(SCHEMA)
        con.execute(ORDER_SCHEMA)


def log(path: Path, row: dict) -> None:
    with _db(path) as con:
        con.execute(f"INSERT INTO media_log ({', '.join(COLUMNS)}) VALUES ({', '.join(':' + c for c in COLUMNS)})",
                    {c: row.get(c) for c in COLUMNS} | {"date": row["date"].isoformat()})


def page_rows(path: Path) -> list[dict]:
    with _db(path) as con:
        rows = con.execute(f"SELECT {', '.join(COLUMNS)} FROM media_log ORDER BY id").fetchall()
    return [data.media_row({k: "" if v is None else str(v) for k, v in dict(r).items()}) for r in rows]


def merged(csv_rows: list[dict], log_rows: list[dict]) -> list[dict]:
    """Both logs as one, oldest first; at the same moment the page's row comes last."""
    return sorted(csv_rows + log_rows, key=lambda r: (r["date"], r["time"]))


def titles(rows: list[dict]) -> list[dict]:
    """One entry per title (any case), last watched first. Later rows win; a blank cell keeps the
    earlier value. A new season without an episode starts before its first episode. A row with an episode
    and a position is partway through that episode; with an episode and no position, that episode is done."""
    out: dict[str, dict] = {}
    for r in rows:
        t = out.setdefault(r["title"].casefold(), {
            "title": r["title"], "kind": "", "season": None, "episode": None, "episodes": {},
            "position": "", "runtime": None, "status": "watching", "last": r["date"]})
        t["title"] = r["title"]
        t["kind"] = r["kind"] or t["kind"]
        t["runtime"] = r["runtime"] or t["runtime"]
        if r["season"] is not None and r["season"] != t["season"]:
            t["season"], t["episode"], t["position"] = r["season"], None, ""
        if r["episode"] is not None:
            t["episode"], t["position"] = r["episode"], r["position"]
        else:
            t["position"] = r["position"] or t["position"]
        if r["episodes"]:
            t["episodes"][t["season"]] = r["episodes"]
        t["status"] = r["status"] if r["status"] in data.MEDIA_STATUSES else t["status"]
        t["last"], t["at"] = r["date"], (r["date"], r["time"])
    return sorted(out.values(), key=lambda t: t["at"], reverse=True)


def placed(path: Path) -> dict[str, int]:
    with _db(path) as con:
        return dict(con.execute("SELECT key, pos FROM media_order").fetchall())


def set_order(path: Path, titles: list[str]) -> None:
    """The list as dragged: every title shown takes its place."""
    with _db(path) as con:
        con.execute("DELETE FROM media_order")
        con.executemany("INSERT OR IGNORE INTO media_order (key, pos) VALUES (?, ?)",
                        [(title.casefold(), i) for i, title in enumerate(titles)])


def move(order: list[str], title: str, to: str) -> list[str]:
    """The list after moving `title` to the top, up one, down one or to the bottom (the right-click menu)."""
    i = order.index(title)
    rest = order[:i] + order[i + 1:]
    j = {"top": 0, "up": max(i - 1, 0), "down": min(i + 1, len(rest)), "bottom": len(rest)}[to]
    return rest[:j] + [title] + rest[j:]


def arrange(ts: list[dict], order: dict[str, int]) -> list[dict]:
    """Titles never dragged first, last watched first (as titles() gives them), then the dragged ones in their places."""
    return ([t for t in ts if t["title"].casefold() not in order]
            + sorted((t for t in ts if t["title"].casefold() in order), key=lambda t: order[t["title"].casefold()]))


def is_movie(t: dict) -> bool:
    return t["kind"] == "movie" or (t["episode"] is None and bool(t["position"]))


def paused(t: dict) -> bool:
    """Partway through its current episode, at t["position"]."""
    return t["episode"] is not None and bool(t["position"]) and t["status"] != "done"


def seasons(t: dict) -> list[dict]:
    """The tick rows: every season up to the current one whose length is known, plus the current one.
    Each episode is watched ("w"), in progress ("ip": paused partway), next ("nx") or not yet ("n");
    unknown lengths show what was watched and the next one."""
    out = []
    cur = t["season"]
    for s in [*sorted(s for s in t["episodes"] if s is not None and cur is not None and s < cur), cur]:
        total = t["episodes"].get(s)
        if s != cur:
            watched = total
        elif t["status"] == "done":
            watched = max(total or 0, t["episode"] or 0)
        else:
            watched = (t["episode"] or 0) - paused(t)
        nxt = watched + 1 if s == cur and (paused(t) or t["status"] == "watching" and (not total or watched < total)) else 0
        out.append({"season": s, "total": total, "watched": watched,
                    "eps": [(e, "w" if e <= watched else ("ip" if paused(t) else "nx") if e == nxt else "n")
                            for e in range(1, max(total or 0, watched, nxt) + 1)]})
    return out


def step(t: dict) -> tuple[int | None, int]:
    """(season, episode) after the current one: the next season once a season's known length is done.
    Paused partway through an episode, that episode: +1 finishes it."""
    if paused(t):
        return t["season"], t["episode"]
    total = t["episodes"].get(t["season"])
    if t["season"] is not None and total and (t["episode"] or 0) >= total:
        return t["season"] + 1, 1
    return t["season"], (t["episode"] or 0) + 1


def parse(q: str) -> dict:
    """Quick add: a title, then optionally an episode (s2e8, e20), a time into it or into a movie (5:45,
    1:12:40), and done or drop. "error" (a key of ERRORS) says why nothing can be logged."""
    q = " ".join(q.split())
    row = {"season": None, "episode": None, "position": "", "status": "", "error": None}
    if m := STATUS_RE.search(q):
        row["status"] = "done" if m[1].lower() == "done" else "dropped"
        q = q[:m.start()]
    if m := POSITION_RE.search(" " + q):
        if data.seconds(m[1]) is None:
            return {**row, "title": "", "error": "time"}
        row["position"], q = m[1], (" " + q)[:m.start()]
    if m := EPISODE_RE.search(" " + q):
        row |= {"season": int(m[1]) if m[1] else None, "episode": int(m[2] or m[3])}
        q = (" " + q)[:m.start()]
    title = q.strip()
    return {**row, "title": title, "error": None if title else "title"}


def ago(d: dt.date, today: dt.date) -> str:
    """today, yesterday, Wed 21 within the last week, else Oct 11; plus the year if not this one."""
    if d == today:
        return "today"
    if d == today - dt.timedelta(days=1):
        return "yesterday"
    if today - dt.timedelta(days=6) <= d < today:
        return d.strftime("%a %d")
    return f"{d:%b} {d.day}" if d.year == today.year else f"{d:%b} {d.day} {d.year}"


def clock(seconds: int) -> str:
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}" + (f":{seconds % 60:02d}" if seconds % 60 else "")
