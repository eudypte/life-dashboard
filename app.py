"""Life Dashboard: read-only views over the nutrition-log data folder, plus a todo list."""
import datetime as dt
import hashlib
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, quote

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import data
import todo

HERE = Path(__file__).parent
CFG = data.load_config()
THEMES = {
    "solarized-dark": "Solarized Dark", "solarized-light": "Solarized Light",
    "gruvbox": "Gruvbox", "nord": "Nord", "dracula": "Dracula",
    "tokyo-night": "Tokyo Night", "amber": "Amber CRT", "paper": "Paper",
}
RANGES = (7, 30, 90)
TODO_DB = todo.db_path(CFG.data_dir)  # refuses a path inside NUTRITION_DIR
todo.init(TODO_DB)

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


templates.env.filters |= {"n": fmt, "pct": pct, "plural": plural, "wt": weight, "wtd": weight_delta,
                          "day_label": todo.day_label, "task_when": todo.when, "task_meta": todo.meta}


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


# ---------- todo ----------
# The only writes. Plain forms that answer 303 back to the page they came from;
# with JS on, todo.html sends them with fetch and swaps in the changed parts of that page.

def todo_render(request: Request, page: str, project: str | None = None, edit: int | None = None,
                added: int | None = None, done: int | None = None, week: dt.date | None = None):
    today = data.logical_today(CFG)
    ts = todo.tasks(TODO_DB, today)
    week_view = todo.week_view(today, week) if page == "upcoming" else None
    groups = todo.groups(ts, today, page, project, week_view and week_view["first"])
    ticked = todo.get(TODO_DB, done) if done else None
    if ticked and any(ticked["id"] == t["id"] for g in groups if g["done"] for t in g["rows"]):
        ticked = None  # still on the page, struck through in the done group
    return render(
        request, "todo.html", "todo", page=page, project=project, groups=groups, counts=todo.counts(ts, today),
        here="/todo/project/" + quote(project, safe="") if project else f"/todo/upcoming/{week_view['start']}"
        if week else {"today": "/todo"}.get(page, "/todo/" + page), week=week_view,
        edit=edit, added=todo.get(TODO_DB, added) if added else None, ticked=ticked, rules=todo.RULES,
        date_options=todo.date_options(today),
    )


@app.get("/todo/parse")
def todo_parse(request: Request, q: str = "", page: str = "", project: str = ""):
    """The quick-add preview line, as an HTML fragment."""
    today = data.logical_today(CFG)
    p = todo.parse(q, today, today if page == "today" else None, project or None)
    return templates.TemplateResponse(request, "_todo_preview.html", {"p": p, "today": today})


@app.get("/todo")
@app.get("/todo/{page}")
def todo_view(request: Request, page: str = "today", edit: int | None = None,
              added: int | None = None, done: int | None = None):
    if request.url.path != "/todo" and page not in ("inbox", "upcoming"):
        raise HTTPException(404)
    return todo_render(request, page, edit=edit, added=added, done=done)


@app.get("/todo/upcoming/{week}")
def todo_week(request: Request, week: str, edit: int | None = None,
              added: int | None = None, done: int | None = None):
    if not (day := todo.iso_date(week)):
        raise HTTPException(404)
    return todo_render(request, "upcoming", edit=edit, added=added, done=done, week=day)


@app.get("/todo/project/{name}")
def todo_project(request: Request, name: str, edit: int | None = None,
                 added: int | None = None, done: int | None = None):
    return todo_render(request, "project", name, edit=edit, added=added, done=done)


async def form(request: Request) -> dict:
    """Form fields, last value wins. Stdlib parsing, so no python-multipart dependency."""
    return {k: v[-1] for k, v in parse_qs((await request.body()).decode(errors="replace"), keep_blank_values=True).items()}


def back(f: dict, query: str = "", task_id: int | None = None) -> RedirectResponse:
    page = f.get("next", "")
    page = page if page.startswith("/todo") else "/todo"  # only ever back to a todo page
    return RedirectResponse(page + query + (f"#t{task_id}" if task_id else ""), status_code=303)


@app.post("/todo/add")
async def todo_add(request: Request):
    f = await form(request)
    today = data.logical_today(CFG)
    due = todo.iso_date(f.get("due", "")) or (today if f.get("page") == "today" else None)  # the page's default
    t = todo.parse(f.get("q", ""), today, due, f.get("project") or None)
    if not t["title"]:
        return back(f)
    task_id = todo.add(TODO_DB, t, dt.datetime.now())
    return back(f, f"?added={task_id}", task_id)


@app.post("/todo/{task_id}/done")
async def todo_done(request: Request, task_id: int):
    f = await form(request)
    if f.get("undo"):
        todo.untick(TODO_DB, task_id)
        return back(f, task_id=task_id)
    todo.tick(TODO_DB, task_id, data.logical_today(CFG))
    return back(f, f"?done={task_id}", task_id)


@app.post("/todo/{task_id}/edit")
async def todo_edit(request: Request, task_id: int):
    f = await form(request)
    t = todo.from_form(f, data.logical_today(CFG))
    if t:
        todo.update(TODO_DB, task_id, t)
    return back(f, task_id=task_id)


@app.post("/todo/{task_id}/due")
async def todo_due(request: Request, task_id: int):
    """The date menu: one of its options ("none" clears the date) or a picked ISO date.
    drop=1: dragged onto a day of the Upcoming board."""
    f = await form(request)
    options = {value: d for value, _, d in todo.date_options(data.logical_today(CFG))}
    if f.get("due") in options:
        todo.move(TODO_DB, task_id, options[f["due"]])
    elif due := todo.iso_date(f.get("due", "")):
        (todo.drop if f.get("drop") else todo.move)(TODO_DB, task_id, due)
    return back(f, task_id=task_id)


@app.post("/todo/{task_id}/delete")
async def todo_delete(request: Request, task_id: int):
    f = await form(request)
    todo.delete(TODO_DB, task_id)
    return back(f)
