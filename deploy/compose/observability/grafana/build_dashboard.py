"""Dashboards as code: regenerates dashboards/rpm-latency.json. Edit here, not in the Grafana UI.

    python3 deploy/compose/observability/grafana/build_dashboard.py

The same JSON is loaded by the local Grafana (compose) and by kube-prometheus-stack on EKS (Helm ConfigMap), so the
datasource is a template variable rather than a fixed uid.
"""

import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "${datasource}"}
GOOD, WARN, BAD = "green", "#E0B400", "red"
P50, P95 = "#7EB26D", "#1F78C1"


class Dashboard:
    def __init__(self) -> None:
        self.panels: list[dict] = []
        self.y = 0

    def _id(self) -> int:
        return len(self.panels) + 1

    def row(self, title: str) -> None:
        self.panels.append({"type": "row", "title": title, "id": self._id(), "collapsed": False, "panels": [],
                            "gridPos": {"h": 1, "w": 24, "x": 0, "y": self.y}})
        self.y += 1

    def stat(self, title: str, expr: str, x: int, unit: str, steps: list[dict], desc: str, decimals: int = 2) -> None:
        if expr.startswith("histogram_quantile"):
            expr = f"({expr}) >= 0"  # no observations in the window gives NaN; filter it so the tile reads "–"
        self.panels.append({
            "type": "stat", "title": title, "id": self._id(), "description": desc, "datasource": DS,
            "gridPos": {"h": 4, "w": 4, "x": x, "y": self.y}, "targets": [target(expr, title)],
            "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
                        "colorMode": "background", "graphMode": "none", "textMode": "value",
                        "justifyMode": "center"},
            "fieldConfig": {"defaults": {"unit": unit, "decimals": decimals, "noValue": "–",
                                         "thresholds": {"mode": "absolute", "steps": steps}}, "overrides": []}})

    def timeseries(self, title: str, targets: list[dict], x: int, w: int, unit: str, desc: str,
                   overrides: tuple = (), stack: bool = False) -> None:
        custom = {"drawStyle": "line", "lineWidth": 2, "fillOpacity": 10 if stack else 0, "showPoints": "never",
                  "spanNulls": True, "stacking": {"mode": "normal" if stack else "none"}}
        self.panels.append({
            "type": "timeseries", "title": title, "id": self._id(), "description": desc, "datasource": DS,
            "gridPos": {"h": 8, "w": w, "x": x, "y": self.y}, "targets": targets,
            "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True},
                        "tooltip": {"mode": "multi", "sort": "desc"}},
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "color": {"mode": "palette-classic"},
                                         "custom": custom}, "overrides": list(overrides)}})

    def bars(self, title: str, targets: list[dict], x: int, w: int, unit: str, desc: str) -> None:
        self.panels.append({
            "type": "bargauge", "title": title, "id": self._id(), "description": desc, "datasource": DS,
            "gridPos": {"h": 8, "w": w, "x": x, "y": self.y}, "targets": targets,
            "options": {"orientation": "horizontal", "displayMode": "basic", "showUnfilled": True,
                        "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}},
            "fieldConfig": {"defaults": {"unit": unit, "min": 0, "decimals": 2,
                                         "color": {"mode": "fixed", "fixedColor": P95}}, "overrides": []}})


def target(expr: str, legend: str, ref: str = "A") -> dict:
    return {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref, "range": True}


def color(series: str, value: str) -> dict:
    return {"matcher": {"id": "byName", "options": series},
            "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": value}}]}


def latency_steps(warn: float, bad: float) -> list[dict]:
    return [{"color": GOOD, "value": None}, {"color": WARN, "value": warn}, {"color": BAD, "value": bad}]


def quantile(q: float, metric: str, selector: str, by: str = "") -> str:
    return f"histogram_quantile({q}, sum by (le{by}) (rate({metric}_bucket{{{selector}}}[5m])))"


def percentiles(metric: str, selector: str) -> list[dict]:
    return [target(quantile(0.5, metric, selector), "p50", "A"), target(quantile(0.95, metric, selector), "p95", "B")]


