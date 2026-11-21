"""HTTP server: web UI, JSON API, and an OpenAI-compatible verifying proxy (POST /v1/chat/completions)."""
import json, sys, threading
from http.cookies import SimpleCookie
from urllib.parse import urlparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import http as fphttp
from .auth import AuthError, Store
from .bifrost import Gateway
from .config import Config
from .mock import MockGateway, MockSearch, SAMPLE
from .pipeline import Deps, verify
from .tavily import Search

UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"


class App(BaseHTTPRequestHandler):
    cfg = None
    deps = None
    store = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json", extra=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "same-origin")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

    # ---- identity ----
    def ip(self):
        return (self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip()

    def session_token(self):
        try:
            c = SimpleCookie(self.headers.get("Cookie") or "")
            return c["fp_session"].value if "fp_session" in c else None
        except Exception:  # noqa: BLE001
            return None

    def bearer(self):
        a = self.headers.get("Authorization") or ""
        return a[7:].strip() if a.lower().startswith("bearer ") else ""

    def user(self):
        return self.store.user_by_session(self.session_token()) or self.store.user_by_key(self.bearer())

    def cookie(self, token, max_age):
        flags = "HttpOnly; SameSite=Lax; Path=/; Max-Age=%d" % max_age + ("; Secure" if self.cfg.secure_cookie else "")
        return {"Set-Cookie": "fp_session=%s; %s" % (token, flags)}

    def csrf_ok(self):
        ct = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ct != "application/json":
            return False
        o = self.headers.get("Origin")
        return not o or urlparse(o).netloc == self.headers.get("Host")

    def gate(self, user):
        """Anonymous visitors get a small hourly allowance; accounts are unlimited."""
        if user or self.store.throttle("anon:" + self.ip(), self.cfg.anon_per_hour, 3600):
            return True
        self.send(429, {"error": "Free limit reached (%d checks/hour). Sign up for unlimited checks." % self.cfg.anon_per_hour, "signup": True})
        return False

    def body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 2_000_000:
            raise ValueError("body too large")
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        p = self.path.split("?")[0]
        if p == "/":
            return self.send(200, UI.read_bytes(), "text/html; charset=utf-8")
        if p == "/api/health":
            c = self.cfg
            return self.send(200, {"ok": True, "mock": c.mock, "tavily_configured": bool(c.tavily_key), "bifrost_url": c.bifrost_url, "models": c.models, "sample": SAMPLE if c.mock else ""})
        if p == "/api/me":
            u = self.user()
            return self.send(200, {"user": u, "stats": self.store.stats(u["id"]) if u else None, "anon_per_hour": self.cfg.anon_per_hour})
        if p == "/api/history":
            u = self.user()
            return self.send(200, {"items": self.store.history(u["id"])}) if u else self.send(401, {"error": "Log in to see your history."})
        if p.startswith("/api/history/"):
            u = self.user()
            if not u:
                return self.send(401, {"error": "Log in to see your history."})
            try:
                r = self.store.get_check(u["id"], int(p.rsplit("/", 1)[1]))
            except ValueError:
                r = None
            return self.send(200, r) if r else self.send(404, {"error": "not found"})
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        self.u = None
        if p != "/v1/chat/completions" and not self.csrf_ok():
            return self.send(400, {"error": "Bad request (expected JSON from this site)."})
        try:
            data = self.body()
        except Exception as e:  # noqa: BLE001
            return self.send(400, {"error": str(e)})
        try:
            if p in ("/api/signup", "/api/login", "/api/logout", "/api/key", "/api/password", "/api/history/delete"):
                return self.account(p, data)
            if p.startswith("/api/") and p != "/api/health" or p == "/v1/chat/completions":
                u = self.user()
                if not self.gate(u):
                    return
                self.u = u
            if p == "/api/verify":
                ans = str(data.get("answer") or "").strip()
                if not ans:
                    return self.send(400, {"error": "Provide an 'answer' to verify."})
                return self.send(200, self.record(verify(ans, self.deps, self.cfg)))
            if p == "/api/verify-stream":
                return self.stream_verify(str(data.get("answer") or "").strip())
            if p == "/api/ask":
                q = str(data.get("question") or "").strip()
                if not q:
                    return self.send(400, {"error": "Provide a 'question'."})
                ans = self.deps.gateway.chat([{"role": "user", "content": q}], max_tokens=700)
                r = verify(ans, self.deps, self.cfg)
                r["original_answer"] = ans
                return self.send(200, self.record(r, "ask"))
            if p == "/v1/chat/completions":
                return self.proxy(data)
        except Exception as e:  # noqa: BLE001
            return self.send(502, {"error": str(e)[:300]})
        return self.send(404, {"error": "not found"})

    def record(self, report, source="web"):
        u = getattr(self, "u", None)
        if u:
            report["id"] = self.store.save_check(u["id"], report, source)
            report["saved"] = True
        return report

    def account(self, p, d):
        ip = self.ip()
        try:
            if p in ("/api/signup", "/api/login"):
                if not self.store.throttle("auth:" + ip, 10, 600):
                    return self.send(429, {"error": "Too many attempts. Try again in a few minutes."})
                uid = self.store.signup(d.get("email"), d.get("password"), d.get("name")) if p == "/api/signup" else self.store.login(d.get("email"), d.get("password"))
                tok = self.store.new_session(uid)
                u = self.store.user_by_session(tok)
                return self.send(200, {"user": u}, extra=self.cookie(tok, 14 * 86400))
            if p == "/api/logout":
                self.store.end_session(self.session_token())
                return self.send(200, {"ok": True}, extra=self.cookie("", 0))
            u = self.user()
            if not u:
                return self.send(401, {"error": "Log in first."})
            if p == "/api/key":
                return self.send(200, {"api_key": self.store.rotate_key(u["id"])})
            if p == "/api/password":
                self.store.change_password(u["id"], d.get("old"), d.get("new"))
                return self.send(200, {"ok": True}, extra=self.cookie("", 0))
            if p == "/api/history/delete":
                self.store.delete_check(u["id"], int(d.get("id") or 0))
                return self.send(200, {"ok": True})
        except AuthError as e:
            return self.send(e.code, {"error": str(e)})

    def stream_verify(self, ans):
        """Newline-delimited JSON events so the UI can show progress live."""
        if not ans:
            return self.send(400, {"error": "Provide an 'answer' to verify."})
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        lock = threading.Lock()

        def emit(ev, data):
            with lock:
                self.wfile.write((json.dumps({"event": ev, "data": data}) + "\n").encode())
                self.wfile.flush()
        try:
            emit("done", self.record(verify(ans, self.deps, self.cfg, emit=emit)))
        except Exception as e:  # noqa: BLE001
            emit("error", {"error": str(e)[:300]})

    def proxy(self, data):
        """Drop-in for any OpenAI-compatible client: forward to Bifrost, then attach a freshness report."""
        if data.get("stream"):
            return self.send(400, {"error": "FreshProxy v0.1 does not support streaming; send stream=false."})
        if self.cfg.mock:
            text = self.deps.gateway.chat(data.get("messages") or [])
            resp = {"id": "mock", "object": "chat.completion", "model": "mock/model", "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}]}
        else:
            h = {"Authorization": "Bearer " + self.cfg.bifrost_key} if self.cfg.bifrost_key else {}
            if self.headers.get("Authorization") and not h and not self.bearer().startswith("fp_"):
                h["Authorization"] = self.headers["Authorization"]
            resp = fphttp.post_json(self.cfg.bifrost_url + "/v1/chat/completions", data, h, timeout=90)
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rep = verify(text, self.deps, self.cfg) if text else None
        if rep:
            rep["original_answer"] = text
            self.record(rep, "proxy")
        resp["freshproxy"] = rep
        self.send(200, resp, extra={"X-FreshProxy-Score": str(rep["overall"]) if rep and rep["overall"] is not None else "n/a"})


def build(cfg):
    if cfg.mock:
        return Deps(MockGateway(), MockSearch())
    return Deps(Gateway(cfg), Search(cfg))


def main():
    cfg = Config()
    App.cfg, App.deps, App.store = cfg, build(cfg), Store(cfg.db_path)
    srv = ThreadingHTTPServer((cfg.host, cfg.port), App)
    mode = "MOCK demo data" if cfg.mock else "live (Tavily + Bifrost at %s)" % cfg.bifrost_url
    print("FreshProxy on http://%s:%d  mode: %s" % (cfg.host, cfg.port, mode), flush=True)
    if not cfg.mock and not cfg.tavily_key:
        print("WARNING: TAVILY_API_KEY is not set; claims will be reported as unverified.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
