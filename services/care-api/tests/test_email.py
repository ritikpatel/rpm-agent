"""Alert e-mail: one message per person on the tier, nothing clinical in it, one bad address blocks nobody."""

from datetime import UTC, datetime

import pytest

from care_api.alerts import CareTeam
from care_api.alerts.notify import EmailChannel, recipients

ALERT = {"alert_id": "a-1", "patient_id": "p-90000132", "display_name": "Daniel Fischer", "severity": "high",
         "opened_at": datetime(2026, 10, 10, 11, 0, tzinfo=UTC)}


class FakeSes:
    def __init__(self, reject=()):
        self.sent, self.reject = [], set(reject)

    def send_email(self, **kw):
        to = kw["Destination"]["ToAddresses"]
        if to[0] in self.reject:
            raise RuntimeError("MessageRejected: Email address is not verified")
        self.sent.append(kw)
        return {"MessageId": f"m-{len(self.sent)}"}


def test_everyone_on_the_tier_gets_their_own_message_without_clinical_details():
    ses = FakeSes()
    ch = EmailChannel("ses", "alerts@example.org", ses=ses)
    to, ref, note = ch.send(ALERT, "Physician on call", {"email": ["a@example.org", "b@example.org"]}, "https://c/x")
    assert to == "a@example.org, b@example.org" and ref == "m-1" and note is None
    assert [m["Destination"]["ToAddresses"] for m in ses.sent] == [["a@example.org"], ["b@example.org"]]
    msg = ses.sent[0]
    assert msg["FromEmailAddress"] == "alerts@example.org"
    text = msg["Content"]["Simple"]["Subject"]["Data"] + msg["Content"]["Simple"]["Body"]["Text"]["Data"]
    assert "D.F." in text and "0132" in text and "Daniel" not in text and "Fischer" not in text


def test_one_unverified_address_does_not_block_the_others():
    ses = FakeSes(reject={"new@example.org"})
    to, _, _ = EmailChannel("ses", "s@example.org", ses=ses).send(
        ALERT, "RPM nurse", {"email": ["new@example.org", "ok@example.org"]}, "https://c/x")
    assert to.startswith("ok@example.org (failed: new@example.org") and len(ses.sent) == 1
    with pytest.raises(RuntimeError, match="not verified"):
        EmailChannel("ses", "s@example.org", ses=FakeSes(reject={"new@example.org"})).send(
            ALERT, "RPM nurse", {"email": "new@example.org"}, "https://c/x")


def test_contacts_accept_one_address_or_a_list_and_a_missing_file_uses_the_demo_team(tmp_path):
    assert recipients({"email": "a@x.org"}) == ["a@x.org"] and recipients({"email": ["a@x.org", "b@x.org"]}) == \
        ["a@x.org", "b@x.org"] and recipients({}) == []
    assert CareTeam.load(str(tmp_path / "absent.yaml")).contact("nurse")["email"] == ["nurse@rpm.local"]
    f = tmp_path / "team.yaml"
    f.write_text("nurse: {name: N, email: [t@x.org, n@x.org]}\n")
    assert CareTeam.load(str(f)).contact("nurse")["email"] == ["t@x.org", "n@x.org"]
