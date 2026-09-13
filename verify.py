#!/usr/bin/env python3
"""
Measure how accurate the identity matching actually is.

Any dedup tool can produce a smaller file. The question that matters is whether it
merged the RIGHT rows, and almost no data-cleaning code answers it. This script does,
by running the matcher against generated data whose true answers are known.

The metric is pairwise, which is the standard way to score record linkage:

    For every pair of rows the pipeline put in the same cluster,
    was that pair really the same person?              -> precision
    For every pair that really is the same person,
    did the pipeline find it?                          -> recall

Precision and recall trade off against each other, and which one you want depends on
the job. Merging two different customers into one record (low precision) is usually
worse than leaving one customer as two records (low recall): a wrong merge sends the
wrong person an email about someone else's account. This pipeline is tuned to favour
precision for that reason.

Ground truth is never given to the pipeline — only to this script, afterwards.
"""

import sys
from collections import defaultdict
from itertools import combinations

import pandas as pd

from cleaner.pipeline import ingest, normalize_records
from cleaner.matching import resolve_identities

CLIENT_FILES = [
    "sample_data/booking_export.csv",
    "sample_data/crm_contacts.csv",
    "sample_data/payments_sheet.csv",
]


def pairs_within(groups):
    """Every unordered pair of items sharing a group."""
    out = set()
    for members in groups.values():
        for pair in combinations(sorted(members), 2):
            out.add(pair)
    return out


def main(min_confidence=0.85):
    raw = ingest(CLIENT_FILES)
    records = normalize_records(raw)
    clusters, merge_log = resolve_identities(records, min_confidence=min_confidence)

    truth_df = pd.read_csv("sample_data/ground_truth.csv")
    truth_lookup = {
        (row.source_file, row.source_row): row.true_person_id
        for row in truth_df.itertuples()
    }

    # Group record indexes by the person the pipeline THINKS they are.
    predicted = {i: idxs for i, idxs in enumerate(clusters)}
    # Group the same indexes by who they REALLY are.
    actual = defaultdict(list)
    for i, record in enumerate(records):
        key = (record["source_file"], record["source_row"])
        actual[truth_lookup[key]].append(i)

    predicted_pairs = pairs_within(predicted)
    actual_pairs = pairs_within(actual)

    true_positives = predicted_pairs & actual_pairs
    false_positives = predicted_pairs - actual_pairs   # wrongly merged
    false_negatives = actual_pairs - predicted_pairs   # missed merges

    precision = len(true_positives) / len(predicted_pairs) if predicted_pairs else 1.0
    recall = len(true_positives) / len(actual_pairs) if actual_pairs else 1.0
    f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0

    # Which rule produced each wrong merge — this is what makes failures fixable.
    rule_stats = defaultdict(lambda: {"correct": 0, "wrong": 0})
    for merge in merge_log:
        pair = (merge["a"], merge["b"])
        bucket = "correct" if pair in actual_pairs else "wrong"
        rule_stats[merge["rule"]][bucket] += 1

    print("=" * 58)
    print("  MATCHING ACCURACY  (measured against known ground truth)")
    print("=" * 58)
    print(f"  Rows in                       {len(records):>8,}")
    print(f"  People the pipeline found     {len(clusters):>8,}")
    print(f"  People that actually exist    {len(actual):>8,}")
    print("-" * 58)
    print(f"  Precision                     {precision:>8.2%}   (merges that were right)")
    print(f"  Recall                        {recall:>8.2%}   (real duplicates found)")
    print(f"  F1                            {f1:>8.2%}")
    print("-" * 58)
    print(f"  Correct merges                {len(true_positives):>8,}")
    print(f"  Wrong merges                  {len(false_positives):>8,}")
    print(f"  Missed duplicates             {len(false_negatives):>8,}")
    print("-" * 58)
    print("  Per rule:")
    print(f"    {'rule':<32}{'right':>7}{'wrong':>7}")
    for rule, counts in sorted(rule_stats.items(), key=lambda x: -x[1]["correct"]):
        print(f"    {rule:<32}{counts['correct']:>7}{counts['wrong']:>7}")
    print("=" * 58)

    # A rule that is wrong more often than right is worse than no rule.
    broken = [r for r, c in rule_stats.items() if c["wrong"] > c["correct"]]
    if broken:
        print(f"\n  WARNING: these rules do more harm than good: {', '.join(broken)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
