"""
Identity resolution: decide which rows are the same human being.

The problem this solves: the same customer shows up in three exports as
"Jonathan Reyes" (booking system), "Jon Reyes" (CRM), and "Jonathon Reyes"
(a typo in the payments sheet). Naive dedup on name misses all three. Dedup on
email misses the one who used a work address once.

The approach is tiered rules with explicit confidence, not a single fuzzy score:

    1.00  same normalized name + same DOB
    0.95  same normalized name + DOB within 1 day   (keystroke errors)
    0.90  same normalized name + same phone
    0.85  name within edit distance 2 + same email  (typos)

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

    if name_a and name_b and name_a == name_b:
        if dob_usable and dob_a == dob_b:
            return 1.00, "exact_name_exact_dob"
        if dob_usable and abs((dob_a - dob_b).days) <= 1:
            return 0.95, "exact_name_dob_within_1_day"
        phone_a, phone_b = a.get("phone_normalized"), b.get("phone_normalized")
        if phone_a and phone_a == phone_b:
            return 0.90, "exact_name_exact_phone"

    email_a, email_b = a.get("email_normalized"), b.get("email_normalized")
    if email_a and email_a == email_b and name_a and name_b:
        if levenshtein(name_a, name_b) <= 2:
            return 0.85, "fuzzy_name_exact_email"

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
