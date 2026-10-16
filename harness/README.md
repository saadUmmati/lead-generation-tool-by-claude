# Measurement harness: is the tool actually finding the right emails?

Goal: replace "it feels like more emails" with numbers a second person can check.
It measures **email recall** (how many published addresses the tool finds) and **precision**
(how many it reports that are really the business's), on a hand-labelled sample.
It does NOT measure replies or deliverability; those need a real test send (see the report).

## Steps (about 2-3 hours for 50 businesses the first time)

1. **Draw the sample** (random, seeded, only businesses with a website):
   `python harness/make_label_sheet.py --results downloads/*.csv --n 50 --seed 7 --out harness/work`
   Creates `harness/work/labels.csv` (blank answer key) and `harness/work/sample_sites.csv`.
2. **Label it by hand, WITHOUT looking at the tool's output.** For each row open the website in a normal
   browser window and fill:
   - `label_status`
     - `HAS_EMAIL`: an email address is published on the business's own site
     - `NONE_PUBLISHED`: the site loads and shows no email (contact form / phone only)
     - `SOCIAL_ONLY`: the only email is on a Facebook/Instagram/LinkedIn page, not on the site
     - `UNREACHABLE`: you could not load the site either (excluded from scoring)
   - `true_emails`: every address the site publishes for the business, separated by `;`
     (check homepage, footer, contact page, about, legal/imprint; also addresses written as "name [at] domain").
   - `where_found`: `homepage`, `footer`, `contact page`, `imprint`, `image`, `pdf`, `js popup`, or `other`
   - `source_page_url`: the page where you saw it (so a second person can verify)
   - Never guess or build an address from a pattern (info@...). Only record what is printed.
3. **Run the tool on exactly those sites** (this measures the extractor itself):
   `python enrich.py --input harness/work/sample_sites.csv --output harness/work/results.csv`
4. **Score it:**
   `python harness/evaluate.py --labels harness/work/labels.csv --results harness/work/results.csv --save harness/work/run_1.json`
5. **After any code change**, redo step 3 into a new file and compare with the earlier run:
   `python harness/evaluate.py --labels ... --results harness/work/results_new.csv --baseline harness/work/run_1.json --save harness/work/run_2.json`
   It lists businesses gained and lost and an exact sign-test p-value. With 50 businesses, only a clear
   gain counts; the report says so when a change is not distinguishable from luck.
   To measure the code BEFORE a change, run step 3 once on the old commit (`git stash` or `git checkout <old commit> -- enrich.py email_extract.py`) first.

## Quick mode (about 40 minutes)
`python harness/make_label_sheet.py --results "downloads/*.csv" --n 20 --only-missing --out harness/work`
samples 20 businesses where the tool found NO email. Open each site (about 2 minutes) and fill `label_status`
and `true_emails`. The question it answers: of the sites we gave up on, how many really publish an email,
and where is it (`where_found`)? That one number tells you whether more extraction work is worthwhile.
It does not measure precision (wrong emails), and with 20 rows the uncertainty is large, so treat it as a first look.

## Checking the answer key itself
Have a second person independently fill `label_status_2` / `true_emails_2` / `labelled_by_2` for about 15 rows.
The report prints agreement and Cohen's kappa. Below ~0.6 means the rules are being read differently;
settle those rows before trusting any number.

## How to read the report
- **Business recall**: of businesses that publish an email, the share where the tool found at least one correct one.
  The 95% interval is wide at n=50 (about +/-13 points); that is the honest size of what this sample can show.
- **Strict precision**: addresses the tool reported that match your key. Addresses on rows you labelled
  "no email" appear under REVIEW: either the tool is wrong, or you missed one. Fix the label if you missed it.
- **Why addresses were missed**: the failure taxonomy. Build for the biggest bucket first.
- Recall on this sample only describes businesses like the sample (your countries, your business types).
