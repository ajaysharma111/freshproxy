"""HTTP server: web UI, JSON API, and an OpenAI-compatible verifying proxy (POST /v1/chat/completions)."""
import json, sys, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import http as fphttp
from .bifrost import Gateway
from .config import Config
from .mock import MockGateway, MockSearch, SAMPLE
from .pipeline import Deps, verify
from .tavily import Search

UI = Path(__file__).resolve().parent.parent / "ui" / "index.html"


class App(BaseHTTPRequestHandler):
    cfg = None
    deps = None
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, code, obj, ctype="application/json", extra=None):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(b)

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
        return self.send(404, {"error": "not found"})

    def do_POST(self):
        p = self.path.split("?")[0]
        try:
            data = self.body()
        except Exception as e:  # noqa: BLE001
            return self.send(400, {"error": str(e)})
        try:
            if p == "/api/verify":
                ans = str(data.get("answer") or "").strip()
                if not ans:
                    return self.send(400, {"error": "Provide an 'answer' to verify."})
                return self.send(200, verify(ans, self.deps, self.cfg))
            if p == "/api/verify-stream":
                return self.stream_verify(str(data.get("answer") or "").strip())
            if p == "/api/ask":
                q = str(data.get("question") or "").strip()
                if not q:
                    return self.send(400, {"error": "Provide a 'question'."})
                ans = self.deps.gateway.chat([{"role": "user", "content": q}], max_tokens=700)
                r = verify(ans, self.deps, self.cfg)
                r["original_answer"] = ans
                return self.send(200, r)
            if p == "/v1/chat/completions":
                return self.proxy(data)
        except Exception as e:  # noqa: BLE001
            return self.send(502, {"error": str(e)[:300]})
        return self.send(404, {"error": "not found"})

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
            emit("done", verify(ans, self.deps, self.cfg, emit=emit))
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
            if self.headers.get("Authorization") and not h:
                h["Authorization"] = self.headers["Authorization"]
            resp = fphttp.post_json(self.cfg.bifrost_url + "/v1/chat/completions", data, h, timeout=90)
        text = ((resp.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        rep = verify(text, self.deps, self.cfg) if text else None
        resp["freshproxy"] = rep
        self.send(200, resp, extra={"X-FreshProxy-Score": str(rep["overall"]) if rep and rep["overall"] is not None else "n/a"})


def build(cfg):
    if cfg.mock:
        return Deps(MockGateway(), MockSearch())
    return Deps(Gateway(cfg), Search(cfg))


def main():
    cfg = Config()
    App.cfg, App.deps = cfg, build(cfg)
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
