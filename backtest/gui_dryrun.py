"""
Dry run of the web GUI (tools/gui.py): access control, every API endpoint against stubbed
node / prices / volume data, settings validation and backups, pause/resume, jobs with a
harmless fake hydra-mm, and that secrets never come back out. No node, no funds, no network
(the test server listens on 127.0.0.1 only, for the duration of the test).

    python3 backtest/gui_dryrun.py        # from the repo root, project venv
"""
import copy, glob, http.client, io, json, os, stat, sys, tempfile, threading, time, types, logging
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
logging.basicConfig(level=logging.ERROR)
for k in ("ALERTS_TELEGRAM_BOT_TOKEN", "ALERTS_TELEGRAM_CHAT_ID"):
    os.environ.pop(k, None)

import yaml
from lib import planner as PL
from lib import easy_ops as E
import importlib.util
_spec = importlib.util.spec_from_file_location("gui_under_test", os.path.join(ROOT, "tools", "gui.py"))
G = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(G)
from strategies.market_maker import MarketMakerStrategy as MM

RESULTS = []
def check(label, ok, detail=""):
    RESULTS.append(bool(ok))
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


class Tee(io.TextIOBase):
    """Captures what the server logs (stderr), so we can prove no secret is ever logged."""
    def __init__(self):
        self.buf = []
    def write(self, s):
        self.buf.append(s)
        return len(s)
    def text(self):
        return "".join(self.buf)
LOG = Tee()
sys.stderr = LOG

# ------------------------------------------------------------------ a temporary install
TMP = tempfile.mkdtemp(prefix="gui_dryrun_")
for d in ("config", "state"):
    os.makedirs(os.path.join(TMP, d))
PX = {"BTC": 83500.0, "ETH": 2690.0}
MARKETS = ["BTC/USDC.arb", "USDC.arb/USDC.eth"]
plan0 = PL.plan(2000, "balanced", MARKETS, PX)
E.write_bot_config(plan0, os.path.join(TMP, "config", "bot_config.yaml"))
E.write_ops_config(plan0, os.path.join(TMP, "config", "ops.yaml"))
json.dump({"budget": 2000, "preset": "balanced", "markets": MARKETS, "prices": PX},
          open(os.path.join(TMP, "state", "easy_plan.json"), "w"))
ENV_SECRETS = {"ALERTS_TELEGRAM_BOT_TOKEN": "111111111:AAEnvTokenEnvTokenEnvTokenEnvToken1"}
E.set_env(os.path.join(TMP, ".env"), dict(ENV_SECRETS, OTHER_SETTING="keep-me-123456"))
TYPED_TG = "222222222:AATypedTokenTypedTokenTypedToken22"
NEW_TG = "333333333:AANewBotNewBotNewBotNewBotNewBot333"
INVITE = "INV-7f3k2-QWERTY-zx81Lm"
ALL_SECRETS = list(ENV_SECRETS.values()) + [TYPED_TG, NEW_TG, INVITE]

# ------------------------------------------------------------------ stubs (no node, no network)
CALLS = {"pause": [], "volume": []}
class FakeNode:
    pass
STATUS = [
    {"at": "2026-10-06 12:00:00", "name": "mm_btc_usdc", "pair": "BTC/USDC.arb", "fair": 83510.0, "bids": 5, "asks": 5,
     "position": 0.0012, "pct": "+20%", "fills": 7, "pnl": 3.25, "paused": False},
    {"at": "2026-10-06 12:00:01", "name": "mm_usdc_usdc", "pair": "USDC.arb/USDC.eth", "fair": 1.0, "bids": 4, "asks": 3,
     "position": -12.0, "pct": "-10%", "fills": 3, "pnl": -0.75, "paused": False},
]
FILLS = [{"at": f"2026-10-06 11:0{i}:00", "pair": "BTC/USDC.arb", "side": "buy" if i % 2 else "sell", "amount": 0.0003,
          "price": 83400.0 + i, "how": "maker", "position": 0.0003 * i} for i in range(5)]
E.connect = lambda: FakeNode()
E.market_status = lambda root=None, **kw: copy.deepcopy(STATUS)
E.capacity = lambda c: {"BTC": {"send_free": 0.002, "send": 0.003, "recv_free": 0.001, "recv": 0.004},
                        "USDC.arb": {"send_free": 300.0, "send": 420.0, "recv_free": 200.0, "recv": 500.0}}
E.leases = lambda: [{"chain": "arbitrum", "channel": "abcdef0123456789", "asset": "USDC.arb",
                     "lease_end": time.time() + 3 * 86400, "liquidity_end": time.time() + 5 * 86400}]
E.wallet_onchain = lambda c: {"BTC": 0.001, "USDC.arb": 0.0, "USDC.eth": 25.0}
E.native_balances = lambda c: {"ethereum": 0.002, "arbitrum": 0.0}
E.recent_fills = lambda n=5, root=None, **kw: copy.deepcopy(FILLS[-n:])
E.dex_markets = lambda c: {"BTC/USDC.arb": ("b1", "q1"), "USDC.arb/USDC.eth": ("b2", "q2"), "ETH/BTC": ("b3", "q3")}
BOOKS = {"BTC/USDC.arb": {"bids": [(83400.0, 0.001, True), (83300.0, 0.002, False)], "asks": [(83600.0, 0.0011, True)]},
         "USDC.arb/USDC.eth": {"bids": [(0.999, 20.0, True)], "asks": [(1.001, 20.0, True), (1.002, 50.0, False)]},
         "ETH/BTC": {"bids": [], "asks": []}}
E.book = lambda c, pair, cur=None: copy.deepcopy(BOOKS[pair])
ADDRS = {"bitcoin": "bc1qdryrunaddress", "ethereum": "0xEthDryRun", "arbitrum": "0xArbDryRun"}
E.deposit_addresses = lambda c: dict(ADDRS)
E.prices = lambda: dict(PX)
_real_pause = E.set_pause
def _pause(on, market=None, root=E.ROOT):
    CALLS["pause"].append((on, market, root))
    return _real_pause(on, market, root=root)
E.set_pause = _pause
DOCTOR = [{"ok": True, "what": "node answers (1:f9beb4d9)", "hint": "docker compose up -d"},
          {"ok": True, "what": "admitted to mainnet (12 markets)", "hint": "hydra-mm invite <CODE>"},
          {"ok": False, "what": "channels hold $120 of $1,600 planned", "hint": "hydra-mm fund   (shows what to send where)"}]
G.doctor_steps = lambda: copy.deepcopy(DOCTOR)

# lib/volume.py stub, to the contract in the task (MARKETS as a dict, like the real module)
VOL = types.ModuleType("lib.volume")
VOL.MARKETS = {"USDC.arb/USDC.eth": {"max_size": 70.0}, "BTC/USDC.arb": {"max_size": 0.002}}
VOL.cfg = {"enabled": False, "pause_market_maker": True, "burst_every_min": 20, "active_hours": [0, 24],
           "markets": {"USDC.arb/USDC.eth": {"daily_usd": 0.0, "max_size": 70.0},
                       "BTC/USDC.arb": {"daily_usd": 0.0, "max_size": 0.002}}}
