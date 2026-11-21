import os


def env(name, default=""):
    return os.environ.get(name, default)


class Config:
    """Read once per process; every value can be overridden by an environment variable."""

    def __init__(self):
        self.tavily_key = env("TAVILY_API_KEY")
        self.tavily_url = env("TAVILY_URL", "https://api.tavily.com/search")
        self.bifrost_url = env("BIFROST_URL", "http://localhost:8080").rstrip("/")
        self.bifrost_key = env("BIFROST_API_KEY")
        # Bifrost model IDs use provider/model. Gemini Flash is the default;
        # comma-separated alternatives are tried in order if the primary fails.
        model_list = env("BIFROST_MODELS", env("BIFROST_MODEL", "gemini/gemini-2.5-flash"))
        self.models = [m.strip() for m in model_list.split(",") if m.strip()]
        if not self.models:
            self.models = ["gemini/gemini-2.5-flash"]
        self.mock = env("FRESHPROXY_MOCK", "0").lower() in ("1", "true", "yes")
        self.port = int(env("PORT", env("FRESHPROXY_PORT", "8787")))
        self.host = env("HOST", "127.0.0.1")
        self.cache_ttl_s = int(env("FRESHPROXY_CACHE_TTL_S", str(6 * 3600)))
        self.max_claims = int(env("FRESHPROXY_MAX_CLAIMS", "8"))
        self.assumed_answer_age_days = int(env("FRESHPROXY_ASSUMED_ANSWER_AGE_DAYS", "365"))
        self.db_path = env("FRESHPROXY_DB", "data/freshproxy.db")
        self.anon_per_hour = int(env("FRESHPROXY_ANON_PER_HOUR", "5"))
        self.secure_cookie = env("FRESHPROXY_SECURE_COOKIE", "0").lower() in ("1", "true", "yes")
