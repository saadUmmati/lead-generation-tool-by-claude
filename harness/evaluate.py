"""
evaluate.py - reports email recall / precision against your hand-labelled sample.

  python harness/evaluate.py --labels harness/work/labels.csv --results harness/work/results.csv
  python harness/evaluate.py ... --save harness/work/run_after_change.json --baseline harness/work/run_before.json
"""

import argparse
import csv
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from metrics import compare, evaluate, inter_rater  # noqa: E402


def read(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def pct(x):
    return "n/a" if x is None else f"{100 * x:.0f}%"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--results", required=True)
    ap.add_argument("--save", help="write this run's numbers to JSON (use as a later --baseline)")
    ap.add_argument("--baseline", help="JSON from an earlier run, for a before/after comparison")
    a = ap.parse_args()

    labels = read(a.labels)
    rep = evaluate(labels, read(a.results))
    br, er, sp = rep["business_recall"], rep["email_recall"], rep["strict_precision"]

    print(f"\nScored {rep['n_scored']} businesses ({rep['n_has_email']} with a published email); {rep['n_skipped']} skipped.")
    lo, hi = br["ci95"]
    print(f"\nBUSINESS RECALL  {pct(br['value'])}  ({br['k']}/{br['n']} had >=1 correct email found)   95% CI {pct(lo)}-{pct(hi)}")
    print(f"EMAIL RECALL     {pct(er['value'])}  ({er['k']}/{er['n']} published addresses found)")
    lo, hi = sp["ci95"]
    print(f"STRICT PRECISION {pct(sp['value'])}  ({sp['k']}/{sp['n']} reported addresses match the answer key)   95% CI {pct(lo)}-{pct(hi)}")
    ne = rep["no_email_rows_with_output"]
    print(f"Rows labelled 'no email on site' where the tool still reported one: {ne['k']}/{ne['n']}")

    if rep["review_needed"]:
        print("\nREVIEW THESE (tool reported an address that is not in your labels). Either the tool is wrong, or the labeller missed it - fix the label if so:")
        for t, em in rep["review_needed"]:
            print(f"  - {t}: {', '.join(em)}")
    if rep["miss_reasons"]:
        print("\nWHY ADDRESSES WERE MISSED (the failure taxonomy; fix the biggest bucket first):")
        for reason, names in rep["miss_reasons"].items():
            print(f"  {len(names):>3}  {reason}   e.g. {', '.join(names[:3])}")
    if rep["skipped"]:
        print("\nSkipped:", "; ".join(f"{t} ({why})" for t, why in rep["skipped"][:8]) + (" ..." if len(rep["skipped"]) > 8 else ""))

    ir = inter_rater(labels)
    if ir["n"]:
        print(f"\nTWO-LABELLER AGREEMENT on {ir['n']} businesses: status {pct(ir['status_agreement'])}, kappa {ir['kappa']:.2f}, same email set {pct(ir['email_set_agreement'])}")
        if ir["kappa"] < 0.6:
            print("  Kappa below ~0.6: the labelling rules are being read differently; settle disagreements before trusting the numbers.")
    else:
        print("\n(No second labeller found: fill label_status_2 / true_emails_2 for ~15 businesses to check the answer key itself.)")

    if a.baseline:
        with open(a.baseline, encoding="utf-8") as fh:
            old = json.load(fh)
        old["hit_by_domain"] = old.get("hit_by_domain", {})
        c = compare(rep, old)
        print(f"\nVS BASELINE on {c['n_paired']} paired businesses: gained {len(c['gained'])}, lost {len(c['lost'])}, exact sign-test p = {c['p_value']:.3f}")
        if c["lost"]:
            print("  LOST (regressions - look at these first):", ", ".join(c["lost"]))
        if c["gained"]:
            print("  gained:", ", ".join(c["gained"]))
        print("  " + ("Likely a real change." if c["p_value"] < 0.05 else "Not distinguishable from luck on this sample size; add more labelled businesses before claiming a win."))

    if a.save:
        with open(a.save, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2, default=list)
        print(f"\nSaved {a.save}")


if __name__ == "__main__":
    main()
