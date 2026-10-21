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


class LoginTest(unittest.TestCase):
    def setUp(self):
        patch = mock.patch.dict(os.environ, {"LIFE_DASHBOARD_TOKEN": TOKEN})
        patch.start()
        self.addCleanup(patch.stop)

    def login(self, token=TOKEN, next="/todo/inbox", **headers):
        return call("POST", "/login", {"token": token, "next": next}, **headers)

    def jar(self):
        return self.login()[1]["set-cookie"].split(";")[0]

    def test_no_cookie(self):
        for page in ("/", "/todo", "/todo/upcoming?edit=3"):  # a page load: the login form in place
            status, headers, body = call("GET", page, sec_fetch_mode="navigate")
            self.assertEqual((status, headers["cache-control"]), (200, "no-store"))
            self.assertIn(LOGIN_FORM, body)
        self.assertIn('name="next" value="/todo/upcoming?edit=3"', call("GET", "/todo/upcoming?edit=3")[2])
        self.assertEqual(call("GET", "/todo/parse?q=x", sec_fetch_mode="cors")[0], 401)  # fetch from todo.html
        self.assertEqual(call("POST", "/todo/add", {"q": "x"}, sec_fetch_mode="cors")[0], 401)
        self.assertEqual(call("POST", "/todo/1/delete")[0], 401)
        for page in ("/calories", "/macros", "/weight", "/presets"):  # the other views stay public
            status, _, body = call("GET", page)
            self.assertEqual(status, 200)
            self.assertNotIn(LOGIN_FORM, body)

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
        self.assertIn(LOGIN_FORM, call("GET", "/todo", cookie="auth=" + TOKEN)[2])  # the raw token is no cookie

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
        self.assertNotIn('action="/logout"', call("GET", "/calories")[2])
        self.assertEqual(call("GET", "/todo/parse?q=x", cookie=jar)[0], 200)
        self.assertEqual(call("POST", "/todo/add", {"q": "x", "next": "/todo"}, cookie=jar)[0], 303)
        self.assertEqual(call("GET", "/login?next=/todo/inbox", cookie=jar)[1]["location"], "/todo/inbox")
        with mock.patch.dict(os.environ, {"LIFE_DASHBOARD_TOKEN": TOKEN + "2"}):  # a new token logs it out
            self.assertIn(LOGIN_FORM, call("GET", "/todo", cookie=jar)[2])
        status, headers, _ = call("POST", "/logout", cookie=jar)
        self.assertEqual((status, headers["location"]), (303, "/login"))
        self.assertIn('auth=""', headers["set-cookie"])

    def test_token_unset(self):
        jar = self.jar()
        with mock.patch.dict(os.environ):
            del os.environ["LIFE_DASHBOARD_TOKEN"]
            self.assertEqual(self.login("")[0], 401)
            for cookie in (jar, "auth="):
                self.assertIn(LOGIN_FORM, call("GET", "/todo", cookie=cookie)[2])
                self.assertEqual(call("POST", "/todo/add", {"q": "x"}, cookie=cookie)[0], 401)

    def test_foreign_origin(self):
        jar, evil = self.jar(), {"origin": "https://evil.example"}
        self.assertEqual(call("POST", "/todo/add", {"q": "x"}, cookie=jar, **evil)[0], 403)
        self.assertEqual(self.login(**evil)[0], 403)
        self.assertEqual(call("POST", "/todo/add", {"q": "x", "next": "/todo"}, cookie=jar,
                              origin="http://127.0.0.1:8090")[0], 303)

    def test_next_stays_on_site(self):
        for target in ("//evil.example", "/\\evil.example", "https://evil.example"):
            self.assertEqual(self.login(next=target)[1]["location"], "/")
        self.assertEqual(self.login(next="/?date=2026-10-01")[1]["location"], "/?date=2026-10-01")


if __name__ == "__main__":
    unittest.main()
