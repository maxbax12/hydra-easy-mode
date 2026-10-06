"""Building blocks for easy mode, shared by tools/hydra_mm.py and tools/tg_control.py.

Everything here talks to the local node (gRPC, localhost / the compose network)
or reads the bot's own files; nothing needs the user to know about channels,
allowances or peers.
"""
import datetime as dt
import glob
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple

import yaml

from lib.planner import CHAIN, Plan

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

NETWORKS = {  # chain -> (protocol, network id)
    "bitcoin": (1, "f9beb4d9"),
    "ethereum": (2, "1"),
    "arbitrum": (2, "42161"),
}
ASSET_IDS = {
    "BTC": "0x" + "0" * 64,
    "ETH": "0x" + "0" * 40,
    "USDC.eth": "erc20:0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",
    "USDC.arb": "erc20:0xaf88d065e77c8cc2239327c5edb3a432268e5831",
}
# Funding order: Arbitrum first — its USDC.arb channel pays the fees of requests
# that have nothing to deposit (and of every Bitcoin lease).
CHAIN_ORDER = ("arbitrum", "ethereum", "bitcoin")
# The Hydranet hub on each mainnet chain (docs.hydranet.ai → Setup Guide → Mainnet peers).
# The port is always 443: the endpoints sit behind Cloudflare.
HUB_PEERS = {
    "bitcoin": "03eaace825811aee04ea8e56d31a8927104d5f301ff2aa8998cc431903ec26289a@wss://lightning-ws.hydranet.app:443",
    "ethereum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3@wss://ethereum-ws.hydranet.app:443",
    "arbitrum": "0x53f1067c4d85000f24d8019b8d676fbf7f321ca3@wss://arbitrum-ws.hydranet.app:443",
}
LEASE_MIN_USD = 10.0
LEASE_MAX_SECS = 7 * 24 * 3600


def network(chain: str):
    from lib.hydra_pb import primitives_pb2 as P
    proto, nid = NETWORKS[chain]
    return P.Network(protocol=proto, id=nid)


def connect():
    """Node client (HYDRA_HOST / HYDRA_PORT, default localhost:5008)."""
    from lib.grpc_client import HydraGRPCClient
    c = HydraGRPCClient(host=os.getenv("HYDRA_HOST", "localhost"), port=int(os.getenv("HYDRA_PORT", "5008")))
    c.connect()
    c._log = lambda *a, **k: None
    return c


def liquidity_stub():
    import grpc
    from lib.hydra_pb import liquidity_pb2_grpc as LG
    return LG.LiquidityServiceStub(grpc.insecure_channel(
        f"{os.getenv('HYDRA_HOST', 'localhost')}:{os.getenv('HYDRA_PORT', '5008')}"))


def _channel():
    import grpc
    return grpc.insecure_channel(f"{os.getenv('HYDRA_HOST', 'localhost')}:{os.getenv('HYDRA_PORT', '5008')}")


def node_ready(timeout: float = 5.0) -> Tuple[bool, str]:
    """(True, networks) once the node answers GetNetworks."""
    import grpc
    from lib.hydra_pb import app_pb2, app_pb2_grpc
    try:
        r = app_pb2_grpc.AppServiceStub(_channel()).GetNetworks(app_pb2.GetNetworksRequest(), timeout=timeout)
        return True, ", ".join(f"{n.protocol}:{n.id}" for n in r.networks)
    except grpc.RpcError as e:
        return False, (e.details() or str(e.code()))[:120]


def booted(timeout: float = 5.0) -> bool:
    """The node finished starting (its networks are up). A node whose identity is not
    admitted yet answers its API but reports no networks until an invite is redeemed."""
    ok, nets = node_ready(timeout)
    return ok and bool(nets)


def waiting_for_invite(timeout: float = 5.0) -> bool:
    ok, nets = node_ready(timeout)
    return ok and not nets


def identity_key() -> str:
    """The node's Ed25519 identity (what an invite admits / the team whitelists)."""
    from lib.hydra_pb import app_pb2, app_pb2_grpc
    r = app_pb2_grpc.AppServiceStub(_channel()).GetPublicKey(app_pb2.GetPublicKeyRequest(), timeout=10)
    k = getattr(r, "public_key", b"") or getattr(r, "key", b"")
    return k.hex() if isinstance(k, (bytes, bytearray)) else str(k)


def redeem_invite(code: str) -> str:
    """Redeem a mainnet invite code (mainnet is invite-gated). Returns the inviter's key."""
    from lib.hydra_pb import app_pb2, app_pb2_grpc
    r = app_pb2_grpc.AppServiceStub(_channel()).RedeemInvite(app_pb2.RedeemInviteRequest(code=code.strip()), timeout=30)
    return r.referral_public_key.hex()


