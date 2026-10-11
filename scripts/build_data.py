#!/usr/bin/env python3
"""Rebuild site/data/guidelines.json from the curated sources.

Pulls each organization's guidelines straight from official channels:
  * PubMed via NCBI E-utilities (esearch + efetch), using the query defined
    for that organization in sources.yaml;
  * the organization's own RSS/Atom feeds, when listed;
  * the organization's own guideline or press-release web pages, when listed,
    so guidelines released on a society website before any journal paper
    (for example ACR's 2026 axial spondyloarthritis guideline) still appear.
Records are de-duplicated, tagged with conditions and specialties using
conditions.yaml, merged with the previous data file (so first-seen dates
survive), and written along with an RSS feed of the newest guidelines.

Usage:
  python scripts/build_data.py                # full rebuild
  python scripts/build_data.py --only acr,who # just some sources (for tuning)

Set NCBI_API_KEY to raise the E-utilities rate limit from 3 to 10 req/s.
"""

from __future__ import annotations

import argparse
import datetime as dt
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "sources.yaml"
CONDITIONS_FILE = ROOT / "conditions.yaml"
DATA_FILE = ROOT / "site" / "data" / "guidelines.json"
FEED_FILE = ROOT / "site" / "feed.xml"

EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/"
TOOL = "guidelines-webapp"
USER_AGENT = "GuidelinesWebApp/1.0 (+https://github.com/Airheumatologist/Guidelines)"

GUIDELINE_FILTER = (
    '(guideline*[ti] OR recommendation*[ti] OR consensus[ti] OR statement*[ti] '
    'OR advisory[ti] OR "position paper"[ti] OR "points to consider"[ti] '
    'OR "classification criteria"[ti] OR "technical standard"[ti] OR "care pathway"[ti] '
    'OR "decision pathway"[ti] OR "practice update"[ti] OR "controversies conference"[ti] '
    'OR "practice parameter"[ti] OR "practice guideline"[pt] OR guideline[pt] '
    'OR "consensus statement"[pt])'
)
# Publication types never wanted. Reviews and meta-analyses are dropped only when
# they are not also indexed as a guideline (ASCO, ESMO and EULAR guidelines often
# carry "Systematic Review" too). A source can re-allow some via allow_pubtypes.
EXCLUDED_PUBTYPES = ["erratum", "published erratum", "comment", "editorial", "letter",
                     "news", "retracted publication", "retraction of publication"]
REVIEW_PUBTYPES = ["systematic review", "meta-analysis"]
GUIDELINE_PUBTYPES = ["practice guideline", "guideline", "consensus statement"]


def not_filter(allow: list[str] | None = None) -> str:
    allow = {a.lower() for a in allow or []}
    pt = lambda xs: " OR ".join(f'"{x}"[pt]' for x in xs if x not in allow)  # noqa: E731
    return (f"NOT ({pt(EXCLUDED_PUBTYPES)}) "
            f"NOT (({pt(REVIEW_PUBTYPES)}) NOT ({pt(GUIDELINE_PUBTYPES)}) "
            f"NOT (guideline*[ti] OR statement*[ti] OR recommendation*[ti] OR consensus[ti]))")


def title_term(t: str) -> str:
    """A society name in the title or as corporate author.

    PubMed silently ignores a quoted phrase that is missing from its phrase
    index (e.g. "Heart Failure Society of America"), so multi-word names are
    also searched by proximity, which does not depend on that index."""
    q = f'"{t}"[ti] OR "{t}"[cn]'
    if " " in t:
        q += f' OR "{t}"[ti:~2]'
    return q


NOT_FILTER = not_filter()
MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


# --------------------------------------------------------------------------- io

def http_get(url: str, params: dict | None = None, retries: int = 3) -> bytes:
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read()
        except Exception as exc:  # network errors, 429s, 5xx
            last = exc
            time.sleep(2 ** attempt * 2)
    raise RuntimeError(f"GET failed for {url}: {last}")


