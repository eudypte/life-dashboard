"""Read-only access to the nutrition-log data folder.

Implements the rules in the data handoff. log_entry.py (the only writer) is the
source of truth; where they disagree, the script wins. Nothing here writes.
"""
import csv
import datetime as dt
import io
import json
import os
from dataclasses import dataclass
from pathlib import Path

NUTRIENTS = (
    "calories", "fat_g", "sat_fat_g", "trans_fat_g", "cholesterol_mg", "sodium_mg",
    "carbs_g", "fiber_g", "sugar_g", "added_sugar_g", "protein_g",
)
KG_PER_LB = 0.45359237
ENV_KEYS = ("NUTRITION_DIR", "NUTRITION_DAY_START_HOUR", "NUTRITION_WEIGHT_UNIT")


@dataclass(frozen=True)
class Config:
    data_dir: Path
    day_start_hour: int = 0
    weight_unit: str = "kg"


def load_config() -> Config:
    """Read the env block of the agent's settings.json; environment variables override."""
    settings = Path(os.environ.get(
        "LIFE_DASHBOARD_SETTINGS", "~/nutrition-agent/.claude/settings.json")).expanduser()
    env = json.loads(settings.read_text()).get("env", {}) if settings.exists() else {}
    env |= {k: os.environ[k] for k in ENV_KEYS if k in os.environ}
    if not env.get("NUTRITION_DIR"):
        raise RuntimeError(f"NUTRITION_DIR not set in {settings} or the environment")
    unit = env.get("NUTRITION_WEIGHT_UNIT", "kg").lower()
    return Config(
        data_dir=Path(env["NUTRITION_DIR"]).expanduser(),
        day_start_hour=int(env.get("NUTRITION_DAY_START_HOUR", 0)),
        weight_unit=unit if unit in ("kg", "lb") else "kg",
    )


def logical_today(cfg: Config, now: dt.datetime | None = None) -> dt.date:
    """A day starts at NUTRITION_DAY_START_HOUR, local time (the VM runs America/New_York)."""
    now = now or dt.datetime.now()
    return (now - dt.timedelta(hours=cfg.day_start_hour)).date()


# ---------- file reading with an mtime-keyed cache ----------

_cache: dict[Path, tuple[tuple[int, int], object]] = {}


def read_cached(path: Path, parse):
    """Parse a file, re-reading only when its mtime or size changed. None if missing."""
    try:
        st = path.stat()
    except FileNotFoundError:
        _cache.pop(path, None)
        return None
    key = (st.st_mtime_ns, st.st_size)
    hit = _cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    value = parse(path.read_text(encoding="utf-8"))
    _cache[path] = (key, value)
    return value


def _csv(text: str):
    # Adds append in place, so a read mid-write can see a partial last line. Every
    # complete row ends with LF; drop anything after the last one.
    return csv.DictReader(io.StringIO(text[: text.rfind("\n") + 1]))


def _num(cell) -> float | None:
    cell = (cell or "").strip()
    return float(cell) if cell else None  # empty = unknown, never zero


# ---------- food ----------

def macro_mismatch(row: dict) -> bool:
    """log_entry.py's write-time warning: kcal off 4P+4C+9F by more than 20%."""
    p, c, f, kcal = row["protein_g"], row["carbs_g"], row["fat_g"], row["calories"]
    if p is None or c is None or f is None or kcal < 40:
        return False
    # ponytail: 20% measured against stated calories; confirm against log_entry.py.
    return abs(kcal - (4 * p + 4 * c + 9 * f)) > 0.2 * kcal


def _food_row(r: dict) -> dict | None:
    try:
        row = {k: (r.get(k) or "").strip()
               for k in ("time", "food", "source", "serving_size", "source_url", "notes")}
        row["id"] = int(r["id"])
        row["servings"] = _num(r.get("servings"))
        for n in NUTRIENTS:
            row[n] = _num(r.get(n))
    except (KeyError, TypeError, ValueError):
        return None
    if row["calories"] is None:  # always present on a valid row
        return None
    row["suspect"] = macro_mismatch(row)
    return row


def _parse_day(text: str) -> list[dict]:
    return [row for row in map(_food_row, _csv(text)) if row]


def day_rows(cfg: Config, date: dt.date) -> list[dict]:
    """Rows of one logical day in log order. [] means nothing was logged."""
    return read_cached(cfg.data_dir / f"{date.isoformat()}.csv", _parse_day) or []


def totals(rows: list[dict]) -> dict:
    """Per nutrient: sum of known values and how many rows knew it."""
    out = {"items": len(rows)}
    for n in NUTRIENTS:
        known = [r[n] for r in rows if r[n] is not None]
        out[n] = sum(known)
        out[n + "_known"] = len(known)
    return out


