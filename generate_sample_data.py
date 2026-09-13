"""
Generate synthetic messy customer data.

The point of this file is that the repo is runnable by anyone in 10 seconds with no
real customer data involved. Every defect it injects is one seen in real exports:

    - the same person in multiple files under different name spellings
    - phone numbers in six different formats
    - Gmail dot-aliases and +tags
    - state written as CA / California / Calif. / cali
    - placeholder birthdays (1/1/1900, Unix epoch bleed-through)
    - typos in names introduced by manual entry
    - missing fields, inconsistently

Deterministic by default (seed=42) so the README's numbers reproduce exactly.
"""

import csv
import random
from datetime import date, timedelta

FIRST_NAMES = ["Jonathan", "Maria", "David", "Sarah", "Miguel", "Emily", "James",
               "Priya", "Robert", "Ana", "Kevin", "Rachel", "Thomas", "Nicole",
               "Daniel", "Jessica", "Andrew", "Laura", "Christopher", "Michelle"]
LAST_NAMES = ["Reyes", "Lopez", "Chen", "Okafor", "Martinez", "Nguyen", "Patel",
              "Johnson", "Kim", "Rodriguez", "Williams", "Brown", "Garcia",
              "Muller", "Silva", "OBrien", "Anderson", "Thompson", "Walker", "Hall"]
STATE_VARIANTS = ["CA", "California", "Calif.", "cali", "ca", "WA", "Washington",
                  "wash", "NY", "New York", "TX", "Texas", "tex"]
# Must stay in sync with normalize._PLACEHOLDER_DOBS. 1990-01-01 was here and is not a
# placeholder: it is a plausible birthday, and emitting it as fake while the detector
# treats it as real made the pipeline see a genuine DOB conflict where none existed.
PLACEHOLDER_DOBS = ["1900-01-01", "1969-12-31", "1970-01-01", "1800-01-01"]


def _phone_variant(digits, rng):
    """Format the same 10 digits one of six ways, as real systems do."""
    a, b, c = digits[:3], digits[3:6], digits[6:]
    return rng.choice([
        f"({a}) {b}-{c}", f"{a}-{b}-{c}", f"{a}.{b}.{c}",
        f"+1 {a} {b} {c}", f"1-{a}-{b}-{c}", digits,
    ])


def _shift_a_day(iso, rng):
    """Move a date one day, the way a typed digit goes wrong."""
    from datetime import date, timedelta
    y, m, d = (int(x) for x in iso.split("-"))
    return (date(y, m, d) + timedelta(days=rng.choice([-1, 1]))).isoformat()


def _typo(text, rng):
    """Introduce one realistic keystroke error."""
    if len(text) < 4:
        return text
    i = rng.randrange(1, len(text) - 1)
    kind = rng.choice(["swap", "drop", "double"])
    if kind == "swap":
        return text[:i] + text[i + 1] + text[i] + text[i + 2:]
    if kind == "drop":
        return text[:i] + text[i + 1:]
    return text[:i] + text[i] + text[i:]


