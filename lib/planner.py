"""Capital planner for easy mode: budget + preset -> market-maker configs and a funding plan.

Instead of ~30 parameters per market, a user picks a budget in USD, a preset
(conservative / balanced / aggressive) and the markets. plan() turns that into:

  entries   bot_config.yaml strategy entries (validated by MarketMakerStrategy)
  assets    per asset: what the user deposits (own, "send" side) and what is
            leased from the hub (inbound, "receive" side), in asset units
  capacity  config/ops.yaml capacity_need (so the alerts know what "enough" is)
  cost      lease cost per week (estimate; the real quote comes from the node)
  warnings  plain-language notes (budget too small, a market dropped, ...)

How the numbers come about (the same reasoning as the live mainnet setup):
- Each market gets a share of the budget; half of it backs the bids (paid in
  the quote asset), half the asks (paid in the base asset).
- A bid that fills RECEIVES base, an ask that fills RECEIVES quote, so every
  asset needs inbound capacity about as large as what the ladders on the other
  side can sell into it. Flow on the DEX is mostly one-way, so inbound gets a
  margin (inbound_ratio).
- max_position ~ 60% of one side's ladder: the soft limits start shrinking the
  side that grows the position well before a side is exhausted.
- The bot carries the price risk of its inventory, so spreads leave room for it
  and positions stay well below a full side (except on the parity pair).
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

# Markets the bot knows. base/quote are the DEX assets; fair_value = fixed fair
# value (parity pair), else the price oracle.
MARKETS: Dict[str, Dict] = {
    "BTC/USDC.arb": dict(name="mm_btc_usdc", base="BTC", quote="USDC.arb",
                         taker_fee_pct=0.3, maker_fee_pct=-0.05, weight=0.35, min_level_usd=15),
    "ETH/USDC.arb": dict(name="mm_eth_usdc", base="ETH", quote="USDC.arb",
                         taker_fee_pct=0.2, maker_fee_pct=-0.075, weight=0.25, min_level_usd=15),
    "ETH/BTC": dict(name="mm_eth_btc", base="ETH", quote="BTC",
                    taker_fee_pct=0.4, maker_fee_pct=-0.05, weight=0.20, min_level_usd=15),
    "USDC.arb/USDC.eth": dict(name="mm_usdc_usdc", base="USDC.arb", quote="USDC.eth",
                              taker_fee_pct=0.1, maker_fee_pct=-0.05, weight=0.20, min_level_usd=20,
                              fair_value=1.0),
}

# Chains each asset lives on (a lease/channel is per chain; ETH and USDC.eth share one).
CHAIN = {"BTC": "bitcoin", "ETH": "ethereum", "USDC.eth": "ethereum", "USDC.arb": "arbitrum"}
USD_ASSETS = {"USDC.arb", "USDC.eth"}

PRESETS: Dict[str, Dict] = {
    # spread = innermost distance from fair (%); step = extra per level (%);
    # take = arbitrage edge over the taker fee on the parity pair (None = no taking)
    "conservative": dict(levels=4, half_spread_pct=0.45, level_step_pct=0.20, skew_pct_at_max=0.2,
                         size_skew=0.25, take_edge_pct=None, usage=0.6,
                         stable=dict(half_spread_pct=0.15, level_step_pct=0.10)),
    "balanced": dict(levels=5, half_spread_pct=0.35, level_step_pct=0.15, skew_pct_at_max=0.1,
                     size_skew=0.25, take_edge_pct=0.10, usage=0.8,
                     stable=dict(half_spread_pct=0.10, level_step_pct=0.10)),
    "aggressive": dict(levels=6, half_spread_pct=0.25, level_step_pct=0.15, skew_pct_at_max=0.1,
                       size_skew=0.25, take_edge_pct=0.05, usage=0.9,
                       stable=dict(half_spread_pct=0.08, level_step_pct=0.08)),
}
INVENTORY_POSITION = 0.6          # x max_position on markets with price risk (not the parity pair)
LEASE_FEE_PER_HOUR = 0.00002      # 0.002%/h — the node's quote is authoritative
MIN_BUDGET = 300


@dataclass
class Plan:
    budget_usd: float
    preset: str
    entries: List[Dict] = field(default_factory=list)
    assets: Dict[str, Dict[str, float]] = field(default_factory=dict)   # asset -> {own, inbound, own_usd, inbound_usd}
    capacity: Dict[str, Dict[str, float]] = field(default_factory=dict)
    lease_cost_week_usd: float = 0.0
    warnings: List[str] = field(default_factory=list)


def _round_size(x: float, asset: str) -> float:
    if asset in USD_ASSETS:
        return float(max(1, round(x)))
    if asset == "BTC":
        return round(x, 5)
    return round(x, 4)


def plan(budget_usd: float, preset: str = "balanced", markets: Optional[List[str]] = None,
         prices: Optional[Dict[str, float]] = None, inbound_ratio: float = 1.2,
         lease_hours: int = 168, state_dir: str = "state") -> Plan:
    """Budget in USD -> Plan. prices: {"BTC": usd, "ETH": usd} (USDC = 1)."""
    if preset not in PRESETS:
        raise ValueError(f"unknown preset {preset!r} (choose {', '.join(PRESETS)})")
    if not prices or not prices.get("BTC") or not prices.get("ETH"):
        raise ValueError("prices for BTC and ETH are needed")
    px = {"BTC": float(prices["BTC"]), "ETH": float(prices["ETH"]), "USDC.arb": 1.0, "USDC.eth": 1.0}
    p = PRESETS[preset]
    working = budget_usd * p["usage"]
    out = Plan(budget_usd=float(budget_usd), preset=preset)
    if budget_usd < MIN_BUDGET:
        out.warnings.append(f"A budget below ${MIN_BUDGET} spreads too thin: each quote would be a few dollars "
                            f"and the fixed costs (leases, gas) eat the earnings.")
    chosen = [m for m in (markets or list(MARKETS)) if m in MARKETS]
    for m in (markets or []):
        if m not in MARKETS:
            out.warnings.append(f"Unknown market {m} skipped.")
    if not chosen:
        raise ValueError("no known market chosen")

    # `working` is what the ladders use; the rest of the budget stays as headroom
    # (capacity margins, fees, the side that grows after fills).
    wsum = sum(MARKETS[m]["weight"] for m in chosen)
    own = {a: 0.0 for a in CHAIN}          # own funds on the "send" side, in asset units
    inbound = {a: 0.0 for a in CHAIN}      # inbound capacity to lease, in asset units

    for m in chosen:
        spec = MARKETS[m]
        base, quote = spec["base"], spec["quote"]
        alloc = working * spec["weight"] / wsum
        stable = spec.get("fair_value") is not None
        levels = p["levels"]
        side_usd = alloc / 2
        level_usd = side_usd / levels
        while levels > 2 and level_usd < spec["min_level_usd"]:     # fewer, bigger levels rather than dust
            levels -= 1
            level_usd = side_usd / levels
        if level_usd < spec["min_level_usd"]:
            out.warnings.append(f"{m}: ${alloc:,.0f} is too little to quote (under ${spec['min_level_usd']} per "
                                f"level) — skipped. Raise the budget or pick fewer markets.")
            continue
        size = _round_size(level_usd / px[base], base)
        spread = (p["stable"]["half_spread_pct"] if stable else p["half_spread_pct"])
        step = (p["stable"]["level_step_pct"] if stable else p["level_step_pct"])
        max_pos = size * levels * 0.6 * (1.0 if stable else INVENTORY_POSITION)
        params = dict(levels=levels, level_size=size, bid_size=size, ask_size=size,
                      half_spread_pct=round(spread, 4), level_step_pct=step,
                      max_position=_round_size(max_pos, base) if base not in USD_ASSETS else float(round(max_pos)),
                      skew_pct_at_max=p["skew_pct_at_max"], size_skew=p["size_skew"],
                      reprice_threshold_pct=0.02 if stable else 0.05, fail_pause=120,
                      maker_fee_pct=spec["maker_fee_pct"], taker_fee_pct=spec["taker_fee_pct"],
                      state_dir=state_dir)
        if stable:
            params.update(fair_value=spec["fair_value"], over_limit_spread_pct=0.1)
        if p["take_edge_pct"] is not None and stable:
            params.update(take_edge_pct=p["take_edge_pct"], take_max_size=size)
        out.entries.append({"name": spec["name"], "type": "market_maker", "exchange": "hydra", "pair": m,
                            "enabled": True, "params": params})

        ladder_base = size * levels                      # base units on one side
        ladder_quote = size * levels * px[base] / px[quote]
        own[base] += ladder_base                         # asks sell base
        own[quote] += ladder_quote                       # bids pay quote
        inbound[base] += ladder_base * inbound_ratio     # filled bids receive base
        inbound[quote] += ladder_quote * inbound_ratio   # filled asks receive quote

    for a in CHAIN:
        if own[a] or inbound[a]:
            out.assets[a] = {"own": _round_size(own[a] * 1.05, a), "inbound": _round_size(inbound[a], a),
                             "own_usd": round(own[a] * 1.05 * px[a], 2), "inbound_usd": round(inbound[a] * px[a], 2)}
            out.capacity[a] = {"send": _round_size(own[a], a), "recv": _round_size(inbound[a] / inbound_ratio, a)}
    if "ETH" in out.assets:
        out.warnings.append("Keep ~0.003 ETH in the node wallet on Ethereum for gas (ETH deposits).")
    inbound_usd = sum(v["inbound_usd"] for v in out.assets.values())
    out.lease_cost_week_usd = round(inbound_usd * LEASE_FEE_PER_HOUR * lease_hours * (168 / lease_hours), 2)
    for a, v in out.assets.items():
        if 0 < v["inbound_usd"] < 10:
            out.warnings.append(f"{a}: inbound ${v['inbound_usd']:.0f} is under the $10 lease minimum — "
                                f"it will be leased at $10.")
    return out


def ops_capacity_yaml(p: Plan) -> Dict[str, Dict[str, float]]:
    return {a: dict(v) for a, v in p.capacity.items()}


def describe(p: Plan) -> str:
    """The plan in plain words, for the wizard and Telegram."""
    lines = [f"Plan: ${p.budget_usd:,.0f}, {p.preset}", ""]
    lines.append("Markets:")
    for e in p.entries:
        q = e["params"]
        lines.append(f"  {e['pair']:<18} {q['levels']} quotes per side of {q['level_size']:g} "
                     f"{e['pair'].split('/')[0]}, first quote {q['half_spread_pct']:g}% from the price")
    lines += ["", "What you deposit (your money) and what is leased (room to receive):"]
    for a, v in p.assets.items():
        lines.append(f"  {a:<9} deposit {v['own']:g} (~${v['own_usd']:,.0f})   lease inbound {v['inbound']:g} "
                     f"(~${v['inbound_usd']:,.0f})   on {CHAIN[a]}")
    lines.append(f"  lease cost ~${p.lease_cost_week_usd:,.2f} per week")
    if p.warnings:
        lines += [""] + [f"Note: {w}" for w in p.warnings]
    return "\n".join(lines)
