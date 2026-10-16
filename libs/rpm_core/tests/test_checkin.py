"""A check-in answer reads as words: the question id's random suffix is never shown."""

from rpm_core.checkin import describe


def test_an_answer_names_what_was_asked_never_the_question_id():
    assert describe("Yes", "breathless-d4e7e415") == "Yes to “more short of breath than usual”"
    assert describe("no", "readings-clin") == "No to “home blood pressure and temperature measured”"
    assert describe("Yes", "unknown-1") == "Yes" and describe(None, None) == "No answer"