VOL.running = None
def _vstatus():
    return {"running": VOL.running, "last_run": {"market": "USDC.arb/USDC.eth", "target_usd": 100, "volume_usd": 101.5,
            "rounds": 2, "cost_usd": 0.07, "started_at": time.time() - 600, "ended_at": time.time() - 500, "pid": 1,
            "source": "manual", "message": "", "result": "done"},
            "today": {"USDC.arb/USDC.eth": {"volume_usd": 101.5, "cost_usd": 0.07, "rounds": 2}},
            "history": [{"date": "2026-10-05", "market": "USDC.arb/USDC.eth", "volume_usd": 500.0, "cost_usd": 0.35, "rounds": 8}],
            "cost_per_1000": {"USDC.arb/USDC.eth": 0.7, "BTC/USDC.arb": 2.5}, "config": copy.deepcopy(VOL.cfg)}
def _vsave(cfg, path="config/volume.yaml"):
    if not 5 <= cfg.get("burst_every_min", 20) <= 240:
        raise ValueError("burst_every_min must be between 5 and 240 minutes")
    CALLS["volume"].append(("save", copy.deepcopy(cfg)))
    VOL.cfg = copy.deepcopy(cfg)
def _vstart(market, usd, size=None):
    if VOL.running:
        raise RuntimeError(f"a volume run is already active on {VOL.running['market']} (pid 4242)")
    CALLS["volume"].append(("start", market, usd, size))
    VOL.running = {"market": market, "target_usd": usd, "volume_usd": 0.0, "rounds": 0, "cost_usd": 0.0,
                   "started_at": time.time(), "pid": 4242, "source": "manual", "message": "starting"}
    return 4242
def _vstop():
    CALLS["volume"].append(("stop",))
    return VOL.running is not None
VOL.status, VOL.load_config, VOL.save_config = _vstatus, lambda path=None: copy.deepcopy(VOL.cfg), _vsave
VOL.start_run, VOL.stop_run = _vstart, _vstop
sys.modules["lib.volume"] = VOL

# the fake hydra-mm a job runs: records its argv and the secrets it received, echoes a secret
# on purpose (a careless tool must not leak it through the GUI), can be slow or fail
FAKE = os.path.join(TMP, "fake_hydra_mm.py")
open(FAKE, "w").write(r'''
import json, os, sys, time
args = sys.argv[1:]
rec = {"args": args, "env": {k: os.environ.get(k) for k in ("ALERTS_TELEGRAM_BOT_TOKEN", "HYDRA_INVITE_CODE")},
       "other_secret_env": sorted(k for k in os.environ if k.endswith(("_API_KEY", "_API_SECRET")))}
json.dump(rec, open(os.path.join(os.environ["GUI_DRYRUN_OUT"], "job_" + "_".join(a.strip("-") for a in args[:2]) + ".json"), "w"))
print("fake hydra-mm " + " ".join(args), flush=True)
if args[:1] == ["setup"]:
    print("careless echo: " + str(os.environ.get("ALERTS_TELEGRAM_BOT_TOKEN")), flush=True)
if args[:1] == ["invite"]:
    print("careless echo: redeeming " + str(os.environ.get("HYDRA_INVITE_CODE")), flush=True)
    print("✅ Invite redeemed (invited by abcdef0123456789…) — the node finishes starting now …", flush=True)
if "--go" in args:
    time.sleep(1.0)
    print("opening channels ... done", flush=True)
sys.exit(int(os.environ.get("GUI_DRYRUN_EXIT", "0")))
''')
os.environ["GUI_DRYRUN_OUT"] = TMP
G.HYDRA_MM = [sys.executable, FAKE]

# ------------------------------------------------------------------ the server
PATHS = G.Paths(TMP)
SRV = G.make_server("127.0.0.1", 0, PATHS)
PORT = SRV.server_address[1]
threading.Thread(target=SRV.serve_forever, daemon=True).start()
TOKEN = G.gui_token(PATHS.token)
BODIES, HEADERS = [], []

def req(method, path, body=None, token="__default__", host=None, headers=None, raw=None, ctype="application/json",
        hosts=None):
    conn = http.client.HTTPConnection("127.0.0.1", PORT, timeout=15)
    conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
    for hv in (hosts if hosts is not None else [host or f"localhost:{PORT}"]):
        conn.putheader("Host", hv)
    tok = TOKEN if token == "__default__" else token
    if tok is not None:
        conn.putheader("X-Hydra-Token", tok)
    data = None
    if body is not None or raw is not None:
        data = raw if raw is not None else json.dumps(body).encode()
        conn.putheader("Content-Type", ctype)
        conn.putheader("Content-Length", str(len(data)))
    for k, v in (headers or {}).items():
        conn.putheader(k, v)
    conn.endheaders(data)
    r = conn.getresponse()
    b = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    conn.close()
    BODIES.append(b)
    HEADERS.append(hdrs)
    try:
        j = json.loads(b)
    except ValueError:
        j = None
    return r.status, j, b, hdrs

def wait_job(job_id, timeout=20):
    t0, j = time.time(), None
    while time.time() - t0 < timeout:
        _, j, _, _ = req("GET", f"/api/jobs/{job_id}")
        if j and j.get("status") != "running":
            return j
        time.sleep(0.1)
    return j

def fake_record(name):
    try:
        return json.load(open(os.path.join(TMP, f"job_{name}.json")))
    except (OSError, ValueError):
        return None

# ------------------------------------------------------------------ 1. access token
print("\n1. access token")
mode = stat.S_IMODE(os.stat(PATHS.token).st_mode)
check("state/gui_token created, mode 0600, url-safe and long enough", mode == 0o600 and len(TOKEN) >= 30
      and all(c.isalnum() or c in "-_" for c in TOKEN), f"mode {oct(mode)}, {len(TOKEN)} chars")
check("gui_token() is stable (create-if-missing, not rotate)", G.gui_token(PATHS.token) == TOKEN)
p2 = os.path.join(TMP, "state", "other_token")
open(p2, "w").close()
t2 = G.gui_token(p2)
check("an empty token file gets a fresh token", len(t2) >= 30 and open(p2).read().strip() == t2)

# ------------------------------------------------------------------ 2. access control
print("\n2. access control")
s, j, _, _ = req("GET", "/api/overview", token=None)
check("no token: 401", s == 401 and "token" in (j or {}).get("error", ""), f"{s}")
s, _, _, _ = req("GET", "/api/overview", token="wrong-" + TOKEN[6:])
check("wrong token: 401", s == 401, f"{s}")
s, _, _, _ = req("GET", "/api/overview", token=TOKEN + "x")
check("token with extra characters: 401", s == 401, f"{s}")
s, _, _, _ = req("POST", "/api/pause", {}, token=None)
check("POST without token: 401 and nothing paused", s == 401 and not CALLS["pause"], f"{s}")
s, _, _, _ = req("GET", "/api/overview", host="evil.com")
check("foreign Host (DNS rebinding) with a valid token: 403", s == 403, f"{s}")
s, _, _, _ = req("GET", "/", host="evil.com:8080")
check("foreign Host for the page itself: 403", s == 403, f"{s}")
s, _, _, _ = req("GET", "/api/overview", host="localhost.evil.com")
check("Host localhost.evil.com: 403", s == 403, f"{s}")
s, _, _, _ = req("GET", "/api/overview", hosts=[f"localhost:{PORT}", "evil.com"])
check("two Host headers: 403", s == 403, f"{s}")
s, _, _, _ = req("GET", "/api/overview", hosts=[])
check("no Host header: 403", s == 403, f"{s}")
ok = all(req("GET", "/api/telegram", host=h)[0] == 200 for h in (f"127.0.0.1:{PORT}", f"[::1]:{PORT}", "localhost"))
check("localhost, 127.0.0.1 and [::1] (any port) are accepted", ok)
s, _, _, _ = req("POST", "/api/pause", {}, headers={"Origin": "https://evil.com"})
check("a foreign Origin is refused even with the token", s == 403 and not CALLS["pause"], f"{s}")
s, _, _, _ = req("GET", "/api/overview", headers={"Sec-Fetch-Site": "cross-site"})
check("Sec-Fetch-Site: cross-site is refused", s == 403, f"{s}")
s, _, _, hd = req("OPTIONS", "/api/pause", token=None, headers={"Origin": "https://evil.com",
                                                               "Access-Control-Request-Method": "POST",
                                                               "Access-Control-Request-Headers": "x-hydra-token"})
