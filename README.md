# Guidelines

A small static web app listing clinical practice guidelines from major medical
societies and the World Health Organization. It updates itself every day and
presents the guidelines two ways: an **All Diseases** list (A–Z by condition)
and a **Specialties** list, with a search box across diseases, specialties,
societies and guideline titles.

## Where the guidelines come from

Only the organizations listed in [`sources.yaml`](sources.yaml) are used, and
each one is pulled from its own official publication channel. Nothing is
scraped from third-party aggregator sites.

| Method | What it does |
| --- | --- |
| **PubMed (NCBI E-utilities)** | For each organization, a PubMed query restricted to that society's journals and to titles/corporate authors naming the society, filtered to guidelines, recommendations, consensus statements and practice parameters, and excluding errata, comments, editorials and letters. WHO and NICE guidelines are found through their NCBI Bookshelf records (publisher = the organization). |
| **RSS / Atom** | Optional per organization: the society journal's own table-of-contents feed, kept only for items whose title matches the guideline keywords in `settings.rss_keywords`. Catches new guidelines a few days before PubMed indexes them. |

Records found more than once (same DOI, PMID or title) are merged and show
every issuing society.

## Editing the lists

* **Add or remove a society:** edit `sources.yaml`. Each entry is either a set of
  `journals` + `title_terms` (the query is generated) or a raw PubMed `query`.
  Set `enabled: false` to pause a source.
* **Add a condition / change tagging:** edit `conditions.yaml`. A guideline is
  tagged with a condition when one of its `match` phrases appears in the title,
  MeSH headings or keywords; the condition's specialty is added too.
* After a change, run the **Refresh guidelines** workflow manually (Actions tab →
  *Refresh guidelines* → *Run workflow*) or wait for the daily run. The
  *Sources and coverage* page in the app shows how many guidelines each source
  produced on the last run and any errors.

## How it runs

* `scripts/build_data.py` rebuilds `site/data/guidelines.json` and the RSS feed
  `site/feed.xml` (newest guidelines, for any feed reader).
* `.github/workflows/refresh.yml` runs it daily at 05:17 UTC, commits changed
  data, and deploys `site/` to GitHub Pages once Pages is enabled.
* Optional settings: repository secret `NCBI_API_KEY` (faster PubMed access) and
  variable `NCBI_EMAIL` (contact address NCBI asks API users to send).

### Turning on GitHub Pages

1. Settings → Pages → *Build and deployment* → Source: **GitHub Actions**.
2. Settings → Secrets and variables → Actions → Variables → add `PAGES_ENABLED` = `true`.
3. Run the *Refresh guidelines* workflow on `main`.

Pages on a private repository needs a paid GitHub plan; otherwise make the
repository public or host `site/` on any static host.

## Local use

```sh
pip install pyyaml
python scripts/build_data.py            # fetch everything
python scripts/build_data.py --only acr,eular --dry-run   # tune one source
python -m unittest discover -s tests    # offline tests
python -m http.server -d site 8000      # open http://localhost:8000
```
