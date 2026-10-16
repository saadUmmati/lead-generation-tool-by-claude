"""
make_label_sheet.py - draws a random sample of businesses (with a website) from your results
CSVs and writes (1) a BLANK labelling sheet and (2) the matching input file for enrich.py.

The sheet deliberately does NOT contain what the tool found, so whoever labels it cannot be
nudged by the tool's answer.

  python harness/make_label_sheet.py --results downloads/*.csv --n 50 --seed 7 --out harness/work
"""

import argparse
import csv
import glob
import os
import random
import sys

sys.path.insert(0, os.path.dirname(__file__))
from metrics import norm_domain  # noqa: E402

LABEL_COLUMNS = ["title", "website", "google_maps_link", "label_status", "true_emails", "where_found", "source_page_url",
                 "labelled_by", "notes", "label_status_2", "true_emails_2", "labelled_by_2"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", nargs="+", required=True, help="results CSV file(s); globs are fine")
    ap.add_argument("--n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", default="harness/work")
    ap.add_argument("--only-missing", action="store_true",
                    help="quick mode: sample only businesses where the tool found NO email (checks whether they really publish none)")
    a = ap.parse_args()

    files = [f for pat in a.results for f in glob.glob(pat)]
    seen, pool = set(), []
    for f in sorted(files):
        with open(f, newline="", encoding="utf-8-sig") as fh:
            for r in csv.DictReader(fh):
                d = norm_domain(r.get("website", ""))
                if a.only_missing and ((r.get("emails") or "").strip() or (r.get("extra_emails") or "").strip()):
                    continue
                if d and d not in seen:
                    seen.add(d)
                    pool.append(r)
    if not pool:
        sys.exit("No businesses with a website found in the given files.")
    rnd = random.Random(a.seed)
    sample = rnd.sample(pool, min(a.n, len(pool)))
    os.makedirs(a.out, exist_ok=True)
    with open(os.path.join(a.out, "labels.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=LABEL_COLUMNS)
        w.writeheader()
        for r in sample:
            w.writerow({"title": r.get("title", ""), "website": r.get("website", ""), "google_maps_link": r.get("google_maps_link", "")})
    with open(os.path.join(a.out, "sample_sites.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["title", "website"])
        w.writeheader()
        for r in sample:
            w.writerow({"title": r.get("title", ""), "website": r.get("website", "")})
    print(f"Pool: {len(pool)} unique sites; sampled {len(sample)} (seed {a.seed}).")
    print(f"  Fill in:        {os.path.join(a.out, 'labels.csv')}")
    print(f"  Run the tool on {os.path.join(a.out, 'sample_sites.csv')} (see README step 3)")


if __name__ == "__main__":
    main()