def create_invite() -> str:
    """Mint an invite code for a new user (needs referral_config in the node config)."""
    from lib.hydra_pb import app_pb2, app_pb2_grpc
    return app_pb2_grpc.AppServiceStub(_channel()).CreateInvite(app_pb2.CreateInviteRequest(), timeout=30).code


def admitted(client) -> Tuple[bool, str]:
    """Is this identity past the mainnet gate? The orderbook only answers admitted nodes."""
    try:
        n = len(client.get_markets_info() or [])
        return (n > 0, f"{n} markets" if n else "no markets visible (not admitted yet?)")
    except Exception as e:
        return False, f"{type(e).__name__}: {str(e)[:100]}"


def connect_peers(client) -> Dict[str, str]:
    """Connect to the hub on every chain (idempotent). {chain: 'ok' | 'already' | error}."""
    import grpc
    from lib.hydra_pb import node_pb2
    out = {}
    for chain, url in HUB_PEERS.items():
        pub = url.split("@")[0]
        try:
            if any(pub.lower() in str(p).lower() for p in client.get_connected_peers(network(chain))):
                out[chain] = "already"
                continue
            client.node_stub.ConnectToPeer(node_pb2.ConnectToPeerRequest(network=network(chain), peer_url=url), timeout=60)
            out[chain] = "ok"
        except grpc.RpcError as e:
            out[chain] = explain_error(e.details() or str(e.code()))
        except Exception as e:
            out[chain] = f"{type(e).__name__}: {str(e)[:80]}"
    return out


def new_seed(words: int = 24) -> str:
    """A fresh BIP-39 recovery phrase (english)."""
    from mnemonic import Mnemonic
    return Mnemonic("english").generate(strength={12: 128, 24: 256}[words])


def write_node_env(path: str, mnemonic: str, password: str = "") -> str:
    """node/.env for daemon mode (MNEMONIC + PASSWORD, 0600). Refuses to overwrite a wallet.

    PASSWORD stays EMPTY by default: the node derives its identity from the seed AND
    the password, and the Hydra web app (where invites are redeemed) has no password —
    with one set, the node's identity would differ from the admitted one."""
    if os.path.exists(path) and "MNEMONIC=" in open(path).read() and \
            open(path).read().split("MNEMONIC=", 1)[1].split("\n", 1)[0].strip():
        raise FileExistsError(f"{path} already holds a wallet seed — not overwriting it")
    words = mnemonic.split()
    if len(words) not in (12, 24):
        raise ValueError("a recovery phrase has 12 or 24 words")
    set_env(path, {"MNEMONIC": " ".join(words), "RUST_LOG": "info"})
    with open(path, "a") as f:
        f.write(f"PASSWORD={password}\n")        # set_env drops empty values; keep the key
    return password


def prices() -> Dict[str, float]:
    """BTC and ETH in USD from MEXC's public tickers (no API key needed)."""
    import ccxt
    t = ccxt.mexc().fetch_tickers(["BTC/USDC", "ETH/USDC"])
    return {"BTC": float(t["BTC/USDC"]["last"]), "ETH": float(t["ETH/USDC"]["last"])}


def _v(d) -> float:
    return float(d.value) if getattr(d, "value", "") else 0.0


def wallet_onchain(client) -> Dict[str, float]:
    """Confirmed on-chain balance per asset in the node wallet (what can be deposited)."""
    out = {}
    for asset, chain in CHAIN.items():
        try:
            bals = client.get_balances(network(chain)) or {}
        except Exception:
            continue
        for aid, b in bals.items():
            if aid.lower() == ASSET_IDS[asset]:
                out[asset] = _v(b.onchain.confirmed)
    return out


def native_balances(client) -> Dict[str, float]:
    """Confirmed native coin per EVM chain (gas for approvals / native deposits)."""
    out = {}
    for chain in ("ethereum", "arbitrum"):
        try:
            for aid, b in (client.get_balances(network(chain)) or {}).items():
                if aid.lower() == "0x" + "0" * 40:
                    out[chain] = _v(b.onchain.confirmed)
        except Exception:
            pass
    return out


def capacity(client) -> Dict[str, Dict[str, float]]:
    """Channel capacity per asset: free and total (free + held by quotes) to send / receive."""
    ids = {(NETWORKS[CHAIN[a]][1], i): a for a, i in ASSET_IDS.items()}
    out = {}
    for b in client.get_orderbook_balances():
        a = ids.get((b.currency.network_id, b.currency.asset_id.lower()))
        if not a:
            continue
        x = b.balance
        cur = out.setdefault(a, {"send_free": 0.0, "send": 0.0, "recv_free": 0.0, "recv": 0.0})
        cur["send_free"] += _v(x.sending)
        cur["send"] += _v(x.sending) + _v(x.in_use_sending)
        cur["recv_free"] += _v(x.receiving)
        cur["recv"] += _v(x.receiving) + _v(x.in_use_receiving)
    return out