def macro_split(rows: list[dict]) -> dict | None:
    """kcal from P/C/F over rows that have all three; None if no row does."""
    full = [r for r in rows if None not in (r["protein_g"], r["carbs_g"], r["fat_g"])]
    if not full:
        return None
    kcal = {"p": 4 * sum(r["protein_g"] for r in full),
            "c": 4 * sum(r["carbs_g"] for r in full),
            "f": 9 * sum(r["fat_g"] for r in full)}
    macro_total = sum(kcal.values()) or 1
    return {
        **{k + "_kcal": v for k, v in kcal.items()},
        **{k + "_pct": 100 * v / macro_total for k, v in kcal.items()},
        "rows": len(full), "of_rows": len(rows),
        "kcal_covered": sum(r["calories"] for r in full),
        "kcal_total": sum(r["calories"] for r in rows),
    }


def history(cfg: Config, end: dt.date, days: int) -> list[dict]:
    """One entry per date for `days` days ending at `end`, oldest first."""
    out = []
    for i in range(days - 1, -1, -1):
        date = end - dt.timedelta(days=i)
        rows = day_rows(cfg, date)
        out.append({"date": date, "rows": rows, "logged": bool(rows), "totals": totals(rows)})
    return out


def average(hist: list[dict], nutrient: str) -> float | None:
    """Mean over logged days only; unlogged days are gaps, not zeros."""
    vals = [d["totals"][nutrient] for d in hist if d["logged"]]
    return sum(vals) / len(vals) if vals else None


# ---------- weight ----------

def _parse_weights(text: str) -> list[dict]:
    out = []
    for r in _csv(text):
        try:
            out.append({
                "id": int(r["id"]),
                "date": dt.date.fromisoformat(r["date"].strip()),
                "time": r["time"].strip(),
                "kg": float(r["weight_kg"]),
                "entered": f'{(r.get("weight") or "").strip()} {(r.get("unit") or "").strip()}'.strip(),
                "source": (r.get("source") or "").strip(),
                "notes": (r.get("notes") or "").strip(),
            })
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda w: (w["date"], w["time"]))  # file is append order


def weights(cfg: Config) -> list[dict]:
    return read_cached(cfg.data_dir / "weight.csv", _parse_weights) or []


def weights_in(ws: list[dict], end: dt.date, days: int) -> list[dict]:
    start = end - dt.timedelta(days=days - 1)
    return [w for w in ws if start <= w["date"] <= end]


def weight_stats(ws: list[dict]) -> dict | None:
    """Matches `weight show`: mean of all entries (not per day), change = last - first."""
    if not ws:
        return None
    return {"latest": ws[-1], "mean": sum(w["kg"] for w in ws) / len(ws),
            "change": ws[-1]["kg"] - ws[0]["kg"], "count": len(ws)}


def rolling_mean(ws: list[dict], all_ws: list[dict], window: int = 7) -> list[float]:
    """For each entry in ws, mean of all entries dated within the `window` days ending that day."""
    out = []
    for w in ws:
        start = w["date"] - dt.timedelta(days=window - 1)
        vals = [x["kg"] for x in all_ws if start <= x["date"] <= w["date"]]
        out.append(sum(vals) / len(vals))
    return out


def to_unit(kg: float, unit: str) -> float:
    return kg / KG_PER_LB if unit == "lb" else kg


# ---------- reading ----------
# reading.csv: one row per update, appended in time order; the page reached so far in
# that book. Columns: date,time,book,author,page,pages,status (reading | done | dropped).

STATUSES = ("reading", "done", "dropped")


def _int(cell) -> int | None:
    cell = (cell or "").strip()
    return int(cell) if cell else None


def _parse_reading(text: str) -> list[dict]:
    out = []
    for r in _csv(text):
        try:
            row = {
                "date": dt.date.fromisoformat(r["date"].strip()),
                "time": (r.get("time") or "").strip(),
                "book": r["book"].strip(),
                "author": (r.get("author") or "").strip(),
                "page": _int(r.get("page")),
                "pages": _int(r.get("pages")),
                "status": (r.get("status") or "").strip().lower(),
            }
        except (AttributeError, KeyError, TypeError, ValueError):
            continue
        if row["book"]:
            out.append(row)
    return sorted(out, key=lambda r: (r["date"], r["time"]))


def reading(cfg: Config) -> list[dict]:
    return read_cached(cfg.data_dir / "reading.csv", _parse_reading) or []


