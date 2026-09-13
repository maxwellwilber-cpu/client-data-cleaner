"""
The pipeline: messy files in, one clean master out.

Stages, in order:

    1. Ingest      - read every source file, remember which file each row came from
    2. Normalize   - clean every field (normalize.py)
    3. Resolve     - cluster rows into people (matching.py)
    4. Golden      - build one best record per person
    5. Attribute   - link transactions to the resolved person, not the raw row
    6. Score       - RFM segmentation (rfm.py)
    7. Export      - write the master, the segments, and the merge log

Two design decisions worth calling out:

Nothing is deleted. Duplicate rows are merged into a person and the merge log records
why. Placeholder birthdays are flagged rather than blanked. If the pipeline gets a
merge wrong, the evidence to find and fix it still exists, which is not true of a
pipeline that quietly drops rows.

Attribution accuracy is measured, not assumed. Stage 5 reports what percentage of
transactions were linked to a resolved person. That number is the honest headline for
any data-cleaning job: a master list nobody can tie revenue to is a phone book.
"""

import pandas as pd

from . import normalize as N
from .matching import resolve_identities
from .rfm import score_customers, reactivation_targets


# Column names this pipeline understands, mapped to the many things real exports call them.
_FIELD_ALIASES = {
    "name": ["name", "full_name", "customer_name", "client_name", "contact"],
    "first_name": ["first_name", "firstname", "fname", "given_name"],
    "last_name": ["last_name", "lastname", "lname", "surname", "family_name"],
    "email": ["email", "email_address", "e_mail", "primary_email"],
    "phone": ["phone", "phone_number", "mobile", "cell", "telephone", "primary_phone"],
    "state": ["state", "st", "province", "region"],
    "date_of_birth": ["date_of_birth", "dob", "birthdate", "birth_date"],
}


def _find_column(df, field):
    """Locate a single column by any of its known aliases, case-insensitively."""
    lookup = {c.lower().strip(): c for c in df.columns}
    for alias in _FIELD_ALIASES.get(field, []):
        if alias in lookup:
            return lookup[alias]
    return None


def _find_columns(df, field):
    """
    Locate EVERY column matching a field's aliases.

    This exists because sources rarely agree on column names: the booking export
    calls it `email_address`, the CRM calls it `e_mail`, the payments sheet calls it
    `email`. Once those files are concatenated, all three columns are present and
    each row has a value in exactly one of them.

    Picking a single column here silently blanks the field for every row that came
    from a different file — which looks like "these customers have no email" rather
    than like a bug, and quietly destroys matching. Returning all of them and
    coalescing per row is the fix.
    """
    lookup = {c.lower().strip(): c for c in df.columns}
    return [lookup[alias] for alias in _FIELD_ALIASES.get(field, []) if alias in lookup]


def _coalesce(row, columns):
    """First non-empty value across a set of candidate columns."""
    for column in columns:
        value = row.get(column)
        if value is not None and pd.notna(value) and str(value).strip():
            return value
    return None


def ingest(paths):
    """
    Read every source file into one frame, tagging each row with its origin.

    source_file and source_row survive the whole pipeline so any value in the final
    master can be traced back to the exact line it came from.
    """
    frames = []
    for path in paths:
        df = pd.read_csv(path, dtype=str, keep_default_na=False, na_values=[""])
        df["source_file"] = str(path).split("/")[-1]
        df["source_row"] = range(len(df))
        frames.append(df)
    if not frames:
        raise ValueError("No source files provided.")
    return pd.concat(frames, ignore_index=True)


def normalize_records(df):
    """Apply field normalization, returning a list of dicts ready for matching."""
    # All candidate columns per field, because concatenated sources disagree on names.
    col = {field: _find_columns(df, field) for field in _FIELD_ALIASES}

    records = []
    for _, row in df.iterrows():
        # Build a display name from whatever this row's source file offers:
        # a single full-name column, or separate first/last columns.
        display_name = _coalesce(row, col["name"])
        if display_name:
            display_name = str(display_name).strip()
        else:
            first = _coalesce(row, col["first_name"])
            last = _coalesce(row, col["last_name"])
            parts = [str(p).strip() for p in (first, last) if p]
            display_name = " ".join(parts)

        email_raw = _coalesce(row, col["email"])
        phone_raw = _coalesce(row, col["phone"])
        state_raw = _coalesce(row, col["state"])
        dob = _coalesce(row, col["date_of_birth"])

        records.append({
            "source_file": row["source_file"],
            "source_row": row["source_row"],
            "name_display": display_name or None,
            "name_normalized": N.normalize_name(display_name),
            "email_raw": email_raw,
            "email_normalized": N.normalize_email(email_raw),
            "phone_raw": phone_raw,
            "phone_normalized": N.normalize_phone(phone_raw),
            "state": N.normalize_state(state_raw),
            "date_of_birth": dob,
            "dob_is_placeholder": N.is_placeholder_dob(dob),
        })
    return records