def deposit_addresses(client) -> Dict[str, str]:
    import grpc
    from lib.hydra_pb import wallet_pb2, wallet_pb2_grpc
    stub = wallet_pb2_grpc.WalletServiceStub(grpc.insecure_channel(
        f"{os.getenv('HYDRA_HOST', 'localhost')}:{os.getenv('HYDRA_PORT', '5008')}"))
    out = {}
    for chain in NETWORKS:
        try:
            out[chain] = stub.GetDepositAddress(wallet_pb2.GetDepositAddressRequest(network=network(chain))).address
        except grpc.RpcError as e:
            out[chain] = f"(unavailable: {e.details()[:60]})"
    return out


def funding_state(p: Plan, cap: Dict[str, Dict[str, float]], px: Dict[str, float]):
    """(funded, have_usd, plan_usd, empty_assets). A market maker's value moves between its
    two assets as it trades, so funding is judged by the TOTAL; an asset only counts as
    unfunded when (almost) nothing of it is in the channels."""
    px = {"USDC.arb": 1.0, "USDC.eth": 1.0, **(px or {})}
    have = sum(cap.get(a, {}).get("send", 0.0) * px.get(a, 1.0) for a in p.assets)
    plan = sum(v["own_usd"] for v in p.assets.values())
    empty = [a for a, v in p.assets.items() if cap.get(a, {}).get("send", 0.0) < v["own"] * 0.1]
    return (not empty and have >= plan * 0.9), have, plan, empty


def funding_gaps(p: Plan, wallet: Dict[str, float], cap: Dict[str, Dict[str, float]]) -> Dict[str, float]:
    """What still has to reach the node wallet before the channels can be funded (asset units)."""
    gaps = {}
    for a, need in p.assets.items():
        have_channel = cap.get(a, {}).get("send", 0.0)
        missing = need["own"] - have_channel - wallet.get(a, 0.0)
        if missing > 1e-9:
            gaps[a] = missing
    return gaps


# ------------------------------------------------------------------- channels
def channel_requests(p: Plan, px: Dict[str, float], cap: Dict[str, Dict[str, float]],
                     lease_hours: int = 168, hub: Optional[Dict[str, str]] = None,
                     gas: Optional[Dict[str, float]] = None) -> List[Dict]:
    """One liquidity request per chain: our deposit (client_amount) + leased inbound
    (server_amount) for every asset on it, in a single OpenOrDeposit — the hub opens
    the channel if there is none, else deposits into it. Only what is still missing
    compared to the current channel capacity is requested."""
    reqs = []
    for chain in CHAIN_ORDER:
        assets = {}
        for a, need in p.assets.items():
            if CHAIN[a] != chain:
                continue
            have = cap.get(a, {})
            own = max(0.0, need["own"] - have.get("send", 0.0))
            inbound = max(0.0, need["inbound"] - have.get("recv", 0.0))
            if inbound > 0 and inbound * px.get(a, 1.0) < LEASE_MIN_USD:
                inbound = LEASE_MIN_USD / px.get(a, 1.0) * 1.01
            if own > 1e-9 or inbound > 1e-9:
                assets[a] = {"own": own, "inbound": inbound}
        if assets:
            # No gas on an EVM chain: token deposits go the sponsored (permit) way.
            no_gas = gas is not None and chain in GAS_MIN and gas.get(chain, 0.0) < GAS_MIN[chain]
            reqs.append({"chain": chain, "assets": assets, "hours": lease_hours,
                         "hub": (hub or {}).get(chain, ""), "no_gas": no_gas})
    return reqs


def hub_nodes() -> Dict[str, str]:
    """The liquidity service's node per chain (the only peer the node needs)."""
    from lib.hydra_pb import liquidity_pb2 as L
    info = liquidity_stub().GetLiquidityServiceInfo(L.GetLiquidityServiceInfoRequest())
    by_id = {nid: chain for chain, (_, nid) in NETWORKS.items()}
    return {by_id[n.network.id]: n.node_pubkey for n in info.node_pubkeys if n.network.id in by_id}


def _amount(a: str, x: float) -> str:
    return f"{x:.8f}" if a == "BTC" else f"{x:.6f}" if a.startswith("USDC") else f"{x:.18f}".rstrip("0")


NATIVE = {"BTC", "ETH"}
GAS_MIN = {"ethereum": 0.0005, "arbitrum": 0.00005}   # native coin for one approval transaction