def books(rows: list[dict]) -> list[dict]:
    """One entry per book (title, any case), in the order first logged. Later rows win;
    a blank cell keeps the earlier value. A done row without a page counts as the last page."""
    out: dict[str, dict] = {}
    for r in rows:
        b = out.setdefault(r["book"].casefold(), {
            "title": r["book"], "author": "", "pages": None, "page": None,
            "status": "reading", "started": r["date"], "finished": None})
        b["title"] = r["book"]
        b["author"] = r["author"] or b["author"]
        b["pages"] = r["pages"] or b["pages"]
        b["page"] = r["page"] if r["page"] is not None else b["page"]
        b["status"] = r["status"] if r["status"] in STATUSES else b["status"]
        b["finished"] = r["date"] if b["status"] == "done" else None
        if b["status"] == "done" and r["page"] is None:
            b["page"] = b["pages"] or b["page"]
    return list(out.values())


def pages_by_day(rows: list[dict]) -> dict[dt.date, int]:
    """Pages read per date: how far each book moved past its furthest page before.
    A book's first row counts from page 0; going back a page never counts."""
    furthest: dict[str, int] = {}
    length: dict[str, int] = {}
    out: dict[dt.date, int] = {}
    for r in rows:
        key = r["book"].casefold()
        length[key] = r["pages"] or length.get(key)
        page = r["page"] if r["page"] is not None else length[key] if r["status"] == "done" else None
        if page is None:
            continue
        gain = page - furthest.get(key, 0)
        if gain > 0:
            furthest[key] = page
            out[r["date"]] = out.get(r["date"], 0) + gain
    return out


def streaks(by_day: dict[dt.date, int], today: dt.date) -> tuple[int, int]:
    """(current, longest) runs of days with pages read. Today not read yet keeps yesterday's run."""
    day = today if by_day.get(today) else today - dt.timedelta(days=1)
    current = 0
    while by_day.get(day):
        current += 1
        day -= dt.timedelta(days=1)
    longest = run = 0
    prev = None
    for day in sorted(d for d, n in by_day.items() if n):
        run = run + 1 if prev and (day - prev).days == 1 else 1
        longest, prev = max(longest, run), day
    return current, longest


# ---------- media ----------
# media.csv: one row per update, appended in time order, where a title is now.
# Columns: date,time,title,kind,season,episode,episodes,position,runtime,status
# kind: show | anime | movie | youtube; episodes: how many the season has; position: H:MM:SS (or M:SS)
# into a movie, or with an episode, paused that far into it (no position: that episode is done);
# runtime: a movie's length in minutes; status: watching | done | dropped.
# The media page logs rows of the same shape to its own table (media.py), never here.

MEDIA_STATUSES = ("watching", "done", "dropped")


def seconds(position: str) -> int | None:
    """H:MM:SS or M:SS as seconds; None if it is neither."""
    parts = position.split(":")
    if not 2 <= len(parts) <= 3 or not all(p.isdigit() for p in parts) or any(int(p) > 59 for p in parts[1:]):
        return None
    return sum(int(p) * 60 ** i for i, p in enumerate(reversed(parts)))


def media_row(r: dict) -> dict | None:
    """One update from either log; None if it has no date or title, or a number is not one."""
    try:
        row = {
            "date": dt.date.fromisoformat(r["date"].strip()),
            "time": (r.get("time") or "").strip(),
            "title": r["title"].strip(),
            "kind": (r.get("kind") or "").strip().lower(),
            **{k: _int(r.get(k)) for k in ("season", "episode", "episodes", "runtime")},
            "position": (r.get("position") or "").strip(),
            "status": (r.get("status") or "").strip().lower(),
        }
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    if seconds(row["position"]) is None:
        row["position"] = ""
    return row if row["title"] else None


def _parse_media(text: str) -> list[dict]:
    return [row for row in map(media_row, _csv(text)) if row]


def media(cfg: Config) -> list[dict]:
    return read_cached(cfg.data_dir / "media.csv", _parse_media) or []


# ---------- presets ----------

def presets(cfg: Config) -> list[dict]:
    """One-serving totals per preset. Templates only; never intake."""
    data = read_cached(cfg.data_dir / "presets.json", json.loads) or {}
    out = []
    for key, p in sorted(data.get("presets", {}).items()):
        items = p.get("items", [])
        per = {}
        for n in NUTRIENTS:
            known = [i[n] * i.get("servings", 1) for i in items if i.get(n) is not None]
            per[n] = sum(known) if known else None
            per[n + "_known"] = len(known)
        out.append({"key": key, "name": p.get("name", key), "items": items, "per": per})
    return out


# ---------- folder health ----------

def conflicts(cfg: Config) -> list[str]:
    """Syncthing conflict copies mean two devices wrote at once. Never read them."""
    try:
        return sorted(p.name for p in cfg.data_dir.iterdir() if ".sync-conflict-" in p.name)
    except FileNotFoundError:
        return []
