"""Life Dashboard: read-only views over the nutrition-log data folder (food, weight, reading, media), plus a todo list."""
import datetime as dt
import hashlib
import hmac
import logging
import os
import re
import tomllib
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import data
import media
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
media.init(TODO_DB)  # the media page's own log shares the todo file

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
                          "day_label": todo.day_label, "ago": media.ago, "clock": media.clock, "task_when": todo.when, "task_meta": todo.meta}


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
        "themes": THEMES, "ranges": RANGES, "signed_in": signed_in(request),
        "read_only": not signed_in(request), **ctx,
    })


# ---------- login ----------
# Anyone can read every page; one token, set on the server, is needed to change anything (require_login).
# Signed out, the home page, /todo and /media render read-only, with a "log in" link in the footer.
# A signed-out write is refused: a plain form post goes to the login form, a fetch from todo.html gets 403.

COOKIE = "auth"
if not os.environ.get("LIFE_DASHBOARD_TOKEN"):
    logging.getLogger("uvicorn.error").warning("LIFE_DASHBOARD_TOKEN is not set: the home page and /todo stay locked")


def token() -> str:
    """Read per request, so a missing token only locks the pages instead of stopping the app."""
    return os.environ.get("LIFE_DASHBOARD_TOKEN", "")


def session() -> str | None:
    """The cookie value: an HMAC of the token, so the cookie never holds the token itself
    and changing the token logs every device out. None while no token is set."""
    return hmac.new(token().encode(), b"life-dashboard login", hashlib.sha256).hexdigest() if token() else None


def signed_in(request: Request) -> bool:
    expected = session()
    return expected is not None and hmac.compare_digest(request.cookies.get(COOKIE, ""), expected)


def same_origin(request: Request) -> bool:
    """A browser POST from another site carries its Origin; no Origin (curl, old browsers) is allowed."""
    origin = request.headers.get("origin")
    return origin is None or urlsplit(origin).netloc == request.headers.get("host")


def local(page: str) -> str:
    """Only ever back to a page of this site."""
    return page if page.startswith("/") and not page.startswith(("//", "/\\")) else "/"


def login_page(request: Request, page: str, wrong: bool = False):
    return templates.TemplateResponse(request, "login.html", {"next": local(page), "wrong": wrong},
                                      status_code=401 if wrong else 200, headers={"Cache-Control": "no-store"})


class LoginNeeded(Exception):
    pass


@app.exception_handler(LoginNeeded)
def login_needed(request: Request, _: LoginNeeded):
    """A form sent signed out: nothing changes; log in, then back to the page it came from."""
    ref = urlsplit(request.headers.get("referer", ""))
    page = ref.path + (f"?{ref.query}" if ref.query else "") if ref.netloc == request.headers.get("host") else "/todo"
    return RedirectResponse("/login?next=" + quote(local(page), safe="/"), status_code=303)


def require_login(request: Request):
    """Reads are open to anyone; every write needs the login."""
    if request.method == "POST" and not same_origin(request):
        raise HTTPException(403)
    if request.method == "GET" or signed_in(request):
        return
    if request.headers.get("sec-fetch-mode", "navigate") == "navigate":
        raise LoginNeeded
    raise HTTPException(403)


@app.get("/login")
def login_view(request: Request, next: str = "/"):
    return RedirectResponse(local(next), status_code=303) if signed_in(request) else login_page(request, next)


@app.post("/login")
async def login(request: Request):
    if not same_origin(request):
        raise HTTPException(403)
    f = await form(request)
    if not token() or not hmac.compare_digest(f.get("token", "").encode(), token().encode()):
        return login_page(request, f.get("next", ""), wrong=True)
    r = RedirectResponse(local(f.get("next", "")), status_code=303)
    r.set_cookie(COOKIE, session(), max_age=365 * 24 * 3600, httponly=True, samesite="lax",
                 secure=request.url.scheme == "https" or request.url.hostname not in ("127.0.0.1", "localhost"))
    return r


@app.post("/logout")
def logout():
    r = RedirectResponse("/login", status_code=303)
    r.delete_cookie(COOKIE)
    return r


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


@app.get("/reading")
def reading_view(request: Request):
    today = data.logical_today(CFG)
    rows = data.reading(CFG)
    books = data.books(rows)
    by_day = data.pages_by_day(rows)
    week = [(d, by_day.get(d, 0)) for d in (today - dt.timedelta(days=i) for i in range(6, -1, -1))]
    finished = sorted((b for b in books if b["finished"]), key=lambda b: b["finished"], reverse=True)
    return render(
        request, "reading.html", "reading", logged=bool(rows), week=week,
        week_pages=sum(n for _, n in week), week_top=max(n for _, n in week) or 1,
        now_reading=[b for b in reversed(books) if b["status"] == "reading"], finished=finished,
        year_books=sum(b["finished"].year == today.year for b in finished),
        year_pages=sum(n for d, n in by_day.items() if d.year == today.year),
        streak=data.streaks(by_day, today),
    )


@app.get("/presets")
def presets_view(request: Request):
    return render(request, "presets.html", "presets", presets=data.presets(CFG))


