"""
Identity resolution: decide which rows are the same human being.

The problem this solves: the same customer shows up in three exports as
"Jonathan Reyes" (booking system), "Jon Reyes" (CRM), and "Jonathon Reyes"
(a typo in the payments sheet). Naive dedup on name misses all three. Dedup on
email misses the one who used a work address once.

The approach is tiered rules with explicit confidence, not a single fuzzy score:

    VETO  two usable DOBs more than a day apart  -> never merge, whatever else agrees
    1.00  same normalized name + same DOB
    0.95  same normalized name + DOB within 1 day   (keystroke errors)
    0.93  same email + same phone, no name condition
    0.90  same normalized name + same phone
    0.85  name within edit distance 2 + same email  (typos)
    0.85  name within edit distance 2 + same phone  (typos)

The 0.93 tier is the one people leave out. Every other rule here requires the names to
agree, which means a record with a mistyped name can never merge no matter how much else
lines up. Email and phone come from different systems, so both matching is independent
corroboration, and it is stronger evidence than a name that two unrelated people can
share.

Why tiers instead of one similarity score: when a merge is wrong, you need to know
WHICH rule fired so you can fix that rule. A single blended score tells you nothing
and cannot be tuned safely. Every merge here is traceable to one rule and one number.

Rules are ordered strongest-first and the first match wins, so a 1.00 match is never
downgraded by a weaker rule firing later.
"""

from collections import defaultdict
from datetime import datetime

from .normalize import levenshtein


def _parse_date(value):
    """Parse a date string leniently. Returns a date, or None if unparseable."""
    if value is None:
        return None
    text = str(value).strip()[:10]
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y", "%m-%d-%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


class _UnionFind:
    """
    Groups records into clusters.

    Needed because matching is transitive: if row A matches row B, and row B matches
    row C, then A, B and C are all the same person even though A and C may share no
    field at all. Union-find tracks that transitivity without comparing every pair
    to every other pair.
    """

    def __init__(self):
        self.parent = {}

    def find(self, item):
        self.parent.setdefault(item, item)
        # Path compression: point every node straight at the root as we go.
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, a, b):
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            self.parent[root_b] = root_a

    def clusters(self):
        groups = defaultdict(list)
        for item in self.parent:
            groups[self.find(item)].append(item)
        return list(groups.values())


def _blocking_keys(record):
    """
    Return the keys this record could match on.

    "Blocking" is the standard trick for making record linkage tractable: instead of
    comparing all N^2 pairs, only compare records that share at least one cheap key.
    On 12,000 records N^2 is 72 million comparisons; blocking cuts that to thousands.
    """
    keys = []
    name = record.get("name_normalized")
    if name:
        keys.append(("name", name))
        # A surname key lets typo'd first names still find each other.
        parts = name.split()
        if len(parts) > 1:
            keys.append(("surname", parts[-1]))
    if record.get("email_normalized"):
        keys.append(("email", record["email_normalized"]))
    if record.get("phone_normalized"):
        keys.append(("phone", record["phone_normalized"]))
    return keys


def _compare(a, b):
    """
    Apply the tiered rules to one pair of records.

    Returns (confidence, rule_name), or (0.0, None) when they are not the same person.
    """
    name_a, name_b = a.get("name_normalized"), b.get("name_normalized")
    dob_a, dob_b = _parse_date(a.get("date_of_birth")), _parse_date(b.get("date_of_birth"))
    dob_usable = (
        dob_a and dob_b
        and not a.get("dob_is_placeholder") and not b.get("dob_is_placeholder")
    )

    email_a, email_b = a.get("email_normalized"), b.get("email_normalized")
    phone_a, phone_b = a.get("phone_normalized"), b.get("phone_normalized")

    # VETO FIRST. Two usable dates of birth more than a day apart mean these are two
    # different people, and no amount of other agreement changes that. Without this, a
    # father and son on the same household phone merged on name + phone, and a mother
    # and son on the family email merged on email + phone. Disconfirming evidence has to
    # outrank confirming evidence or the rule set only ever argues one side.
    if dob_usable and abs((dob_a - dob_b).days) > 1:
        return 0.0, None

    # Rules below are ordered strongest first and the first match wins.
    if name_a and name_b and name_a == name_b:
        if dob_usable and dob_a == dob_b:
            return 1.00, "exact_name_exact_dob"
        if dob_usable:                       # already within 1 day, the veto guarantees it
            return 0.95, "exact_name_dob_within_1_day"
        if phone_a and phone_a == phone_b:
            return 0.90, "exact_name_exact_phone"

    # Email and phone both agreeing, with no name condition. Placed AFTER the name+DOB
    # tiers, not before: an earlier version had it first, which meant byte-identical
    # records scored 0.93 instead of 1.00 and were rejected outright at
    # --min-confidence 0.95, the opposite of what raising the threshold should do.
    #
    # Known limit: household members who share both an inbox and a phone AND have no
    # date of birth on file will still merge here. The veto above catches them whenever
    # a DOB exists on both sides, which is the common case in booking data. Without one,
    # this rule cannot tell a family from a person. See test_known_limit_* in the tests.
    if email_a and email_a == email_b and phone_a and phone_a == phone_b:
        return 0.93, "exact_email_exact_phone"

    if name_a and name_b and levenshtein(name_a, name_b) <= 2:
        if email_a and email_a == email_b:
            return 0.85, "fuzzy_name_exact_email"
        # The mirror image, and it was missing. A typo'd name with a matching phone is
        # exactly as strong as a typo'd name with a matching email, and leaving it out
        # meant more than half the unmatched pairs were ordinary typos on records that
        # shared a phone but no email.
        if phone_a and phone_a == phone_b:
            return 0.85, "fuzzy_name_exact_phone"

    return 0.0, None


def resolve_identities(records, min_confidence=0.85):
    """
    Cluster raw records into people.

    Args:
        records: list of dicts, each already passed through normalization.
        min_confidence: merges scoring below this are not applied.

    Returns:
        (clusters, merge_log)
        clusters:  list of lists of record indexes, one list per resolved person
        merge_log: every applied merge as
                   {"a": i, "b": j, "confidence": float, "rule": str}

    The merge log is the point. Any dedup can produce a smaller file; only a logged
    dedup lets you answer "why did these two become one person?" six months later,
    when someone insists their record is wrong.
    """
    blocks = defaultdict(list)
    for index, record in enumerate(records):
        for key in _blocking_keys(record):
            blocks[key].append(index)

    uf = _UnionFind()
    for index in range(len(records)):
        uf.find(index)  # every record starts as its own cluster

    merge_log = []
    seen_pairs = set()

    for candidates in blocks.values():
        # Skip absurdly large blocks: a shared key that thousands of rows have is
        # almost always junk data (an empty string, a placeholder office number),
        # and comparing inside it is both slow and dangerous.
        if len(candidates) > 100:
            continue
        for position, i in enumerate(candidates):
            for j in candidates[position + 1:]:
                pair = (i, j) if i < j else (j, i)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                confidence, rule = _compare(records[i], records[j])
                if confidence >= min_confidence:
                    uf.union(i, j)
                    merge_log.append(
                        {"a": pair[0], "b": pair[1], "confidence": confidence, "rule": rule}
                    )

    return uf.clusters(), merge_log