check("CORS preflight refused, no Access-Control-* headers", s in (403, 405) and
      not any(k.startswith("access-control") for k in hd), f"{s}")

print("\n3. static files")
s, _, b, hd = req("GET", "/", token=None)
check("index served for localhost without a token (the page reads it from the hash)", s == 200 and
      b"Hydra market maker" in b and hd.get("content-type", "").startswith("text/html"), f"{s}")
csp = hd.get("content-security-policy", "")
check("strict CSP: only own scripts/styles, no framing", "script-src 'self'" in csp and "frame-ancestors 'none'" in csp
      and "default-src 'none'" in csp and hd.get("x-frame-options") == "DENY" and hd.get("cache-control") == "no-store")
s1, _, b1, h1 = req("GET", "/app.js", token=None)
s2, _, b2, h2 = req("GET", "/style.css", token=None)
check("app.js and style.css served with the right types", s1 == 200 and "javascript" in h1.get("content-type", "")
      and s2 == 200 and "text/css" in h2.get("content-type", ""))
SVG_NS = b"http://www.w3.org/2000/svg"          # a namespace name, never fetched
check("no third-party code: no external URLs in the page, script or styles",
      all(x not in blob.replace(SVG_NS, b"") for blob in (b, b1, b2) for x in (b"http://", b"https://", b"//cdn", b"@import", b"url(")))
check("the page reads the token from the hash, keeps it in sessionStorage, sends X-Hydra-Token, no cookies",
      b"sessionStorage" in b1 and b"X-Hydra-Token" in b1 and b"replaceState" in b1 and b"document.cookie" not in b1
      and b"innerHTML" not in b1)
bad = [p for p in ("/../tools/gui.py", "/%2e%2e/tools/gui.py", "/gui.py", "/tools/gui.py", "/.env", "/state/gui_token")
       if req("GET", p, token=None)[0] != 404]
check("nothing but the four page files is served (no traversal)", not bad, f"{bad}")
s, _, _, _ = req("POST", "/", {"x": 1}, token=None)
check("POST to a page path: 405", s == 405, f"{s}")

print("\n4. request bodies")
s, j, _, _ = req("POST", "/api/pause", raw=b"{}", ctype="text/plain")
check("non-JSON content type: 415", s == 415, f"{s}")
s, j, _, _ = req("POST", "/api/pause", raw=b'{"market": "x"' + b" " * 70000 + b"}")
check("body over 64 KB: 413", s == 413, f"{s}")
s, j, _, _ = req("POST", "/api/pause", raw=b"{not json")
check("malformed JSON: 400", s == 400 and "JSON" in j["error"], f"{s}")
s, j, _, _ = req("POST", "/api/pause", raw=b"[1, 2]")
check("a JSON array instead of an object: 400", s == 400, f"{s}")
s, j, _, _ = req("POST", "/api/settings/markets", raw=b'{"name": "mm_btc_usdc", "changes": {"levels": NaN}}')
check("NaN in JSON: 400", s == 400, f"{s}")
s, _, _, _ = req("GET", "/api/pause")
check("GET on a POST endpoint: 405", s == 405, f"{s}")
s, _, _, _ = req("GET", "/api/nope")
check("unknown endpoint: 404", s == 404, f"{s}")
check("nothing paused by any refused request", not CALLS["pause"])

# ------------------------------------------------------------------ 5. dashboard data
print("\n5. dashboard endpoints (stubbed node)")
s, j, _, _ = req("GET", "/api/overview")
d = (j or {}).get("doctor", {})
check("overview: doctor steps from hydra-mm, next step = the first failing one", s == 200 and
      [x["what"] for x in d.get("steps", [])] == [x["what"] for x in DOCTOR] and d.get("next", "").startswith("hydra-mm fund")
      and d.get("all_good") is False, f"{s} {d.get('next')}")
check("overview: prices and P&L total in USD (USDC-quoted markets)", j["prices"] == PX and
      abs(j["totals"]["pnl_usd"] - 2.5) < 1e-9 and j["totals"]["fills"] == 10 and j["totals"]["quoting"] == 2,
      f"{j['totals']}")
DOCTOR[2] = {"ok": True, "what": "channels funded ($1,600 of $1,600 planned)", "hint": "hydra-mm fund"}
s, j, _, _ = req("GET", "/api/overview?fresh=1")
check("overview ?fresh=1 re-runs the checks (all good now)", j["doctor"]["all_good"] is True and j["doctor"]["next"] is None)
s, j, _, _ = req("GET", "/api/markets")
m = {x["name"]: x for x in (j or {}).get("markets", [])}
check("markets: config + status + order book merged", s == 200 and m["mm_btc_usdc"]["best_bid"] == 83400.0 and
      m["mm_btc_usdc"]["best_ask"] == 83600.0 and m["mm_btc_usdc"]["our_bids"] == 1 and m["mm_usdc_usdc"]["fills"] == 3
      and m["mm_btc_usdc"]["position_pct"] == "+20%" and m["mm_btc_usdc"]["paused"] is None, f"{s}")
s, j, _, _ = req("GET", "/api/capacity")
caps = {a["asset"]: a for a in (j or {}).get("assets", [])}
check("capacity: per asset, with what the plan needs (ops.yaml)", s == 200 and caps["BTC"]["send"] == 0.003 and
      caps["USDC.arb"]["need_send"] == plan0.capacity["USDC.arb"]["send"], f"{s}")
s, j, _, _ = req("GET", "/api/leases")
check("leases", s == 200 and j["leases"][0]["asset"] == "USDC.arb" and j["now"] > 0, f"{s}")
s, j, _, _ = req("GET", "/api/wallet")
check("wallet: on-chain + gas", s == 200 and j["onchain"]["BTC"] == 0.001 and j["gas"]["ethereum"] == 0.002, f"{s}")
s, j, _, _ = req("GET", "/api/fills?n=2")
check("fills ?n=2: the latest two, newest first", s == 200 and len(j["fills"]) == 2 and
      j["fills"][0]["at"] > j["fills"][1]["at"], f"{s}")
s, j, _, _ = req("GET", "/api/fills?n=abc")
check("fills with a bad n: 400", s == 400, f"{s}")
s, j, _, _ = req("GET", "/api/book?pair=USDC.arb/USDC.eth")
check("book: bids/asks with our quotes marked", s == 200 and j["asks"][0] == [1.001, 20.0, True] and
      "ETH/BTC" in j["markets"], f"{s}")
