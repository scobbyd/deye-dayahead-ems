"""Keep the ladder card's period where the reader left it when the grain changes.

The card ("The ladder" on the private dashboard tab, not shipped) selects its period
with two helpers: input_select.emhass_ladder_grain (week, month, year) and
input_number.emhass_ladder_offset, an integer count of GRAIN units back from
the period containing today. The offset is in units of the grain, so a reader
eleven weeks back who switches to the year grain landed in 2015 (observed
2026-09-14). This converts the offset on every grain change so the period the
reader was looking at stays in view: the new period is the one containing the
START of the old one (April at month grain becomes the week of 1 April; that
week becomes April; a year becomes its January). Weeks start on Monday, as
the card counts them. The offset can never point past today.
"""
from datetime import date, timedelta

GRAIN = "input_select.emhass_ladder_grain"
OFFSET = "input_number.emhass_ladder_offset"


def _period_start(grain, off, today):
    if grain == "year":
        return date(today.year + off, 1, 1)
    if grain == "month":
        m = today.month - 1 + off
        return date(today.year + m // 12, m % 12 + 1, 1)
    monday = today - timedelta(days=today.weekday())
    return monday + timedelta(weeks=off)


def offset_for(grain, anchor, today):
    """The offset that puts `anchor`'s period of `grain` in view, never > 0."""
    if grain == "year":
        off = anchor.year - today.year
    elif grain == "month":
        off = (anchor.year - today.year) * 12 + (anchor.month - today.month)
    else:
        off = (anchor - timedelta(days=anchor.weekday()) - (today - timedelta(days=today.weekday()))).days // 7
    return min(0, off)


def convert_offset(old_grain, new_grain, off, today):
    return offset_for(new_grain, _period_start(old_grain, off, today), today)


@state_trigger(f"{GRAIN}")
def emhass_ladder_grain_changed(var_name=None, value=None, old_value=None):
    if value not in ("week", "month", "year") or old_value not in ("week", "month", "year") or value == old_value:
        return
    try:
        off = int(round(float(state.get(OFFSET))))
    except (TypeError, ValueError):
        return
    new_off = convert_offset(old_value, value, off, date.today())
    if new_off != off:
        input_number.set_value(entity_id=OFFSET, value=new_off)
        log.info(f"ladder grain {old_value} -> {value}: offset {off} -> {new_off}")
