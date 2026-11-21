"""Accounts, sessions, per-user history and API keys (SQLite + scrypt; stdlib only)."""
import hashlib, hmac, json, os, re, secrets, sqlite3, threading, time

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,190}\.[^@\s]{2,24}$")
SESSION_TTL = 14 * 86400


def _hash(pw, salt=None):
    salt = salt or secrets.token_bytes(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (salt.hex(), h.hex())


def _verify(pw, stored):
    try:
        _, salt, h = stored.split("$")
        cand = _hash(pw, bytes.fromhex(salt)).split("$")[2]
        return hmac.compare_digest(cand, h)
    except Exception:  # noqa: BLE001
        return False


def _tok(t):
    return hashlib.sha256(t.encode()).hexdigest()


class AuthError(Exception):
    def __init__(self, msg, code=400):
        super().__init__(msg)
        self.code = code


class Store:
    def __init__(self, path):
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        self.fails = {}  # key -> [timestamps]
        with self.lock:
            self.db.executescript("""
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, pw TEXT NOT NULL, api_key_hash TEXT, api_key_hint TEXT, created REAL);
            CREATE TABLE IF NOT EXISTS sessions(th TEXT PRIMARY KEY, user_id INTEGER, expires REAL);
            CREATE TABLE IF NOT EXISTS checks(id INTEGER PRIMARY KEY, user_id INTEGER, created REAL, source TEXT, overall REAL, n_claims INTEGER, n_changed INTEGER, preview TEXT, report TEXT);
            CREATE INDEX IF NOT EXISTS ck_user ON checks(user_id, created DESC);
            """)
            self.db.commit()

    def _q(self, sql, a=(), one=False, commit=False):
        with self.lock:
            cur = self.db.execute(sql, a)
            if commit:
                self.db.commit()
            return (cur.fetchone() if one else cur.fetchall()) if not commit else cur

    # rate limiting (login / signup / anonymous use)
    def throttle(self, key, limit, window):
        now = time.time()
        with self.lock:
            xs = [t for t in self.fails.get(key, []) if now - t < window]
            if len(xs) >= limit:
                self.fails[key] = xs
                return False
            xs.append(now)
            self.fails[key] = xs
            return True

    def signup(self, email, password, name=""):
        email = (email or "").strip().lower()
        if not EMAIL_RE.match(email):
            raise AuthError("Enter a valid email address.")
        if len(password or "") < 8:
            raise AuthError("Password must be at least 8 characters.")
        if len(password) > 200:
            raise AuthError("Password is too long.")
        try:
            cur = self._q("INSERT INTO users(email,name,pw,created) VALUES(?,?,?,?)", (email, (name or "").strip()[:60], _hash(password), time.time()), commit=True)
        except sqlite3.IntegrityError:
            raise AuthError("An account with this email already exists.", 409)
        return cur.lastrowid

    def login(self, email, password):
        row = self._q("SELECT * FROM users WHERE email=?", ((email or "").strip().lower(),), one=True)
        ok = _verify(password or "", row["pw"] if row else _hash("x"))
        if not row or not ok:
            raise AuthError("Wrong email or password.", 401)
        return row["id"]

    def new_session(self, uid):
        t = secrets.token_urlsafe(32)
        self._q("INSERT INTO sessions VALUES(?,?,?)", (_tok(t), uid, time.time() + SESSION_TTL), commit=True)
        self._q("DELETE FROM sessions WHERE expires<?", (time.time(),), commit=True)
        return t

    def end_session(self, t):
        if t:
            self._q("DELETE FROM sessions WHERE th=?", (_tok(t),), commit=True)

    def user_by_session(self, t):
        if not t:
            return None
        r = self._q("SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.th=? AND s.expires>?", (_tok(t), time.time()), one=True)
        return self.public(r)

    def user_by_key(self, key):
        if not key or not key.startswith("fp_"):
            return None
        return self.public(self._q("SELECT * FROM users WHERE api_key_hash=?", (_tok(key),), one=True))

    @staticmethod
    def public(r):
        return {"id": r["id"], "email": r["email"], "name": r["name"] or r["email"].split("@")[0], "key_hint": r["api_key_hint"]} if r else None

    def rotate_key(self, uid):
        k = "fp_" + secrets.token_urlsafe(24)
        self._q("UPDATE users SET api_key_hash=?, api_key_hint=? WHERE id=?", (_tok(k), k[:7] + "…" + k[-4:], uid), commit=True)
        return k  # shown once; only a hash is stored

    def change_password(self, uid, old, new):
        r = self._q("SELECT pw FROM users WHERE id=?", (uid,), one=True)
        if not r or not _verify(old or "", r["pw"]):
            raise AuthError("Current password is wrong.", 401)
        if len(new or "") < 8:
            raise AuthError("New password must be at least 8 characters.")
        self._q("UPDATE users SET pw=? WHERE id=?", (_hash(new), uid), commit=True)
        self._q("DELETE FROM sessions WHERE user_id=?", (uid,), commit=True)

    # history
    def save_check(self, uid, report, source="web"):
        cl = report.get("claims") or []
        prev = (report.get("original_answer") or "")[:140]
        cur = self._q("INSERT INTO checks(user_id,created,source,overall,n_claims,n_changed,preview,report) VALUES(?,?,?,?,?,?,?,?)",
                      (uid, time.time(), source, report.get("overall"), len(cl), sum(1 for c in cl if c.get("label") == "changed"), prev, json.dumps(report)), commit=True)
        return cur.lastrowid

    def history(self, uid, limit=50):
        rows = self._q("SELECT id,created,source,overall,n_claims,n_changed,preview FROM checks WHERE user_id=? ORDER BY created DESC LIMIT ?", (uid, limit))
        return [dict(r) for r in rows]

    def get_check(self, uid, cid):
        r = self._q("SELECT report FROM checks WHERE id=? AND user_id=?", (cid, uid), one=True)
        return json.loads(r["report"]) if r else None

    def delete_check(self, uid, cid):
        self._q("DELETE FROM checks WHERE id=? AND user_id=?", (cid, uid), commit=True)

    def stats(self, uid):
        r = self._q("SELECT COUNT(*) n, AVG(overall) avg, COALESCE(SUM(n_claims),0) claims, COALESCE(SUM(n_changed),0) changed FROM checks WHERE user_id=?", (uid,), one=True)
        return {"checks": r["n"], "avg_freshness": r["avg"], "claims": r["claims"], "stale_found": r["changed"]}
