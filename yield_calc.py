"""
Yield math for the akiya bot (ski-lodge focused).
- AirROI only used for ADR + occupancy → gross rental income
- Scoring is done elsewhere (location + age only)
- This module calculates realistic all-in costs + final verdict
"""

import os

def _env(name, default):
    return os.getenv(name) or default

NIGHTS        = 180
MGMT_PCT      = float(_env("MGMT_PCT", "0.30"))
SETUP_JPY     = int(_env("SETUP_JPY", "500000"))
MAX_SHOWN_ROI = float(_env("MAX_SHOWN_ROI", "0.60"))
RENO_WORDS    = ("リフォーム済", "リノベ済", "リノベーション済", "改装済")

# ---------- renovation (from real Matsudo invoice) ----------
M2_PER_ROOM     = 20
DEFAULT_M2      = 100
WORKS_PER_M2    = int(_env("WORKS_PER_M2", "31000"))
SEISMIC_JPY     = int(_env("SEISMIC_JPY", "1500000"))
COORD_FEE       = float(_env("COORD_FEE", "0.20"))
CONSUMPTION_TAX = 0.10
LICENSE_JPY     = int(_env("LICENSE_JPY", "800000"))


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def reno_jpy(year_built, floor_m2=None, renovated=False, rooms=None):
    year_built = _to_int(year_built)
    rooms = _to_int(rooms)
    if not floor_m2:
        floor_m2 = rooms * M2_PER_ROOM if rooms else DEFAULT_M2
    works = WORKS_PER_M2 * floor_m2
    if not year_built or year_built < 1981:
        works += SEISMIC_JPY
    elif year_built >= 2000:
        works *= 0.6
    if renovated:
        works *= 0.5
    total = works * (1 + COORD_FEE) * (1 + CONSUMPTION_TAX)
    return total + SETUP_JPY + LICENSE_JPY


def fees_jpy(price_jpy):
    if price_jpy <= 8_000_000:
        agent = 330_000
    else:
        agent = (price_jpy * 0.03 + 60_000) * 1.10
    scrivener = 100_000
    taxes = price_jpy * 0.04
    return agent + scrivener + taxes


def is_renovated(listing):
    title = listing.get("title") or ""
    return any(w in title for w in RENO_WORDS)


# ---------- NEW: realistic ski-lodge running costs (scaled) ----------
def ski_lodge_running_cost(price_yen, floor_m2=None, bedrooms=None, year_built=None):
    if not floor_m2:
        floor_m2 = (bedrooms or 5) * 20
    floor_m2 = max(60, min(floor_m2 or 100, 250))

    tax = round(price_yen * 0.014 * 0.70)
    insurance = round(40_000 + floor_m2 * 400)
    utilities = round(10_000 * 12 + floor_m2 * 50)
    snow = round(250_000 + floor_m2 * 2_500)
    heating = round(120_000 + floor_m2 * 1_800)
    age_factor = 1.4 if (not year_built or year_built < 1981) else 1.0
    maintenance = round((200_000 + floor_m2 * 1_500) * age_factor)

    total = tax + insurance + utilities + snow + heating + maintenance

    return {
        "tax": tax,
        "insurance": insurance,
        "utilities": utilities,
        "snow": snow,
        "heating": heating,
        "maintenance": maintenance,
        "total": total,
    }


# ---------- AirROI ----------
def airroi_inputs(est):
    if not est:
        return None
    adr, occ, rev = est.get("adr"), est.get("occupancy"), est.get("revenue")
    if occ and occ > 1:
        occ = occ / 100
    if not adr and rev and occ:
        adr = rev / (occ * 365)
    if not adr or not occ:
        return None
    return round(adr), round(occ, 2)


def _r100(usd):
    return round(usd / 100) * 100


def estimate(price_jpy, year_built, airroi_est, fx, floor_m2=None,
             renovated=False, yearly_fees_jpy=0, rooms=None):
    """
    Returns full financial picture + verdict.
    AirROI only used for ADR + occupancy.
    """
    rental = airroi_inputs(airroi_est)
    if rental is None:
        return None
    adr, occ = rental

    gross = adr * occ * NIGHTS
    fees_yearly = round((yearly_fees_jpy or 0) * fx)

    # New: realistic ski-lodge operating costs
    running = ski_lodge_running_cost(
        price_jpy, floor_m2=floor_m2, bedrooms=rooms, year_built=year_built
    )
    running_usd = round(running["total"] * fx)

    net = _r100(gross * (1 - MGMT_PCT) - fees_yearly - running_usd)

    house = round(price_jpy * fx)
    reno = _r100(reno_jpy(year_built, floor_m2, renovated, rooms) * fx)
    fees = _r100(fees_jpy(price_jpy) * fx)
    all_in = house + reno + fees

    roi = net / all_in if all_in else 0

    # Verdict rule: > 5% net yield after ALL costs
    verdict = "Worth a Look" if roi > 0.05 else "NO"

    return {
        "source": "AirROI",
        "house": house,
        "reno": reno,
        "fees": fees,
        "all_in": all_in,
        "adr": adr,
        "occ": occ,
        "nights": NIGHTS,
        "gross": round(gross),
        "mgmt_pct": MGMT_PCT,
        "fees_yearly": fees_yearly,
        "running_cost": running_usd,          # new
        "running_detail": {k: round(v * fx) for k, v in running.items() if k != "total"},
        "net": net,
        "roi": roi,
        "monthly": round(net / 12),
        "breakeven_yrs": all_in / net if net > 0 else None,
        "renovated": renovated,
        "verdict": verdict,                   # new
    }


def show_yield(e):
    return e["roi"] <= MAX_SHOWN_ROI


def caption_text(e, headline=None):
    if e is None or e["net"] <= 0:
        return None
    head = headline or ""
    if show_yield(e):
        head = f"{head} {e['roi'] * 100:.0f}% net yield.".strip()

    after = f"After {e['mgmt_pct'] * 100:.0f}% management"
    if e["fees_yearly"]:
        after += f" & ${e['fees_yearly']:,} yearly fees"
    after += f" + ${e['running_cost']:,} operating costs"

    body = [
        f"Costs: House {fmt_k(e['house'])} + Reno & license {fmt_k(e['reno'])} + "
        f"Fees {fmt_k(e['fees'])} = {fmt_k(e['all_in'])} in",
        f"Rental: ${e['adr']}/night x {e['occ'] * 100:.0f}% x {e['nights']} days",
        f"{after}: {fmt_k(e['net'])}/yr net",
        "",
        f"Avg. monthly income: ~${e['monthly']:,} net / "
        f"Break even {e['breakeven_yrs']:.1f} yrs" if e["breakeven_yrs"] else "",
        f"Verdict: {e['verdict']}",
    ]
    return "\n".join(([head, ""] if head else []) + [b for b in body if b])


def fmt_k(usd):
    if usd == 0:
        return "FREE"
    return "$" + f"{usd / 1000:.1f}".removesuffix(".0") + "k"
