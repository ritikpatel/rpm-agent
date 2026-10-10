from care_api.engine import AgentThrottle


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def make(daily_limit=0, recorded=0, monitor_interval_s=3600):
    clock = Clock()
    return AgentThrottle(600, monitor_interval_s, daily_limit, lambda: recorded, clock), clock


def test_one_alert_run_per_patient_per_window_unless_severity_rose():
    t, clock = make()
    assert t.admit("alert", "p1", rank=2) == "queued"
    assert t.admit("alert", "p1", rank=2) == "debounced"
    assert t.admit("alert", "p1", rank=3) == "queued"       # worse: the agents look again
    assert t.admit("alert", "p2", rank=2) == "queued"       # another patient is unaffected
    clock.t += 601
    assert t.admit("alert", "p1", rank=2) == "queued"


def test_check_ins_use_the_longer_window_and_never_block_an_alert():
    t, clock = make()
    assert t.admit("monitor", "p1") == "queued"
    clock.t += 900
    assert t.admit("monitor", "p1") == "debounced"          # inside the hour
    assert t.admit("alert", "p1", rank=2) == "queued"       # an alert is never held back by a check-in
    clock.t += 3601
    assert t.admit("monitor", "p1") == "queued"


def test_daily_ceiling_stops_automatic_runs_but_not_people():
    t, _ = make(daily_limit=3, recorded=1)
    assert [t.admit("alert", p, rank=2) for p in ("a", "b", "c")] == ["queued", "queued", "capped"]
    assert t.admit("discharge", "d") == "capped"
    assert t.admit("on_demand", "a") == "queued"            # a clinician asked
    assert t.admit("reply", "a") == "queued"                # the patient answered


def test_no_ceiling_by_default():
    t, _ = make(daily_limit=0, recorded=10_000)
    assert t.admit("alert", "p", rank=1) == "queued"
