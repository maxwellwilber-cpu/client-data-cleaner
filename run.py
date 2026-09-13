#!/usr/bin/env python3
"""
Entry point.

    python run.py                      # run on the bundled sample data
    python run.py --generate           # regenerate sample data first, then run
    python run.py --clients a.csv b.csv --transactions tx.csv --output out/

Exit code is 0 on success, 1 if the pipeline could not produce a master list.
That makes it safe to drop into a scheduled job or CI step.
"""

import argparse
import glob
import sys

from cleaner.pipeline import run


def main():
    parser = argparse.ArgumentParser(description="Clean and segment messy customer data.")
    parser.add_argument("--clients", nargs="+", help="Client/contact CSV files")
    parser.add_argument("--transactions", help="Transaction CSV file")
    parser.add_argument("--output", default="output", help="Output directory")
    parser.add_argument("--generate", action="store_true", help="Regenerate sample data first")
    parser.add_argument("--min-confidence", type=float, default=0.85,
                        help="Minimum match confidence to merge two records (0-1)")
    args = parser.parse_args()

    if args.generate:
        from generate_sample_data import generate
        generate()
        print("Regenerated sample data.\n")

    clients = args.clients
    transactions = args.transactions
    if not clients:
        # Only the three client/contact exports are inputs. transactions.csv is the
        # revenue log and ground_truth.csv is the answer key used by verify.py —
        # feeding either one in as a contact list silently corrupts the run, which is
        # exactly what happened the first time this glob was written too loosely.
        excluded = ("transactions", "ground_truth")
        clients = sorted(
            path for path in glob.glob("sample_data/*.csv")
            if not any(name in path for name in excluded)
        )
        transactions = "sample_data/transactions.csv"
        print("No input given — running on bundled sample data.\n")

    if not clients or not transactions:
        print("Error: need at least one client file and a transaction file.", file=sys.stderr)
        return 1

    stats = run(clients, transactions, output_dir=args.output,
                min_confidence=args.min_confidence)

    print("=" * 58)
    print("  RESULTS")
    print("=" * 58)
    print(f"  Source rows read              {stats['source_rows']:>8,}")
    print(f"  Unique people resolved        {stats['unique_people']:>8,}")
    print(f"  Duplicate rows collapsed      {stats['duplicates_collapsed']:>8,}")
    print(f"  Merges applied (all logged)   {stats['merges_applied']:>8,}")
    print(f"  Placeholder birthdays flagged {stats['placeholder_dobs_flagged']:>8,}")
    print("-" * 58)
    print(f"  Transactions processed        {stats['transactions']:>8,}")
    print(f"  Attribution rate              {stats['attribution_rate']:>7}%")
    print(f"  Customers scored              {stats['customers_scored']:>8,}")
    print("-" * 58)
    print(f"  Reactivation targets          {stats['reactivation_targets']:>8,}")
    print(f"  Value of lapsed customers     ${stats['reactivation_value']:>10,.0f}")
    print("-" * 58)
    print("  Segments:")
    for segment, count in sorted(stats["segments"].items(), key=lambda x: -x[1]):
        print(f"    {segment:<22}{count:>6,}")
    print("=" * 58)
    print(f"\n  Written to ./{args.output}/")
    print("    clients_master.csv        one row per real person")
    print("    customer_segments.csv     RFM scores and segment per person")
    print("    reactivation_targets.csv  lapsed customers ranked by spend")
    print("    merge_log.csv             every merge, with rule and confidence")

    return 0 if stats["unique_people"] else 1


if __name__ == "__main__":
    sys.exit(main())