def fee_mode(req: Dict) -> Tuple[str, str]:
    """(mode, payment asset) for the liquidity-service request of one chain.

    dual       our token deposit + leased inbound in one round; the fee comes out of the
               deposit. Needs an ERC-20 approval first, i.e. a little gas in the wallet.
    sponsored  our token deposit alone, authorised by a permit the node signs — no gas at
               all. The service refuses inbound in the same request, so the lease follows
               as its own request (fee paid off-chain from the USDC.arb channel).
    offchain   nothing (token) to deposit: lease only, fee paid off-chain from USDC.arb.
    Native coins can't ride the service's transaction and Bitcoin has no dual funding:
    those deposits go in with our own DepositChannel."""
    if req["chain"] != "bitcoin":
        for a, v in req["assets"].items():
            if v["own"] > 0 and a not in NATIVE:
                return ("sponsored" if req.get("no_gas") else "dual"), a
    return "offchain", "USDC.arb"


def _liquidity_request(req: Dict):
    from lib.hydra_pb import liquidity_pb2 as L, primitives_pb2 as P
    mode, pay = fee_mode(req)
    if req.get("step") == "lease":
        mode, pay = "offchain", "USDC.arb"               # second step of the sponsored path
    # Native coins (and all of Bitcoin): lease only here; our own deposit goes in
    # afterwards with DepositChannel (see open_channel). Sponsored: deposit only.
    client = lambda a, v: 0.0 if (a in NATIVE or mode not in ("dual", "sponsored")) else v["own"]
    server = lambda a, v: 0.0 if mode == "sponsored" else v["inbound"]
    liq = {ASSET_IDS[a]: L.AssetLiquidity(server_amount=P.DecimalString(value=_amount(a, server(a, v))),
                                          client_amount=P.DecimalString(value=_amount(a, client(a, v))))
           for a, v in req["assets"].items() if server(a, v) > 0 or client(a, v) > 0}
    # target_node_pubkey is an optional field: leave it UNSET so the service picks its
    # own node (set — even to the hub's key — the estimate fails for inbound-only requests).
    op = L.ChannelLiquidityRequestOperation(open_or_deposit=L.ChannelLiquidityRequestOperation.OpenOrDeposit(
        asset_liquidity=liq))
    kw = {"dual_fund_fee_payment": L.DualFundFeePayment()} if mode == "dual" else \
         {"sponsored_deposit_fee_payment": L.SponsoredDepositFeePayment()} if mode == "sponsored" else \
         {"offchain_fee_payment": L.OffchainFeePayment()}
    return L.RequestChannelLiquidityRequest(
        network=network(req["chain"]), operation=op, lease_duration_seconds=req["hours"] * 3600,
        payment_network=network(CHAIN[pay]), payment_asset_id=ASSET_IDS[pay], **kw)


def needs_request(req: Dict) -> bool:
    """A liquidity-service request is needed for inbound, or for a token deposit
    (which it makes for us). Native deposits alone go straight to DepositChannel."""
    return any(v["inbound"] > 0 or (v["own"] > 0 and a not in NATIVE) for a, v in req["assets"].items())


def native_deposits(req: Dict) -> Dict[str, float]:
    return {a: v["own"] for a, v in req["assets"].items() if a in NATIVE and v["own"] > 0}


def deposit_native(client, chain: str, channel_id: str, asset: str, amount: float) -> str:
    """Deposit our own on-chain BTC/ETH into a channel (our own transaction pays the gas)."""
    from lib.hydra_pb import node_pb2, balance_pb2, fee_pb2, primitives_pb2 as P
    amt = {ASSET_IDS[asset]: balance_pb2.DepositAmount(exact=balance_pb2.DepositAmount.Exact(
        amount=P.DecimalString(value=_amount(asset, amount))))}
    r = client.node_stub.DepositChannel(node_pb2.DepositChannelRequest(
        network=network(chain), channel_id=channel_id, asset_amounts=amt,
        fee_option=fee_pb2.FeeOption(medium=fee_pb2.FeeOption.Medium())))
    return r.txid


def hub_channel(client, chain: str) -> Optional[str]:
    """Our channel with the hub on a chain that has the most capacity (to deposit into)."""
    best, size = None, -1.0
    for ch in client.get_channels(network(chain)):
        for k, a in ch.asset_channels.items():
            b = a.balance
            tot = _v(getattr(b, "free_local", None)) + _v(getattr(b, "free_remote", None))
            if tot > size:
                best, size = ch.id, tot
    return best


def estimate_channel(req: Dict) -> Tuple[Optional[float], Optional[str]]:
    """(fee, None) or (None, reason) — the reason in plain words where we know it.
    A request that only deposits native coins needs no service call: fee 0 here."""
    import grpc
    if not needs_request(req):
        return 0.0, None
    try:
        fee = liquidity_stub().EstimateRequestChannelLiquidityFee(_liquidity_request(req)).fee.value
        return float(fee), None
    except grpc.RpcError as e:
        return None, explain_error(e.details() or str(e))


TOKEN_DECIMALS = {"USDC.eth": 6, "USDC.arb": 6, "HDN": 18}


