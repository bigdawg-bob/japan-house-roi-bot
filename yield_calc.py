"""
Yield math for the akiya bot.
Rental data comes ONLY from AirROI (nightly rate + occupancy).
Reno cost and buying fees are rule-of-thumb estimates, not data.
"""
import os

def _env(name, default):
    return os.getenv(name) or default                  # empty string -> default

NIGHTS        = 180                                     # 民泊 legal cap - fixed
MGMT_PCT      = float(_env("MGMT_PCT", "0.30"))         # management company cut
SETUP_JPY     = int(_env("SETUP_JPY", "500000"))        # furniture, fire safety, registration
MAX_SHOWN_ROI = float(_env("MAX_SHOWN_ROI", "0.60"))    # hide yield % above this (looks fake)
RENO_WORDS    = ("リフォーム済", "リノベ済", "リノベーション済", "改装済")


# ---------- costs (yen) ----------
def reno_jpy(year_built, floor_m2=None, renovated=False):
    try:
        year_built = int(year_built)
    except (TypeError, ValueError):
        year_built = None
    if not year_built:
        per_m2, flat = None, 3_000_000                  # unknown -> assume the worst
    elif year_built < 1981:
        per_m2, flat = 50_000, 3_500_000                # old earthquake code, pipes, wiring
    elif year_built < 2000:
        per_m2, flat = 30_000, 2_200_000                # kitchen, bath, interior
    else:
        per_m2, flat = 15_000, 1_200_000                # mostly cosmetic
    cost = per_m2 * floor_m2 if (per_m2 and floor_m2) else flat
    if renovated:
        cost *= 0.5
    return cost + SETUP_JPY


def fees_jpy(price_jpy):
    """One-time buying costs: agent + scrivener + registration/acquisition tax."""
    if price_jpy <= 8_000_000:
        agent = 330_000                                 # 2024 low-cost akiya cap (tax incl.)
    else:
        agent = (price_jpy * 0.03 + 60_000) * 1.10
    scrivener = 100_000
    taxes = price_jpy * 0.04                            # proxy: real tax uses assessed value
    return agent + scrivener + taxes


def is_renovated(listing):
    title = listing.get("title") or ""
    return any(w in title for w in RENO_WORDS)


# ---------- AirROI only ----------
def airroi_inputs(est):
    """est = main.py's cached AirROI result {"revenue", "occupancy" (%), "adr" (USD)}.
    Returns (nightly rate USD, occupancy 0-1) or None if AirROI gave no usable data."""
    if not est:
        return None
    adr, occ, rev = est.get("adr"), est.get("occupancy"), est.get("revenue")
    if occ and occ > 1:                                 # 41 -> 0.41
        occ = occ / 100
    if not adr and rev and occ:                         # still AirROI data, just derived
        adr = rev / (occ * 365)
    if not adr or not occ:
        return None
    return round(adr), round(occ, 2)


# ---------- estimate (USD) ----------
def _r100(usd):
    return round(usd / 100) * 100


def estimate(price_jpy, year_built, airroi_est, fx, floor_m2=None,
             renovated=False, yearly_fees_jpy=0):
    """fx = USD per 1 JPY (same as main.py, e.g. 0.0067). None = no AirROI data."""
    rental = airroi_inputs(airroi_est)
    if rental is None:
        return None
    adr, occ = rental

    gross = adr * occ * NIGHTS
    fees_yearly = round((yearly_fees_jpy or 0) * fx)
    net = _r100(gross * (1 - MGMT_PCT) - fees_yearly)

    house = round(price_jpy * fx)
    reno = _r100(reno_jpy(year_built, floor_m2, renovated) * fx)
    fees = _r100(fees_jpy(price_jpy) * fx)
    all_in = house + reno + fees

    return {
        "source": "AirROI",
        "house": house, "reno": reno, "fees": fees, "all_in": all_in,
        "adr": adr, "occ": occ, "nights": NIGHTS,
        "gross": round(gross), "mgmt_pct": MGMT_PCT, "fees_yearly": fees_yearly,
        "net": net,
        "roi": net / all_in if all_in else 0,
        "monthly": round(net / 12),
        "breakeven_yrs": all_in / net if net > 0 else None,
        "renovated": renovated,
    }


def yield_points(e):
    """Ranking bonus: location stays the main score, this only adjusts it."""
    if e is None:
        return -5
    r = e["roi"]
    if r >= 0.25: return 15
    if r >= 0.15: return 10
    if r >= 0.08: return 5
    if r >= 0:    return 0
    return -10


# ---------- text ----------
def fmt_k(usd):
    if usd == 0:
        return "FREE"
    return "$" + f"{usd / 1000:.1f}".removesuffix(".0") + "k"


def show_yield(e):
    return e["roi"] <= MAX_SHOWN_ROI


def caption_text(e, headline=None):
    """The ROI block of the caption. None if the house loses money."""
    if e is None or e["net"] <= 0:
        return None
    head = headline or ""
    if show_yield(e):
        head = f"{head} {e['roi'] * 100:.0f}% net yield.".strip()

    after = f"After {e['mgmt_pct'] * 100:.0f}% management"
    if e["fees_yearly"]:
        after += f" & ${e['fees_yearly']:,} yearly fees"

    body = [
        f"Costs: House {fmt_k(e['house'])} + Reno {fmt_k(e['reno'])} + "
        f"Fees {fmt_k(e['fees'])} = {fmt_k(e['all_in'])} in",
        f"Rental: ${e['adr']}/night x {e['occ'] * 100:.0f}% x {e['nights']} days",
        f"{after}: {fmt_k(e['net'])}/yr net",
        "",
        f"Avg. monthly income: ~${e['monthly']:,} net / "
        f"Break even {e['breakeven_yrs']:.1f} yrs",
    ]
    return "\n".join(([head, ""] if head else []) + body)
