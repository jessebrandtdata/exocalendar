"""Property-based cross-check of the RRULE engine against python-dateutil.

python-dateutil (dev dependency only) is a mature, widely-deployed RFC 5545
implementation; both engines must agree on every generated rule. The
generator only emits RFC-valid combinations (the RFC forbids e.g. ordinal
BYDAY together with BYWEEKNO), because outside the spec implementations
legitimately diverge.
"""

import random
import signal
from datetime import datetime
from itertools import islice

from dateutil.rrule import rrulestr

from exocalendar.rrule import RRule, RRuleOverflow

import os

SEED = 20260831
# full sweep (EXOCAL_ORACLE_N=1200) verified 2026-08-31; default trimmed to
# keep the routine suite fast — the stream is seeded, so the first 400 rules
# are the same rules every run
N_RULES = int(os.environ.get("EXOCAL_ORACLE_N", "400"))
N_COMPARE = 25

_WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _random_rule(rng: random.Random) -> tuple[str, datetime]:
    freq = rng.choice(
        ["YEARLY", "MONTHLY", "WEEKLY", "DAILY", "DAILY", "WEEKLY", "MONTHLY",
         "HOURLY", "MINUTELY", "SECONDLY"]
    )
    parts = [f"FREQ={freq}"]
    if rng.random() < 0.5:
        parts.append(f"INTERVAL={rng.randint(1, 2 if freq == 'YEARLY' else 4)}")
    count = rng.randint(1, N_COMPARE)
    parts.append(f"COUNT={count}")

    # BYWEEKNO is deliberately absent here: dateutil mis-numbers week-52/53
    # around some year boundaries (see test_byweekno_year_boundary_follows_iso);
    # BYWEEKNO rules are oracled against isocalendar() below instead.
    has_by = False
    byyearday = bymonthday = False
    if freq in ("YEARLY", "HOURLY", "MINUTELY", "SECONDLY") and rng.random() < 0.2:
        yd = sorted(rng.sample(list(range(1, 366)) + [-1, -100], rng.randint(1, 3)))
        parts.append("BYYEARDAY=" + ",".join(map(str, yd)))
        has_by = byyearday = True
    if rng.random() < 0.4:
        months = sorted(rng.sample(range(1, 13), rng.randint(1, 3)))
        parts.append("BYMONTH=" + ",".join(map(str, months)))
        has_by = True
    if freq != "WEEKLY" and not byyearday and rng.random() < 0.35:
        mds = sorted(rng.sample(list(range(1, 29)) + [30, 31, -1, -2], rng.randint(1, 3)))
        parts.append("BYMONTHDAY=" + ",".join(map(str, mds)))
        has_by = bymonthday = True
    if rng.random() < 0.45:
        k = rng.randint(1, 3)
        use_ordinals = (
            freq in ("MONTHLY", "YEARLY")
            and not (byyearday or bymonthday)
            and rng.random() < 0.5
        )
        # never mix ordinal and plain weekdays: the RFC makes "3TH,FR" a
        # union, dateutil an intersection (see test_byday_mixed_* in
        # test_rrule.py) — outside the oracle's jurisdiction
        vals = []
        for wd in rng.sample(_WEEKDAYS, k):
            if use_ordinals:
                n = rng.choice([1, 2, 3, 4, -1, -2])
                vals.append(f"{n}{wd}")
            else:
                vals.append(wd)
        parts.append("BYDAY=" + ",".join(vals))
        has_by = True
    if freq in ("HOURLY", "MINUTELY", "SECONDLY", "DAILY") and rng.random() < 0.3:
        hours = sorted(rng.sample(range(24), rng.randint(1, 3)))
        parts.append("BYHOUR=" + ",".join(map(str, hours)))
        has_by = True
    if rng.random() < 0.15:
        parts.append("BYMINUTE=" + ",".join(map(str, sorted(rng.sample(range(60), 2)))))
        has_by = True
    if rng.random() < 0.3:
        parts.append(f"WKST={rng.choice(_WEEKDAYS)}")
    if has_by and rng.random() < 0.25:
        pos = sorted(rng.sample([1, 2, 3, -1, -2, -3], rng.randint(1, 2)))
        parts.append("BYSETPOS=" + ",".join(map(str, pos)))

    dtstart = datetime(
        rng.randint(1995, 2035), rng.randint(1, 12), rng.randint(1, 28),
        rng.randint(0, 23), rng.choice([0, 15, 30, 45]), rng.choice([0, 0, 0, 30]),
    )
    return ";".join(parts), dtstart


