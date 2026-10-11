#!/usr/bin/env python3
"""Coverage audit against the reference site's guideline lists.

Reads every specialty page under updatesinmedicine.com/guidelines once, for
comparison only (nothing from it is ingested into the app), and reports which
of its guidelines are missing from site/data/guidelines.json:

  * missing from an organization we already pull  -> an ingestion gap
  * missing from an organization not in sources.yaml -> a source-list gap

For ingestion gaps that carry a DOI, PubMed is queried to explain the miss
(not indexed yet, wrong journal, title lacks the society name, ...).

Usage: python scripts/audit_reference.py [--since 2024-01-01] [--out audit]
"""
import argparse
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_data  # noqa: E402
UA = "Mozilla/5.0 (compatible; GuidelinesWebApp-audit/1.0; +https://github.com/Airheumatologist/Guidelines)"
BASE = "https://updatesinmedicine.com"
EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"

ENTRY = re.compile(
    r'<p class="guideline-entry"><a href="([^"]+)"[^>]*>(.*?)</a>'
    r'<span class="guideline-entry-meta">(.*?)<time dateTime="([0-9-]+)"', re.S)


def get(url, params=None):
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=60) as r:
                return r.read().decode("utf-8", "replace")
        except Exception:
            if attempt == 2:
                raise
            time.sleep(3)


def clean(s):
    return html.unescape(re.sub(r"<!--.*?-->|<[^>]+>", "", s)).strip()


def reference_entries():
    index = get(BASE + "/guidelines")
    pages = sorted(set(re.findall(r'href="(/guidelines/[a-z0-9-]+)"', index)))
    entries = {}
    for path in pages:
        body = get(BASE + path)
        specialty = path.rsplit("/", 1)[-1]
        for url, title, meta, date in ENTRY.findall(body):
            org = clean(meta).strip(", ").strip()
            key = url.lower()
            e = entries.setdefault(key, {"url": html.unescape(url), "title": clean(title),
                                         "org": org, "date": date, "specialties": []})
            e["specialties"].append(specialty)
        time.sleep(0.5)
    print(f"reference: {len(pages)} specialty pages, {len(entries)} unique guidelines", flush=True)
    return list(entries.values())


def doi_of(url):
    m = re.search(r"(10\.\d{4,9}/[^\s?#]+)", urllib.parse.unquote(url))
    return m.group(1).lower().rstrip(".") if m else None


def tokens(s):
    stop = {"the", "of", "and", "for", "in", "with", "a", "an", "to", "on", "guideline",
            "guidelines", "recommendations", "management", "update", "clinical", "practice"}
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in stop and len(w) > 2}


def org_parts(org):
    return {p.strip().upper() for p in re.split(r"[/,]| and ", re.sub(r"\(.*?\)", "", org)) if p.strip()}


def pubmed_explain(doi, api_key=None):
    params = {"db": "pubmed", "term": f"{doi}[doi]", "retmode": "json"}
    if api_key:
        params["api_key"] = api_key
    ids = json.loads(get(EUTILS + "esearch.fcgi", params))["esearchresult"].get("idlist", [])
    time.sleep(0.35)
    if not ids:
        return {"indexed": False}
    s = json.loads(get(EUTILS + "esummary.fcgi", {"db": "pubmed", "id": ids[0], "retmode": "json"}))
    time.sleep(0.35)
    r = s["result"][ids[0]]
    return {"indexed": True, "pmid": ids[0], "journal": r.get("source"), "title": r.get("title"),
            "pubtypes": r.get("pubtype", [])}