def generate(n_people=300, seed=42, outdir="sample_data"):
    import os
    os.makedirs(outdir, exist_ok=True)
    rng = random.Random(seed)

    people = []
    # 20 first names x 20 last names means name collisions are guaranteed at n=300.
    # That is realistic (two Maria Lopezes is normal), but their EMAIL ADDRESSES must
    # still differ, because email providers enforce uniqueness. Real signup flows append
    # a number when the handle is taken; this does the same.
    #
    # The first version of this generator gave both Marias maria.lopez@gmail.com, and
    # verify.py caught it immediately: the fuzzy-name + exact-email rule scored 133 wrong
    # merges against 80 right ones. The rule was fine; the fake data was impossible.
    # That is exactly what a verification harness is for.
    handle_counts = {}
    for i in range(n_people):
        first = rng.choice(FIRST_NAMES)
        last = rng.choice(LAST_NAMES)
        digits = f"{rng.randrange(200, 999)}{rng.randrange(200, 999)}{rng.randrange(1000, 9999)}"
        gmail = rng.random() < 0.55
        domain = "gmail.com" if gmail else rng.choice(["outlook.com", "yahoo.com", "icloud.com"])
        base_handle = f"{first.lower()}.{last.lower()}"
        seen = handle_counts.get(base_handle, 0)
        handle_counts[base_handle] = seen + 1
        local = base_handle if seen == 0 else f"{base_handle}{seen + 1}"
        dob = date(1960, 1, 1) + timedelta(days=rng.randrange(0, 60 * 365))
        people.append({
            "first": first, "last": last, "digits": digits, "gmail": gmail,
            "local": local, "domain": domain, "dob": dob.isoformat(),
            "state": rng.choice(STATE_VARIANTS),
        })

    booking, crm, payments, transactions = [], [], [], []
    # Ground truth: which real person each written row belongs to. Never fed to the
    # pipeline — it exists so accuracy can be measured instead of asserted.
    truth = []

    for pid, p in enumerate(people):
        name = f"{p['first']} {p['last']}"
        email = f"{p['local']}@{p['domain']}"

        # --- Booking system: everyone appears here, the cleanest source ---
        truth.append({"source_file": "booking_export.csv", "source_row": len(booking),
                      "true_person_id": pid})
        booking.append({
            "full_name": name,
            "email_address": email,
            "primary_phone": _phone_variant(p["digits"], rng),
            "state": p["state"],
            "dob": p["dob"] if rng.random() > 0.15 else rng.choice(PLACEHOLDER_DOBS),
        })

        # --- CRM: ~55% also here, with drift ---
        if rng.random() < 0.55:
            truth.append({"source_file": "crm_contacts.csv", "source_row": len(crm),
                          "true_person_id": pid})
            crm_first = p["first"][:3] if rng.random() < 0.2 else p["first"]  # nickname
            crm_name = f"{crm_first} {p['last']}"
            if rng.random() < 0.25:
                crm_name = _typo(crm_name, rng)
            # Gmail users often re-enter with dots or a +tag
            if p["gmail"] and rng.random() < 0.5:
                crm_email = f"{p['local'].replace('.', '')}+news@gmail.com"
            else:
                crm_email = email
            crm.append({
                "name": crm_name,
                "e_mail": crm_email if rng.random() > 0.12 else "",
                "mobile": _phone_variant(p["digits"], rng) if rng.random() > 0.2 else "",
                "st": p["state"],
                # 15% get a date one day off, which is what a keystroke error looks
                # like in practice. Without these the 0.95 tier is advertised in the
                # README and never once exercised by the benchmark.
                "birthdate": _shift_a_day(p["dob"], rng) if rng.random() < 0.15
                             else (p["dob"] if rng.random() > 0.3 else ""),
            })

        # --- Payments sheet: ~35%, first/last split, messiest ---
        if rng.random() < 0.35:
            truth.append({"source_file": "payments_sheet.csv", "source_row": len(payments),
                          "true_person_id": pid})
            payments.append({
                "fname": p["first"],
                "lname": _typo(p["last"], rng) if rng.random() < 0.3 else p["last"],
                "email": email if rng.random() > 0.25 else "",
                "cell": _phone_variant(p["digits"], rng),
                "province": p["state"],
                "date_of_birth": p["dob"],
            })

        # --- Transactions: the revenue log ---
        base = date(2024, 1, 1)
        n_tx = rng.choices([1, 2, 3, 5, 8, 14], weights=[28, 20, 18, 16, 12, 6])[0]
        # A third of customers went quiet a while ago — these become the lapsed segments.
        lapsed = rng.random() < 0.33
        for _ in range(n_tx):
            span = rng.randrange(400, 950) if lapsed else rng.randrange(0, 400)
            tx_date = date(2026, 9, 1) - timedelta(days=span)
            if tx_date < base:
                tx_date = base + timedelta(days=rng.randrange(0, 60))
            transactions.append({
                "customer_name": name,
                "email": email,
                "phone": _phone_variant(p["digits"], rng),
                "transaction_date": tx_date.isoformat(),
                "amount": rng.choice([45, 60, 75, 90, 120, 150, 200, 350]),
            })

    def write(path, rows):
        if not rows:
            return
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    rng.shuffle(transactions)
    write(f"{outdir}/booking_export.csv", booking)
    write(f"{outdir}/crm_contacts.csv", crm)
    write(f"{outdir}/payments_sheet.csv", payments)
    write(f"{outdir}/transactions.csv", transactions)
    write(f"{outdir}/ground_truth.csv", truth)

    return {
        "people": n_people,
        "booking_rows": len(booking),
        "crm_rows": len(crm),
        "payments_rows": len(payments),
        "total_client_rows": len(booking) + len(crm) + len(payments),
        "transactions": len(transactions),
    }


if __name__ == "__main__":
    stats = generate()
    print("Generated sample data in ./sample_data/")
    for key, value in stats.items():
        print(f"  {key}: {value}")