s, j, _, _ = req("GET", "/api/book")
check("book without a pair: the first configured market", s == 200 and j["pair"] == "BTC/USDC.arb", f"{s}")
s, j, _, _ = req("GET", "/api/book?pair=DOGE/USD")
check("book of an unknown market: 400 with the list", s == 400 and "BTC/USDC.arb" in j["error"], f"{s}")
s, j, _, _ = req("GET", "/api/addresses")
ad = {a["chain"]: a for a in (j or {}).get("addresses", [])}
check("addresses: one per chain, with the assets that go there", s == 200 and ad["arbitrum"]["address"] == "0xArbDryRun"
      and "USDC.arb" in ad["arbitrum"]["assets"] and set(ad["ethereum"]["assets"]) == {"ETH", "USDC.eth"}, f"{s}")
s, j, _, _ = req("GET", "/api/funding")
send = {x["asset"]: x for x in (j or {}).get("send", [])}
gaps = E.funding_gaps(plan0, E.wallet_onchain(None), E.capacity(None))
check("funding: what to send where (from the saved plan, wallet and channels)", s == 200 and j["configured"] and
      set(send) == set(gaps) and send["USDC.arb"]["address"] == "0xArbDryRun" and not j["ready_to_open"], f"{list(send)}")
s, j, _, _ = req("GET", "/api/setup/info")
check("setup info: markets, presets, saved plan, secrets only as set / not set", s == 200 and
      len(j["markets"]) == 4 and {p["name"] for p in j["presets"]} == set(PL.PRESETS) and j["current"]["budget"] == 2000
      and j["secrets"] == {"telegram": True, "telegram_chat": False}, f"{s}")
check("setup info: markets say which networks hold the funds; arbitrage is a USDC/USDC thing",
      {m["pair"]: m["chains"] for m in j["markets"]}["BTC/USDC.arb"] == ["bitcoin", "arbitrum"] and
      all("arbitrage_stable" in p and "stable_half_spread_pct" in p for p in j["presets"]), f"{[m.get('chains') for m in j['markets']]}")

# ------------------------------------------------------------------ 6. pause / resume
print("\n6. pause / resume")
s, j, _, _ = req("POST", "/api/pause", {})
check("pause all: easy_ops.set_pause(True, None), state/pause written", s == 200 and CALLS["pause"][-1][:2] == (True, None)
      and os.path.exists(os.path.join(TMP, "state", "pause")), f"{s}")
s, j, _, _ = req("GET", "/api/overview")
check("overview shows everything paused", j["paused_all"] is True and j["totals"]["quoting"] == 0)
s, j, _, _ = req("POST", "/api/resume", {})
check("resume all: set_pause(False, None), state/pause gone", s == 200 and CALLS["pause"][-1][:2] == (False, None)
      and not os.path.exists(os.path.join(TMP, "state", "pause")), f"{s}")
s, j, _, _ = req("POST", "/api/pause", {"market": "mm_btc_usdc"})
check("pause one market: state/pause_mm_btc_usdc", s == 200 and CALLS["pause"][-1][:2] == (True, "mm_btc_usdc")
      and os.path.exists(os.path.join(TMP, "state", "pause_mm_btc_usdc")), f"{s}")
s, j, _, _ = req("GET", "/api/markets")
check("markets show that one as paused", {x["name"]: x["paused"] for x in j["markets"]}["mm_btc_usdc"] == "market")
s, j, _, _ = req("POST", "/api/resume", {"market": "mm_btc_usdc"})
check("resume one market", s == 200 and not os.path.exists(os.path.join(TMP, "state", "pause_mm_btc_usdc")), f"{s}")
n = len(CALLS["pause"])
s, j, _, _ = req("POST", "/api/pause", {"market": "../../etc/x"})
check("unknown market name refused (no file written)", s == 400 and len(CALLS["pause"]) == n, f"{s}")
open(os.path.join(TMP, "state", "pause_mm_usdc_usdc"), "w").write("volume-mode 4242")
open(os.path.join(TMP, "state", "pause_mm_btc_usdc"), "w").close()
s, j, _, _ = req("POST", "/api/resume", {})
check("resume all keeps a pause the running volume test owns, says so", s == 200 and
      os.path.exists(os.path.join(TMP, "state", "pause_mm_usdc_usdc")) and
      not os.path.exists(os.path.join(TMP, "state", "pause_mm_btc_usdc")) and "mm_usdc_usdc" in j["message"], j.get("message"))
s, j, _, _ = req("POST", "/api/resume", {"market": "mm_usdc_usdc"})
check("resuming a market held by the volume test: 409 with what to do", s == 409 and "volume" in j["error"], f"{s}")
os.remove(os.path.join(TMP, "state", "pause_mm_usdc_usdc"))

# ------------------------------------------------------------------ 7. market settings
print("\n7. market settings")
CFG = os.path.join(TMP, "config", "bot_config.yaml")
s, j, _, _ = req("GET", "/api/settings/markets")
ms = {x["name"]: x for x in (j or {}).get("markets", [])}
btc = ms["mm_btc_usdc"]["values"]
check("settings: current values + field labels/limits", s == 200 and btc["levels"] == plan0.entries[0]["params"]["levels"]
      and btc["bid_size"] == plan0.entries[0]["params"]["bid_size"] and len(j["fields"]) == 6, f"{s}")
before = open(CFG).read()
bad_cases = [({"levels": 0}, "Quotes per side"), ({"levels": 2.5}, "whole number"), ({"half_spread_pct": "abc"}, "not a number"),
             ({"half_spread_pct": -1}, "at least"), ({"max_position": 0}, "more than"), ({"enabled": "yes"}, "on or off"),
             ({"fair_value": 2}, "can't be changed"), ({"bid_size": btc["bid_size"] * 50}, "smaller steps"),
             ({"levels": True}, "number")]
bad = []
for changes, words in bad_cases:
    s, j, _, _ = req("POST", "/api/settings/markets", {"name": "mm_btc_usdc", "changes": changes})
    if s != 400 or words not in (j or {}).get("error", ""):
        bad.append((changes, s, (j or {}).get("error")))
check("bad values refused with a clear message (9 cases)", not bad, f"{bad}")
s, j, _, _ = req("POST", "/api/settings/markets", {"name": "mm_nope", "changes": {"levels": 3}})
check("unknown market: 404", s == 404, f"{s}")
check("nothing written by the refused changes, no backup, no reload", open(CFG).read() == before and
      not glob.glob(CFG + ".bak-*") and not os.path.exists(os.path.join(TMP, "state", "reload")))
s, j, _, _ = req("POST", "/api/settings/markets", {"name": "mm_btc_usdc",
                                                   "changes": {"levels": "4", "half_spread_pct": 0.3, "ask_size": btc["ask_size"] * 2}})
doc = yaml.safe_load(open(CFG))
e = next(x for x in doc["strategies"] if x["name"] == "mm_btc_usdc")
baks = glob.glob(CFG + ".bak-*")
check("good change written (levels 4, spread 0.3%, ask size x2)", s == 200 and e["params"]["levels"] == 4 and
      e["params"]["half_spread_pct"] == 0.3 and abs(e["params"]["ask_size"] - btc["ask_size"] * 2) < 1e-12, f"{s} {j}")
check("the old file kept as .bak-<ts>, the new one still valid for the bot", len(baks) == 1 and open(baks[0]).read() == before
      and MM.build_config("mm_btc_usdc", e).levels == 4 and j["backup"] == os.path.basename(baks[0]))
check("state/reload touched (the bot applies it live)", os.path.exists(os.path.join(TMP, "state", "reload")))
txt = open(CFG).read()
check("the header comment is kept, the other market untouched", txt.startswith("# Generated by `hydra-mm setup`") and
      "web panel" in txt and next(x for x in doc["strategies"] if x["name"] == "mm_usdc_usdc") == plan0.entries[1])
