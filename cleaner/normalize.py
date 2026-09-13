"""
Field-level normalization.

Every function here takes one raw value and returns a cleaned value (or None when the
input is unusable). They are deliberately boring and deterministic: no fuzzy logic, no
guessing. Fuzzy work happens later, in matching.py, where it can be scored.

The reason to separate them: normalization is what makes matching cheap. If two records
both say "(310) 555-0142" and "+1 310-555-0142", you do not need fuzzy matching at all
once both are normalized to "3105550142". Clean first, match second.
"""

import re
import unicodedata
from datetime import datetime

# US state names and common misspellings -> USPS two-letter code.
# Real exports contain all of these. This dict is the boring fix.
_STATE_MAP = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "calif": "CA", "cali": "CA", "ca.": "CA",
    "colorado": "CO", "connecticut": "CT", "delaware": "DE", "florida": "FL",
    "fla": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "ill": "IL", "indiana": "IN", "iowa": "IA",
    "kansas": "KS", "kentucky": "KY", "louisiana": "LA", "maine": "ME",
    "maryland": "MD", "massachusetts": "MA", "mass": "MA", "michigan": "MI",
    "minnesota": "MN", "mississippi": "MS", "missouri": "MO", "montana": "MT",
    "nebraska": "NE", "nevada": "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC",
    "north dakota": "ND", "ohio": "OH", "oklahoma": "OK", "oregon": "OR",
    "pennsylvania": "PA", "penn": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "tenn": "TN",
    "texas": "TX", "utah": "UT", "vermont": "VT", "virginia": "VA",
    "washington": "WA", "wash": "WA", "west virginia": "WV", "wisconsin": "WI",
    "wisc": "WI", "wyoming": "WY", "district of columbia": "DC",
}

# Dates that mean "nobody entered a real birthday."
# 1/1/1900 and 1/1/1990 are lazy defaults; 12/31/1969 and 1/1/1970 are Unix epoch
# bleed-through from a system that stored 0 as a timestamp.
# Kept deliberately short. 1990-01-01 and 2000-01-01 were on this list and have been
# removed: those are ordinary birthdays, and flagging them stripped real people of their
# strongest matching signal. A placeholder list that eats real data is worse than a
# shorter one that misses a few.
_PLACEHOLDER_DOBS = {
    "1900-01-01", "1969-12-31", "1970-01-01", "1800-01-01", "1111-11-11",
}


def normalize_phone(value):
    """
    Return a 10-digit US phone string, or None.

    Handles: (310) 555-0142, 310.555.0142, +1 310 555 0142, 1-310-555-0142.
    Rejects anything that is not a plausible 10-digit US number, because a bad
    phone number is worse than no phone number: it will false-match two people.
    """
    if value is None:
        return None
    digits = re.sub(r"\D", "", str(value))
    # Strip a leading country code if it left us with 11 digits.
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return None
    # US area codes and exchanges never start with 0 or 1.
    if digits[0] in "01" or digits[3] in "01":
        return None
    return digits


def normalize_email(value):
    """
    Return a comparable email string, or None.

    Lowercases and trims. For Gmail specifically, strips dots from the local part
    and drops +tags, because john.doe+receipts@gmail.com and johndoe@gmail.com are
    literally the same inbox. Doing this for every domain would be wrong: plenty of
    providers treat dots as significant.
    """
    if value is None:
        return None
    email = str(value).strip().lower()
    if "@" not in email or email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or "." not in domain:
        return None
    if domain in ("gmail.com", "googlemail.com"):
        local = local.split("+")[0].replace(".", "")
        domain = "gmail.com"
    if not local:
        return None
    return f"{local}@{domain}"


def normalize_state(value):
    """Return a USPS two-letter state code, or None."""
    if value is None:
        return None
    raw = str(value).strip().lower().rstrip(".")
    if not raw:
        return None
    if len(raw) == 2 and raw.isalpha():
        return raw.upper()
    return _STATE_MAP.get(raw)


def normalize_name(value):
    """
    Return a comparable name string, or None.

    Strips accents (José -> jose), removes punctuation (O'Brien -> obrien),
    collapses whitespace, lowercases. This is for COMPARISON only — always keep the
    original spelling for anything a human will read.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    # NFKD splits accented characters into base + accent, then we drop the accents.
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower() or None


def _to_iso_date(value):
    """
    Parse a date written any of the common ways and return it as YYYY-MM-DD, or None.

    Exists because the placeholder check used to compare raw text against a set of ISO
    strings. An export writing 1/1/1900 therefore sailed through undetected, the record
    kept a usable date of birth, and two unrelated people sharing a name merged at
    confidence 1.00 on a birthday neither of them has. Any comparison against a known
    date has to normalize the format first.
    """
    if value is None:
        return None
    text = str(value).strip()[:10]
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            continue
    return None


def is_placeholder_dob(value):
    """
    True when a date of birth is a known filler value, in any common date format.

    Flagging beats deleting. A flagged birthday can still be displayed; it just must
    never be trusted as matching evidence, which is where fake dates do real damage.
    """
    iso = _to_iso_date(value)
    return iso in _PLACEHOLDER_DOBS if iso else False


def levenshtein(a, b):
    """
    Edit distance between two strings, iteratively (no recursion limit problems).

    Used by the matching tier that catches typos: "jonathon" vs "jonathan" is
    distance 1. Written out rather than imported so the repo has no extra dependency
    for one small function.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        current = [i]
        for j, cb in enumerate(b, start=1):
            cost = 0 if ca == cb else 1
            current.append(min(
                previous[j] + 1,      # deletion
                current[j - 1] + 1,   # insertion
                previous[j - 1] + cost,  # substitution
            ))
        previous = current
    return previous[-1]
