import os

NIGHTS         = 180                                          # 民泊 legal cap — fixed
MGMT_PCT       = float(os.getenv("MGMT_PCT", "0.30"))         # management fee shown in caption
SETUP_JPY      = int(os.getenv("SETUP_JPY", "500000"))        # furniture, fire safety, registration
MAX_SHOWN_ROI  = float(os.getenv("MAX_SHOWN_ROI", "0.60"))    # hide yield % above this


# ---------- costs ----------
def reno_jpy(year_built, floor_m2=None, renovated=False):
    if year_built is None:
        per_m2, flat = None, 3_000_000
    elif year_built < 1981:
        per_m2, flat = 50_000, 3_500_000
    elif year_built < 2000:
        per_m2, flat = 30_000, 2_200_000
    else:
        per_m2, flat = 15_000, 1_200_000
    cost = per_m2 * floor_m2 if (per_m2 and floor_m2) else flat
    if renovated:
        cost *= 0.5
    return cost + SETUP_JPY


def fees_jpy(price_jpy):
    if price_jpy <= 8_000_000:
        agent = 330_000                                   # 2024 low-cost akiya cap (tax incl.)
    else:
        agent = (price_jpy * 0.03 + 60_000) * 1.10
    scrivener = 100_000
    taxes = price_jpy * 0.04                              # registration + acquisition tax (proxy)
    return agent + scrivener + taxes


# ---------- AirROI only ----------
def airroi_inputs(airroi, jpy_per_usd):
    """Nightly rate (USD) and occupancy (0-1) from AirROI. None if missing."""
    if not airroi:
        return None

    def p50(key):
        p = (airroi.get("percentiles") or {}).get(key) or {}
        return p.get("p50") or airroi.get(key)

    adr, occ = p50("average_daily_rate"), p50("occupancy")
    if not adr or not occ:
        return None
    if str(airroi.get("currency", "USD")).upper() == "JPY":
        adr = adr / jpy_per_usd
    if occ > 1:                                           # 41 -> 0.41
        occ = occ / 100
    return round(adr), round(occ, 2)


# ---------- estimate ----------
def _r100(usd):
    return round(usd / 100) * 100


def estimate(price_jpy, year_built, airroi, jpy_per_usd, floor_m2=None, renovated=False):
    rental = airroi_inputs(airroi, jpy_per_usd)
    if rental is None:
        return None
    adr, occ = rental

    gross = adr * occ * NIGHTS
    net = _r100(gross * (1 - MGMT_PCT))

    house = _r100(price_jpy / jpy_per_usd)
    reno = _r100(reno_jpy(year_built, floor_m2, renovated) / jpy_per_usd)
    fees = _r100(fees_jpy(price_jpy) / jpy_per_usd)
    all_in = house + reno + fees

    return {
        "source": "AirROI",
        "house": house, "reno": reno, "fees": fees, "all_in": all_in,
        "adr": adr, "occ": occ, "nights": NIGHTS,
        "gross": round(gross), "mgmt_pct": MGMT_PCT, "net": net,
        "roi": net / all_in if all_in else 0,
        "monthly": round(net / 12),
        "breakeven_yrs": all_in / net if net > 0 else None,
    }


def yield_points(e):
    if e is None:
        return -5
    r = e["roi"]
    if r >= 0.25: return 15
    if r >= 0.15: return 10
    if r >= 0.08: return 5
    if r >= 0:    return 0
    return -10


# ---------- caption ----------
def fmt_k(usd):
    return "$" + f"{usd / 1000:.1f}".removesuffix(".0") + "k"


def caption_text(e, drive_mins=None, resort=None):
    if e is None or e["net"] <= 0:
        return None

    head = []
    if drive_mins is not None and resort:
        head.append(f"{round(drive_mins)} mins drive to {resort}.")
    if e["roi"] <= MAX_SHOWN_ROI:
        head.append(f"{e['roi'] * 100:.0f}% net yield.")

    body = [
        f"Costs: House {fmt_k(e['house'])} + Reno {fmt_k(e['reno'])} + Fees {fmt_k(e['fees'])} = {fmt_k(e['all_in'])} in",
        f"Rental: ${e['adr']}/night x {e['occ'] * 100:.0f}% x {e['nights']} days",
        f"After {e['mgmt_pct'] * 100:.0f}% management: {fmt_k(e['net'])}/yr net",
        "",
        f"Avg. monthly income: ~${e['monthly']:,} net / Break even {e['breakeven_yrs']:.1f} yrs",
    ]
    return "\n".join(([" ".join(head), ""] if head else []) + body)