# ---------- media ----------
# media.csv (written outside the app) and the page's own log, read as one. +1, done, a tick or quick add append a row
# to the page's log; dragging a title saves the list's order. Plain forms that answer 303 back to /media.
# Writes need the login, as on todo_app.

media_app = APIRouter(dependencies=[Depends(require_login)])


def media_titles() -> list[dict]:
    return media.titles(media.merged(data.media(CFG), media.page_rows(TODO_DB)))


@media_app.get("/media")
def media_view(request: Request):
    ts = media_titles()
    return render(request, "media.html", "media", logged=bool(ts),
                  watching=media.arrange([t for t in ts if t["status"] == "watching"], media.placed(TODO_DB)),
                  finished=[t for t in ts if t["status"] != "watching"],
                  seasons=media.seasons, is_movie=media.is_movie, seconds=data.seconds)


def media_log(**row) -> RedirectResponse:
    now = dt.datetime.now()
    media.log(TODO_DB, {"date": data.logical_today(CFG), "time": now.strftime("%H:%M:%S"), **row})
    return RedirectResponse("/media", status_code=303)


@media_app.post("/media/log")
async def media_add(request: Request):
    """Quick add. A title already logged keeps its spelling."""
    f = await form(request)
    row = media.parse(f.get("q", ""))
    if not row["title"]:
        return RedirectResponse("/media", status_code=303)
    known = next((t for t in media_titles() if t["title"].casefold() == row["title"].casefold()), None)
    if known:
        row["title"] = known["title"]
    elif row["position"]:
        row["kind"] = "movie"
    return media_log(**row)


@media_app.post("/media/step")
async def media_step(request: Request):
    """+1, done, or a tick (at=season:episode, season blank when the title has none) on a logged title."""
    f = await form(request)
    t = next((t for t in media_titles() if t["title"] == f.get("title")), None)
    if not t:
        raise HTTPException(404)
    if f.get("do") == "done":
        return media_log(title=t["title"], status="done")
    if f.get("do") == "next":
        season, episode = media.step(t)
    elif m := re.fullmatch(r"(\d*):(\d+)", f.get("at", "")):
        season, episode = int(m[1]) if m[1] else None, int(m[2])
    else:
        return RedirectResponse("/media", status_code=303)
    return media_log(title=t["title"], season=season, episode=episode, status="watching")


@media_app.post("/media/order")
async def media_order(request: Request):
    """Dragged (media.html): the titles in their new order, one per line."""
    f = await form(request)
    media.set_order(TODO_DB, [title for title in f.get("order", "").split("\n") if title])
    return Response(status_code=204)


app.include_router(media_app)


# ---------- todo ----------
# The other writes. Plain forms that answer 303 back to the page they came from;
# with JS on, todo.html sends them with fetch and swaps in the changed parts of that page.
# Anyone can read todo_app's pages; its writes need the login (require_login).

todo_app = APIRouter(dependencies=[Depends(require_login)])


def todo_render(request: Request, page: str, project: str | None = None, edit: int | None = None,
                added: int | None = None, done: int | None = None, week: dt.date | None = None,
                label: str | None = None):
    today = data.logical_today(CFG)
    edit = edit if signed_in(request) else None  # signed out: read-only, no edit form
    ts = todo.tasks(TODO_DB, today)
    week_view = todo.week_view(today, week) if page == "upcoming" else None
    groups = todo.groups(ts, today, page, project or label, week_view and week_view["first"])
    ticked = todo.get(TODO_DB, done) if done else None
    if ticked and any(ticked["id"] == t["id"] for g in groups if g["done"] for t in g["rows"]):
        ticked = None  # still on the page, struck through in the done group
    return render(
        request, "todo.html", "todo", page=page, project=project, groups=groups,
        counts=todo.counts(ts, today, todo.projects(TODO_DB), todo.label_colors(TODO_DB)), label=label,
        here="/todo/project/" + quote(project, safe="") if project else "/todo/label/" + quote(label, safe="")
        if label else f"/todo/upcoming/{week_view['start']}" if week else {"today": "/todo"}.get(page, "/todo/" + page),
        week=week_view, palette=todo.PALETTE,
        edit=edit, added=todo.get(TODO_DB, added) if added else None, ticked=ticked, rules=todo.RULES,
        date_options=todo.date_options(today),
    )


@todo_app.get("/todo/parse")
def todo_parse(request: Request, q: str = "", page: str = "", project: str = "", due: str = "", label: str = ""):
    """The quick-add preview line, as an HTML fragment that also carries the token spans to highlight."""
    today = data.logical_today(CFG)
    p = todo.parse(q, today, todo.iso_date(due) or (today if page == "today" else None), project or None, label or None)
    colors = todo.label_colors(TODO_DB)
    color = lambda name: colors.get(name) or todo.default_color(name)  # noqa: E731  a new label's coming color
    p["labels"] = [(name, color(name)) for name in p["labels"]]
    p["tokens"] = [(a, b, f"label lc-{color(q[a + 1:b].lower())}" if kind == "label" else kind) for a, b, kind in p["tokens"]]
    return templates.TemplateResponse(request, "_todo_preview.html", {"p": p, "today": today})