def base_units_to_decimal(units: int, decimals: int) -> str:
    """Exact decimal string for an integer amount in base units. Never via float: a float
    keeps ~16 significant digits, so an 18-decimal token (HDN) approval comes out short and
    the node's pre-flight check refuses it (seen 2026-09-30: …926509105 vs …929665958)."""
    whole, frac = divmod(int(units), 10 ** decimals)
    return f"{whole}.{frac:0{decimals}d}".rstrip("0").rstrip(".") if decimals else str(whole)


def allowance_needed(err: str) -> Optional[Tuple[str, int]]:
    m = re.search(r"InsufficientAllowance: spender=(0x[0-9a-fA-F]{40}) current=(\d+)", err or "")
    return (m.group(1), int(m.group(2))) if m else None


def allowance_request(raw: str) -> Optional[Tuple[str, str, int]]:
    """(spender, token id, needed base units) from the node's raw pre-flight error.
    The amount includes the service fee carved out of a dual-funded deposit."""
    m = re.search(r"InsufficientAllowance: spender=(0x[0-9a-fA-F]{40}) current=\d+ needed=(\d+)", raw or "")
    t = re.search(r"allowance check failed for token (erc20:0x[0-9a-fA-F]{40})", raw or "")
    return (m.group(1), t.group(1).lower() if t else "", int(m.group(2))) if m else None


def approve(client, chain: str, asset: str, spender: str, amount) -> str:
    """Approve exactly `amount` of an ERC-20 for the channel contract (never unlimited).
    `amount`: an exact decimal string (preferred) or a number."""
    import grpc
    from lib.hydra_pb import client_pb2, client_pb2_grpc, allowance_pb2, fee_pb2, primitives_pb2 as P
    stub = client_pb2_grpc.ClientServiceStub(grpc.insecure_channel(
        f"{os.getenv('HYDRA_HOST', 'localhost')}:{os.getenv('HYDRA_PORT', '5008')}"))
    r = stub.SetTokenAllowance(client_pb2.SetTokenAllowanceRequest(
        network=network(chain), spender=spender, fee_option=fee_pb2.FeeOption(medium=fee_pb2.FeeOption.Medium()),
        allowance=allowance_pb2.SetTokenAllowance(token=allowance_pb2.SetTokenAllowance.Token(
            token_id=ASSET_IDS[asset],
            amount=allowance_pb2.AllowanceAmount(exact=allowance_pb2.AllowanceAmount.Exact(
                amount=P.DecimalString(value=amount if isinstance(amount, str) else _amount(asset, amount))))))))
    return r.txid