class EUtils:
    def __init__(self, api_key: str | None = None, email_addr: str | None = None):
        self.api_key = api_key
        self.email = email_addr
        self.delay = 0.11 if api_key else 0.35
        self._last = 0.0

    def _call(self, endpoint: str, params: dict) -> bytes:
        params = dict(params, tool=TOOL)
        if self.api_key:
            params["api_key"] = self.api_key
        if self.email:
            params["email"] = self.email
        wait = self.delay - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        try:
            return http_get(EUTILS + endpoint, params)
        finally:
            self._last = time.time()

    def search(self, term: str, retmax: int) -> tuple[int, list[str]]:
        raw = self._call("esearch.fcgi", {
            "db": "pubmed", "term": term, "retmax": retmax,
            "sort": "pub_date", "retmode": "json",
        })
        res = json.loads(raw)["esearchresult"]
        if "ERROR" in res:
            raise RuntimeError(res["ERROR"])
        return int(res.get("count", 0)), res.get("idlist", [])

    def fetch(self, pmids: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(pmids), 200):
            raw = self._call("efetch.fcgi", {
                "db": "pubmed", "id": ",".join(pmids[i:i + 200]), "retmode": "xml",
            })
            out.extend(parse_pubmed_xml(raw))
        return out


# ---------------------------------------------------------------- parsing

def _text(el) -> str:
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip() if el is not None else ""


def _date_from(el) -> str | None:
    if el is None:
        return None
    year = el.findtext("Year")
    if not year:
        md = el.findtext("MedlineDate") or ""
        m = re.search(r"(\d{4})", md)
        return f"{m.group(1)}-01-01" if m else None
    month = (el.findtext("Month") or "1").strip()
    month_n = int(month) if month.isdigit() else MONTHS.get(month[:3].lower(), 1)
    day = el.findtext("Day") or "1"
    try:
        return dt.date(int(year), month_n, int(day)).isoformat()
    except ValueError:
        return f"{year}-01-01"


def parse_pubmed_xml(raw: bytes) -> list[dict]:
    """Turn an efetch PubmedArticleSet (journal articles or Bookshelf books) into records."""
    root = ET.fromstring(raw)
    records = []
    for art in root.findall("PubmedArticle"):
        mc = art.find("MedlineCitation")
        a = mc.find("Article")
        pmid = mc.findtext("PMID")
        title = _text(a.find("ArticleTitle")).rstrip(".")
        journal = a.findtext("Journal/ISOAbbreviation") or a.findtext("Journal/Title") or ""
        date = (_date_from(a.find("ArticleDate"))
                or _date_from(a.find("Journal/JournalIssue/PubDate")))
        doi = None
        for aid in art.findall("PubmedData/ArticleIdList/ArticleId"):
            if aid.get("IdType") == "doi":
                doi = (aid.text or "").strip().lower()
        if not doi:
            for eid in a.findall("ELocationID"):
                if eid.get("EIdType") == "doi":
                    doi = (eid.text or "").strip().lower()
        mesh = [_text(d) for d in mc.findall("MeshHeadingList/MeshHeading/DescriptorName")]
        keywords = [_text(k) for k in mc.findall("KeywordList/Keyword")]
        ptypes = [_text(p) for p in a.findall("PublicationTypeList/PublicationType")]
        records.append({
            "pmid": pmid, "doi": doi, "title": title, "journal": journal,
            "date": date, "mesh": mesh, "keywords": keywords, "pub_types": ptypes,
        })
    for book in root.findall("PubmedBookArticle"):
        bd = book.find("BookDocument")
        pmid = bd.findtext("PMID")
        title = _text(bd.find("ArticleTitle")) or _text(bd.find("Book/BookTitle"))
        publisher = bd.findtext("Book/Publisher/PublisherName") or ""
        date = _date_from(bd.find("Book/PubDate")) or _date_from(bd.find("ContributionDate"))
        keywords = [_text(k) for k in bd.findall("KeywordList/Keyword")]
        records.append({
            "pmid": pmid, "doi": None, "title": title.rstrip("."), "journal": publisher,
            "date": date, "mesh": [], "keywords": keywords, "pub_types": ["Book"],
        })
    return records


