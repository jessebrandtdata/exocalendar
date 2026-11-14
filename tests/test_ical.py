from pathlib import Path

import pytest

from exocalendar.ical import (
    Component,
    ContentLine,
    escape_text,
    fold_line,
    unescape_text,
    unfold,
)

CORPUS = Path(__file__).parent / "corpus"


# --- folding -----------------------------------------------------------------

def test_unfold_joins_continuations():
    text = "SUMMARY:Hello\r\n  world\r\nUID:x\r\n"
    assert unfold(text) == ["SUMMARY:Hello world", "UID:x"]


def test_unfold_accepts_lf_and_tab_continuation():
    text = "SUMMARY:He\n\tllo\nUID:x"
    assert unfold(text) == ["SUMMARY:Hello", "UID:x"]


def test_fold_line_limits_octets():
    line = "SUMMARY:" + "a" * 200
    folded = fold_line(line)
    for part in folded.split("\r\n"):
        assert len(part.encode()) <= 75
    assert unfold(folded) == [line]


def test_fold_line_never_splits_utf8_sequence():
    line = "SUMMARY:" + "ä" * 100
    folded = fold_line(line)
    for part in folded.split("\r\n"):
        part.encode()  # each physical line must be valid on its own
        assert len(part.encode()) <= 75
    assert unfold(folded) == [line]


# --- escaping ----------------------------------------------------------------

def test_text_escaping_round_trip():
    raw = "a, b;\nc\\d"
    esc = escape_text(raw)
    assert esc == "a\\, b\\;\\nc\\\\d"
    assert unescape_text(esc) == raw


def test_unescape_capital_n():
    assert unescape_text("x\\Ny") == "x\ny"


# --- content lines -----------------------------------------------------------

def test_parse_simple_line():
    cl = ContentLine.parse("SUMMARY:Team sync")
    assert cl.name == "SUMMARY"
    assert cl.params == []
    assert cl.value == "Team sync"
    assert cl.serialize() == "SUMMARY:Team sync"


def test_parse_params_with_quoted_comma():
    cl = ContentLine.parse(
        'ATTENDEE;CN="Doe, John";ROLE=REQ-PARTICIPANT:mailto:x@y.z'
    )
    assert cl.name == "ATTENDEE"
    assert cl.param("CN") == "Doe, John"
    assert cl.param("ROLE") == "REQ-PARTICIPANT"
    assert cl.value == "mailto:x@y.z"
    # quoting preserved on serialize
    assert cl.serialize() == 'ATTENDEE;CN="Doe, John";ROLE=REQ-PARTICIPANT:mailto:x@y.z'


def test_parse_multi_value_param():
    cl = ContentLine.parse("EXDATE;TZID=Europe/Berlin;VALUE=DATE-TIME:20260119T103000,20260120T103000")
    assert cl.param("TZID") == "Europe/Berlin"
    assert cl.value == "20260119T103000,20260120T103000"


def test_param_name_case_insensitive():
    cl = ContentLine.parse("DTSTART;tzid=UTC:20260101T000000Z")
    assert cl.param("TZID") == "UTC"


def test_value_with_colon_after_params():
    cl = ContentLine.parse("ATTENDEE;CN=NoQuotes:mailto:nq@example.com")
    assert cl.param("CN") == "NoQuotes"
    assert cl.value == "mailto:nq@example.com"


def test_lowercase_name_normalized():
    cl = ContentLine.parse("summary:x")
    assert cl.name == "SUMMARY"


def test_bad_line_raises():
    with pytest.raises(ValueError):
        ContentLine.parse("NO-COLON-HERE")


# --- components --------------------------------------------------------------

SIMPLE = """BEGIN:VCALENDAR\r
VERSION:2.0\r
PRODID:-//test//EN\r
BEGIN:VEVENT\r
UID:u1\r
SUMMARY:Hello\r
X-CUSTOM;X-P=1:kept\r
END:VEVENT\r
END:VCALENDAR\r
"""


def test_parse_component_tree():
    cal = Component.parse(SIMPLE)
    assert cal.name == "VCALENDAR"
    assert cal.get("VERSION").value == "2.0"
    (ev,) = cal.find_children("VEVENT")
    assert ev.get("UID").value == "u1"
    assert ev.get("X-CUSTOM").param("X-P") == "1"
    assert ev.get("MISSING") is None
    assert ev.get_all("UID")[0].value == "u1"


def test_component_mutation():
    cal = Component.parse(SIMPLE)
    (ev,) = cal.find_children("VEVENT")
    ev.set("SUMMARY", "Changed")
    assert ev.get("SUMMARY").value == "Changed"
    ev.set("LOCATION", "Here")
    assert ev.get("LOCATION").value == "Here"
    ev.remove("X-CUSTOM")
    assert ev.get("X-CUSTOM") is None


def test_mismatched_end_raises():
    with pytest.raises(ValueError):
        Component.parse("BEGIN:VCALENDAR\r\nEND:VEVENT\r\n")


def test_unterminated_component_raises():
    with pytest.raises(ValueError):
        Component.parse("BEGIN:VCALENDAR\r\nVERSION:2.0\r\n")


def normalized(text: str) -> list[str]:
    return unfold(text)


@pytest.mark.parametrize("fname", sorted(p.name for p in CORPUS.glob("*.ics")))
def test_corpus_round_trip(fname):
    text = (CORPUS / fname).read_text()
    cal = Component.parse(text)
    out = cal.serialize()
    # byte-equivalent modulo folding and CRLF normalization
    assert normalized(out) == normalized(text)
    # serialized form uses CRLF and stays under fold limit
    for part in out.split("\r\n"):
        assert len(part.encode()) <= 75


def test_property_order_preserved():
    text = (CORPUS / "outlook-invite.ics").read_text()
    cal = Component.parse(text)
    (ev,) = cal.find_children("VEVENT")
    names = [l.name for l in ev.lines]
    assert names.index("ORGANIZER") < names.index("ATTENDEE") < names.index("DESCRIPTION")


def test_parse_all_multiple_vcalendars():
    from exocalendar.ical import parse_all

    two = SIMPLE + SIMPLE
    cals = parse_all(two)
    assert [c.name for c in cals] == ["VCALENDAR", "VCALENDAR"]