@todo_app.get("/todo")
@todo_app.get("/todo/{page}")
def todo_view(request: Request, page: str = "today", edit: int | None = None,
              added: int | None = None, done: int | None = None):
    if request.url.path != "/todo" and page not in ("inbox", "upcoming"):
        raise HTTPException(404)
    return todo_render(request, page, edit=edit, added=added, done=done)


@todo_app.get("/todo/upcoming/{week}")
def todo_week(request: Request, week: str, edit: int | None = None,
              added: int | None = None, done: int | None = None):
    if not (day := todo.iso_date(week)):
        raise HTTPException(404)
    return todo_render(request, "upcoming", edit=edit, added=added, done=done, week=day)


@todo_app.get("/todo/project/{name}")
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


@todo_app.post("/todo/add")
async def todo_add(request: Request):
    f = await form(request)
    today = data.logical_today(CFG)
    due = todo.iso_date(f.get("due", "")) or (today if f.get("page") == "today" else None)  # the page's default
    t = todo.parse(f.get("q", ""), today, due, f.get("project") or None, f.get("label") or None)
    if not t["title"]:
        return back(f)
    task_id = todo.add(TODO_DB, t, dt.datetime.now())
    return back(f, f"?added={task_id}", task_id)


@todo_app.post("/todo/{task_id}/done")
async def todo_done(request: Request, task_id: int):
    f = await form(request)
    if f.get("undo"):
        todo.untick(TODO_DB, task_id)
        return back(f, task_id=task_id)
    todo.tick(TODO_DB, task_id, data.logical_today(CFG))
    return back(f, f"?done={task_id}", task_id)


@todo_app.post("/todo/{task_id}/edit")
async def todo_edit(request: Request, task_id: int):
    f = await form(request)
    t = todo.from_form(f, data.logical_today(CFG))
    if t:
        todo.update(TODO_DB, task_id, t)
    return back(f, task_id=task_id)


@todo_app.post("/todo/{task_id}/due")
async def todo_due(request: Request, task_id: int):
    """The date menu: one of its options ("none" clears the date) or a picked ISO date.
    drop=1: dragged on the Upcoming board onto that day, with the day's new order as ids in `order`."""
    f = await form(request)
    options = {value: d for value, _, d in todo.date_options(data.logical_today(CFG))}
    if f.get("due") in options:
        todo.move(TODO_DB, task_id, options[f["due"]])
    elif (due := todo.iso_date(f.get("due", ""))) and f.get("drop"):
        todo.drop(TODO_DB, task_id, due, [int(i) for i in f.get("order", "").split(",") if i.isdigit()])
    elif due:
        todo.move(TODO_DB, task_id, due)
    return back(f, task_id=task_id)


@todo_app.get("/todo/label/{name}")
def todo_label(request: Request, name: str, edit: int | None = None,
               added: int | None = None, done: int | None = None):
    return todo_render(request, "label", edit=edit, added=added, done=done, label=name)


@todo_app.post("/todo/label/{name}/rename")
async def todo_label_rename(request: Request, name: str):
    f = await form(request)
    if (new := todo.project_name(f.get("name", ""))) and new != name:
        todo.rename_label(TODO_DB, name, new)
        if f.get("next") == "/todo/label/" + quote(name, safe=""):  # was on its page: follow the new name
            f["next"] = "/todo/label/" + quote(new, safe="")
    return back(f)


@todo_app.post("/todo/label/{name}/color")
async def todo_label_color(request: Request, name: str):
    f = await form(request)
    todo.color_label(TODO_DB, name, f.get("color", ""))
    return back(f)


@todo_app.post("/todo/label/{name}/delete")
async def todo_label_delete(request: Request, name: str):
    """It comes off its tasks; the tasks stay."""
    f = await form(request)
    todo.delete_label(TODO_DB, name)
    if f.get("next") == "/todo/label/" + quote(name, safe=""):
        f["next"] = "/todo"
    return back(f)


@todo_app.post("/todo/project/{name}/rename")
async def todo_project_rename(request: Request, name: str):
    f = await form(request)
    if (new := todo.project_name(f.get("name", ""))) and new != name:
        todo.rename_project(TODO_DB, name, new)
        if f.get("next") == "/todo/project/" + quote(name, safe=""):  # was on its page: follow the new name
            f["next"] = "/todo/project/" + quote(new, safe="")
    return back(f)


@todo_app.post("/todo/project/{name}/delete")
async def todo_project_delete(request: Request, name: str):
    """Its tasks move to the inbox."""
    f = await form(request)
    todo.delete_project(TODO_DB, name)
    if f.get("next") == "/todo/project/" + quote(name, safe=""):
        f["next"] = "/todo/inbox"
    return back(f)


@todo_app.post("/todo/{task_id}/delete")
async def todo_delete(request: Request, task_id: int):
    f = await form(request)
    todo.delete(TODO_DB, task_id)
    return back(f)


app.include_router(todo_app)