def parse_feed(raw: bytes) -> list[dict]:
    """Parse RSS 2.0, RSS 1.0 (RDF) or Atom into {title, url, date, doi}."""
    root = ET.fromstring(raw)
    items = []

    def local(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    for el in root.iter():
        if local(el.tag) not in ("item", "entry"):
            continue
        fields: dict[str, str] = {}
        for child in el:
            name = local(child.tag)
            if name == "link" and child.get("href"):
                fields.setdefault("link", child.get("href"))
            elif name in ("title", "link", "pubDate", "date", "updated", "published",
                          "identifier", "doi", "guid"):
                fields.setdefault(name, _text(child))
        title = html.unescape(re.sub(r"<[^>]+>", "", fields.get("title", ""))).strip()
        if not title:
            continue
        date = None
        for key in ("pubDate", "date", "published", "updated"):
            if fields.get(key):
                date = _parse_any_date(fields[key])
                if date:
                    break
        doi = None
        for key in ("doi", "identifier", "link", "guid"):
            m = re.search(r"(10\.\d{4,9}/[^\s?#&\"<>]+)", fields.get(key, ""))
            if m:
                doi = m.group(1).lower().rstrip(".")
                break
        items.append({"title": title, "url": fields.get("link"), "date": date, "doi": doi})
    return items


def _parse_any_date(value: str) -> str | None:
    value = value.strip()
    try:
        return email.utils.parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        pass
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value)
    return "-".join(m.groups()) if m else None


# ---------------------------------------------------------------- web pages

class _Anchors(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href, self._text = dict(attrs).get("href"), []

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, " ".join(" ".join(self._text).split())))
            self._href = None


_TEXT_DATES = [
    (re.compile(r"(?<!\d)(\d{1,2})/(\d{1,2})/(\d{4}|\d{2})(?!\d)"), "mdy"),
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "iso"),
    (re.compile(r"\b([A-Za-z]{3})[a-z]*\.? (\d{1,2}), (\d{4})\b"), "mon-d-y"),
    (re.compile(r"\b([A-Za-z]{3})[a-z]*\.? (\d{4})\b"), "mon-y"),
]


def date_in_text(text: str) -> str | None:
    """Find a publication date written in link text ("6/24/26", "AUG 2026", ...)."""
    for rx, kind in _TEXT_DATES:
        for m in rx.finditer(text):
            g = m.groups()
            try:
                if kind == "mdy":
                    y = int(g[2]) + (2000 if len(g[2]) == 2 else 0)
                    return dt.date(y, int(g[0]), int(g[1])).isoformat()
                if kind == "iso":
                    return dt.date(int(g[0]), int(g[1]), int(g[2])).isoformat()
                mon = MONTHS.get(g[0][:3].lower())
                if not mon:
                    continue
                if kind == "mon-d-y":
                    return dt.date(int(g[2]), mon, int(g[1])).isoformat()
                return dt.date(int(g[1]), mon, 1).isoformat()
            except ValueError:
                continue
    return None