s, j, _, _ = req("POST", "/api/settings/markets", {"name": "mm_btc_usdc", "changes": {"levels": 4}})
check("saving the same values again: no change, no new backup", s == 200 and j["changed"] is False and
      len(glob.glob(CFG + ".bak-*")) == 1, f"{s}")
s, j, _, _ = req("POST", "/api/settings/markets", {"name": "mm_usdc_usdc", "changes": {"enabled": False}})
doc = yaml.safe_load(open(CFG))
check("a market can be switched off", s == 200 and next(x for x in doc["strategies"] if x["name"] == "mm_usdc_usdc")["enabled"] is False)

# ------------------------------------------------------------------ 8. alert settings
print("\n8. alert settings")
OPS = os.path.join(TMP, "config", "ops.yaml")
s, j, _, _ = req("GET", "/api/settings/alerts")
check("alerts: values from ops.yaml, defaults for keys it lacks", s == 200 and
      j["values"]["lease_autorenew.enabled"] is True
      and j["values"]["fill_alert_min_usd"] == 5.0 and j["values"]["lease_warn_hours"] == [24, 3], f"{s}")
before = open(OPS).read()
s, j, _, _ = req("POST", "/api/settings/alerts", {"changes": {"daily_report_hour": 25}})
s2, j2, _, _ = req("POST", "/api/settings/alerts", {"changes": {"lease_warn_hours": "24, 900"}})
s3, j3, _, _ = req("POST", "/api/settings/alerts", {"changes": {"capacity_need": {}}})
check("bad alert values refused (hour 25, 900 h, a key outside the safe subset)", s == 400 and s2 == 400 and s3 == 400
      and open(OPS).read() == before, f"{s} {s2} {s3}")
s, j, _, _ = req("POST", "/api/settings/alerts", {"changes": {"lease_warn_hours": "6, 24", "lease_autorenew.max_fee_usd": "12.5",
                                                              "digest_every": 600, "lease_autorenew.enabled": False}})
doc = yaml.safe_load(open(OPS))
check("good alert values written, the rest of ops.yaml kept", s == 200 and doc["lease_warn_hours"] == [24, 6] and
      doc["lease_autorenew"]["max_fee_usd"] == 12.5 and doc["lease_autorenew"]["enabled"] is False and
      doc["lease_autorenew"]["renew_below_hours"] == 24 and doc["digest_every"] == 600 and
      doc["capacity_need"] == plan0.capacity, f"{s}")
check("the response says it applies after a container restart, backup kept", "restart" in j["message"] and
      j["restart_needed"] and len(glob.glob(OPS + ".bak-*")) == 1)

# ------------------------------------------------------------------ 9. plan
print("\n9. plan")
s, j, _, _ = req("POST", "/api/plan", {"budget": 1000, "preset": "balanced", "markets": ["BTC/USDC.arb"]})
ref = PL.plan(1000, "balanced", ["BTC/USDC.arb"], PX)
check("plan: structured result + describe() text, same as the planner", s == 200 and
      [x["pair"] for x in j["plan"]["markets"]] == ["BTC/USDC.arb"] and j["plan"]["markets"][0]["size"] == ref.entries[0]["params"]["level_size"]
      and j["text"] == PL.describe(ref) and j["plan"]["lease_cost_week_usd"] == ref.lease_cost_week_usd, f"{s}")
s, j, _, _ = req("POST", "/api/plan", {"budget": 6000, "preset": "aggressive"})
arb = {m["pair"]: m["arbitrage"] for m in (j or {}).get("plan", {}).get("markets", [])}
check("plan over all markets: arbitrage only on USDC/USDC", s == 200 and len(arb) == 4 and
      [p for p, on in arb.items() if on] == ["USDC.arb/USDC.eth"], f"{s} {arb}")
s, j, _, _ = req("POST", "/api/plan", {"budget": 1000, "leverage": 3})
check("/api/plan refuses unknown fields", s == 400 and "Unknown field" in j["error"], f"{s}")
bad = [b for b in ({"budget": "abc"}, {"budget": 1000, "preset": "yolo"}, {"budget": 1000, "markets": ["DOGE/USD"]},
                   {"budget": 1000, "markets": "BTC/USDC.arb"}, {"budget": -5}) if req("POST", "/api/plan", b)[0] != 400]
check("bad plan input refused (5 cases)", not bad, f"{bad}")

# ------------------------------------------------------------------ 10. jobs, setup, funding
print("\n10. jobs: setup, fund, fund --go")
s, j, _, _ = req("POST", "/api/setup", {"budget": 100, "markets": MARKETS})
check("setup below the $300 minimum refused", s == 400 and "300" in j["error"], f"{s}")
n_jobs = len(req("GET", "/api/jobs")[1]["jobs"])
s, j, _, _ = req("POST", "/api/setup", {"budget": 1500, "leverage": 3})
s2, j2, _, _ = req("POST", "/api/setup", {"budget": 1500, "api_key": "abcdefgh12345678"})
check("/api/setup refuses unknown fields: 400, no job started", s == 400 and s2 == 400 and
      "Unknown field" in j["error"] and len(req("GET", "/api/jobs")[1]["jobs"]) == n_jobs, f"{s} {s2}")
s, j, _, _ = req("POST", "/api/setup", {"budget": 1500, "telegram_token": "not-a-token"})
check("a malformed Telegram token refused", s == 400 and "Telegram" in j["error"], f"{s}")
s, j, _, _ = req("POST", "/api/setup", {"budget": 1500, "telegram_token": "123456:abc\nOTHER_KEY=evil"})
check("a token with a newline (.env injection) refused", s == 400, f"{s}")
s, j, _, _ = req("POST", "/api/setup", {"budget": 1500, "preset": "balanced", "markets": MARKETS,
                                        "telegram_token": TYPED_TG})
job = (j or {}).get("job", {})
check("setup starts a job (202)", s == 202 and job.get("status") == "running" and job.get("kind") == "setup", f"{s} {j}")
done = wait_job(job["id"])
rec = fake_record("setup_budget")
check("setup job: unattended flags, secrets NOT in argv", done["status"] == "done" and rec and rec["args"] ==
      ["setup", "--budget", "1500", "--preset", "balanced", "--markets", "BTC/USDC.arb,USDC.arb/USDC.eth", "--yes"]
      and TYPED_TG not in " ".join(rec["args"]), f"{rec and rec['args']}")
check("setup job: the Telegram token reached hydra-mm through its environment, and nothing else",
      rec and rec["env"] == {"ALERTS_TELEGRAM_BOT_TOKEN": TYPED_TG, "HYDRA_INVITE_CODE": None} and rec["other_secret_env"] == [],
      f"{rec and rec['other_secret_env']}")
check("setup job: a secret echoed by the tool is blanked in the output",
      "careless echo: ••••••" in done["output"] and TYPED_TG not in done["output"], done["output"][-120:])
check("the job's command line is shown (without secrets)", done["command"].startswith("hydra-mm setup --budget 1500"))
check("secrets are not left in the GUI's own environment",
      not any(v for k, v in os.environ.items() if k.startswith("ALERTS_") and G.SECRET_ENV_RX.search(k)))

s, j, _, _ = req("POST", "/api/fund/go", {})
s2, j2, _, _ = req("POST", "/api/fund/go", {"confirm": "yes"})
check("fund --go refused without confirm (and with a non-boolean confirm)", s == 400 and s2 == 400 and
      "confirm" in j["error"] and not fake_record("fund_go"), f"{s} {s2}")
