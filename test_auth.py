"""Checks for the login. Run: .venv/bin/python -m unittest"""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

TOKEN = "test-token-not-a-real-one-x"  # 26+ chars, like a real one
_tmp = tempfile.TemporaryDirectory()
unittest.addModuleCleanup(_tmp.cleanup)
TMP = Path(_tmp.name)
with mock.patch.dict(os.environ, {"NUTRITION_DIR": str(TMP / "nutrition"), "TODO_DB": str(TMP / "todo.sqlite"),
                                  "LIFE_DASHBOARD_SETTINGS": str(TMP / "none.json"), "LIFE_DASHBOARD_TOKEN": TOKEN}):
    import app


def call(method: str, path: str, form: dict | None = None, cookie: str = "", **headers):
    """One request straight into the ASGI app (no httpx needed). Returns (status, headers, body)."""
    path, _, query = path.partition("?")
    body = urlencode(form or {}).encode()
    headers = {"host": "127.0.0.1:8090", **({"cookie": cookie} if cookie else {}),
               **{k.replace("_", "-"): v for k, v in headers.items()}}
    scope = {"type": "http", "http_version": "1.1", "method": method, "scheme": "http", "path": path,
             "raw_path": path.encode(), "query_string": query.encode(), "root_path": "",
             "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
             "server": ("127.0.0.1", 8090), "client": ("127.0.0.1", 50000)}
    sent = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        sent.append(message)

    asyncio.run(app.app(scope, receive, send))
    return (sent[0]["status"], {k.decode(): v.decode() for k, v in sent[0]["headers"]},
            b"".join(m.get("body", b"") for m in sent[1:]).decode())


LOGIN_FORM = 'type="password" autocomplete="current-password"'
ADD_FORM = 'action="/todo/add"'
LOG_IN = '>log in</a>'


class LoginTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"LIFE_DASHBOARD_TOKEN": TOKEN})
        patch.start()
        self.addCleanup(patch.stop)

    def login(self, token=TOKEN, next="/todo/inbox", **headers):
        return call("POST", "/login", {"token": token, "next": next}, **headers)

    def jar(self):
        return self.login()[1]["set-cookie"].split(";")[0]

    def test_no_cookie_reads_everything_read_only(self):
        task = app.todo.add(app.TODO_DB, app.todo.parse("read me today #home @deep", app.data.logical_today(app.CFG), None),
                            app.dt.datetime.now())
        for page in ("/", "/todo", "/todo/inbox", "/todo/upcoming", f"/todo/inbox?edit={task}", "/todo/project/home",
                     "/todo/label/deep", "/calories", "/macros", "/weight", "/presets", "/reading", "/media"):
            status, _, body = call("GET", page, sec_fetch_mode="navigate")
            self.assertEqual(status, 200, page)
            self.assertNotIn(LOGIN_FORM, body)
            self.assertNotIn('action="/logout"', body)
            for control in ('method="post"', 'draggable="true"', 'class="edit"', 'details class="when"', 'id="ctx"', "<script>\n  // Quick add"):
                self.assertNotIn(control, body, page)
        body = call("GET", "/todo/upcoming?x=1")[2]
        self.assertIn("read me", body)
        self.assertIn('href="/login?next=/todo/upcoming%3Fx%3D1"', body)  # back to the same page after logging in
        self.assertEqual(call("GET", "/todo/parse?q=x", sec_fetch_mode="cors")[0], 200)  # a read: open too

    def test_no_cookie_cannot_write(self):
        posts = lambda routes: [r.path for r in routes if "POST" in (getattr(r, "methods", None) or ())]  # noqa: E731
        self.assertEqual(posts(app.app.routes), ["/login", "/logout"])  # every other write is on todo_app
        writes = posts(app.todo_app.routes) + posts(app.media_app.routes)
        self.assertGreaterEqual(len(writes), 12)
        today = app.data.logical_today(app.CFG)
        task = app.todo.add(app.TODO_DB, app.todo.parse("keep me #home @deep", today, None), app.dt.datetime.now())
        app.media.log(app.TODO_DB, {"date": today, "time": "09:00:00", "title": "changed", "season": 1, "episode": 1})
        before = (app.todo.tasks(app.TODO_DB, today), app.todo.projects(app.TODO_DB), app.todo.label_colors(app.TODO_DB),
                  app.media.page_rows(app.TODO_DB), app.media.placed(app.TODO_DB))
        f = {"q": "new task", "next": "/todo", "title": "changed", "name": "y", "due": "today", "color": "p", "do": "next", "order": "changed",
             "priority": "1", "project": "elsewhere", "to": "top"}
        for path in writes:
            path = path.replace("{task_id}", str(task)).replace("{name}", "home" if "project" in path else "deep")
            self.assertEqual(call("POST", path, f, sec_fetch_mode="cors")[0], 403, path)  # a fetch from todo.html
            status, headers, _ = call("POST", path, f, referer="http://127.0.0.1:8090/todo/inbox?x=1")  # a plain form
            self.assertEqual((status, headers["location"]), (303, "/login?next=/todo/inbox%3Fx%3D1"), path)
        self.assertEqual(call("POST", "/todo/add", f, referer="https://evil.example/x")[1]["location"], "/login?next=/todo")
        self.assertEqual((app.todo.tasks(app.TODO_DB, today), app.todo.projects(app.TODO_DB), app.todo.label_colors(app.TODO_DB),
                          app.media.page_rows(app.TODO_DB), app.media.placed(app.TODO_DB)), before)

    def test_login_page(self):
        status, _, body = call("GET", "/login")
        self.assertEqual(status, 200)
        self.assertIn(LOGIN_FORM, body)
        self.assertIn('autocomplete="username" value="life-dashboard"', body)

    def test_wrong_token(self):
        status, headers, body = self.login("nope")
        self.assertEqual(status, 401)
        self.assertNotIn("set-cookie", headers)
        self.assertIn("wrong token", body)
        self.assertIn('name="next" value="/todo/inbox"', body)
        self.assertNotIn(ADD_FORM, call("GET", "/todo", cookie="auth=" + TOKEN)[2])  # the raw token is no cookie

    def test_right_token(self):
        status, headers, _ = self.login()
        self.assertEqual((status, headers["location"]), (303, "/todo/inbox"))
        cookie = headers["set-cookie"]
        self.assertNotIn(TOKEN, cookie)
        for part in ("HttpOnly", "Max-Age=31536000", "Path=/", "SameSite=lax"):
            self.assertIn(part, cookie)
        self.assertNotIn("Secure", cookie)  # plain http on 127.0.0.1 for local dev
        self.assertIn("Secure", self.login(host="life.selimd.me")[1]["set-cookie"])
        jar = cookie.split(";")[0]
        for page in ("/", "/todo"):
            status, _, body = call("GET", page, cookie=jar)
            self.assertEqual(status, 200)
            self.assertNotIn(LOGIN_FORM, body)
            self.assertIn('action="/logout"', body)
            self.assertNotIn(LOG_IN, body)
        self.assertIn(ADD_FORM, call("GET", "/todo", cookie=jar)[2])
        self.assertIn('class="edit"', call("GET", "/todo/inbox?edit=1", cookie=jar)[2])
        self.assertNotIn('action="/logout"', call("GET", "/calories")[2])
        self.assertEqual(call("GET", "/todo/parse?q=x", cookie=jar)[0], 200)
        self.assertEqual(call("POST", "/todo/add", {"q": "x", "next": "/todo"}, cookie=jar)[0], 303)
        menu = app.todo.add(app.TODO_DB, app.todo.parse("menu task p1", app.data.logical_today(app.CFG)), app.dt.datetime.now())
        self.assertIn(f'id="t{menu}" data-pri="1" data-proj="" data-due=""', call("GET", "/todo/inbox", cookie=jar)[2])
        self.assertIn('data-task-verb="priority"', call("GET", "/todo/inbox", cookie=jar)[2])
        for verb, f in (("priority", {"priority": "3"}), ("project", {"project": "menu-proj"}), ("due", {"due": "tomorrow"})):
            self.assertEqual(call("POST", f"/todo/{menu}/{verb}", {**f, "next": "/todo/inbox"}, cookie=jar)[1]["location"],
                             f"/todo/inbox#t{menu}")
        t = app.todo.get(app.TODO_DB, menu)
        self.assertEqual((t["priority"], t["project"], t["due"]), (3, "menu-proj", app.data.logical_today(app.CFG) + app.dt.timedelta(days=1)))
        self.assertEqual(call("GET", "/login?next=/todo/inbox", cookie=jar)[1]["location"], "/todo/inbox")
        self.assertIn('action="/media/log"', call("GET", "/media", cookie=jar)[2])
        self.assertEqual(call("POST", "/media/log", {"q": "Signed In s1e1"}, cookie=jar)[1]["location"], "/media")
        self.assertEqual(call("POST", "/media/step", {"title": "Signed In", "do": "next"}, cookie=jar)[0], 303)
        self.assertEqual(call("POST", "/media/step", {"title": "Signed In", "at": "1:5"}, cookie=jar)[0], 303)
        self.assertEqual([(r["season"], r["episode"]) for r in app.media.page_rows(app.TODO_DB) if r["title"] == "Signed In"],
                         [(1, 1), (1, 2), (1, 5)])
        self.assertEqual(call("POST", "/media/step", {"title": "nobody", "do": "next"}, cookie=jar)[0], 404)
        self.assertEqual(call("POST", "/media/log", {"q": "Signed In s1e6 5:45"}, cookie=jar)[1]["location"], "/media")
        self.assertIn("S1E6</span> at <span class=\"ts\">5:45", call("GET", "/media", cookie=jar)[2])
        status, headers, _ = call("POST", "/media/log", {"q": "s1e6 5:75"}, cookie=jar)
        self.assertEqual((status, headers["location"]), (303, "/media?error=time&q=s1e6%205%3A75"))
        body = call("GET", headers["location"], cookie=jar)[2]
        self.assertIn(app.media.ERRORS["time"], body)
        self.assertIn('value="s1e6 5:75"', body)
        self.assertEqual(call("POST", "/media/order", {"order": "Signed In\nOther"}, cookie=jar, sec_fetch_mode="cors")[0], 204)
        self.assertEqual(app.media.placed(app.TODO_DB), {"signed in": 0, "other": 1})
        self.assertIn('draggable="true" data-title="Signed In"', call("GET", "/media", cookie=jar)[2])
        with mock.patch.dict(os.environ, {"LIFE_DASHBOARD_TOKEN": TOKEN + "2"}):  # a new token logs it out
            self.assertIn(LOG_IN, call("GET", "/todo", cookie=jar)[2])
            self.assertEqual(call("POST", "/todo/add", {"q": "x"}, cookie=jar, sec_fetch_mode="cors")[0], 403)
        status, headers, _ = call("POST", "/logout", cookie=jar)
        self.assertEqual((status, headers["location"]), (303, "/login"))
        self.assertIn('auth=""', headers["set-cookie"])

    def test_token_unset(self):
        jar = self.jar()
        with mock.patch.dict(os.environ):
            del os.environ["LIFE_DASHBOARD_TOKEN"]
            self.assertEqual(self.login("")[0], 401)
            for cookie in (jar, "auth="):
                self.assertNotIn(ADD_FORM, call("GET", "/todo", cookie=cookie)[2])
                self.assertEqual(call("POST", "/todo/add", {"q": "x"}, cookie=cookie, sec_fetch_mode="cors")[0], 403)

    def test_foreign_origin(self):
        jar, evil = self.jar(), {"origin": "https://evil.example"}
        self.assertEqual(call("POST", "/todo/add", {"q": "x"}, cookie=jar, **evil)[0], 403)
        self.assertEqual(call("POST", "/media/log", {"q": "x"}, cookie=jar, **evil)[0], 403)
        self.assertEqual(self.login(**evil)[0], 403)
        self.assertEqual(call("POST", "/todo/add", {"q": "x", "next": "/todo"}, cookie=jar,
                              origin="http://127.0.0.1:8090")[0], 303)

    def test_next_stays_on_site(self):
        for target in ("//evil.example", "/\\evil.example", "https://evil.example"):
            self.assertEqual(self.login(next=target)[1]["location"], "/")
        self.assertEqual(self.login(next="/?date=2026-10-01")[1]["location"], "/?date=2026-10-01")


if __name__ == "__main__":
    unittest.main()