def build() -> dict:
    d = Dashboard()
    hop = "rpm_hop_latency_seconds"
    page = "rpm_page_latency_seconds"

    d.row("Service levels · p95 latency over 5 min · counts over 1 h")
    d.stat("Sensor→page", quantile(0.95, page, 'cause="event"'), 0, "s", latency_steps(5, 15),
           "Sensor window end → care-team notification handed to the channel, for pages caused by a clinical "
           "event. Target < 5 s.")
    d.stat("Home E2E", quantile(0.95, hop, 'hop="end_to_end",context="home"'), 4, "s", latency_steps(3, 10),
           "Window end on the watch/phone → scored by the stream engine (live data; backfill excluded).")
    d.stat("ICU E2E", quantile(0.95, hop, 'hop="end_to_end",context="icu"'), 8, "s", latency_steps(3, 10),
           "Window end at the ICU gateway → scored by the stream engine.")
    d.stat("Alerts 1h", "sum(increase(rpm_alerts_opened_total[1h]))", 12, "short", [{"color": "text", "value": None}],
           "New alerts (one active alert per patient; later events attach).", decimals=0)
    d.stat("Failed pages", 'sum(increase(rpm_notifications_total{status="failed"}[1h])) or vector(0)', 16, "short",
           [{"color": GOOD, "value": None}, {"color": BAD, "value": 1}],
           "Any failure is recorded per channel; the other channels still deliver.", decimals=0)
    d.stat("ICU backlog", "max(rpm_gw_outbox_depth) or vector(0)", 20, "short",
           [{"color": GOOD, "value": None}, {"color": WARN, "value": 50}, {"color": BAD, "value": 500}],
           "Messages waiting in the gateway's durable outbox (grows while the cloud link is down).", decimals=0)
    d.y += 4

    d.row("Latency")
    for i, (ctx, label) in enumerate((("home", "home (watch / phone)"), ("icu", "ICU (bedside gateway)"))):
        d.timeseries(f"End-to-end latency · {label}", percentiles(hop, f'hop="end_to_end",context="{ctx}"'),
                     i * 8, 8, "s", "Sensor window end → processed. Separate panels, same meaning: compare them.",
                     overrides=(color("p50", P50), color("p95", P95)))
    d.bars("Where the time goes · p95 per hop",
           [target(quantile(0.95, hop, 'hop!="end_to_end"', ", hop, context"), "{{context}} · {{hop}}")], 16, 8, "s",
           "edge = window end → sent by the device/gateway; uplink = sent → IoT ingest; pipeline = ingest → scored.")
    d.y += 8
    d.timeseries("Sensor → page p95 by channel",
                 [target(quantile(0.95, page, 'cause="event"', ", channel"), "{{channel}}")], 0, 12, "s",
                 "Pages caused by clinical events (timer, agent and manual pages measure dispatch delay instead).")
    d.timeseries("Notifications by status",
                 [target("sum by (status) (rate(rpm_notifications_total[5m])) * 60", "{{status}}")], 12, 12, "short",
                 "Per minute. Skipped = channel not applicable (e.g. patient reassurance for ICU patients).",
                 overrides=(color("sent", GOOD), color("failed", BAD), color("skipped", "#8F8F8F")), stack=True)
    d.y += 8

    d.row("Agents")
    d.timeseries("Agent runs per minute",
                 [target("sum by (trigger) (rate(rpm_agent_runs_total[5m])) * 60", "{{trigger}}")], 0, 8, "short",
                 "Clinical events that reached the model.")
    d.timeseries("Model calls avoided per minute",
                 [target("sum by (reason) (rate(rpm_agent_skipped_total[5m])) * 60", "{{reason}}")], 8, 8, "short",
                 "covered = a run on a more severe event of the same telemetry burst already saw it; "
                 "debounced = same or lower severity within the debounce window.")
    d.timeseries("Agent run duration", percentiles("rpm_agent_run_seconds", ""), 16, 8, "s",
                 "Event → brief attached (assessment, retrieval, brief).",
                 overrides=(color("p50", P50), color("p95", P95)))
    d.y += 8

    d.row("ICU bedside gateway")
    d.timeseries("HL7 messages per second",
                 [target("sum by (result) (rate(rpm_gw_hl7_messages_total[1m]))", "{{result}}")], 0, 8, "short",
                 "ORU^R01 from bedside monitors; anything but ok was NACKed (AE/AR) back to the monitor.")
    d.timeseries("Bedside alarms per hour",
                 [target("sum by (rule) (rate(rpm_gw_bedside_alarms_total[15m])) * 3600", "{{rule}}")], 8, 8,
                 "short", "Raised locally at the gateway (works with the cloud link down); latched, re-announced "
                 "every 10 min while active.")
    d.timeseries("Window → accepted by uplink p95",
                 [target(quantile(0.95, "rpm_gw_window_to_publish_seconds", ""), "p95")], 16, 8, "s",
                 "How long a bed's window waits in the gateway before the cloud link accepts it (rises in outages).",
                 overrides=(color("p95", P95),))

    return {"uid": "rpm-latency", "title": "RPM · latency and alerting", "tags": ["rpm"], "timezone": "browser",
            "schemaVersion": 39, "version": 1, "refresh": "10s", "time": {"from": "now-30m", "to": "now"},
            "panels": d.panels, "annotations": {"list": []}, "editable": True,
            "templating": {"list": [{"name": "datasource", "label": "Prometheus", "type": "datasource",
                                     "query": "prometheus", "hide": 0, "current": {}}]}}


if __name__ == "__main__":
    out = Path(__file__).with_name("dashboards") / "rpm-latency.json"
    out.write_text(json.dumps(build(), indent=1, ensure_ascii=False) + "\n")
    print(f"wrote {out}")
