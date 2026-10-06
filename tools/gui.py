#!/usr/bin/env python3
"""Web GUI for easy mode: dashboard, controls, setup & funding, volume — in the browser.

  python3 tools/gui.py [--host 0.0.0.0] [--port 8080]

It is reached through an SSH tunnel, never over the open internet:

  ssh -L 8080:127.0.0.1:8080 user@server        (on your own computer)
  http://localhost:8080/#t=<token>               (`hydra-mm gui` prints this link)

Security — this page controls real money:
  * every /api/ request needs the header X-Hydra-Token = state/gui_token (0600, created on
    first use), compared in constant time; the page reads it from the URL hash once, keeps
    it in sessionStorage and removes it from the address bar. No cookies.
  * the Host header must be localhost / 127.0.0.1 / [::1] (any port), for the page too:
    a DNS-rebinding site sends its own host name and gets 403.
  * no CORS headers at all: the custom header forces a preflight that no foreign page passes.
  * POST bodies are JSON only, at most 64 KB.
  * secrets typed into the page (Telegram bot token, invite code) reach hydra-mm through the
    subprocess environment only (never argv), are blanked out of its output and are never
    logged or sent back. The GUI never reads the wallet seed.
  * long or money actions (setup, fund, fund --go) run as jobs, one at a time;
    fund --go and a volume run need an explicit confirm.

Only the standard library (http.server) plus the bot's own modules.
"""
import argparse
import copy
import hmac
import importlib
import importlib.util
import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import yaml                                   # noqa: E402  (a bot dependency already)
from lib import easy_ops as E                 # noqa: E402
from lib import planner as PL                 # noqa: E402

# The command a job runs (tests point it at a harmless fake).
HYDRA_MM = [sys.executable, os.path.join(ROOT, "tools", "hydra_mm.py")]
MAX_BODY = 64 * 1024
MAX_OUTPUT = 256 * 1024                       # characters of job output kept (the tail)
MAX_JOBS = 20                                 # finished jobs kept in memory
TOKEN_HEADER = "X-Hydra-Token"
ALLOWED_HOSTS = ("localhost", "127.0.0.1", "[::1]")
SECRET_ENV_RX = re.compile(r"KEY|SECRET|TOKEN|PASS|MNEMONIC|SEED", re.I)    # .env values blanked from job output
VOLUME_PAUSE_MARK = "volume-mode"             # lib/volume.py writes this into the pause files it owns
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
SECURITY_HEADERS = (
    ("Cache-Control", "no-store"),
    ("X-Content-Type-Options", "nosniff"),
    ("X-Frame-Options", "DENY"),
    ("Referrer-Policy", "no-referrer"),
    ("Cross-Origin-Opener-Policy", "same-origin"),
    ("Cross-Origin-Resource-Policy", "same-origin"),
    ("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
)


# ------------------------------------------------------------------ access token
def gui_token(path: str = "state/gui_token") -> str:
    """The GUI access token; created (secrets.token_urlsafe(24), mode 0600) if missing."""
    for _ in range(100):
        try:
            with open(path) as f:
                tok = f.read().strip()
        except FileNotFoundError:
            tok = None
        if tok:
            try:
                if os.stat(path).st_mode & 0o077:
                    os.chmod(path, 0o600)
            except OSError:
                pass
            return tok
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        try:   # O_EXCL: of two processes creating it at once, one wins and the other reads it
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | (os.O_EXCL if tok is None else os.O_TRUNC), 0o600)
        except FileExistsError:
            time.sleep(0.01)
            continue
        os.fchmod(fd, 0o600)
        new = secrets.token_urlsafe(24)
        with os.fdopen(fd, "w") as f:
            f.write(new + "\n")
        return new
    raise RuntimeError(f"could not create {path}")


def host_ok(values) -> bool:
    """Exactly one Host header, naming localhost / 127.0.0.1 / [::1] with an optional port."""
    if not values or len(values) != 1:
        return False
    h = values[0].strip().lower()
    if h.startswith("["):
        end = h.find("]")
        if end < 0:
            return False
        name, rest = h[:end + 1], h[end + 1:]
    else:
        name, sep, port = h.partition(":")
        rest = sep + port
    return name in ALLOWED_HOSTS and (rest == "" or re.fullmatch(r":\d{1,5}", rest) is not None)


def origin_ok(origin: str) -> bool:
    try:
        u = urlsplit(origin)
    except ValueError:
        return False
    return u.scheme in ("http", "https") and host_ok([u.netloc])


# ------------------------------------------------------------------ helpers
class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