s, j, _, _ = req("POST", "/api/fund", {})
done = wait_job(j["job"]["id"])
check("fund preview job: hydra-mm fund (nothing moves), output captured", s == 202 and done["status"] == "done" and
      fake_record("fund")["args"] == ["fund"] and "fake hydra-mm fund" in done["output"], f"{s}")
s, j, _, _ = req("POST", "/api/fund/go", {"confirm": True})
s2, j2, _, _ = req("POST", "/api/fund", {})
s3, j3, _, _ = req("POST", "/api/volume/start", {"market": "USDC.arb/USDC.eth", "usd": 50, "confirm": True})
check("only one money job at a time: 409 while fund --go runs (also for a volume run)", s == 202 and s2 == 409 and s3 == 409
      and "still running" in j2["error"], f"{s} {s2} {s3}")
s4, j4, _, _ = req("GET", "/api/jobs")
check("jobs list shows the running one", s4 == 200 and j4["running"] == j["job"]["id"] and len(j4["jobs"]) >= 3)
done = wait_job(j["job"]["id"])
check("fund --go job: argv fund --go, status done, exit 0, output", done["status"] == "done" and done["exit_code"] == 0 and
      fake_record("fund_go")["args"] == ["fund", "--go"] and "opening channels ... done" in done["output"], f"{done['status']}")
os.environ["GUI_DRYRUN_EXIT"] = "3"
s, j, _, _ = req("POST", "/api/fund", {})
done = wait_job(j["job"]["id"])
del os.environ["GUI_DRYRUN_EXIT"]
check("a failing job reports failed + its exit code", done["status"] == "failed" and done["exit_code"] == 3, f"{done}")
s, _, _, _ = req("GET", "/api/jobs/0123456789abcdef")
s2, _, _, _ = req("GET", "/api/jobs/../../etc")
check("unknown job id: 404", s == 404 and s2 == 404, f"{s} {s2}")
os.rename(PATHS.plan, PATHS.plan + ".off")
s, j, _, _ = req("POST", "/api/fund/go", {"confirm": True})
os.rename(PATHS.plan + ".off", PATHS.plan)
check("funding before any setup: 409 (save a setup first)", s == 409 and "setup" in j["error"], f"{s}")

# ------------------------------------------------------------------ 11. Telegram
print("\n11. Telegram")
s, j, _, _ = req("GET", "/api/telegram")
check("telegram: bot set, no chat yet (set / not set only)", s == 200 and j["token_set"] and not j["chat_set"] and
      j["state"] in ("unpaired", "waiting"), f"{j}")
s, j, _, _ = req("POST", "/api/telegram", {"token": "garbage"})
check("a malformed bot token refused", s == 400, f"{s}")
s, j, b, _ = req("POST", "/api/telegram", {"token": NEW_TG})
envtxt = open(PATHS.env).read()
check("new bot token: saved to .env (0600), pairing code returned, token not echoed", s == 200 and j["code"].isdigit() and
      len(j["code"]) == 6 and f"ALERTS_TELEGRAM_BOT_TOKEN={NEW_TG}" in envtxt and NEW_TG.encode() not in b and
      stat.S_IMODE(os.stat(PATHS.env).st_mode) == 0o600 and f"/start {j['code']}" in j["message"], f"{s}")
code = j["code"]
s, j, _, _ = req("GET", "/api/telegram")
check("telegram: now waiting for /start <code>", j["state"] == "waiting" and j["waiting_code"] == code, f"{j}")
s, j, _, _ = req("POST", "/api/telegram", {"token": ""})
check("empty token: a new pairing code for the current bot", s == 200 and j["code"] != code and
      E.pairing_code(PATHS.pair) == j["code"], f"{s}")
check("the other lines in .env are untouched", "OTHER_SETTING=keep-me-123456" in open(PATHS.env).read())

# ------------------------------------------------------------------ 12. volume
print("\n12. volume")
s, j, _, _ = req("GET", "/api/volume")
check("volume: status from lib.volume, markets, strategy of each market", s == 200 and j["available"] and
      j["markets"] == list(VOL.MARKETS) and j["status"]["today"]["USDC.arb/USDC.eth"]["volume_usd"] == 101.5 and
      j["strategies"]["USDC.arb/USDC.eth"] == "mm_usdc_usdc", f"{s}")
cfg = copy.deepcopy(VOL.cfg); cfg["burst_every_min"] = 1
s, j, _, _ = req("POST", "/api/volume/config", {"config": cfg})
check("volume config: lib.volume's ValueError becomes a 400 with its message", s == 400 and "burst_every_min" in j["error"], f"{s}")
cfg["burst_every_min"] = 30; cfg["enabled"] = True; cfg["markets"]["USDC.arb/USDC.eth"]["daily_usd"] = 2000.0
s, j, _, _ = req("POST", "/api/volume/config", {"config": cfg})
check("volume config saved through save_config()", s == 200 and CALLS["volume"][-1] == ("save", cfg), f"{s}")
s, j, _, _ = req("POST", "/api/volume/start", {"market": "USDC.arb/USDC.eth", "usd": 50})
check("volume run refused without confirm", s == 400 and not any(c[0] == "start" for c in CALLS["volume"]), f"{s}")
s, j, _, _ = req("POST", "/api/volume/start", {"market": "DOGE/USD", "usd": 50, "confirm": True})
s2, j2, _, _ = req("POST", "/api/volume/start", {"market": "USDC.arb/USDC.eth", "usd": 0, "confirm": True})
check("unknown market / zero amount refused", s == 400 and s2 == 400, f"{s} {s2}")
s, j, _, _ = req("POST", "/api/volume/start", {"market": "USDC.arb/USDC.eth", "usd": "50", "confirm": True})
check("volume run started: start_run(market, 50.0, None) → pid", s == 200 and j["pid"] == 4242 and
      CALLS["volume"][-1] == ("start", "USDC.arb/USDC.eth", 50.0, None), f"{s} {CALLS['volume'][-1:]}")
s, j, _, _ = req("POST", "/api/volume/start", {"market": "BTC/USDC.arb", "usd": 10, "size": 0.001, "confirm": True})
check("a second run while one is active: 409", s == 409 and "already active" in j["error"], f"{s}")
s, j, _, _ = req("GET", "/api/volume")
check("running run visible with its progress fields", j["status"]["running"]["market"] == "USDC.arb/USDC.eth")
s, j, _, _ = req("POST", "/api/fund/go", {"confirm": True})
check("opening channels is refused while a volume run is active", s == 409 and "volume run" in j["error"], f"{s}")
s, j, _, _ = req("POST", "/api/volume/stop", {})
check("stop: stop_run() called", s == 200 and j["stopping"] is True and CALLS["volume"][-1] == ("stop",), f"{s}")
sys.modules["lib.volume"] = None                        # as if lib/volume.py were missing
s, j, _, _ = req("GET", "/api/volume")
s2, j2, _, _ = req("POST", "/api/volume/start", {"market": "USDC.arb/USDC.eth", "usd": 5, "confirm": True})
sys.modules["lib.volume"] = VOL
check("without lib/volume.py: 'not available' (GET 200, actions 503)", s == 200 and j["available"] is False and s2 == 503, f"{s} {s2}")

