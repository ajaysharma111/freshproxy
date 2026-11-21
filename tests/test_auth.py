import json, os, threading, unittest, urllib.request, urllib.error
from http.server import ThreadingHTTPServer

os.environ["FRESHPROXY_MOCK"] = "1"
from freshproxy.auth import AuthError, Store
from freshproxy.config import Config
from freshproxy.server import App, build


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.s = Store(":memory:")

    def test_signup_login(self):
        uid = self.s.signup("A@x.com", "password1", "Ann")
        self.assertEqual(self.s.login("a@x.com", "password1"), uid)
        with self.assertRaises(AuthError):
            self.s.login("a@x.com", "nope")
        with self.assertRaises(AuthError):
            self.s.signup("a@x.com", "password1")
        with self.assertRaises(AuthError):
            self.s.signup("bad", "password1")
        with self.assertRaises(AuthError):
            self.s.signup("b@x.com", "short")

    def test_password_not_stored_plain(self):
        self.s.signup("a@x.com", "password1")
        self.assertNotIn("password1", self.s._q("SELECT pw FROM users", one=True)["pw"])

    def test_session_and_key(self):
        uid = self.s.signup("a@x.com", "password1")
        t = self.s.new_session(uid)
        self.assertEqual(self.s.user_by_session(t)["id"], uid)
        self.s.end_session(t)
        self.assertIsNone(self.s.user_by_session(t))
        k = self.s.rotate_key(uid)
        self.assertEqual(self.s.user_by_key(k)["id"], uid)
        k2 = self.s.rotate_key(uid)
        self.assertIsNone(self.s.user_by_key(k))
        self.assertTrue(self.s.user_by_key(k2))

    def test_history_isolated(self):
        a = self.s.signup("a@x.com", "password1")
        b = self.s.signup("b@x.com", "password1")
        cid = self.s.save_check(a, {"overall": .5, "claims": [{"label": "changed"}], "original_answer": "x"})
        self.assertIsNone(self.s.get_check(b, cid))
        self.assertEqual(self.s.stats(a)["stale_found"], 1)
        self.assertEqual(len(self.s.history(b)), 0)

    def test_throttle(self):
        self.assertTrue(all(self.s.throttle("k", 3, 60) for _ in range(3)))
        self.assertFalse(self.s.throttle("k", 3, 60))


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cfg = Config()
        cfg.mock, cfg.anon_per_hour, cfg.db_path = True, 2, ":memory:"
        App.cfg, App.deps, App.store = cfg, build(cfg), Store(":memory:")
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), App)
        cls.base = "http://127.0.0.1:%d" % cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def call(self, path, body=None, cookie=None, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        if cookie:
            h["Cookie"] = cookie
        req = urllib.request.Request(self.base + path, json.dumps(body).encode() if body is not None else None, h)
        try:
            r = urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            r = e
        return r.status, json.loads(r.read() or b"{}"), r.headers

    def test_flow(self):
        st, j, h = self.call("/api/signup", {"email": "z@x.com", "password": "password1", "name": "Z"})
        self.assertEqual(st, 200)
        ck = h["Set-Cookie"].split(";")[0]
        self.assertIn("HttpOnly", h["Set-Cookie"])
        self.assertEqual(self.call("/api/me", cookie=ck)[1]["user"]["email"], "z@x.com")
        sample = self.call("/api/health")[1]["sample"]
        for _ in range(4):  # beyond the anonymous limit: accounts are unlimited
            st, r, _ = self.call("/api/verify", {"answer": sample}, cookie=ck)
            self.assertEqual(st, 200)
        self.assertTrue(r["saved"])
        self.assertEqual(len(self.call("/api/history", cookie=ck)[1]["items"]), 4)
        self.assertEqual(self.call("/api/history/%d" % r["id"], cookie=ck)[0], 200)
        key = self.call("/api/key", {}, cookie=ck)[1]["api_key"]
        st, r, _ = self.call("/v1/chat/completions", {"messages": [{"role": "user", "content": "hi"}]}, headers={"Authorization": "Bearer " + key})
        self.assertEqual(st, 200)
        self.assertEqual(self.call("/api/history", cookie=ck)[1]["items"][0]["source"], "proxy")
        self.call("/api/logout", {}, cookie=ck)
        self.assertEqual(self.call("/api/history", cookie=ck)[0], 401)

    def test_anon_limit_and_csrf(self):
        sample = self.call("/api/health")[1]["sample"]
        App.store.fails.clear()
        codes = [self.call("/api/verify", {"answer": sample})[0] for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])
        req = urllib.request.Request(self.base + "/api/login", b"email=a", {"Content-Type": "text/plain"})
        with self.assertRaises(urllib.error.HTTPError) as c:
            urllib.request.urlopen(req)
        self.assertEqual(c.exception.code, 400)

    def test_bad_login(self):
        self.assertEqual(self.call("/api/login", {"email": "no@x.com", "password": "x"})[0], 401)


if __name__ == "__main__":
    unittest.main()
