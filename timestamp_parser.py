"""
timestamp_parser.py

Turns raw OCR output into a datetime. Rather than letting a general fuzzy
parser guess at the format (which is how stray OCR noise - a stray letter,
a misread separator - was leaking into results), this extracts exactly the
digit groups it expects, in the order the user has told it to expect them,
and ignores everything else as separator noise. That also removes the
DD/MM vs MM/DD ambiguity a fuzzy parser has to guess at.

Public entry point: parse_timestamp(raw_text, date_order, hour_format).
"""

import re
from datetime import datetime
from typing import Optional

DateOrder = str  # "DMY" | "MDY" | "YMD"
HourFormat = str  # "24" | "12"

_OCR_FIXUPS = {
    "O": "0", "o": "0",
    "l": "1", "I": "1", "|": "1",
    "S": "5",
    "B": "8",
}

# A separator is normally 1-2 non-digit characters, but OCR regularly reads
# a "/" as a 1 or 7 (e.g. "11/09" -> "117 09"). Allow that, but only as a
# digit *immediately followed by* a non-digit, so it can't eat into a real
# digit group.
_SEP = r"(?:\D{1,2}|[17](?=\D)\D?)"

# Date component patterns by order - named groups so we can pull them out
# regardless of order without three separate code paths downstream. The
# (?<!\d) anchor stops a match starting part-way through a digit run, which
# would otherwise silently shift the date (e.g. "117 09" matching as day 17).
# A 2-digit year must not run straight into more digits ("09/025" is a
# misread, not the year 2002); a 4-digit one may, when OCR drops the space
# before the time.
_YEAR = r"(?P<y>\d{4}|\d{2}(?!\d))"

_DATE_PATTERNS = {
    "DMY": rf"(?<!\d)(?P<d>\d{{1,2}}){_SEP}(?P<mo>\d{{1,2}}){_SEP}{_YEAR}",
    "MDY": rf"(?<!\d)(?P<mo>\d{{1,2}}){_SEP}(?P<d>\d{{1,2}}){_SEP}{_YEAR}",
    "YMD": rf"(?<!\d){_YEAR}{_SEP}(?P<mo>\d{{1,2}}){_SEP}(?P<d>\d{{1,2}})(?!\d)",
}

# 24-hour overlays always zero-pad the hour; only 12-hour ones show "9:05"
_HOUR = {"24": r"(?P<h>\d{2})", "12": r"(?P<h>\d{1,2})"}
_TIME_REST = rf"{_SEP}(?P<mi>\d{{2}}){_SEP}(?P<s>\d{{2}})"
# AM/PM is optional: plenty of 12-hour overlays don't show it
_AMPM = r"(?:\s{0,2}(?P<ap>[AaPp]\.?[Mm]\.?))?"


def _fix_common_ocr_errors(text: str) -> str:
    """Digit-run-scoped OCR letter/digit confusion fixups (O->0, l->1, etc.)."""
    def fix_token(tok: str) -> str:
        if re.fullmatch(r"[0-9OolIS|B]+", tok):
            return "".join(_OCR_FIXUPS.get(ch, ch) for ch in tok)
        return tok

    return "".join(fix_token(tok) for tok in re.findall(r"[0-9OolIS|B]+|[^0-9OolIS|B]+", text))


def _build_pattern(date_order: DateOrder, hour_format: HourFormat) -> re.Pattern:
    date_part = _DATE_PATTERNS[date_order]
    time_part = _HOUR[hour_format] + _TIME_REST + (_AMPM if hour_format == "12" else "")
    # date and time can be separated by a space, or by OCR noise the
    # preprocessing/OCR step introduced (e.g. a misread separator glyph)
    pattern = date_part + r"\D{0,3}" + time_part
    return re.compile(pattern)


def _year_to_full(y: int) -> int:
    if y >= 100:
        return y
    # 2-digit year: CCTV footage is never from before 2000 in practice
    return 2000 + y


def parse_timestamp(
    raw_text: str,
    date_order: DateOrder = "DMY",
    hour_format: HourFormat = "24",
) -> Optional[datetime]:
    """
    date_order: "DMY", "MDY" or "YMD" - which order the date components
    appear in on screen.
    hour_format: "24" or "12" (with AM/PM).
    Returns None if no plausible timestamp matching that shape is found -
    the caller should treat that as "needs manual review", not guess.
    """
    if not raw_text:
        return None

    cleaned = _fix_common_ocr_errors(raw_text.strip())
    pattern = _build_pattern(date_order, hour_format)
    match = pattern.search(cleaned)
    if not match:
        return None

    groups = match.groupdict()
    try:
        year = _year_to_full(int(groups["y"]))
        month = int(groups["mo"])
        day = int(groups["d"])
        hour = int(groups["h"])
        minute = int(groups["mi"])
        second = int(groups["s"])

        if hour_format == "12":
            ap = (groups.get("ap") or "").lower()
            if not (1 <= hour <= 12):
                return None
            # no AM/PM on screen: nothing to convert, keep the hour as read
            if ap.startswith("p") and hour != 12:
                hour += 12
            elif ap.startswith("a") and hour == 12:
                hour = 0

        dt = datetime(year, month, day, hour, minute, second)
    except (ValueError, KeyError):
        return None

    if dt.year < 2000 or dt.year > 2100:
        return None
    return dt
