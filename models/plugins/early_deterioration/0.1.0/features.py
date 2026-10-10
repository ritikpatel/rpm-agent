"""Features for the early-deterioration model (PLACEHOLDER; Harnoor's feature module replaces this file).

Self-contained on purpose: a plug-in's feature code imports nothing from the platform, so the same file runs in a
notebook during training and inside the risk service."""

FEATURES = ["news2_now", "news2_delta_1h", "hr_now", "hr_slope_per_h", "spo2_min_1h", "rr_now", "partial_news2"]
NORMAL = {"heart_rate": 75.0, "spo2": 97.0, "resp_rate": 16.0}


def build(ctx: dict) -> dict:
    now = ctx["now"]
    recent = [r for r in ctx["vitals"] if (now - r["ts"]).total_seconds() <= 3600]   # oldest first
    scored = [r for r in recent if r.get("news2") is not None]
    news2_now = float(scored[-1]["news2"]) if scored else 0.0
    news2_first = float(scored[0]["news2"]) if scored else 0.0
    hr = [(r["ts"], float(r["heart_rate"])) for r in recent if r.get("heart_rate") is not None]
    slope = 0.0
    if len(hr) >= 2 and (hr[-1][0] - hr[0][0]).total_seconds() > 0:
        slope = (hr[-1][1] - hr[0][1]) / ((hr[-1][0] - hr[0][0]).total_seconds() / 3600)
    spo2 = [float(r["spo2"]) for r in recent if r.get("spo2") is not None]
    rr = [float(r["resp_rate"]) for r in recent if r.get("resp_rate") is not None]
    return {
        "news2_now": news2_now,
        "news2_delta_1h": news2_now - news2_first,
        "hr_now": hr[-1][1] if hr else NORMAL["heart_rate"],
        "hr_slope_per_h": max(-120.0, min(120.0, slope)),
        "spo2_min_1h": min(spo2) if spo2 else NORMAL["spo2"],
        "rr_now": rr[-1] if rr else NORMAL["resp_rate"],
        "partial_news2": 1.0 if (scored and scored[-1].get("news2_partial")) else 0.0,
    }