# ------------------------------------------------------------------ 13. hydra_mm import hygiene
print("\n13. doctor via tools/hydra_mm.py")
env_before, cwd_before = dict(os.environ), os.getcwd()
try:
    hm = G._hydra_mm()
    ok, why = callable(getattr(hm, "doctor_steps", None)), ""
except Exception as ex:
    ok, why = False, f"{type(ex).__name__}: {ex}"
check("hydra_mm imported for doctor_steps; its .env loading and chdir are undone", ok and dict(os.environ) == env_before
      and os.getcwd() == cwd_before, why)

# ------------------------------------------------------------------ 14. setup flow (stepper, banner, invite, funding)
print("\n14. setup flow")
HINT_NODE = "docker compose up -d   (then wait a minute: docker compose logs -f node)"
HINT_INV = "hydra-mm invite <CODE>   (an existing user mints one; or ask the team to whitelist abcd)"
D = {
    "node_down": {"ok": False, "what": "node not reachable (failed to connect to all addresses)", "hint": HINT_NODE},
    "node": {"ok": True, "what": "node answers (1:f9beb4d9, 2:1, 2:42161)", "hint": HINT_NODE},
    "invite": {"ok": False, "what": "node is waiting for a mainnet invite (mainnet is invite-gated)", "hint": HINT_INV},
    "admitted": {"ok": True, "what": "admitted to mainnet (12 markets)", "hint": HINT_INV},
    "hub": {"ok": True, "what": "connected to the Hydranet hub on every chain", "hint": "hydra-mm peers   (retry; the hub is always on port 443)"},
    "hub_bad": {"ok": False, "what": "hub not reachable: {'bitcoin': 'timeout'}", "hint": "hydra-mm peers   (retry; the hub is always on port 443)"},
    "conf": {"ok": True, "what": "configured", "hint": "hydra-mm setup   (or unattended: hydra-mm setup --budget 500 --yes)"},
    "noconf": {"ok": False, "what": "not configured yet", "hint": "hydra-mm setup   (or unattended: hydra-mm setup --budget 500 --yes)"},
    "unfunded": {"ok": False, "what": "channels hold $120 of $1,600 planned", "hint": "hydra-mm fund   (shows what to send where; then hydra-mm fund --go)"},
    "funded": {"ok": True, "what": "channels funded ($1,600 of $1,600 planned)", "hint": "hydra-mm fund   (shows what to send where; then hydra-mm fund --go)"},
    "quotes": {"ok": True, "what": "18 own quotes on the book", "hint": "wait a minute after a (re)start; else docker compose logs --tail 50 bot"},
    "noquotes": {"ok": False, "what": "no quotes on the book yet", "hint": "wait a minute after a (re)start; else docker compose logs --tail 50 bot"},
    "paused": {"ok": False, "what": "paused (state/pause) — hydra-mm resume", "hint": "hydra-mm resume"},
    "tg": {"ok": True, "what": "Telegram off (optional)", "hint": ""},
}
def doctor(*keys):
    G.doctor_steps = lambda: [dict(D[k]) for k in keys]
    s, j, _, _ = req("GET", "/api/setup/state?fresh=1")
    return s, j
E.identity_key = lambda: "ab12cd34" * 8
ACKS = os.path.join(TMP, "state", "gui_setup.json")

s, j = doctor("node_down")
check("node down: stepper on 'node', not complete, red banner pointing at setup", s == 200 and j["node"] == "offline" and
      j["current"] == "node" and not j["complete"] and j["attention"]["level"] == "bad" and j["attention"]["stage"] == "node"
      and j["identity"] is None, f"{j and (j['node'], j['current'], j['attention'])}")
s, o, _, _ = req("GET", "/api/overview")
check("overview carries the setup summary (home = setup while incomplete)", s == 200 and o["setup"]["complete"] is False and
      o["setup"]["current"] == "node" and o["setup"]["attention"]["key"] == "node")
s, j = doctor("node", "invite")
check("waiting for an invite: node state 'invite', the identity key to share", j["node"] == "invite" and j["identity"] == "ab12cd34" * 8
      and j["stages"]["node"] == "action" and "invite" in j["attention"]["text"], f"{j['node']} {j['attention']}")
s, j = doctor("node", "admitted", "hub_bad")
check("hub not reachable: node state 'hub', banner offers a re-check", j["node"] == "hub" and j["attention"]["action"] == "recheck")

os.rename(PATHS.plan, PATHS.plan + ".off")
s, j = doctor("node", "admitted", "hub", "noconf")
check("no plan yet, backup not confirmed: the stepper asks for the backup first", j["node"] == "ok" and not j["configured"] and
      j["stages"]["plan"] == "todo" and j["current"] == "backup" and j["attention"]["key"] == "plan", f"{j['current']}")
s, j, _, _ = req("POST", "/api/setup/ack", {"item": "backup", "value": True})
ack_file = json.load(open(ACKS)) if os.path.exists(ACKS) else {}
check("backup acknowledgement stored in state/ (not only in the browser)", s == 200 and j["acks"]["backup"]["at"] > 0 and
      (ack_file.get("backup") or {}).get("at", 0) > 0, f"{s} {ack_file}")
s, j = doctor("node", "admitted", "hub", "noconf")
check("after the backup: the plan is next", j["stages"]["backup"] == "done" and j["current"] == "plan", j["current"])
bad = [b for b in ({"item": "seed", "value": True}, {"item": "backup", "value": "yes"}, {}) if req("POST", "/api/setup/ack", b)[0] != 400]
check("bad acknowledgements refused (unknown item, non-boolean)", not bad, f"{bad}")
os.rename(PATHS.plan + ".off", PATHS.plan)

s, j = doctor("node", "admitted", "hub", "conf", "unfunded")
check("plan saved, Telegram pairing started: Telegram is next (waiting for /start)", j["configured"] and
      j["stages"]["plan"] == "done" and j["stages"]["telegram"] == "waiting" and j["current"] == "telegram", f"{j['current']}")
s, _, _, _ = req("POST", "/api/setup/ack", {"item": "telegram_skipped", "value": True})
s, j = doctor("node", "admitted", "hub", "conf", "unfunded")
check("Telegram skipped: funding is next, banner says what the channels hold", j["stages"]["telegram"] == "skipped" and
      j["current"] == "funding" and j["stages"]["funding"] == "action" and
      j["attention"]["text"] == "Fund your node: channels hold $120 of $1,600 planned." and j["attention"]["stage"] == "funding",
      f"{j['current']} {j['attention']}")
s, j = doctor("node", "admitted", "hub", "conf", "funded", "noquotes", "tg")
check("funded, no quotes yet: the last step says 'starting'", j["current"] == "done" and j["stages"]["done"] == "starting"
      and not j["complete"] and j["attention"]["key"] == "quotes", f"{j['stages']['done']}")
s, j = doctor("node", "admitted", "hub", "conf", "funded", "quotes", "tg")
s2, o, _, _ = req("GET", "/api/overview")
check("all checks pass: setup complete, no banner, dashboard is home", j["complete"] and j["current"] == "done" and
      j["attention"] is None and o["setup"]["complete"] and j["progress"]["done"] == 6, f"{j['progress']} {j['attention']}")
req("POST", "/api/setup/ack", {"item": "backup", "value": False})
s, j = doctor("node", "admitted", "hub", "conf", "funded", "quotes", "tg")
check("complete but the backup unconfirmed: a gentle (info) banner about the recovery phrase", j["complete"] and
      j["attention"]["key"] == "backup" and j["attention"]["level"] == "info" and j["acks"]["backup"] is None)