def open_channel(client, req: Dict, log=print, approve_wait_s: int = 600) -> Dict:
    """Fund one chain with one RequestChannelLiquidity. A token deposit first needs an
    ERC-20 approval for the channel contract: the node's pre-flight check names the
    spender and the exact amount (deposit + the fee carved out of it) — approve exactly
    that, wait until it is visible, and retry."""
    import grpc
    fee, err = estimate_channel(req)
    if err:
        return {"ok": False, "why": err}
    res = {"ok": True, "fee": fee, "channel": "", "txid": "", "deposits": {}}
    if needs_request(req):
        r, approved_units, approvals, deadline = None, 0, 0, None
        while r is None:
            try:
                r = liquidity_stub().RequestChannelLiquidity(_liquidity_request(req), timeout=1800)
            except grpc.RpcError as e:
                raw = e.details() or str(e)
                need = allowance_request(raw)
                if not need:
                    return {"ok": False, "why": explain_error(raw)}
                spender, token, units = need
                asset = next((a for a, i in ASSET_IDS.items() if i == token), None)
                if asset not in TOKEN_DECIMALS:
                    return {"ok": False, "why": f"an approval is needed for an unknown token {token}"}
                if units > approved_units:        # first time, or the fee quote moved past the headroom
                    if approvals >= 3:
                        return {**res, "ok": False, "why": "the needed approval keeps changing — run it again later"}
                    # The hub reprices its fee on every request, so the exact `needed` is stale
                    # a moment later: approve 0.5% above it (still a tight cap, never unlimited),
                    # computed in integer base units and passed as an exact decimal string.
                    units = units + max(1, units // 200)
                    amount = base_units_to_decimal(units, TOKEN_DECIMALS[asset])
                    log(f"  approving exactly {amount} {asset} for the channel contract {spender[:10]}… "
                        f"(deposit + fee)")
                    res["approve_txid"] = approve(client, req["chain"], asset, spender, amount)
                    log(f"  approve tx {res['approve_txid']} — waiting for it to confirm")
                    approved_units, approvals, deadline = units, approvals + 1, time.time() + approve_wait_s
                elif time.time() > deadline:
                    return {**res, "ok": False, "why": "the approval did not confirm in time — run it again later"}
                time.sleep(15)
        res.update(channel=r.channel_id, txid=r.txid)
        billed = getattr(r, "fee", None)
        if billed is not None and getattr(billed, "value", ""):
            res["fee"] = float(billed.value)          # settled, not quoted (newer nodes)
        if fee_mode(req)[0] == "sponsored" and any(v["inbound"] > 0 for v in req["assets"].values()):
            res["lease_pending"] = True               # hydra-mm fund --go again once the channel is active
    for asset, amount in native_deposits(req).items():
        ch = res["channel"] or hub_channel(client, req["chain"])
        if not ch:
            return {**res, "ok": False, "why": f"no {req['chain']} channel to deposit {asset} into yet — "
                                               f"run it again once it is open"}
        try:
            res["deposits"][asset] = deposit_native(client, req["chain"], ch, asset, amount)
            res["channel"] = ch
        except grpc.RpcError as e:
            return {**res, "ok": False, "why": f"the {asset} deposit failed: " + explain_error(e.details() or "")}
    return res


# ------------------------------------------------------------------- leases
def lease_extension_hours(left_s: float, want_h: int) -> int:
    """Hours a lease can be extended by: the new end may be at most 7 days from now."""
    room = int((LEASE_MAX_SECS - max(0.0, left_s)) // 3600)
    return max(0, min(want_h, room))


def explain_error(msg: str) -> str:
    """Node / liquidity-service errors in plain words (the raw text stays at the end)."""
    m = msg or ""
    inner = re.findall(r'message: "([^"]+)"', m)        # the service's own reason, if it gave one
    tail = (inner[-1] if inner else (m.strip().splitlines()[-1] if m.strip() else "")).strip(" ╰╴─▶")[:160]
    rules = [
        (r"InsufficientAllowance", "the token first needs an approval (done automatically on --go)"),
        (r"beyond the furthest a lease may reach", "a lease can end at most 7 days from now — extend by fewer hours"),
        (r"insufficient (funds|balance)|not enough", "not enough funds in the node wallet on that chain (deposit first)"),
        (r"(?i)min(imum)? capacity|below the minimum", "the amount is under the service minimum ($10 per lease)"),
        (r"(?i)unavailable|connection refused|deadline", "the node or the liquidity service is not reachable right now"),
    ]
    for rx, text in rules:
        if re.search(rx, m, re.I):
            return f"{text} ({tail})"
    return tail or "unknown error"


# ------------------------------------------------------------------- config files
def write_bot_config(p: Plan, path: str, telegram_chat: Optional[str] = None) -> Optional[str]:
    """Write bot_config.yaml from a plan; an existing file is kept as .bak-<ts>. Returns the backup."""
    backup = None
    path = os.path.realpath(path)          # in the container config/ links into the data volume
    if os.path.exists(path):
        backup = f"{path}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        os.replace(path, backup)
    doc = {"log_level": "INFO", "log_file": "trading_bot.log", "cancel_orders_on_exit": True,
           "strategies": p.entries}
    header = (f"# Generated by `hydra-mm setup` on {time.strftime('%Y-%m-%d %H:%M')}: "
              f"${p.budget_usd:,.0f}, {p.preset}.\n"
              f"# Edit freely; apply changes without a restart: touch state/reload\n")
    with open(path, "w") as f:
        f.write(header)
        yaml.safe_dump(doc, f, sort_keys=False, default_flow_style=False)
    return backup


def write_ops_config(p: Plan, path: str, lease_autorenew: bool = True, max_fee_usd: float = 10.0):
    cur = {}
    if os.path.exists(path):
        cur = yaml.safe_load(open(path)) or {}
    cur.update({"interval": cur.get("interval", 300), "fill_alerts": cur.get("fill_alerts", True),
                "digest_every": cur.get("digest_every", 300), "daily_report_hour": cur.get("daily_report_hour", 20),
                "lease_warn_hours": cur.get("lease_warn_hours", [24, 3]),
                "rebalancer": False, "capacity_need": {a: dict(v) for a, v in p.capacity.items()},
                "lease_autorenew": {"enabled": lease_autorenew, "renew_below_hours": 24, "extend_hours": 168,
                                    "max_fee_usd": max_fee_usd}})
    with open(path, "w") as f:
        yaml.safe_dump(cur, f, sort_keys=False)


def set_env(path: str, values: Dict[str, str]):
    """Add/replace KEY=value lines in an .env file (created 0600). Values never printed."""
    lines = open(path).read().splitlines() if os.path.exists(path) else []
    keys = set(values)
    lines = [l for l in lines if l.split("=", 1)[0].strip() not in keys]
    lines += [f"{k}={v}" for k, v in values.items() if v]
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)                   # also when the file already existed
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")


# ------------------------------------------------------------------- status
STATUS_RX = re.compile(r"^(\S+ \S+),\d+ - strategy\.(\S+) - INFO - 📊 ([^:]+): fair ([0-9.e-]+) \| (\d+) bids \S+ / "
                       r"(\d+) asks \S+ \| position ([-+0-9.e]+) \(([-+0-9]+%)\) \| (\d+) fills.*?est\. P&L ([-+0-9.e]+)")


def market_status(root: str = ROOT, log_file: str = "trading_bot.log", lines: int = 4000) -> List[Dict]:
    """Latest status line per market from the bot log."""
    path = os.path.join(root, log_file)
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - lines * 300))
        tail = f.read().decode("utf-8", "replace").splitlines()
    latest = {}
    for l in tail:
        m = STATUS_RX.match(l)
        if m:
            ts, name, pair, fair, bids, asks, pos, pct, fills, pnl = m.groups()
            latest[name] = {"at": ts, "name": name, "pair": pair, "fair": float(fair), "bids": int(bids),
                            "asks": int(asks), "position": float(pos), "pct": pct, "fills": int(fills),
                            "pnl": float(pnl)}
    paused_all = os.path.exists(os.path.join(root, "state", "pause"))
    for s in latest.values():
        s["paused"] = paused_all or os.path.exists(os.path.join(root, "state", f"pause_{s['name']}"))
    return list(latest.values())


