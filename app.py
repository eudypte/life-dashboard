"""Life Dashboard: read-only views over the nutrition-log data folder."""
import datetime as dt
import hashlib
import tomllib
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import data

HERE = Path(__file__).parent
CFG = data.load_config()
THEMES = {
    "solarized-dark": "Solarized Dark", "solarized-light": "Solarized Light",
    "gruvbox": "Gruvbox", "nord": "Nord", "dracula": "Dracula",
    "tokyo-night": "Tokyo Night", "amber": "Amber CRT", "paper": "Paper",
}
RANGES = (7, 30, 90)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")
# Cloudflare caches /static for hours; a content hash in the URL makes each deploy fetch fresh CSS.
templates.env.globals["css_version"] = hashlib.sha256((HERE / "static/style.css").read_bytes()).hexdigest()[:10]


# ---------- template helpers ----------

def fmt(v) -> str:
    """Same rule as the writer: integer within 0.05 of whole, else one decimal. None = unknown."""
    if v is None:
        return "?"
    return str(round(v)) if abs(v - round(v)) < 0.05 else f"{v:.1f}"


def pct(value, top) -> float:
    """Bar fill, 0-100, of value against top."""
    return round(min(max((value or 0) / top, 0), 1) * 100, 1) if top else 0


def plural(count: int, word: str) -> str:
    return f"{count} {word}{'' if count == 1 else 's'}"


def weight(kg) -> str:
    return f"{data.to_unit(kg, CFG.weight_unit):.1f} {CFG.weight_unit}"


def weight_delta(kg) -> str:
    return f"{data.to_unit(kg, CFG.weight_unit):+.1f} {CFG.weight_unit}"


templates.env.filters |= {"n": fmt, "pct": pct, "plural": plural, "wt": weight, "wtd": weight_delta}


def weight_chart(window, all_ws, end: dt.date, days: int, w: int = 600, h: int = 220):
    """Inline-SVG geometry: weigh-in points plus the 7-day rolling mean."""
    if not window:
        return None
    unit = CFG.weight_unit
    vals = [data.to_unit(e["kg"], unit) for e in window]
    means = [data.to_unit(m, unit) for m in data.rolling_mean(window, all_ws)]
    lo, hi = min(vals + means), max(vals + means)
    pad = max((hi - lo) * 0.15, 0.5)
    lo, hi = lo - pad, hi + pad
    start = end - dt.timedelta(days=days - 1)
    left, right, top, bottom = 58, w - 10, 12, h - 30

    def x(e):
        # Center of the logged day: times can be placeholders (a moved entry gets 23:56),
        # and plotting by time pushes late-night weigh-ins onto the next day.
        return left + ((e["date"] - start).days + 0.5) / days * (right - left)

    def y(v):
        return top + (hi - v) / (hi - lo) * (bottom - top)

    xs = [x(e) for e in window]
    return {
        "w": w, "h": h, "left": left, "right": right, "bottom": bottom,
        "points": [(round(px, 1), round(y(v), 1)) for px, v in zip(xs, vals)],
        "mean": " ".join(f"{px:.1f},{y(m):.1f}" for px, m in zip(xs, means)),
        "yticks": [(round(y(v), 1), f"{v:.1f}") for v in (hi - pad, (lo + hi) / 2, lo + pad)],
        "xlabels": [(left, "start", start), (right, "end", end)],
    }


def goals() -> dict:
    """Targets live in this app's config.toml, never in the data folder."""
    return data.read_cached(HERE / "config.toml", tomllib.loads) or {}


def render(request: Request, name: str, view: str, **ctx):
    return templates.TemplateResponse(request, name, {
        "view": view, "today": data.logical_today(CFG), "now": dt.datetime.now(),
        "conflicts": data.conflicts(CFG), "unit": CFG.weight_unit, "goal": goals(),
        "themes": THEMES, "ranges": RANGES, **ctx,
    })


# ---------- views ----------

@app.get("/")
def today_view(request: Request, date: dt.date | None = None):
    today = data.logical_today(CFG)
    date = min(date or today, today)
    rows = data.day_rows(CFG, date)
    week = data.history(CFG, date, 7)
    all_ws = data.weights(CFG)
    month = data.weights_in(all_ws, date, 30)
    return render(
        request, "today.html", "today",
        date=date, rows=rows, t=data.totals(rows), week=week,
        week_avg=data.average(week, "calories"),
        week_top=goals().get("calorie_goal_kcal") or max(d["totals"]["calories"] for d in week),
        protein_top=goals().get("protein_goal_g") or max(d["totals"]["protein_g"] for d in week),
        weighins=[w for w in all_ws if w["date"] == date],
        weight_stats=data.weight_stats(month), chart=weight_chart(month, all_ws, date, 30),
        prev=date - dt.timedelta(days=1),
        next=date + dt.timedelta(days=1) if date < today else None,
    )


@app.get("/calories")
def calories_view(request: Request, days: int = Query(30, ge=1, le=366)):
    hist = data.history(CFG, data.logical_today(CFG), days)
    logged = [d for d in hist if d["logged"]]
    return render(
        request, "calories.html", "calories", days=days, hist=hist[::-1],
        logged=len(logged),
        avg={n: data.average(hist, n) for n in ("calories", "protein_g", "carbs_g", "fat_g")},
        top=goals().get("calorie_goal_kcal") or max((d["totals"]["calories"] for d in hist), default=0),
        goal_hits=sum(d["totals"]["protein_g"] >= goals().get("protein_goal_g", float("inf"))
                      for d in logged),
    )


@app.get("/macros")
def macros_view(request: Request, days: int = Query(30, ge=1, le=366)):
    hist = data.history(CFG, data.logical_today(CFG), days)
    return render(
        request, "macros.html", "macros", days=days,
        hist=[{**d, "split": data.macro_split(d["rows"])} for d in hist[::-1]],
        overall=data.macro_split([r for d in hist for r in d["rows"]]),
    )


@app.get("/weight")
def weight_view(request: Request, days: int = Query(30, ge=1, le=366)):
    today = data.logical_today(CFG)
    all_ws = data.weights(CFG)
    window = data.weights_in(all_ws, today, days)
    return render(
        request, "weight.html", "weight", days=days, entries=window[::-1],
        stats=data.weight_stats(window),
        chart=weight_chart(window, all_ws, today, days, 1000, 280),
        chart_narrow=weight_chart(window, all_ws, today, days, 600, 320),
    )


@app.get("/presets")
def presets_view(request: Request):
    return render(request, "presets.html", "presets", presets=data.presets(CFG))