def _clean(x):
    """JSON-safe: NaN/inf -> None, tuples -> lists, unknown objects -> str."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple, set)):
        return [_clean(v) for v in x]
    if x is None or isinstance(x, (bool, int, str)):
        return x
    return str(x)


def _short(e: BaseException) -> str:
    return f"{type(e).__name__}: {str(e)[:160]}".rstrip(": ")


def _number(v, name: str, lo=None, hi=None, integer=False, lo_open=False):
    """A number from JSON (or a numeric string from a form), within bounds — else ApiError(400)."""
    if isinstance(v, bool) or v is None or (isinstance(v, str) and not v.strip()):
        raise ApiError(400, f"{name}: enter a number.")
    try:
        x = float(v.strip()) if isinstance(v, str) else float(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name}: “{str(v)[:20]}” is not a number (use a dot for decimals).")
    if not math.isfinite(x):
        raise ApiError(400, f"{name}: enter a normal number.")
    if integer:
        if x != int(x):
            raise ApiError(400, f"{name}: enter a whole number.")
        x = int(x)
    if lo is not None and (x <= lo if lo_open else x < lo):
        raise ApiError(400, f"{name}: must be {'more than' if lo_open else 'at least'} {lo:g}.")
    if hi is not None and x > hi:
        raise ApiError(400, f"{name}: must be at most {hi:g}.")
    return x


def _bool(v, name: str) -> bool:
    if isinstance(v, bool):
        return v
    raise ApiError(400, f"{name}: must be on or off (true/false).")


def _read_env(path: str) -> dict:
    """KEY=value pairs of an .env file (only to know what is set — values never leave this process)."""
    try:
        from dotenv import dotenv_values
        return {k: v for k, v in dotenv_values(path).items() if v}
    except Exception:
        out = {}
        try:
            for line in open(path).read().splitlines():
                k, sep, v = line.partition("=")
                if sep and k.strip() and not k.strip().startswith("#") and v.strip():
                    out[k.strip()] = v.strip()
        except OSError:
            pass
        return out


def _atomic_write(path: str, text: str):
    real = os.path.realpath(path)            # in the container config/ links into /app/data
    mode = os.stat(real).st_mode & 0o777 if os.path.exists(real) else 0o644
    tmp = f"{real}.gui-tmp-{os.getpid()}"
    with open(tmp, "w") as f:
        f.write(text)
    os.chmod(tmp, mode)
    os.replace(tmp, real)


def _backup(path: str) -> str:
    real = os.path.realpath(path)
    base = f"{real}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
    bak, i = base, 2
    while os.path.exists(bak):
        bak, i = f"{base}-{i}", i + 1
    shutil.copy2(real, bak)
    return bak


class Background:
    """A slow check (doctor, prices) refreshed in a background thread: get() answers at once
    with the last result and refreshes it when older than `ttl`; only the very first call (or
    force=True) waits — at most `wait` seconds."""

    def __init__(self, fn, ttl: float, wait: float = 12.0):
        self.fn, self.ttl, self.wait = fn, ttl, wait
        self.value, self.error, self.at, self.ok_at = None, None, 0.0, 0.0
        self.lock = threading.Lock()
        self.running = None                    # Event of the refresh in progress

    def _refresh(self, ev):
        try:
            val, err = self.fn(), None
        except BaseException as e:            # noqa: B902 — doctor may sys.exit; keep the thread alive
            val, err = None, _short(e)
        with self.lock:
            if err is None:
                self.value, self.ok_at = val, time.time()
            self.error, self.at, self.running = err, time.time(), None
        ev.set()

    def get(self, force: bool = False):
        with self.lock:
            stale = force or not self.at or time.time() - self.at > self.ttl
            ev = self.running
            if stale and ev is None:
                ev = self.running = threading.Event()
                threading.Thread(target=self._refresh, args=(ev,), daemon=True).start()
            first = not self.at
        if ev is not None and (force or first):
            ev.wait(self.wait)
        with self.lock:
            return self.value, self.error, self.at, self.running is not None


class Cache:
    """Tiny TTL cache for node reads (several tabs refreshing at once ask the node once)."""

    def __init__(self):
        self.d, self.lock, self.locks = {}, threading.Lock(), {}

    def get(self, key, ttl: float, fn):
        with self.lock:
            hit = self.d.get(key)
            if hit and time.time() - hit[0] < ttl:
                return hit[1]
            klock = self.locks.setdefault(key, threading.Lock())
        with klock:
            with self.lock:
                hit = self.d.get(key)
                if hit and time.time() - hit[0] < ttl:
                    return hit[1]
            val = fn()
            with self.lock:
                self.d[key] = (time.time(), val)
            return val

    def clear(self):
        with self.lock:
            self.d.clear()


# ------------------------------------------------------------------ doctor (tools/hydra_mm.py)
_HM, _HM_LOCK = None, threading.Lock()


def _hydra_mm():
    """tools/hydra_mm.py as a module, for doctor_steps(). Importing it chdirs to the repo root
    and loads .env into os.environ — both undone here, so this process never holds the secrets."""
    global _HM
    with _HM_LOCK:
        if _HM is None:
            before, cwd = dict(os.environ), os.getcwd()
            try:
                spec = importlib.util.spec_from_file_location("_hydra_mm_cli", os.path.join(ROOT, "tools", "hydra_mm.py"))
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
            finally:
                for k in set(os.environ) - set(before):
                    del os.environ[k]
                os.chdir(cwd)
            _HM = mod
    return _HM


def doctor_steps():
    """[{ok, what, hint}] — the same checks as `hydra-mm doctor`."""
    return [{"ok": bool(ok), "what": what, "hint": hint} for ok, what, hint in _hydra_mm().doctor_steps()]


def volume_module():
    """(lib.volume, None) or (None, why) — the Volume tab says "not available" without it."""
    try:
        return importlib.import_module("lib.volume"), None
    except Exception as e:
        return None, _short(e)


# ------------------------------------------------------------------ jobs
class Job:
    def __init__(self, kind: str, title: str, args, exclusive: bool):
        self.id = secrets.token_hex(8)
        self.kind, self.title, self.args, self.exclusive = kind, title, list(args), exclusive
        self.status, self.exit_code = "running", None
        self.started, self.ended = time.time(), None
        self.lines, self.size, self.trimmed = [], 0, False
        self.lock = threading.Lock()
        self.scrub = []                        # secret strings blanked out of the output

    def add(self, line: str):
        for s in self.scrub:
            line = line.replace(s, "••••••")
        with self.lock:
            self.lines.append(line)
            self.size += len(line)
            while self.size > MAX_OUTPUT and len(self.lines) > 1:
                self.size -= len(self.lines.pop(0))
                self.trimmed = True

    def public(self, output: bool = True) -> dict:
        with self.lock:
            d = {"id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
                 "exit_code": self.exit_code, "started": self.started, "ended": self.ended,
                 "command": "hydra-mm " + " ".join(self.args)}
            if output:
                d["output"] = ("… (earlier output trimmed)\n" if self.trimmed else "") + "".join(self.lines)
            return d


class Jobs:
    def __init__(self):
        self.jobs, self.lock = {}, threading.Lock()

    def running(self):
        with self.lock:
            return next((j for j in self.jobs.values() if j.status == "running"), None)

    def get(self, job_id: str):
        with self.lock:
            return self.jobs.get(job_id)

    def list(self):
        with self.lock:
            return [j.public(output=False) for j in sorted(self.jobs.values(), key=lambda j: -j.started)]

    def start(self, kind: str, title: str, args, env=None, scrub=(), exclusive: bool = True) -> Job:
        """Run `hydra-mm <args>` in a subprocess. Secrets only through `env`, blanked from the output."""
        with self.lock:
            busy = next((j for j in self.jobs.values() if j.status == "running" and (j.exclusive or exclusive)), None)
            if busy:
                raise ApiError(409, f"“{busy.title}” is still running — wait until it has finished.")
            job = Job(kind, title, args, exclusive)
            job.scrub = sorted({s for s in scrub if s and len(s) >= 6}, key=len, reverse=True)
            child_env = dict(os.environ)
            child_env.update({k: v for k, v in (env or {}).items() if v})
            child_env["PYTHONUNBUFFERED"] = "1"
            try:
                proc = subprocess.Popen(HYDRA_MM + list(args), cwd=ROOT, env=child_env, stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
            except OSError as e:
                raise ApiError(500, f"Could not start hydra-mm: {_short(e)}")
            finally:
                child_env = None              # noqa: F841 — drop our reference to the secrets
            self.jobs[job.id] = job
            done = [j for j in self.jobs.values() if j.status != "running"]
            for old in sorted(done, key=lambda j: j.started)[:max(0, len(done) - MAX_JOBS)]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._pump, args=(job, proc), daemon=True).start()
        return job

    @staticmethod
    def _pump(job: Job, proc):
        try:
            for raw in iter(proc.stdout.readline, b""):
                job.add(raw.decode("utf-8", "replace"))
        finally:
            code = proc.wait()
            with job.lock:
                job.exit_code, job.ended = code, time.time()
                job.status = "done" if code == 0 else "failed"


# ------------------------------------------------------------------ field definitions
MARKET_FIELDS = [
    {"key": "bid_size", "label": "Buy order size", "unit": "{base}", "kind": "size",
     "help": "How much each buy quote is for."},
    {"key": "ask_size", "label": "Sell order size", "unit": "{base}", "kind": "size",
     "help": "How much each sell quote is for."},
    {"key": "levels", "label": "Quotes per side", "unit": "", "kind": "int", "min": 1, "max": 12,
     "help": "How many buy and how many sell quotes sit on the order book at once."},
    {"key": "half_spread_pct", "label": "First quote's distance from the price", "unit": "%", "kind": "float",
     "min": 0.01, "max": 5, "help": "Wider earns more per trade, but fewer people trade with you."},
    {"key": "level_step_pct", "label": "Extra distance for each further quote", "unit": "%", "kind": "float",
     "min": 0, "max": 5, "help": "The gap between one quote and the next."},
    {"key": "max_position", "label": "Position limit", "unit": "{base}", "kind": "size",
     "help": "How much the bot may build up on one side before it shrinks the quotes that add to it."},
]
SIZE_JUMP = 10.0          # a size more than 10x the current one is almost always a typo

ALERT_FIELDS = [
    {"key": "lease_autorenew.enabled", "label": "Renew leases automatically", "kind": "bool", "default": True,
     "help": "Leases give you room to receive. Renewing keeps the quotes on both sides."},
    {"key": "lease_autorenew.max_fee_usd", "label": "Most to pay for one renewal", "unit": "USD", "kind": "float",
     "min": 0.5, "max": 1000, "default": 10.0, "help": "Above this the bot alerts you instead of renewing."},
    {"key": "lease_warn_hours", "label": "Warn when a lease ends within", "unit": "hours", "kind": "hours",
     "default": [24, 3], "help": "One or more numbers, e.g. 24, 3."},
    {"key": "digest_every", "label": "Send Telegram alerts at most every", "unit": "seconds", "kind": "int",
     "min": 0, "max": 86400, "default": 300, "help": "Alerts in between are bundled. 0 sends each one right away."},
    {"key": "daily_report_hour", "label": "Daily report at hour", "unit": "server time, 0–23", "kind": "int",
     "min": 0, "max": 23, "default": 20, "help": "The P&L report once a day."},
    {"key": "fill_alert_min_usd", "label": "Sum up trades smaller than", "unit": "USD", "kind": "float",
     "min": 0, "max": 100000, "default": 5.0, "help": "Smaller trades come as one line per market."},
]
ALERTS_NOTE = ("Saved. The alerts service reads these when the bot container starts: "
               "run `docker compose restart bot` on the server to apply them.")

PRESET_TEXT = {
    "conservative": "Wider spreads and fewer quotes. Fewer trades, less risk, no arbitrage.",
    "balanced": "The middle ground, and the right start for most people.",
    "aggressive": "Tight spreads and more quotes. More trades, and more risk.",
}
MARKET_TEXT = {
    "BTC/USDC.arb": "Bitcoin against USDC on Arbitrum.",
    "ETH/USDC.arb": "Ether against USDC on Arbitrum.",
    "ETH/BTC": "Ether against Bitcoin.",
    "USDC.arb/USDC.eth": "USDC on Arbitrum against USDC on Ethereum. The calmest market: both sides are dollars.",
}
FEE_HOW = {"dual": "taken from the deposit", "offchain": "paid from your USDC.arb channel",
           "sponsored": "for the deposit, no gas needed; the lease follows in a second round"}
TELEGRAM_TOKEN_RX = re.compile(r"^\d{5,16}:[A-Za-z0-9_-]{20,64}$")
INVITE_RX = re.compile(r"^[\x21-\x7e]{4,256}$")          # printable, no spaces or line breaks
SETUP_STAGES = ("node", "backup", "plan", "telegram", "funding", "done")
STALE_AFTER = 300                  # s: how long a failing check may keep showing the last good result


def step_key(step: dict) -> str:
    """Which part of the setup a `hydra-mm doctor` step is about (by its command hint)."""
    hint, what = (step.get("hint") or "").strip(), (step.get("what") or "").lower()
    if what.startswith("telegram"):
        return "telegram"
    for prefix, key in (("docker compose up", "node"), ("hydra-mm invite", "access"), ("hydra-mm peers", "hub"),
                        ("hydra-mm setup", "plan"), ("hydra-mm fund", "funding"), ("hydra-mm resume", "quotes")):
        if hint.startswith(prefix):
            return key
    if "quotes" in what or "paused" in what:
        return "quotes"
    return "other"


def _get_path(doc: dict, dotted: str, default=None):
    cur = doc
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _set_path(doc: dict, dotted: str, value):
    parts = dotted.split(".")
    cur = doc
    for part in parts[:-1]:
        if not isinstance(cur.get(part), dict):
            cur[part] = {}
        cur = cur[part]
    cur[parts[-1]] = value


# ------------------------------------------------------------------ the API
class Paths:
    """Where the GUI reads and writes. Production: the repo root (in the container /app, where
    config/ and state/ link into /app/data). Tests: a temporary directory."""

    def __init__(self, root: str = ROOT, static: str = None):
        self.root = root
        self.bot_config = os.path.join(root, "config", "bot_config.yaml")
        self.ops_config = os.path.join(root, "config", "ops.yaml")
        self.state = os.path.join(root, "state")
        self.env = os.path.join(root, ".env")
        self.token = os.path.join(root, "state", "gui_token")
        self.plan = os.path.join(root, "state", "easy_plan.json")
        self.pair = os.path.join(root, "state", "tg_pair.json")
        self.setup_acks = os.path.join(root, "state", "gui_setup.json")
        self.static = static or os.path.join(ROOT, "gui")


class Api:
    def __init__(self, paths: Paths):
        self.paths = paths
        self.jobs = Jobs()
        self.cache = Cache()
        self.cfg_lock = threading.Lock()
        self._node, self._node_lock = None, threading.Lock()
        self.doctor = Background(lambda: doctor_steps(), ttl=60)      # it fetches prices and checks peers
        self.prices = Background(lambda: E.prices(), ttl=60, wait=8)
        self.GET = {
            "/api/overview": self.get_overview, "/api/markets": self.get_markets,
            "/api/capacity": self.get_capacity, "/api/leases": self.get_leases, "/api/wallet": self.get_wallet,
            "/api/fills": self.get_fills, "/api/book": self.get_book, "/api/addresses": self.get_addresses,
            "/api/funding": self.get_funding, "/api/settings/markets": self.get_market_settings,
            "/api/settings/alerts": self.get_alert_settings, "/api/telegram": self.get_telegram,
            "/api/volume": self.get_volume, "/api/setup/info": self.get_setup_info, "/api/jobs": self.get_jobs,
            "/api/setup/state": self.get_setup_state, "/api/funding/fees": self.get_funding_fees,
        }
        self.POST = {
            "/api/pause": lambda b: self.post_pause(b, True), "/api/resume": lambda b: self.post_pause(b, False),
            "/api/settings/markets": self.post_market_settings, "/api/settings/alerts": self.post_alert_settings,
            "/api/plan": self.post_plan, "/api/setup": self.post_setup, "/api/fund": self.post_fund,
            "/api/fund/go": self.post_fund_go, "/api/telegram": self.post_telegram,
            "/api/volume/config": self.post_volume_config, "/api/volume/start": self.post_volume_start,
            "/api/volume/stop": self.post_volume_stop,
            "/api/invite": self.post_invite, "/api/setup/ack": self.post_setup_ack,
        }

    # -------------------------------------------------- plumbing
    def dispatch(self, method: str, path: str, query: dict, body):
        if method == "GET" and path.startswith("/api/jobs/"):
            return self.get_job(path[len("/api/jobs/"):])
        table = self.GET if method == "GET" else self.POST
        fn = table.get(path)
        if fn is None:
            if path in self.GET or path in self.POST or path.startswith("/api/jobs/"):
                raise ApiError(405, f"{method} is not allowed here.")
            raise ApiError(404, "No such API endpoint.")
        return fn(query) if method == "GET" else fn(body)

    def node(self):
        with self._node_lock:
            if self._node is None:
                self._node = E.connect()
            return self._node

    def node_read(self, what: str, key, ttl: float, fn):
        try:
            return self.cache.get(key, ttl, fn)
        except ApiError:
            raise
        except Exception as e:
            raise ApiError(502, f"Couldn't read {what} from the node — is it running? ({_short(e)})")

    def secrets_in_env(self) -> list:
        env = _read_env(self.paths.env)
        return [v for k, v in env.items() if v and SECRET_ENV_RX.search(k)]

    def price_map(self, required: bool = False):
        px, err, _, _ = self.prices.get()
        if not px and required:
            raise ApiError(503, "Can't get the BTC and ETH prices right now"
                                + (f" ({err})" if err else "") + ". Try again in a minute.")
        return px

    def entries(self) -> list:
        try:
            doc = yaml.safe_load(open(self.paths.bot_config)) or {}
        except FileNotFoundError:
            return []
        except yaml.YAMLError as e:
            raise ApiError(500, f"config/bot_config.yaml can't be read: {_short(e)}")
        return [e for e in (doc.get("strategies") or []) if isinstance(e, dict) and e.get("name")]

    def pause_state(self, name: str):
        """None | 'all' | 'market' | 'volume' (held by a volume run)."""
        if os.path.exists(os.path.join(self.paths.state, "pause")):
            return "all"
        p = os.path.join(self.paths.state, f"pause_{name}")
        if os.path.exists(p):
            return "volume" if self._volume_held(p) else "market"
        return None

    @staticmethod
    def _volume_held(path: str) -> bool:
        try:
            return open(path).read(64).startswith(VOLUME_PAUSE_MARK)
        except OSError:
            return False

    # -------------------------------------------------- dashboard
    # -------------------------------------------------- setup state (shared by overview and the stepper)
    def configured(self) -> bool:
        try:
            return os.path.getsize(os.path.realpath(self.paths.bot_config)) > 0 and os.path.exists(self.paths.plan)
        except OSError:
            return False

    def acks(self) -> dict:
        try:
            d = json.load(open(self.paths.setup_acks))
        except (OSError, ValueError):
            d = {}
        return {"backup": d.get("backup") if isinstance(d.get("backup"), dict) else None,
                "telegram_skipped": d.get("telegram_skipped") is True}

    def telegram_state(self) -> dict:
        env = _read_env(self.paths.env)
        tok, chat = bool(env.get("ALERTS_TELEGRAM_BOT_TOKEN")), bool(env.get("ALERTS_TELEGRAM_CHAT_ID"))
        code = E.pairing_code(self.paths.pair) if tok and not chat else None
        return {"token_set": tok, "chat_set": chat, "waiting_code": code,
                "state": "connected" if tok and chat else ("waiting" if tok and code else ("unpaired" if tok else "off"))}

    def setup_summary(self, force: bool = False) -> dict:
        """Where the setup stands, derived live from `hydra-mm doctor` plus the GUI's own
        acknowledgements (backup, Telegram skipped). Drives the stepper, the banner and home."""
        raw, err, at, refreshing = self.doctor.get(force=force)
        if err and raw is not None and time.time() - self.doctor.ok_at > STALE_AFTER:
            raw = None                    # a hiccup keeps the last good result; a lasting failure shows
        pending = raw is None and not err
        steps = [dict(s, key=step_key(s)) for s in (raw or [])]
        if err and not steps:
            steps = [{"ok": False, "key": "error", "what": f"the checks could not run ({err})",
                      "hint": "docker compose ps   (is the node running?), then check again"}]
        by = {}
        for s in steps:
            by.setdefault(s["key"], s)
        ok = lambda k: k in by and by[k]["ok"]           # noqa: E731
        bad = next((s for s in steps if not s["ok"]), None)
        paused = os.path.exists(os.path.join(self.paths.state, "pause"))
        configured, acks, tg = self.configured(), self.acks(), self.telegram_state()
        if pending:
            node = "checking"
        elif "error" in by:
            node = "error"
        elif not ok("node"):
            node = "offline"
        elif "access" in by and not by["access"]["ok"]:
            node = "invite" if "invite" in by["access"]["what"].lower() and "waiting" in by["access"]["what"].lower() \
                else "not_admitted"
        elif "hub" in by and not by["hub"]["ok"]:
            node = "hub"
        elif ok("access") and ok("hub"):
            node = "ok"
        else:
            node = "error"
        quotes_ok = ok("quotes") or ("quotes" in by and paused)
        complete = node == "ok" and configured and ok("funding") and quotes_ok
        starting = ok("funding") and "quotes" in by and not by["quotes"]["ok"] and not paused
        stages = {
            "node": "done" if node == "ok" else ("checking" if node == "checking" else "action"),
            "backup": "done" if acks["backup"] else "todo",
            "plan": "done" if configured else "todo",
            "telegram": "done" if tg["state"] == "connected" else
                        ("skipped" if acks["telegram_skipped"] else ("waiting" if tg["state"] == "waiting" else "optional")),
            "funding": "done" if ok("funding") else ("action" if "funding" in by else "todo"),
            "done": "done" if complete else ("starting" if starting else "todo"),
        }
        current = "done" if complete else next(k for k in SETUP_STAGES if stages[k] not in ("done", "skipped"))
        return {
            "pending": pending, "error": err, "checked_at": at or None, "refreshing": refreshing,
            "steps": steps, "next": bad["hint"] if bad else None, "all_good": bool(steps) and not bad and not pending,
            "complete": complete, "current": current, "stages": stages, "node": node, "paused": paused,
            "configured": configured, "acks": acks, "telegram": tg,
            "progress": {"done": sum(1 for v in stages.values() if v in ("done", "skipped")), "total": len(stages)},
            "attention": self.attention(steps, bad, pending, node, paused, complete, acks),
        }

    @staticmethod
    def attention(steps, bad, pending, node, paused, complete, acks):
        """The one thing that needs the user now (the top banner), or None."""
        if pending:
            return None
        if bad:
            k, what = bad["key"], bad["what"]
            if k == "quotes" and paused:
                return {"level": "warn", "key": "paused", "text": "Paused: none of your quotes are on the order book.",
                        "action": "resume", "label": "Resume quoting"}
            table = {
                "error": ("bad", f"The health checks couldn't run ({what.split('(', 1)[-1].rstrip(')')}).", "recheck",
                          "Check again", "node"),
                "node": ("bad", "Your node isn't answering. It may still be starting up.", "setup", "Open setup", "node"),
                "access": ("warn", "Your node is waiting for a mainnet invite." if node == "invite" else
                           "Your node isn't admitted to mainnet yet.", "setup", "Enter invite code", "node"),
                "hub": ("warn", "The Hydranet hub isn't reachable on every chain yet.", "recheck", "Check again", "node"),
                "plan": ("info", "Choose your plan to get started: budget, style and markets.", "setup", "Choose a plan", "plan"),
                "funding": ("warn", f"Fund your node: {what}.", "setup", "Fund your node", "funding"),
                "quotes": ("info", "Waiting for the first quotes on the order book. This takes about a minute.",
                           "recheck", "Check again", "done"),
            }
            level, text, action, label, stage = table.get(k, ("warn", what[:1].upper() + what[1:] + ".", "setup",
                                                              "Open setup", "node"))
            return {"level": level, "key": k, "text": text, "action": action, "label": label, "stage": stage}
        if complete and not acks["backup"]:
            return {"level": "info", "key": "backup", "text": "Back up your wallet's recovery phrase: it is the only "
                    "way to recover your funds.", "action": "setup", "label": "Back it up", "stage": "backup"}
        return None

    def get_setup_state(self, q):
        force = (q.get("fresh") or [""])[0] in ("1", "true")
        st = self.setup_summary(force)
        st["identity"] = None
        if st["node"] not in ("offline", "checking", "error"):
            try:
                st["identity"] = self.cache.get("identity", 600, lambda: E.identity_key())
            except Exception:
                pass
        st["plan"] = self.saved_plan()
        job = self.jobs.running()
        st["job"] = job.public(output=False) if job else None
        return 200, st

    def post_setup_ack(self, body):
        item = body.get("item")
        if item not in ("backup", "telegram_skipped"):
            raise ApiError(400, "item must be backup or telegram_skipped.")
        value = _bool(body.get("value", True), "value")
        with self.cfg_lock:
            try:
                d = json.load(open(self.paths.setup_acks))
                d = d if isinstance(d, dict) else {}
            except (OSError, ValueError):
                d = {}
            if item == "backup":
                d["backup"] = {"at": time.time()} if value else None
            else:
                d["telegram_skipped"] = value
            os.makedirs(self.paths.state, exist_ok=True)
            _atomic_write(self.paths.setup_acks, json.dumps(d, indent=1) + "\n")
        return 200, {"ok": True, "acks": self.acks()}

    def post_invite(self, body):
        code = body.get("code")
        if not isinstance(code, str) or not INVITE_RX.match(code.strip()):
            raise ApiError(400, "Paste the invite code exactly as you received it (no spaces).")
        code = code.strip()
        job = self.jobs.start("invite", "Redeem the invite", ["invite", "-"], env={"HYDRA_INVITE_CODE": code},
                              scrub=[code] + self.secrets_in_env())
        self.cache.clear()
        return 202, {"job": job.public()}

    # -------------------------------------------------- dashboard
    def get_overview(self, q):
        force = (q.get("fresh") or [""])[0] in ("1", "true")
        st = self.setup_summary(force)
        doctor = {k: st[k] for k in ("steps", "checked_at", "refreshing", "error", "pending", "next", "all_good")}
        px = self.price_map()
        ms = E.market_status(root=self.paths.root) or []
        by_quote, total_usd = {}, 0.0
        usd = {"USDC.arb": 1.0, "USDC.eth": 1.0, **({"BTC": px["BTC"], "ETH": px["ETH"]} if px else {})}
        for s in ms:
            quote = s["pair"].split("/")[1]
            by_quote[quote] = by_quote.get(quote, 0.0) + s["pnl"]
            if quote in usd:
                total_usd += s["pnl"] * usd[quote]
        paused_all = os.path.exists(os.path.join(self.paths.state, "pause"))
        quoting = sum(1 for s in ms if not s.get("paused") and not paused_all and (s["bids"] + s["asks"]) > 0)
        job = self.jobs.running()
        return 200, {"doctor": doctor, "prices": px, "prices_error": None if px else self.prices.error,
                     "totals": {"pnl_usd": round(total_usd, 6), "pnl_by_quote": by_quote, "markets": len(ms),
                                "quoting": quoting, "fills": sum(s["fills"] for s in ms)},
                     "paused_all": paused_all, "configured": st["configured"],
                     "setup": {k: st[k] for k in ("complete", "current", "node", "attention", "progress", "stages")},
                     "job": job.public(output=False) if job else None}

    def get_markets(self, q):
        entries = self.entries()
        status = {s["name"]: s for s in (E.market_status(root=self.paths.root) or [])}
        pairs = list(dict.fromkeys([e.get("pair") for e in entries if e.get("pair")] +
                                   [s["pair"] for s in status.values()]))
        books, node_error = {}, None

        def read_books():
            n = self.node()
            known = E.dex_markets(n)
            out = {}
            for pair in pairs:
                if pair in known:
                    bk = E.book(n, pair, known[pair])
                    out[pair] = {"best_bid": bk["bids"][0][0] if bk["bids"] else None,
                                 "best_ask": bk["asks"][0][0] if bk["asks"] else None,
                                 "our_bids": sum(1 for x in bk["bids"] if x[2]),
                                 "our_asks": sum(1 for x in bk["asks"] if x[2])}
            return out
        try:
            books = self.node_read("the order book", ("books", tuple(pairs)), 5, read_books)
        except ApiError as e:
            node_error = e.message
        rows, names = [], [e["name"] for e in entries]
        names += [n for n in status if n not in names]
        by_name = {e["name"]: e for e in entries}
        for name in names:
            e, s = by_name.get(name, {}), status.get(name, {})
            pair = e.get("pair") or s.get("pair") or "?/?"
            base, _, quote = pair.partition("/")
            bk = books.get(pair, {})
            rows.append({"name": name, "pair": pair, "base": base, "quote": quote,
                         "enabled": e.get("enabled", True) if e else True, "configured": bool(e),
                         "fair": s.get("fair"), "bids": s.get("bids"),
                         "asks": s.get("asks"), "position": s.get("position"), "position_pct": s.get("pct"),
                         "fills": s.get("fills"), "pnl": s.get("pnl"), "status_at": s.get("at"),
                         "best_bid": bk.get("best_bid"), "best_ask": bk.get("best_ask"),
                         "our_bids": bk.get("our_bids"), "our_asks": bk.get("our_asks"),
                         "paused": self.pause_state(name)})
        return 200, {"markets": rows, "paused_all": os.path.exists(os.path.join(self.paths.state, "pause")),
                     "node_error": node_error}

    def get_capacity(self, q):
        cap = self.node_read("the channel capacity", "capacity", 5, lambda: E.capacity(self.node()))
        try:
            need = (yaml.safe_load(open(self.paths.ops_config)) or {}).get("capacity_need") or {}
        except (OSError, yaml.YAMLError):
            need = {}
        order = list(PL.CHAIN) + sorted(set(cap) - set(PL.CHAIN))
        return 200, {"assets": [{"asset": a, "chain": PL.CHAIN.get(a), **cap[a],
                                 "need_send": (need.get(a) or {}).get("send"), "need_recv": (need.get(a) or {}).get("recv")}
                                for a in order if a in cap]}

    def get_leases(self, q):
        ls = self.node_read("the leases", "leases", 30, lambda: E.leases())
        return 200, {"leases": ls, "now": time.time()}

    def get_wallet(self, q):
        def read():
            n = self.node()
            return {"onchain": E.wallet_onchain(n), "gas": E.native_balances(n)}
        return 200, self.node_read("the wallet", "wallet", 15, read)

    def get_fills(self, q):
        try:
            n = int((q.get("n") or ["20"])[0])
        except ValueError:
            raise ApiError(400, "n must be a whole number.")
        n = max(1, min(n, 100))
        return 200, {"fills": list(reversed(E.recent_fills(n, root=self.paths.root) or []))}

    def get_book(self, q):
        pair = (q.get("pair") or [""])[0]
        n = self.node()
        known = self.node_read("the markets", "dex_markets", 60, lambda: E.dex_markets(n))
        if not pair:
            pair = next((e.get("pair") for e in self.entries() if e.get("pair") in known), None) or next(iter(known), "")
        if pair not in known:
            raise ApiError(400, f"Unknown market {pair[:40]!r}. Markets: {', '.join(known)}")
        bk = self.node_read("the order book", ("book", pair), 3, lambda: E.book(n, pair, known[pair]))
        return 200, {"pair": pair, "markets": list(known), "bids": [list(x) for x in bk["bids"][:15]],
                     "asks": [list(x) for x in bk["asks"][:15]]}

    def get_addresses(self, q):
        addrs = self.node_read("the deposit addresses", "addresses", 300, lambda: E.deposit_addresses(self.node()))
        return 200, {"addresses": [{"chain": ch, "address": ad,
                                    "assets": [a for a, c in PL.CHAIN.items() if c == ch]} for ch, ad in addrs.items()]}

    def saved_plan(self):
        try:
            return json.load(open(self.paths.plan))
        except (OSError, ValueError):
            return None

    def get_funding(self, q):
        d = self.saved_plan()
        if not d:
            return 200, {"configured": False}
        px = self.price_map(required=True)
        p = PL.plan(d["budget"], d["preset"], d["markets"], px)

        def read():
            n = self.node()
            return E.wallet_onchain(n), E.capacity(n), E.deposit_addresses(n), E.native_balances(n)
        wallet, cap, addrs, gas = self.node_read("the wallet and channels", "funding", 10, read)
        gaps = E.funding_gaps(p, wallet, cap)
        funded, have_usd, plan_usd, empty = E.funding_state(p, cap, px)
        usd = {"USDC.arb": 1.0, "USDC.eth": 1.0, "BTC": px["BTC"], "ETH": px["ETH"]}
        chains = []
        for chain in ("bitcoin", "ethereum", "arbitrum"):
            items = []
            for a, v in p.assets.items():
                if PL.CHAIN[a] != chain:
                    continue
                in_ch, in_w, missing = (cap.get(a) or {}).get("send", 0.0), wallet.get(a, 0.0), gaps.get(a, 0.0)
                state = "in_channels" if in_ch >= v["own"] * 0.9 else ("arrived" if missing <= 1e-9 else "waiting")
                items.append({"asset": a, "planned": v["own"], "planned_usd": v["own_usd"], "in_wallet": in_w,
                              "in_channels": in_ch, "missing": missing, "missing_usd": round(missing * usd.get(a, 0), 2),
                              "state": state})
            if items:
                states = {i["state"] for i in items}
                chains.append({"chain": chain, "address": addrs.get(chain), "items": items,
                               "gas": gas.get(chain) if chain in ("ethereum", "arbitrum") else None,
                               "state": "waiting" if "waiting" in states else ("arrived" if "arrived" in states else "in_channels")})
        notes = []
        if "ETH" in p.assets:
            notes.append("Also keep about 0.003 ETH on Ethereum in the node wallet: ETH deposits pay gas.")
        if any(a.startswith("USDC") for a in p.assets):
            notes.append("USDC needs no ETH: without gas the hub deposits it with a signed permit "
                         "(a little ETH on that chain makes it one fee instead of two).")
        return 200, {
            "configured": True,
            "plan": {"budget": d["budget"], "preset": d["preset"], "markets": d["markets"]},
            "send": [{"asset": a, "amount": amt, "usd": round(amt * usd.get(a, 0), 2), "chain": PL.CHAIN[a],
                      "address": addrs.get(PL.CHAIN[a])} for a, amt in gaps.items()],
            "assets": [{"asset": a, "chain": PL.CHAIN[a], "planned": v["own"], "in_channels": (cap.get(a) or {}).get("send", 0.0),
                        "in_wallet": wallet.get(a, 0.0), "inbound": v["inbound"]} for a, v in p.assets.items()],
            "chains": chains, "waiting": sum(1 for c in chains for i in c["items"] if i["state"] == "waiting"),
            "all_arrived": not gaps, "checked_at": time.time(),
            "funded": funded, "have_usd": have_usd, "plan_usd": plan_usd, "empty": empty, "notes": notes,
            "ready_to_open": not gaps and not funded}

    def get_funding_fees(self, q):
        """What `hydra-mm fund --go` would request per chain and what the liquidity service charges
        (read-only estimates, like `hydra-mm fund`)."""
        d = self.saved_plan()
        if not d:
            raise ApiError(409, "Save a plan first.")
        px = self.price_map(required=True)
        p = PL.plan(d["budget"], d["preset"], d["markets"], px)

        def read():
            n = self.node()
            reqs = E.channel_requests(p, px, E.capacity(n), lease_hours=168, gas=E.native_balances(n))
            out = []
            for r in reqs:
                fee, err = E.estimate_channel(r)
                mode, pay = E.fee_mode(r)
                if err and E.allowance_needed(err):
                    err = None                     # the approval is part of --go; the fee is known after it
                out.append({"chain": r["chain"], "hours": r["hours"], "fee": fee, "fee_asset": pay, "mode": mode,
                            "how": FEE_HOW.get(mode, ""), "error": err,
                            "assets": [{"asset": a, "deposit": v["own"], "lease": v["inbound"]} for a, v in r["assets"].items()]})
            return out
        reqs = self.node_read("the fee estimate", "fees", 60, read)
        return 200, {"requests": reqs, "nothing_to_do": not reqs, "lease_hours": 168}

    # -------------------------------------------------- controls
    def post_pause(self, body, on: bool):
        market = body.get("market") or None
        names = [e["name"] for e in self.entries()]
        if market is not None and (not isinstance(market, str) or market not in names):
            raise ApiError(400, f"Unknown market {str(market)[:40]!r}. Markets: {', '.join(names) or 'none yet'}")
        kept = []
        if on:
            E.set_pause(True, market, root=self.paths.root)
        else:
            # A pause file written by a volume run stays while that run is active (its market
            # maker must not quote against the self-matched rounds).
            held = [f for f in os.listdir(self.paths.state) if f.startswith("pause_")
                    and self._volume_held(os.path.join(self.paths.state, f))] if os.path.isdir(self.paths.state) else []
            if market and f"pause_{market}" in held:
                raise ApiError(409, f"{market} is paused by the running volume test — stop that run first "
                                    "(Volume tab); the market resumes on its own when it ends.")
            if market is None and held:
                for f in os.listdir(self.paths.state):
                    if (f == "pause" or f.startswith("pause_")) and f not in held:
                        os.remove(os.path.join(self.paths.state, f))
                kept = [f[len("pause_"):] for f in held]
            else:
                E.set_pause(False, market, root=self.paths.root)
        self.cache.clear()
        what = market or "all markets"
        msg = (f"Paused {what}. Quotes come off the book within seconds." if on else
               f"Resumed {what}. Quotes return within seconds.")
        if kept:
            msg += f" Still paused by the volume test: {', '.join(kept)}."
        return 200, {"ok": True, "message": msg, "kept_paused": kept}

    def get_market_settings(self, q):
        rows = []
        for e in self.entries():
            p = e.get("params") or {}
            base = (e.get("pair") or "?/?").split("/")[0]
            vals = {f["key"]: p.get(f["key"]) for f in MARKET_FIELDS}
            for k in ("bid_size", "ask_size"):
                if vals[k] is None:
                    vals[k] = p.get("level_size")
            rows.append({"name": e["name"], "pair": e.get("pair"), "base": base, "enabled": e.get("enabled", True),
                         "paused": self.pause_state(e["name"]), "values": vals})
        return 200, {"markets": rows, "fields": MARKET_FIELDS, "configured": os.path.exists(self.paths.bot_config)}

    def post_market_settings(self, body):
        from strategies.market_maker import MarketMakerStrategy as MM
        name, changes = body.get("name"), body.get("changes")
        if not isinstance(name, str) or not name:
            raise ApiError(400, "Pick a market.")
        if not isinstance(changes, dict) or not changes:
            raise ApiError(400, "Nothing to change.")
        fields = {f["key"]: f for f in MARKET_FIELDS}
        unknown = [k for k in changes if k not in fields and k != "enabled"]
        if unknown:
            raise ApiError(400, f"These settings can't be changed here: {', '.join(map(str, unknown))}.")
        with self.cfg_lock:
            if not os.path.exists(self.paths.bot_config):
                raise ApiError(409, "There is no configuration yet — save a setup first (Setup & funding).")
            text = open(self.paths.bot_config).read()
            doc = yaml.safe_load(text) or {}
            entries = doc.get("strategies") or []
            entry = next((e for e in entries if isinstance(e, dict) and e.get("name") == name), None)
            if entry is None:
                raise ApiError(404, f"No market called {name[:40]!r} in the configuration.")
            new = copy.deepcopy(entry)
            params = new.setdefault("params", {})
            base = (new.get("pair") or "?/?").split("/")[0]
            for k, v in changes.items():
                if k == "enabled":
                    new["enabled"] = _bool(v, "Market on")
                    continue
                f = fields[k]
                label = f"{f['label']}"
                if f["kind"] == "size":
                    x = _number(v, label, lo=0, lo_open=True)
                    cur = params.get(k) or (params.get("level_size") if k in ("bid_size", "ask_size") else None)
                    if isinstance(cur, (int, float)) and cur > 0 and x > cur * SIZE_JUMP:
                        raise ApiError(400, f"{label}: {x:g} {base} is more than {SIZE_JUMP:g}× the current {cur:g} — "
                                            "to avoid typos, change it in smaller steps.")
                else:
                    x = _number(v, label, lo=f.get("min"), hi=f.get("max"), integer=f["kind"] == "int")
                params[k] = x
            try:
                MM.build_config(name, new)
            except ValueError as e:
                raise ApiError(400, f"The bot would refuse this: {e}")
            doc["strategies"] = [new if (isinstance(e, dict) and e.get("name") == name) else e for e in entries]
            for e in doc["strategies"]:       # the bot reloads the whole file: it must all stay valid
                if isinstance(e, dict) and e.get("enabled", True) and str(e.get("type", "")).lower() == "market_maker":
                    try:
                        MM.build_config(e["name"], e)
                    except ValueError as err:
                        raise ApiError(409, f"Another market in the file is invalid, so nothing was saved: {err}")
            if new == entry:
                return 200, {"ok": True, "changed": False, "message": "No change — those are the current settings."}
            head_len = 0
            for line in text.splitlines():
                if not line.startswith("#"):
                    break
                head_len += 1
            header = [line for line in text.splitlines()[:head_len] if "web panel" not in line]
            out = "\n".join(header + [f"# Last changed in the web panel on {time.strftime('%Y-%m-%d %H:%M')} ({name})."]) + "\n"
            out += yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)
            backup = _backup(self.paths.bot_config)
            _atomic_write(self.paths.bot_config, out)
            os.makedirs(self.paths.state, exist_ok=True)
            with open(os.path.join(self.paths.state, "reload"), "a"):
                pass
            os.utime(os.path.join(self.paths.state, "reload"))
        self.cache.clear()
        return 200, {"ok": True, "changed": True, "backup": os.path.basename(backup),
                     "message": f"Saved. The bot applies it within seconds (old file kept as {os.path.basename(backup)})."}

    def get_alert_settings(self, q):
        try:
            doc = yaml.safe_load(open(self.paths.ops_config)) or {}
        except FileNotFoundError:
            doc = None
        vals = {f["key"]: _get_path(doc or {}, f["key"], f["default"]) for f in ALERT_FIELDS}
        return 200, {"values": vals, "fields": ALERT_FIELDS, "configured": doc is not None,
                     "note": "The alerts service applies changes after a container restart (docker compose restart bot)."}

    def post_alert_settings(self, body):
        changes = body.get("changes")
        if not isinstance(changes, dict) or not changes:
            raise ApiError(400, "Nothing to change.")
        fields = {f["key"]: f for f in ALERT_FIELDS}
        unknown = [k for k in changes if k not in fields]
        if unknown:
            raise ApiError(400, f"These settings can't be changed here: {', '.join(map(str, unknown))}.")
        clean = {}
        for k, v in changes.items():
            f = fields[k]
            if f["kind"] == "bool":
                clean[k] = _bool(v, f["label"])
            elif f["kind"] == "hours":
                items = v if isinstance(v, list) else [x for x in re.split(r"[,\s]+", str(v)) if x]
                if not 1 <= len(items) <= 4:
                    raise ApiError(400, f"{f['label']}: give one to four numbers of hours, e.g. 24, 3.")
                hrs = [_number(x, f["label"], lo=0, hi=168, lo_open=True) for x in items]
                clean[k] = sorted((int(h) if h == int(h) else h for h in hrs), reverse=True)
            else:
                clean[k] = _number(v, f["label"], lo=f.get("min"), hi=f.get("max"), integer=f["kind"] == "int")
        with self.cfg_lock:
            if not os.path.exists(self.paths.ops_config):
                raise ApiError(409, "There is no alerts configuration yet — save a setup first (Setup & funding).")
            doc = yaml.safe_load(open(self.paths.ops_config)) or {}
            for k, v in clean.items():
                _set_path(doc, k, v)
            backup = _backup(self.paths.ops_config)
            _atomic_write(self.paths.ops_config, yaml.safe_dump(doc, sort_keys=False))
        return 200, {"ok": True, "backup": os.path.basename(backup), "message": ALERTS_NOTE, "restart_needed": True}

    # -------------------------------------------------- setup & funding
    def get_setup_info(self, q):
        env = _read_env(self.paths.env)
        return 200, {
            "markets": [{"pair": m, "name": s["name"], "base": s["base"], "quote": s["quote"], "text": MARKET_TEXT.get(m, ""),
                         "share_pct": round(s["weight"] * 100),
                         "chains": list(dict.fromkeys(PL.CHAIN[a] for a in (s["base"], s["quote"])))}
                        for m, s in PL.MARKETS.items()],
            # take_edge_pct (taking mispriced orders) only applies on the stablecoin market
            "presets": [{"name": n, "text": PRESET_TEXT.get(n, ""), "levels": p["levels"],
                         "half_spread_pct": p["half_spread_pct"], "level_step_pct": p["level_step_pct"],
                         "stable_half_spread_pct": (p.get("stable") or {}).get("half_spread_pct"),
                         "arbitrage_stable": p.get("take_edge_pct") is not None, "usage_pct": round(p["usage"] * 100)}
                        for n, p in PL.PRESETS.items()],
            "min_budget": PL.MIN_BUDGET,
            "current": self.saved_plan(),
            "secrets": {"telegram": bool(env.get("ALERTS_TELEGRAM_BOT_TOKEN")),
                        "telegram_chat": bool(env.get("ALERTS_TELEGRAM_CHAT_ID"))},
        }

    def _plan_args(self, body, min_budget: float, extra=()):
        budget = _number(body.get("budget"), "Budget", lo=min_budget, hi=10_000_000)
        preset = body.get("preset") or "balanced"
        if preset not in PL.PRESETS:
            raise ApiError(400, f"Preset must be one of: {', '.join(PL.PRESETS)}.")
        markets = body.get("markets") or list(PL.MARKETS)
        if not isinstance(markets, list) or not all(isinstance(m, str) for m in markets):
            raise ApiError(400, "Markets must be a list.")
        bad = [m for m in markets if m not in PL.MARKETS]
        if bad:
            raise ApiError(400, f"Unknown market: {', '.join(m[:30] for m in bad)}.")
        markets = [m for m in PL.MARKETS if m in markets]         # canonical order, no duplicates
        unknown = sorted(k for k in body if k not in ("budget", "preset", "markets", *extra))
        if unknown:
            raise ApiError(400, f"Unknown field(s): {', '.join(str(k)[:30] for k in unknown)}.")
        return budget, preset, markets

    @staticmethod
    def plan_json(p) -> dict:
        return {
            "budget_usd": p.budget_usd, "preset": p.preset,
            "markets": [{"pair": e["pair"], "name": e["name"], "base": e["pair"].split("/")[0],
                         "levels": e["params"]["levels"], "size": e["params"]["level_size"],
                         "half_spread_pct": e["params"]["half_spread_pct"],
                         "level_step_pct": e["params"]["level_step_pct"],
                         "max_position": e["params"]["max_position"],
                         "arbitrage": "take_edge_pct" in e["params"]} for e in p.entries],
            "assets": [{"asset": a, "chain": PL.CHAIN[a], **v} for a, v in p.assets.items()],
            "deposit_usd": round(sum(v["own_usd"] for v in p.assets.values()), 2),
            "inbound_usd": round(sum(v["inbound_usd"] for v in p.assets.values()), 2),
            "lease_cost_week_usd": p.lease_cost_week_usd,
            "warnings": p.warnings,
        }

    def post_plan(self, body):
        budget, preset, markets = self._plan_args(body, min_budget=1)
        px = self.price_map(required=True)
        try:
            p = PL.plan(budget, preset, markets, px)
        except ValueError as e:
            raise ApiError(400, str(e))
        return 200, {"plan": self.plan_json(p), "text": PL.describe(p), "prices": px}

    def post_setup(self, body):
        budget, preset, markets = self._plan_args(body, min_budget=PL.MIN_BUDGET, extra=("telegram_token",))
        tg = body.get("telegram_token") or ""
        if not isinstance(tg, str):
            raise ApiError(400, "Telegram bot token must be text.")
        tg = tg.strip()
        if tg and not TELEGRAM_TOKEN_RX.match(tg):
            raise ApiError(400, "That doesn't look like a Telegram bot token (it looks like 123456789:AAE…).")
        px = self.price_map()
        if px:
            try:
                p = PL.plan(budget, preset, markets, px)
            except ValueError as e:
                raise ApiError(400, str(e))
            if not p.entries:
                raise ApiError(400, "Nothing to quote with this budget — raise it or pick fewer markets. "
                                    + " ".join(p.warnings))
        amount = f"{budget:.2f}".rstrip("0").rstrip(".")
        args = ["setup", "--budget", amount, "--preset", preset, "--markets", ",".join(markets), "--yes"]
        job = self.jobs.start("setup", f"Save setup (${budget:,.0f}, {preset})", args,
                              env={"ALERTS_TELEGRAM_BOT_TOKEN": tg}, scrub=[tg] + self.secrets_in_env())
        self.cache.clear()
        return 202, {"job": job.public()}

    def post_fund(self, body):
        if not self.saved_plan():
            raise ApiError(409, "Save a setup first — the funding follows from your plan.")
        job = self.jobs.start("fund", "Check funding (nothing moves)", ["fund"], scrub=self.secrets_in_env())
        return 202, {"job": job.public()}

    def post_fund_go(self, body):
        if body.get("confirm") is not True:
            raise ApiError(400, "Opening channels moves money: confirm it first.")
        if not self.saved_plan():
            raise ApiError(409, "Save a setup first — the funding follows from your plan.")
        V, _ = volume_module()
        try:
            run = V.status().get("running") if V else None
        except Exception:
            run = None
        if run:
            raise ApiError(409, f"A volume run is active on {run.get('market', '?')} — stop it first (Volume tab), "
                                "then open the channels.")
        job = self.jobs.start("fund-go", "Open and fund the channels", ["fund", "--go"], scrub=self.secrets_in_env())
        self.cache.clear()
        return 202, {"job": job.public()}

    def get_jobs(self, q):
        run = self.jobs.running()
        return 200, {"jobs": self.jobs.list(), "running": run.id if run else None}

    def get_job(self, job_id: str):
        job = self.jobs.get(job_id) if re.fullmatch(r"[0-9a-f]{16}", job_id or "") else None
        if not job:
            raise ApiError(404, "No such job (the GUI may have restarted).")
        return 200, job.public()

    def get_telegram(self, q):
        return 200, self.telegram_state()

    def post_telegram(self, body):
        token = body.get("token") or ""
        if not isinstance(token, str):
            raise ApiError(400, "The token must be text.")
        token = token.strip()
        if token:
            if not TELEGRAM_TOKEN_RX.match(token):
                raise ApiError(400, "That doesn't look like a Telegram bot token (it looks like 123456789:AAE…). "
                                    "Get one from @BotFather with /newbot.")
            code = E.set_telegram(token, env_path=self.paths.env, pair_path=self.paths.pair)
        else:
            if not _read_env(self.paths.env).get("ALERTS_TELEGRAM_BOT_TOKEN"):
                raise ApiError(400, "No bot yet — create one with @BotFather (/newbot) and paste its token.")
            code = E.new_pairing_code(self.paths.pair)
        return 200, {"ok": True, "code": code,
                     "message": f"Open your bot in Telegram (or add it to a group) and send: /start {code} — "
                                "the code works once, for an hour."}

    # -------------------------------------------------- volume
    def _vol(self):
        V, why = volume_module()
        if V is None:
            raise ApiError(503, f"Volume mode is not available in this version ({why}).")
        return V

    def get_volume(self, q):
        V, why = volume_module()
        if V is None:
            return 200, {"available": False, "reason": why}
        try:
            st = V.status()
        except Exception as e:
            return 200, {"available": False, "reason": f"status failed: {_short(e)}"}
        names = {e.get("pair"): e["name"] for e in self.entries()}
        return 200, {"available": True, "markets": list(V.MARKETS), "status": st,
                     "strategies": {m: names.get(m) for m in V.MARKETS}}

    def post_volume_config(self, body):
        V = self._vol()
        cfg = body.get("config")
        if not isinstance(cfg, dict):
            raise ApiError(400, "Send the volume settings as an object.")
        try:
            V.save_config(cfg)
        except ValueError as e:
            raise ApiError(400, f"Not saved: {e}")
        return 200, {"ok": True, "message": "Saved the daily volume targets.", "config": V.load_config()}

    def post_volume_start(self, body):
        V = self._vol()
        if body.get("confirm") is not True:
            raise ApiError(400, "A volume run pays fees: confirm it first.")
        market = body.get("market")
        if market not in list(V.MARKETS):
            raise ApiError(400, f"Pick one of: {', '.join(V.MARKETS)}.")
        usd = _number(body.get("usd"), "Amount", lo=0, lo_open=True, hi=1_000_000)
        size = body.get("size")
        size = None if size in (None, "") else _number(size, "Round size", lo=0, lo_open=True)
        busy = self.jobs.running()
        if busy and busy.kind in ("setup", "fund-go"):
            raise ApiError(409, f"“{busy.title}” is still running — start the volume run once it has finished.")
        try:
            pid = V.start_run(market, usd, size)
        except RuntimeError as e:
            raise ApiError(409, str(e))
        except ValueError as e:
            raise ApiError(400, str(e))
        return 200, {"ok": True, "pid": pid, "message": f"Started: ${usd:,.0f} of volume on {market}."}

    def post_volume_stop(self, body):
        V = self._vol()
        stopped = bool(V.stop_run())
        return 200, {"ok": True, "stopping": stopped,
                     "message": "Stopping after the current round." if stopped else "No volume run is active."}


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "hydra-gui"
    sys_version = ""

    def version_string(self):
        return "hydra-gui"

    def log_request(self, code="-", size="-"):
        # Quiet: POSTs and errors only. Never bodies (they can carry secrets); the token is a header.
        if self.command != "GET" or (isinstance(code, int) and code >= 400):
            super().log_request(code, size)

    def log_message(self, fmt, *args):
        sys.stderr.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} gui {self.address_string()} {fmt % args}\n")

    def _send(self, status: int, body: bytes, ctype: str):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in SECURITY_HEADERS:
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj):
        self._send(status, json.dumps(_clean(obj), ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):                      # a CORS preflight: refused, and no Access-Control-* headers
        self._json(403 if not host_ok(self.headers.get_all("Host")) else 405, {"error": "Not allowed."})

    def _static(self, path: str):
        hit = STATIC.get(path)
        if not hit:
            return self._json(404, {"error": "Not found."})
        try:
            with open(os.path.join(self.server.api.paths.static, hit[0]), "rb") as f:
                body = f.read()
        except OSError:
            return self._json(404, {"error": "Not found."})
        self._send(200, body, hit[1])

    def _body(self):
        if self.headers.get("Transfer-Encoding"):
            raise ApiError(411, "Send a Content-Length.")
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            raise ApiError(415, "Send JSON (Content-Type: application/json).")
        try:
            n = int(self.headers.get("Content-Length", ""))
        except ValueError:
            raise ApiError(411, "Send a Content-Length.")
        if n < 0:
            raise ApiError(400, "Bad Content-Length.")
        if n > MAX_BODY:
            self.close_connection = True
            left = min(n, 1024 * 1024)        # drain a little so the client can read the answer
            while left > 0:
                chunk = self.rfile.read(min(left, 65536))
                if not chunk:
                    break
                left -= len(chunk)
            raise ApiError(413, f"The request is too large (at most {MAX_BODY // 1024} KB).")
        raw = self.rfile.read(n)

        def no_constants(c):
            raise ValueError(c)
        try:
            data = json.loads(raw.decode("utf-8"), parse_constant=no_constants) if raw.strip() else {}
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "The request is not valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "Send a JSON object.")
        return data

    def _handle(self, method: str):
        api = self.server.api
        try:
            if not host_ok(self.headers.get_all("Host")):
                raise ApiError(403, "Forbidden host. Open the panel as http://localhost:<port>/ through the SSH tunnel.")
            url = urlsplit(self.path)
            if not url.path.startswith("/api/"):
                if method != "GET":
                    raise ApiError(405, "Not allowed.")
                return self._static(url.path)
            if (self.headers.get("Sec-Fetch-Site") or "").lower() == "cross-site":
                raise ApiError(403, "Cross-site requests are not allowed.")
            origin = self.headers.get("Origin")
            if origin and not origin_ok(origin):
                raise ApiError(403, "Cross-site requests are not allowed.")
            given = self.headers.get(TOKEN_HEADER) or ""
            if not given or not hmac.compare_digest(given.encode("utf-8"), gui_token(api.paths.token).encode("utf-8")):
                raise ApiError(401, "Missing or wrong access token. Open the link that `./hydra-mm gui` prints.")
            body = self._body() if method == "POST" else None
            status, obj = api.dispatch(method, url.path, parse_qs(url.query), body)
            self._json(status, obj)
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except Exception as e:
            sys.stderr.write(f"gui: {method} {urlsplit(self.path).path} failed\n{traceback.format_exc()}")
            self._json(500, {"error": f"Something went wrong on the server ({type(e).__name__}). "
                                      "Details are in the bot container's log."})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, api: Api):
        if ":" in addr[0]:
            import socket
            self.address_family = socket.AF_INET6
        super().__init__(addr, Handler)
        self.api = api


def make_server(host: str = "127.0.0.1", port: int = 8080, paths: Paths = None) -> Server:
    paths = paths or Paths()
    gui_token(paths.token)
    return Server((host, port), Api(paths))


def main():
    ap = argparse.ArgumentParser(description="Web GUI for easy mode (reach it through an SSH tunnel).")
    ap.add_argument("--host", default=os.getenv("HYDRA_GUI_HOST", "127.0.0.1"),
                    help="address to listen on (container: 0.0.0.0, published on the host's 127.0.0.1 only)")
    ap.add_argument("--port", type=int, default=int(os.getenv("HYDRA_GUI_PORT", "8080")))
    a = ap.parse_args()
    os.chdir(ROOT)                            # hydra_mm's doctor and lib/volume use repo-relative paths
    srv = make_server(a.host, a.port, Paths(ROOT))
    print(f"hydra-mm GUI on http://{a.host}:{a.port}/ — open it through an SSH tunnel; "
          f"`./hydra-mm gui` prints the link with the access token.", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