def parse_web_links(body: str, base_url: str, page: dict, keep: re.Pattern) -> list[dict]:
    """Pick guideline links from a society web page.

    page: {url, link_pattern (regex on the absolute link), strip (optional regex
    removed from the link text, e.g. dates or category labels), keywords
    (optional regex the text must match; defaults to the rss_keywords)}."""
    parser = _Anchors()
    parser.feed(body)
    link_rx = re.compile(page["link_pattern"], re.I)
    strip_rx = re.compile(page["strip"], re.I) if page.get("strip") else None
    kw = re.compile(page["keywords"], re.I) if page.get("keywords") else keep
    seen, items = set(), []
    for href, text in parser.links:
        if not href:
            continue
        url = urllib.parse.urljoin(base_url, href)
        if url in seen or not link_rx.search(url) or not kw.search(text):
            continue
        seen.add(url)
        date = date_in_text(text)
        title = strip_rx.sub(" ", text) if strip_rx else text
        title = " ".join(title.split()).strip(" -–|:")
        if len(title) < 15:
            continue
        m = re.search(r"(10\.\d{4,9}/[^\s?#&\"<>]+)", urllib.parse.unquote(url))
        items.append({"title": title, "url": url, "date": date,
                      "doi": m.group(1).lower().rstrip(".") if m else None})
    return items


def page_meta(body: str) -> tuple[str | None, str | None]:
    """Title and publication date of a linked page (og:title, <h1>, article dates)."""
    def meta(prop):
        m = (re.search(rf'<meta[^>]+(?:property|name)="{prop}"[^>]+content="([^"]+)"', body, re.I)
             or re.search(rf'<meta[^>]+content="([^"]+)"[^>]+(?:property|name)="{prop}"', body, re.I))
        return html.unescape(m.group(1)).strip() if m else None
    title = meta("og:title") or meta("citation_title")
    if not title:
        m = re.search(r"<h1[^>]*>(.*?)</h1>", body, re.I | re.S)
        title = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip() if m else None
    raw_date = (meta("article:published_time") or meta("citation_publication_date")
                or meta("date") or (re.search(r'<time[^>]+datetime="([^"]+)"', body, re.I) or [None, None])[1])
    date = None
    if raw_date:
        date = _parse_any_date(raw_date) or date_in_text(raw_date.replace("/", "-"))
    return (" ".join(title.split()) if title else None), date


def fetch_web(page: dict, keep: re.Pattern) -> list[dict]:
    body = http_get(page["url"]).decode("utf-8", "replace")
    items = parse_web_links(body, page["url"], page, keep)
    if page.get("follow"):
        items = items[:int(page.get("follow_limit", 25))]
        for item in items:
            try:
                title, date = page_meta(http_get(item["url"], retries=1).decode("utf-8", "replace"))
            except Exception:
                continue
            if title and len(title) >= 15:
                item["title"] = re.sub(r"\s*[|–-]\s*(American College of Rheumatology|ACR)\s*$", "", title)
            item["date"] = item["date"] or date
    if page.get("require_date"):
        # Alphabetical society lists mix decades of guidelines; keep only dated ones.
        items = [i for i in items if i["date"]]
    return items


_WEB_STOP = {"the", "of", "and", "for", "in", "with", "a", "an", "to", "on", "new", "updated",
             "update", "updates", "releases", "release", "guideline", "guidelines", "guidance",
             "recommendations", "recommendation", "treatment", "management", "advance", "adults",
             "american", "college", "society", "association", "clinical", "practice", "patients"}