def set_pause(on: bool, market: Optional[str] = None, root: str = ROOT) -> str:
    """Pause/resume every market (market=None) or one strategy. Takes effect on the next pass (seconds)."""
    d = os.path.join(root, "state")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "pause" if not market else f"pause_{market}")
    if on:
        open(path, "w").close()
    elif market is None:
        for f in glob.glob(os.path.join(d, "pause*")):
            os.remove(f)
    elif os.path.exists(path):
        os.remove(path)
    return path


def strategy_names(config_path: str) -> List[str]:
    try:
        doc = yaml.safe_load(open(config_path)) or {}
    except FileNotFoundError:
        return []
    return [e.get("name") for e in doc.get("strategies") or [] if e.get("enabled", True)]


# ------------------------------------------------------------------- order book & friends
KNOWN_ASSETS = {(NETWORKS[CHAIN[a]][1], i): a for a, i in ASSET_IDS.items()}
KNOWN_ASSETS.update({("42161", "0x" + "0" * 40): "ETH.arb",
                     ("42161", "erc20:0xb0f66bdb39acbb043308eb9dbe78f5bb47ea5430"): "HDN",
                     ("42161", "erc20:0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9"): "USDT.arb",
                     ("1", "erc20:0xdac17f958d2ee523a2206206994597c13d831ec7"): "USDT.eth",
                     ("42161", "erc20:0xff970a61a04b1ca14834a43f5de4533ebddb5cc8"): "USDC.e"})


def asset_name(cur) -> str:
    return KNOWN_ASSETS.get((cur.network_id, cur.asset_id.lower()), f"{cur.network_id}:{cur.asset_id[:10]}")


def dex_markets(client) -> Dict[str, Tuple]:
    """{'BASE/QUOTE': (base OrderbookCurrency, quote OrderbookCurrency)} for every DEX market."""
    from lib.hydra_pb import currency_pb2
    out = {}
    for m in client.get_markets_info() or []:
        b = currency_pb2.OrderbookCurrency(protocol=m.base.protocol, network_id=m.base.network_id, asset_id=m.base.asset_id)
        q = currency_pb2.OrderbookCurrency(protocol=m.quote.protocol, network_id=m.quote.network_id, asset_id=m.quote.asset_id)
        out[f"{asset_name(m.base)}/{asset_name(m.quote)}"] = (b, q)
    return out


def book(client, pair: str, cur: Optional[Tuple] = None) -> Dict[str, List[Tuple[float, float, bool]]]:
    """{'bids': [(price, base_amount, ours)], 'asks': [...]} best first, resting orders only."""
    b, q = cur or dex_markets(client)[pair]
    ob = client.get_orderbook(b, q)
    mine = set((client.get_own_orders(b, q) or {}).keys())
    bids, asks = [], []
    for oid, o in (ob.orders.items() if ob else []):
        if getattr(o, "pending_cancel", False):
            continue
        rem, px = _v(o.amount) - _v(o.matched_amount), _v(o.price)
        if rem <= 0 or px <= 0:
            continue
        if o.side == 1:                                   # buy: amount in quote
            bids.append((px, rem / px, oid in mine))
        else:                                             # sell: amount in base
            asks.append((px, rem, oid in mine))
    return {"bids": sorted(bids, key=lambda x: -x[0]), "asks": sorted(asks, key=lambda x: x[0])}