req("POST", "/api/setup/ack", {"item": "backup", "value": True})
open(os.path.join(TMP, "state", "pause"), "w").close()
s, j = doctor("node", "admitted", "hub", "conf", "funded", "paused", "tg")
os.remove(os.path.join(TMP, "state", "pause"))
check("paused by the user: still set up (home stays the dashboard), banner offers Resume", j["complete"] and j["paused"]
      and j["attention"]["action"] == "resume", f"{j['attention']}")
def _boom():
    raise RuntimeError("prices unavailable")
G.doctor_steps = _boom
s, j, _, _ = req("GET", "/api/setup/state?fresh=1")
check("one failed check run keeps the last good result (and reports the error)", j["node"] == "ok" and j["configured"] and
      "prices unavailable" in (j["error"] or ""), f"{j['node']} {j['error']}")
SRV.api.doctor.ok_at -= G.STALE_AFTER + 1                # the last good result is now too old
s, j, _, _ = req("GET", "/api/setup/state?fresh=1")
check("a lasting failure shows: node step 'error', banner offers a re-check", j["node"] == "error" and not j["complete"] and
      j["attention"]["action"] == "recheck" and "prices unavailable" in j["attention"]["text"], f"{j['attention']}")

# invite: the code goes through the environment only
bad = [c for c in ("", "has space", "line\nbreak", "x" * 300, 12345) if req("POST", "/api/invite", {"code": c})[0] != 400]
check("malformed invite codes refused", not bad, f"{bad}")
s, j, _, _ = req("POST", "/api/invite", {"code": INVITE})
done = wait_job(j["job"]["id"]) if s == 202 else {}
rec = fake_record("invite_")
check("invite job: `hydra-mm invite -`, the code only in HYDRA_INVITE_CODE", s == 202 and done.get("status") == "done" and rec and
      rec["args"] == ["invite", "-"] and rec["env"]["HYDRA_INVITE_CODE"] == INVITE and INVITE not in " ".join(rec["args"]), f"{rec}")
check("invite code blanked out of the job output and the command line", INVITE not in done.get("output", "") and
      "careless echo: redeeming ••••••" in done.get("output", "") and done.get("command") == "hydra-mm invite -")
hm = G._hydra_mm()
got = []
saved = (E.node_ready, E.booted, E.redeem_invite, E.connect)
E.node_ready, E.booted = (lambda timeout=5.0: (True, "")), (lambda timeout=5.0: False)
E.redeem_invite = lambda code: got.append(code) or "ab" * 32
os.environ["HYDRA_INVITE_CODE"] = "  ENV-CODE-123  "
out = io.StringIO()
_stdout, sys.stdout = sys.stdout, out
try:
    hm.cmd_invite(types.SimpleNamespace(code="-", timeout=0))
    hm.cmd_invite(types.SimpleNamespace(code="ARG-CODE-456", timeout=0))
    del os.environ["HYDRA_INVITE_CODE"]
    try:
        hm.cmd_invite(types.SimpleNamespace(code="-", timeout=0))
        refused = False
    except SystemExit as ex:
        refused = "HYDRA_INVITE_CODE" in str(ex)
finally:
    sys.stdout = _stdout
    E.node_ready, E.booted, E.redeem_invite, E.connect = saved
check("hydra-mm invite: `-` reads HYDRA_INVITE_CODE (trimmed), a code argument still works, `-` without it is refused",
      got == ["ENV-CODE-123", "ARG-CODE-456"] and refused, f"{got} {refused}")

# funding status: per chain, what to send and whether it has arrived
SRV.api.cache.clear()
s, f, _, _ = req("GET", "/api/funding")
chains = {c["chain"]: c for c in (f or {}).get("chains", [])}
exp = {}
for a, v in plan0.assets.items():
    in_ch, miss = E.capacity(None).get(a, {}).get("send", 0.0), gaps.get(a, 0.0)
    exp[a] = "in_channels" if in_ch >= v["own"] * 0.9 else ("arrived" if miss <= 1e-9 else "waiting")
got = {i["asset"]: i["state"] for c in chains.values() for i in c["items"]}
check("funding status: one card per chain with its address, each asset waiting / arrived / in channels", s == 200 and
      set(chains) == {PL.CHAIN[a] for a in plan0.assets} and got == exp and chains["arbitrum"]["address"] == "0xArbDryRun"
      and f["waiting"] == sum(1 for v in exp.values() if v == "waiting") and f["all_arrived"] is False and f["checked_at"] > 0,
      f"{got} vs {exp}")
check("a chain is 'waiting' while any of its assets is", all((c["state"] == "waiting") == any(i["state"] == "waiting" for i in c["items"])
      for c in chains.values()))
_wallet = E.wallet_onchain
E.wallet_onchain = lambda c: {a: v["own"] for a, v in plan0.assets.items()}
SRV.api.cache.clear()
s, f, _, _ = req("GET", "/api/funding")
check("everything arrived in the wallet: ready to open the channels", f["all_arrived"] and f["ready_to_open"] and f["waiting"] == 0
      and all(c["state"] in ("arrived", "in_channels") for c in f["chains"]), f"{[c['state'] for c in f['chains']]}")
E.wallet_onchain = _wallet
fee_calls = []
def _estimate(r):
    fee_calls.append(r["chain"])
    if r["chain"] == "ethereum":
        return None, "InsufficientAllowance: spender=0x" + "1" * 40 + " current=0"
    return 0.42, None
E.estimate_channel = _estimate
SRV.api.cache.clear()
s, j, _, _ = req("GET", "/api/funding/fees")
reqs = {r["chain"]: r for r in (j or {}).get("requests", [])}
check("fee preview: per chain what is deposited and leased, the fee and who pays it (read-only estimates)", s == 200 and
      reqs and reqs.get("arbitrum", {}).get("fee") == 0.42 and reqs["arbitrum"]["how"] and
      any(a["lease"] > 0 for a in reqs["arbitrum"]["assets"]) and set(fee_calls) == set(reqs), f"{s} {list(reqs)}")
check("an allowance still to approve is not an error (the fee is known after the approval)",
      "ethereum" not in reqs or (reqs["ethereum"]["fee"] is None and reqs["ethereum"]["error"] is None))
os.rename(PATHS.plan, PATHS.plan + ".off")
s, _, _, _ = req("GET", "/api/funding/fees")
os.rename(PATHS.plan + ".off", PATHS.plan)
check("fee preview before any plan: 409", s == 409, f"{s}")

# ------------------------------------------------------------------ 15. secrets never leave
print("\n15. secrets")
leaks = [s for s in ALL_SECRETS if any(s.encode() in b for b in BODIES)]
check(f"no secret in any of the {len(BODIES)} API/page responses", not leaks, f"{leaks}")
log = LOG.text()
check("no secret in the server log; request bodies are never logged", not any(s in log for s in ALL_SECRETS) and
      "careless echo" not in log)
check("no CORS header on any response", not any(k.startswith("access-control") for h in HEADERS for k in h))
check("no cookie set on any response", not any("set-cookie" in h for h in HEADERS))
check("no unexpected server errors logged", "Traceback" not in log, log[log.find("Traceback"):][:400] if "Traceback" in log else "")

SRV.shutdown()
sys.stderr = sys.__stderr__
passed = sum(RESULTS)
print(f"\n{passed}/{len(RESULTS)} checks passed")
sys.exit(0 if passed == len(RESULTS) else 1)