def _content_tokens(title: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", title.lower()) if w not in _WEB_STOP and len(w) > 2}


def drop_superseded_web(records: dict) -> int:
    """Drop website-only items once the same guideline is indexed in PubMed.

    A press release title rarely equals the journal title, so an item that only
    came from a web page (no PMID, no DOI) is dropped when a PubMed record from
    the same organization, dated no earlier than 60 days before it, shares most
    of its content words."""
    recs = {id(r): r for r in records.values()}.values()
    indexed = [r for r in recs if r.get("pmid")]
    drop = []
    for r in recs:
        if r.get("pmid") or r.get("doi") or not r.get("web"):
            continue
        toks = _content_tokens(r["title"])
        if not toks:
            continue
        for p in indexed:
            if not set(p["sources"]) & set(r["sources"]):
                continue
            if r.get("date") and p.get("date") and p["date"] < (
                    dt.date.fromisoformat(r["date"]) - dt.timedelta(days=60)).isoformat():
                continue
            if len(toks & _content_tokens(p["title"])) / len(toks) >= 0.6:
                drop.append(r)
                break
    for key in [k for k, v in records.items() if any(v is d for d in drop)]:
        del records[key]
    return len(drop)


# ---------------------------------------------------------------- queries

def build_query(src: dict, since_year: int) -> str:
    pm = src.get("pubmed") or {}
    if pm.get("query"):
        core = f"({pm['query']})"
    else:
        parts = [GUIDELINE_FILTER]
        terms = pm.get("title_terms") or []
        if terms:
            parts.append("(" + " OR ".join(title_term(t) for t in terms) + ")")
        journals = pm.get("journals") or []
        if journals:
            parts.append("(" + " OR ".join(f'"{j}"[ta]' for j in journals) + ")")
        core = " AND ".join(parts)
    return f'{core} AND ("{since_year}/01/01"[dp] : "3000"[dp]) {not_filter(pm.get("allow_pubtypes"))}'


# ---------------------------------------------------------------- tagging

def slugify(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


class Tagger:
    def __init__(self, conditions: list[dict]):
        self.conditions = []
        for c in conditions:
            # Whole words; a trailing plural "s" is allowed ("cancers", "guidelines").
            pats = [p if re.search(r"\\b", p) else rf"\b{p}(?:e?s)?\b" for p in c["match"]]
            self.conditions.append((c["name"], c["specialty"],
                                    re.compile("|".join(f"(?:{p})" for p in pats), re.I)))

    def tag(self, rec: dict) -> tuple[list[str], list[str]]:
        haystack = " | ".join([rec["title"], *rec.get("mesh", []), *rec.get("keywords", [])])
        names, specs = [], []
        for name, spec, rx in self.conditions:
            if rx.search(haystack):
                names.append(name)
                specs.append(spec)
        return names, specs


# ---------------------------------------------------------------- main build

def is_excluded(title: str, patterns: list[re.Pattern]) -> bool:
    return any(p.search(title) for p in patterns)


def merge(records: dict, rec: dict, source: dict) -> None:
    keys = [k for k in (f"doi:{rec['doi']}" if rec.get("doi") else None,
                        f"pmid:{rec['pmid']}" if rec.get("pmid") else None,
                        f"t:{norm_title(rec['title'])}") if k]
    existing = next((records[k] for k in keys if k in records), None)
    if existing is None:
        existing = dict(rec, sources=[])
    else:
        if rec.get("pmid") and not existing.get("pmid"):
            # The journal record wins over a web-page or feed item for the same DOI.
            for field in ("title", "journal", "date"):
                existing[field] = rec[field] or existing.get(field)
        for field in ("pmid", "doi", "journal", "date", "url"):
            if not existing.get(field) and rec.get(field):
                existing[field] = rec[field]
        existing["mesh"] = sorted(set(existing.get("mesh", [])) | set(rec.get("mesh", [])))
        existing["keywords"] = sorted(set(existing.get("keywords", [])) | set(rec.get("keywords", [])))
    if source["id"] not in existing["sources"]:
        existing["sources"].append(source["id"])
    for k in keys + [k for k in (f"doi:{existing.get('doi')}", f"pmid:{existing.get('pmid')}")
                     if not k.endswith(":None")]:
        records[k] = existing


def finalize(records: dict, sources: dict, tagger: Tagger, previous: dict, today: str) -> list[dict]:
    seen_ids = set()
    out = []
    for rec in records.values():
        if id(rec) in seen_ids:
            continue
        seen_ids.add(id(rec))
        rid = (f"pmid-{rec['pmid']}" if rec.get("pmid")
               else f"doi-{slugify(rec['doi'])}" if rec.get("doi")
               else f"t-{slugify(rec['title'])[:80]}")
        conditions, cond_specs = tagger.tag(rec)
        specialties = {sources[s]["specialty"] for s in rec["sources"]
                       if sources[s]["specialty"] != "multi"}
        specialties.update(cond_specs)
        url = (f"https://doi.org/{rec['doi']}" if rec.get("doi")
               else f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/" if rec.get("pmid")
               else rec.get("url"))
        prev = previous.get(rid)
        out.append({
            "id": rid,
            "title": rec["title"],
            "url": url,
            "pubmed": f"https://pubmed.ncbi.nlm.nih.gov/{rec['pmid']}/" if rec.get("pmid") else None,
            "doi": rec.get("doi"),
            "journal": rec.get("journal") or "",
            "date": rec.get("date"),
            "sources": sorted(rec["sources"]),
            "specialties": sorted(specialties) or ["General"],
            "conditions": sorted(conditions),
            "first_seen": prev["first_seen"] if prev else today,
            **({"web": True} if rec.get("web") and not rec.get("pmid") else {}),
        })
    out.sort(key=lambda r: (r["date"] or "", r["title"]), reverse=True)
    return out


def write_feed(guidelines: list[dict], sources: dict, generated: str, limit: int = 100) -> None:
    def esc(s: str) -> str:
        return html.escape(s or "", quote=True)

    items = []
    for g in sorted(guidelines, key=lambda r: (r["first_seen"], r["date"] or ""), reverse=True)[:limit]:
        orgs = ", ".join(sources[s]["abbr"] for s in g["sources"])
        pub = dt.datetime.fromisoformat((g["date"] or g["first_seen"]) + "T00:00:00+00:00")
        items.append(
            "<item>"
            f"<title>{esc(g['title'])}</title>"
            f"<link>{esc(g['url'])}</link>"
            f"<guid isPermaLink=\"false\">{esc(g['id'])}</guid>"
            f"<pubDate>{email.utils.format_datetime(pub)}</pubDate>"
            f"<description>{esc(orgs + ' · ' + g['journal'] + ' · ' + ', '.join(g['conditions']))}</description>"
            + "".join(f"<category>{esc(s)}</category>" for s in g["specialties"])
            + "</item>")
    FEED_FILE.write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n<rss version="2.0"><channel>'
        "<title>Society and WHO Guidelines</title>"
        "<link>https://github.com/Airheumatologist/Guidelines</link>"
        "<description>New clinical practice guidelines from major medical societies and WHO</description>"
        f"<lastBuildDate>{email.utils.format_datetime(dt.datetime.fromisoformat(generated))}</lastBuildDate>"
        + "".join(items) + "</channel></rss>\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated source ids to fetch")
    ap.add_argument("--dry-run", action="store_true", help="fetch and report, do not write files")
    args = ap.parse_args(argv)

    cfg = yaml.safe_load(SOURCES_FILE.read_text())
    settings = cfg.get("settings", {})
    since = int(settings.get("since_year", 2018))
    cap = int(settings.get("max_per_source", 400))
    rss_kw = re.compile("|".join(re.escape(k) for k in settings.get("rss_keywords", ["guideline"])), re.I)
    excludes = [re.compile(p, re.I) for p in settings.get("exclude_title_patterns", [])]
    all_sources = {s["id"]: s for s in cfg["sources"]}
    only = set(args.only.split(",")) if args.only else None
    active = [s for s in cfg["sources"] if s.get("enabled", True) and (not only or s["id"] in only)]

    tagger = Tagger(yaml.safe_load(CONDITIONS_FILE.read_text()))
    eu = EUtils(os.environ.get("NCBI_API_KEY"), os.environ.get("NCBI_EMAIL"))

    previous_data = json.loads(DATA_FILE.read_text()) if DATA_FILE.exists() else {}
    previous = {g["id"]: g for g in previous_data.get("guidelines", [])}

    records: dict = {}
    report = []
    for src in active:
        entry = {"id": src["id"], "abbr": src["abbr"], "pubmed": None, "rss": None, "web": None,
                 "errors": []}
        if src.get("pubmed"):
            q = build_query(src, since)
            try:
                count, ids = eu.search(q, cap)
                recs = [r for r in eu.fetch(ids) if not is_excluded(r["title"], excludes)]
                for r in recs:
                    merge(records, r, src)
                entry["pubmed"] = {"matches": count, "kept": len(recs)}
                entry["sample"] = [r["title"][:110] for r in recs[:4]]
            except Exception as exc:
                entry["errors"].append(f"pubmed: {exc}")
        for feed_url in src.get("rss") or []:
            try:
                items = parse_feed(http_get(feed_url))
                kept = [i for i in items if rss_kw.search(i["title"])
                        and not is_excluded(i["title"], excludes)
                        and (not i["date"] or int(i["date"][:4]) >= since)]
                for i in kept:
                    merge(records, {"pmid": None, "doi": i["doi"], "title": i["title"],
                                    "journal": "", "date": i["date"], "url": i["url"],
                                    "mesh": [], "keywords": []}, src)
                entry["rss"] = (entry["rss"] or 0) + len(kept)
            except Exception as exc:
                entry["errors"].append(f"rss {feed_url}: {exc}")
        for page in src.get("web") or []:
            try:
                items = [i for i in fetch_web(page, rss_kw)
                         if not is_excluded(i["title"], excludes)
                         and (not i["date"] or int(i["date"][:4]) >= since)]
                for i in items:
                    merge(records, {"pmid": None, "doi": i["doi"], "title": i["title"],
                                    "journal": f"{src['abbr']} website", "date": i["date"],
                                    "url": i["url"], "mesh": [], "keywords": [], "web": True}, src)
                entry["web"] = (entry["web"] or 0) + len(items)
                entry.setdefault("sample", []).extend(f"[web {i['date']}] {i['title'][:100]}" for i in items[:6])
            except Exception as exc:
                entry["errors"].append(f"web {page['url']}: {exc}")
        report.append(entry)
        print(f"{src['abbr']:<18} pubmed={entry['pubmed']} rss={entry['rss']} web={entry['web']} "
              + (f"ERRORS={entry['errors']}" if entry["errors"] else ""), flush=True)
        for title in entry.pop("sample", []):
            print(f"{'':<18}  - {title}", flush=True)

    dropped = drop_superseded_web(records)
    if dropped:
        print(f"dropped {dropped} website items now indexed in PubMed", flush=True)

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    guidelines = finalize(records, all_sources, tagger, previous, now.date().isoformat())

    # On a partial run keep everything from sources that were not re-fetched.
    if only:
        fresh = {g["id"] for g in guidelines}
        guidelines += [g for g in previous.values()
                       if g["id"] not in fresh and not set(g["sources"]) & only]
        guidelines.sort(key=lambda r: (r["date"] or "", r["title"]), reverse=True)

    untagged = [g for g in guidelines if not g["conditions"]]
    print(f"\n{len(untagged)} guidelines matched no condition, e.g.:")
    for g in untagged[:15]:
        print(f"  - [{','.join(g['sources'])}] {g['title'][:110]}")

    failed = [r for r in report if r["errors"] and not r["pubmed"] and not r["rss"]]
    print(f"\n{len(guidelines)} guidelines from {len(active)} sources "
          f"({len(failed)} sources failed completely)")
    if not guidelines:
        print("No guidelines fetched; leaving existing data untouched.", file=sys.stderr)
        return 1
    if args.dry_run:
        return 0

    payload = {
        "generated": now.isoformat(),
        "sources": [{k: s[k] for k in ("id", "name", "abbr", "specialty")} for s in cfg["sources"]
                    if s.get("enabled", True)],
        "report": report,
        "guidelines": guidelines,
    }
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    DATA_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    write_feed(guidelines, all_sources, now.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main())