def query_hits(pmid, src_ids, cfg, api_key=None):
    """Which of our sources' PubMed queries match this PMID (after title excludes)?"""
    settings = cfg.get("settings", {})
    since = int(settings.get("since_year", 2018))
    by_id = {s["id"]: s for s in cfg["sources"]}
    hits = []
    for sid in src_ids:
        src = by_id.get(sid)
        if not src or not src.get("pubmed"):
            continue
        params = {"db": "pubmed", "term": f"({build_data.build_query(src, since)}) AND {pmid}[uid]",
                  "retmode": "json"}
        if api_key:
            params["api_key"] = api_key
        try:
            if json.loads(get(EUTILS + "esearch.fcgi", params))["esearchresult"].get("idlist"):
                hits.append(sid)
        except Exception:
            pass
        time.sleep(0.35)
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2024-01-01")
    ap.add_argument("--out", default="audit")
    args = ap.parse_args()

    data = json.loads((ROOT / "site/data/guidelines.json").read_text())
    ours = data["guidelines"]
    our_dois = {g["doi"].lower() for g in ours if g.get("doi")}
    our_tokens = [(tokens(g["title"]), g) for g in ours]
    cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
    covered = {}
    for s in cfg["sources"]:
        for part in re.split(r"/", s["abbr"].upper()):
            covered[part.strip()] = s["id"]
    covered.update({"ENDOCRINE SOCIETY": "endo", "ES": "endo", "SSC": "sccm"})

    ref = [e for e in reference_entries() if e["date"] >= args.since]
    print(f"reference since {args.since}: {len(ref)}")

    found, missing = [], []
    for e in ref:
        d = doi_of(e["url"])
        hit = None
        if d and d in our_dois:
            hit = "doi"
        else:
            rt = tokens(e["title"])
            for ot, g in our_tokens:
                if rt and len(rt & ot) / len(rt) >= 0.8 and (g.get("date") or "")[:4] in (e["date"][:4], str(int(e["date"][:4]) - 1), str(int(e["date"][:4]) + 1)):
                    hit = "title"
                    break
        e["doi"] = d
        e["covered_by"] = sorted({covered[p] for p in org_parts(e["org"]) if p in covered})
        (found if hit else missing).append(e)

    gap_ingest = [e for e in missing if e["covered_by"]]
    gap_source = [e for e in missing if not e["covered_by"]]
    print(f"found {len(found)}, missing {len(missing)} "
          f"(ingestion gaps {len(gap_ingest)}, organizations not in sources.yaml {len(gap_source)})")

    import os
    key = os.environ.get("NCBI_API_KEY")
    for e in gap_ingest:
        if e["doi"]:
            try:
                e["pubmed"] = pubmed_explain(e["doi"], key)
                if e["pubmed"].get("pmid"):
                    e["pubmed"]["query_hits"] = query_hits(e["pubmed"]["pmid"], e["covered_by"], cfg, key)
            except Exception as exc:
                e["pubmed"] = {"error": str(exc)}

    out = ROOT / args.out
    out.mkdir(exist_ok=True)
    (out / "audit.json").write_text(json.dumps(
        {"since": args.since, "reference_total": len(ref), "found": len(found),
         "gap_ingest": gap_ingest, "gap_source": gap_source}, indent=1, ensure_ascii=False))

    lines = [f"# Coverage audit vs reference site (since {args.since})", "",
             f"Reference guidelines: {len(ref)}. Already in our data: {len(found)}. "
             f"Missing: {len(missing)}.", "",
             f"## Missing from organizations we already pull ({len(gap_ingest)})", ""]
    reasons = Counter()
    for e in sorted(gap_ingest, key=lambda x: x["date"], reverse=True):
        pm = e.get("pubmed") or {}
        if not e["doi"]:
            why = "no DOI (society web page only)"
        elif not pm.get("indexed"):
            why = "not in PubMed yet"
        elif pm.get("query_hits"):
            why = (f"in PubMed ({pm.get('journal')}) and matched by {', '.join(pm['query_hits'])} "
                   "now; was missing from the data used for this audit")
        else:
            why = f"in PubMed ({pm.get('journal')}; {', '.join(pm.get('pubtypes', []))}) but query missed"
        reasons[why.split(" (")[0] + (" (query matches now)" if pm.get("query_hits") else "")
                if why.startswith("in PubMed") else why] += 1
        lines.append(f"- {e['date']} **{e['org']}**: {e['title']} [{why}]"
                     + (f" — PubMed title: {pm.get('title')}" if pm.get("title") else "")
                     + f" <{e['url']}>")
    lines += ["", "Reasons: " + ", ".join(f"{k}: {v}" for k, v in reasons.most_common()), ""]
    by_org = Counter()
    for e in gap_source:
        for p in org_parts(e["org"]) or {e["org"]}:
            by_org[p] += 1
    lines += [f"## Missing from organizations not in sources.yaml ({len(gap_source)})", "",
              "Count per organization:", ""]
    lines += [f"- {o}: {n}" for o, n in by_org.most_common()]
    lines += ["", "Entries:", ""]
    for e in sorted(gap_source, key=lambda x: (x["org"], x["date"])):
        lines.append(f"- {e['date']} **{e['org']}**: {e['title']} <{e['url']}>")
    (out / "audit.md").write_text("\n".join(lines) + "\n")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
