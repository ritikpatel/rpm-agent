"""Features for the post-discharge recovery model (PLACEHOLDER; Navya's feature module replaces this file).

Compares the last 24 hours of wearable data with the days before it. Self-contained on purpose."""

FEATURES = ["days_since_discharge", "hr_rest_24h", "hr_rest_change", "spo2_mean_24h", "spo2_min_24h",
            "news2_max_24h", "news2_mean_change", "active_share_24h", "data_hours_24h"]
NORMAL = {"hr_rest_24h": 72.0, "spo2_mean_24h": 96.5, "spo2_min_24h": 94.0}


def _mean(xs):
    return sum(xs) / len(xs) if xs else None


def build(ctx: dict) -> dict:
    rows = ctx["vitals"]                       # last 24 h, oldest first
    daily = ctx["daily"]                       # one row per day, last 7 days, oldest first
    resting = [float(r["heart_rate"]) for r in rows
               if r.get("heart_rate") is not None and r.get("activity") in (None, "resting", "sleep", "unknown")]
    spo2 = [float(r["spo2"]) for r in rows if r.get("spo2") is not None]
    news2 = [float(r["news2"]) for r in rows if r.get("news2") is not None]
    active = [r for r in rows if r.get("activity") in ("light", "moderate", "vigorous")]
    earlier = daily[:-1]                       # the days before today
    hr_before = _mean([float(d["hr_mean"]) for d in earlier if d.get("hr_mean") is not None])
    news2_before = _mean([float(d["news2_mean"]) for d in earlier if d.get("news2_mean") is not None])
    hr_rest = _mean(resting)
    span_h = (rows[-1]["ts"] - rows[0]["ts"]).total_seconds() / 3600 if len(rows) >= 2 else 0.0
    return {
        "days_since_discharge": min(90.0, (ctx.get("hours_since_discharge") or 0.0) / 24),
        "hr_rest_24h": hr_rest if hr_rest is not None else NORMAL["hr_rest_24h"],
        "hr_rest_change": (hr_rest - hr_before) if (hr_rest is not None and hr_before is not None) else 0.0,
        "spo2_mean_24h": _mean(spo2) if spo2 else NORMAL["spo2_mean_24h"],
        "spo2_min_24h": min(spo2) if spo2 else NORMAL["spo2_min_24h"],
        "news2_max_24h": max(news2, default=0.0),
        "news2_mean_change": (_mean(news2) - news2_before) if (news2 and news2_before is not None) else 0.0,
        "active_share_24h": len(active) / len(rows) if rows else 0.0,
        "data_hours_24h": min(24.0, span_h),
    }