def leases() -> List[Dict]:
    """Every running lease: chain, channel, asset, paid lease end, and until when the hub keeps its liquidity."""
    from lib.hydra_pb import liquidity_pb2 as L
    out, now = [], time.time()
    s = liquidity_stub()
    for chain in NETWORKS:
        for l in s.GetLeases(L.GetLeasesRequest(network=network(chain))).leases:
            if not l.HasField("expiry") or l.expiry.seconds < now:
                continue
            liq = l.liquidity_expiry.seconds if l.HasField("liquidity_expiry") else 0
            out.append({"chain": chain, "channel": l.channel_id, "asset": KNOWN_ASSETS.get(
                (NETWORKS[chain][1], l.asset_id.lower()), l.asset_id[:12]), "lease_end": l.expiry.seconds,
                "liquidity_end": max(liq, l.expiry.seconds)})
    return sorted(out, key=lambda x: x["liquidity_end"])


FILL_RX = re.compile(r"^(\S+ \S+),\d+ - strategy\.(\S+) - INFO - 💱 ([^:]+): (buy|sell) ([0-9.e-]+) @ ([0-9.e-]+) "
                     r"\(([^)]*)\)(?: — (?:net )?position ([-+0-9.e]+))?")


def recent_fills(n: int = 5, root: str = ROOT, log_file: str = "trading_bot.log") -> List[Dict]:
    path = os.path.join(root, log_file)
    if not os.path.exists(path):
        return []
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 3_000_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    out = []
    for l in lines:
        m = FILL_RX.match(l)
        if m:
            ts, name, pair, side, amt, px, how, pos = m.groups()
            out.append({"at": ts, "pair": pair, "side": side, "amount": float(amt), "price": float(px),
                        "how": how, "position": float(pos) if pos else None})
    return out[-n:]


# ------------------------------------------------------------------- Telegram pairing
PAIR_FILE = os.path.join("state", "tg_pair.json")


def new_pairing_code(path: Optional[str] = None, ttl_s: int = 3600) -> str:
    """A one-time code: the first chat that sends `/start <code>` to the bot becomes its chat."""
    import secrets
    path = path or PAIR_FILE
    code = f"{secrets.randbelow(900000) + 100000}"
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    json.dump({"code": code, "expires": time.time() + ttl_s}, open(path, "w"))
    return code


def pairing_code(path: Optional[str] = None) -> Optional[str]:
    path = path or PAIR_FILE
    try:
        d = json.load(open(path))
    except (FileNotFoundError, ValueError):
        return None
    return d["code"] if d.get("expires", 0) > time.time() else None


def set_telegram(token: str, env_path: str = ".env", pair_path: Optional[str] = None) -> str:
    """Store the bot token (unlinking the old chat) and return a fresh pairing code."""
    set_env(env_path, {"ALERTS_TELEGRAM_BOT_TOKEN": token.strip()})
    lines = [l for l in open(env_path).read().splitlines() if not l.startswith("ALERTS_TELEGRAM_CHAT_ID=")]
    fd = os.open(env_path, os.O_WRONLY | os.O_TRUNC)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write("\n".join(lines) + "\n")
    return new_pairing_code(pair_path)


def market_daily_stats(client) -> Dict[str, Dict[str, float]]:
    """{pair: first/last/high/low/base_volume/quote_volume} over the last 24 h, from the node's
    GetMarketDailyStats (Hydra App 2026-09-30+); markets without stats are left out."""
    from lib.hydra_pb import orderbook_pb2
    out = {}
    for pair, (b, q) in dex_markets(client).items():
        try:
            r = client.orderbook_stub.GetMarketDailyStats(
                orderbook_pb2.GetMarketDailyStatsRequest(base=b, quote=q), timeout=10)
        except Exception:
            continue
        if not r.HasField("stats"):
            continue
        v = r.stats.volatility
        out[pair] = {"first": _v(v.first_price), "last": _v(v.last_price), "high": _v(v.high_price),
                     "low": _v(v.low_price), "base_volume": _v(v.base_volume), "quote_volume": _v(v.quote_volume)}
    return out


def daily_stats_lines(stats: Dict[str, Dict[str, float]]) -> List[str]:
    """Plain lines for a report: 24 h DEX volume per market (USD where the quote is USDC or BTC)."""
    btc = (stats.get("BTC/USDC.arb") or {}).get("last") or 0.0
    lines, total = [], 0.0
    for pair, s in sorted(stats.items(), key=lambda kv: -kv[1]["quote_volume"]):
        if s["quote_volume"] <= 0:
            continue
        quote = pair.split("/")[1]
        usd = s["quote_volume"] if quote.startswith("USDC") else (s["quote_volume"] * btc if quote == "BTC" else 0.0)
        total += usd
        chg = (s["last"] / s["first"] - 1) * 100 if s["first"] else 0.0
        lines.append(f"  {pair:<18} vol {s['quote_volume']:,.6g} {quote}" + (f" (${usd:,.0f})" if usd and not quote.startswith("USDC") else "")
                     + f"  last {s['last']:.6g} ({chg:+.2f}%)")
    if lines:
        lines.append(f"  {'TOTAL':<18} ~${total:,.0f}")
    return lines