def build_golden_records(records, clusters):
    """
    Collapse each cluster into one best record.

    Field-by-field, the most common non-null value wins, ties broken by first seen.
    Picking per field rather than picking a single "best row" matters: the row with
    the good phone number often has a missing email, and vice versa. Choosing per
    field keeps both.
    """
    golden = []
    for person_id, indexes in enumerate(sorted(clusters, key=min), start=1):
        members = [records[i] for i in indexes]

        def best(field):
            values = [m[field] for m in members if m.get(field)]
            if not values:
                return None
            # Most frequent value; ties go to the earliest occurrence.
            return max(set(values), key=lambda v: (values.count(v), -values.index(v)))

        golden.append({
            "person_id": person_id,
            "name": best("name_display"),
            "name_normalized": best("name_normalized"),
            "email": best("email_normalized"),
            "phone": best("phone_normalized"),
            "state": best("state"),
            "date_of_birth": best("date_of_birth"),
            "dob_is_placeholder": any(m["dob_is_placeholder"] for m in members),
            "source_records": len(members),
            "source_files": ", ".join(sorted({m["source_file"] for m in members})),
        })
    return pd.DataFrame(golden)


def attribute_transactions(transactions, master, records, clusters):
    """
    Link every transaction to a resolved person_id.

    Transactions carry whatever customer key their own system used, so we match on
    email first (most reliable), then phone, then normalized name. Anything that
    matches nothing is kept with a null person_id rather than dropped — unattributed
    revenue is a finding, not a rounding error.

    Returns (attributed_transactions, attribution_rate).
    """
    # Map each raw record index to its person_id.
    index_to_person = {}
    for person_id, indexes in enumerate(sorted(clusters, key=min), start=1):
        for i in indexes:
            index_to_person[i] = person_id

    by_email, by_phone, by_name = {}, {}, {}
    for i, record in enumerate(records):
        person_id = index_to_person.get(i)
        if record.get("email_normalized"):
            by_email.setdefault(record["email_normalized"], person_id)
        if record.get("phone_normalized"):
            by_phone.setdefault(record["phone_normalized"], person_id)
        if record.get("name_normalized"):
            by_name.setdefault(record["name_normalized"], person_id)

    df = transactions.copy()
    email_cols = _find_columns(df, "email")
    phone_cols = _find_columns(df, "phone")
    name_cols = _find_columns(df, "name")

    def resolve(row):
        # Email first (most reliable), then phone, then name. Name alone is the
        # weakest key and is only reached when the other two are absent.
        key = N.normalize_email(_coalesce(row, email_cols))
        if key and key in by_email:
            return by_email[key]
        key = N.normalize_phone(_coalesce(row, phone_cols))
        if key and key in by_phone:
            return by_phone[key]
        key = N.normalize_name(_coalesce(row, name_cols))
        if key and key in by_name:
            return by_name[key]
        return None

    df["person_id"] = df.apply(resolve, axis=1)
    rate = df["person_id"].notna().mean() if len(df) else 0.0
    return df, rate


def run(client_files, transaction_file, output_dir="output", min_confidence=0.85):
    """
    Run the whole pipeline and write the outputs.

    Returns a dict of summary statistics — the numbers you would actually report
    back to a client.
    """
    import os
    os.makedirs(output_dir, exist_ok=True)

    raw = ingest(client_files)
    records = normalize_records(raw)
    clusters, merge_log = resolve_identities(records, min_confidence=min_confidence)
    master = build_golden_records(records, clusters)

    transactions = pd.read_csv(transaction_file, dtype=str, keep_default_na=False, na_values=[""])
    transactions["amount"] = pd.to_numeric(transactions["amount"], errors="coerce")
    transactions = transactions.dropna(subset=["amount"])
    attributed, attribution_rate = attribute_transactions(transactions, master, records, clusters)

    linked = attributed.dropna(subset=["person_id"]).copy()
    linked["person_id"] = linked["person_id"].astype(int)
    scored = score_customers(linked)
    scored = scored.merge(master, on="person_id", how="left")
    targets = reactivation_targets(scored)

    master.to_csv(f"{output_dir}/clients_master.csv", index=False)
    scored.to_csv(f"{output_dir}/customer_segments.csv", index=False)
    targets.to_csv(f"{output_dir}/reactivation_targets.csv", index=False)
    pd.DataFrame(merge_log).to_csv(f"{output_dir}/merge_log.csv", index=False)

    placeholder_count = int(master["dob_is_placeholder"].sum())
    return {
        "source_rows": len(raw),
        "unique_people": len(master),
        "duplicates_collapsed": len(raw) - len(master),
        "merges_applied": len(merge_log),
        "placeholder_dobs_flagged": placeholder_count,
        "transactions": len(attributed),
        "attribution_rate": round(attribution_rate * 100, 2),
        "customers_scored": len(scored),
        "reactivation_targets": len(targets),
        "reactivation_value": round(float(targets["monetary"].sum()), 2) if len(targets) else 0.0,
        "segments": scored["segment"].value_counts().to_dict(),
    }