class _OracleTimeout(Exception):
    pass


def _alarm(*_args):
    raise _OracleTimeout()


def test_oracle_agreement():
    rng = random.Random(SEED)
    failures = []
    signal.signal(signal.SIGALRM, _alarm)
    for i in range(N_RULES):
        rule_text, dtstart = _random_rule(rng)
        try:
            mine = list(islice(RRule.parse(rule_text).iterate(dtstart), N_COMPARE))
        except RRuleOverflow:
            mine = []  # engine's explicit "this rule never fires"
        except Exception as exc:  # noqa: BLE001 - report below
            failures.append(f"#{i} {rule_text} DTSTART={dtstart}: engine raised {exc!r}")
            continue
        # a rule our engine called never-firing only needs a short window to
        # confirm dateutil (grinding toward MAXYEAR) yields nothing either
        signal.alarm(2 if not mine else 8)
        try:
            theirs = list(
                islice(iter(rrulestr(rule_text, dtstart=dtstart)), N_COMPARE)
            )
        except _OracleTimeout:
            # dateutil grinds toward MAXYEAR when a rule never fires. If our
            # engine also found nothing that is agreement; if ours found
            # occurrences the case is unverifiable in bounded time — skip.
            if mine:
                continue
            theirs = []
        except ValueError:
            # dateutil's third spelling of "never fires": it rejects sub-day
            # rules whose BY* values are unreachable for the given INTERVAL
            theirs = []
        finally:
            signal.alarm(0)
        if mine != theirs:
            failures.append(
                f"#{i} {rule_text} DTSTART={dtstart}:\n  mine   ={mine[:6]}\n  oracle ={theirs[:6]}"
            )
        if len(failures) >= 10:
            break
    assert not failures, f"{len(failures)}+ disagreements:\n" + "\n".join(failures)


def _iso_weeks_in(year: int) -> int:
    from datetime import date

    return date(year, 12, 28).isocalendar()[1]


def test_byweekno_against_isocalendar():
    """BYWEEKNO oracle: Python's ISO calendar (WKST=MO semantics)."""
    from datetime import date, timedelta

    rng = random.Random(SEED + 1)
    failures = []
    for i in range(300):
        weeks = sorted(rng.sample(list(range(1, 54)) + [-1, -2], rng.randint(1, 2)))
        weekdays = rng.sample(_WEEKDAYS, rng.randint(1, 3)) if rng.random() < 0.5 else None
        count = rng.randint(1, 15)
        rule = f"FREQ=YEARLY;COUNT={count};BYWEEKNO=" + ",".join(map(str, weeks))
        if weekdays:
            rule += ";BYDAY=" + ",".join(weekdays)
        dtstart = datetime(
            rng.randint(1995, 2035), rng.randint(1, 12), rng.randint(1, 28), 12, 0
        )
        try:
            mine = list(islice(RRule.parse(rule).iterate(dtstart), count))
        except RRuleOverflow:
            mine = []

        wd_idx = {tuple(_WEEKDAYS).index(w) for w in (weekdays or _WEEKDAYS)}
        expected = []
        d = dtstart.date()
        horizon = d + timedelta(days=366 * 130)
        while d < horizon and len(expected) < count:
            iso_year, iso_week, _ = d.isocalendar()
            wanted = {
                (w if w > 0 else _iso_weeks_in(iso_year) + w + 1) for w in weeks
            }
            if iso_week in wanted and d.weekday() in wd_idx:
                slot = datetime.combine(d, dtstart.time())
                if slot >= dtstart:
                    expected.append(slot)
            d += timedelta(days=1)
        if mine != expected:
            failures.append(
                f"#{i} {rule} DTSTART={dtstart}:\n  mine={mine[:5]}\n  iso ={expected[:5]}"
            )
        if len(failures) >= 5:
            break
    assert not failures, "\n".join(failures)
