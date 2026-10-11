"""Offline tests for scripts/build_data.py (no network)."""

import re
import sys
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import build_data as bd  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures"


class ParseTests(unittest.TestCase):
    def test_pubmed_article_and_book(self):
        recs = bd.parse_pubmed_xml((FIXTURES / "efetch_sample.xml").read_bytes())
        self.assertEqual(len(recs), 2)
        art, book = recs
        self.assertEqual(art["pmid"], "1000001")
        self.assertEqual(art["doi"], "10.1000/example.1")
        self.assertEqual(art["date"], "2024-03-05")
        self.assertIn("Arthritis, Rheumatoid", art["mesh"])
        self.assertIn("Myocardial Infarction", art["mesh"])
        self.assertEqual(art["mesh_major"], ["Arthritis, Rheumatoid"])
        self.assertFalse(art["title"].endswith("."))
        self.assertEqual(book["journal"], "World Health Organization")
        self.assertEqual(book["date"], "2023-07-01")

    def test_rss_and_atom(self):
        rss = bd.parse_feed((FIXTURES / "feed_rss.xml").read_bytes())
        self.assertEqual(rss[0]["doi"], "10.1161/cir.0000000000009999")
        self.assertEqual(rss[0]["date"], "2025-02-03")
        atom = bd.parse_feed((FIXTURES / "feed_atom.xml").read_bytes())
        self.assertEqual(atom[0]["url"], "https://example.org/a")
        self.assertEqual(atom[0]["date"], "2025-01-10")


class QueryTests(unittest.TestCase):
    def test_generated_query(self):
        q = bd.build_query({"pubmed": {"journals": ["Ann Rheum Dis"], "title_terms": ["EULAR"]}}, 2018)
        self.assertIn('"Ann Rheum Dis"[ta]', q)
        self.assertIn('"EULAR"[ti] OR "EULAR"[cn]', q)
        q2 = bd.build_query({"pubmed": {"title_terms": ["Heart Failure Society of America"]}}, 2018)
        self.assertIn('"Heart Failure Society of America"[ti:~2]', q2)
        self.assertIn('"2018/01/01"[dp]', q)
        self.assertIn('NOT ("erratum"[pt]', q)
        # Reviews are dropped only when not also indexed as a guideline.
        self.assertIn('NOT (("systematic review"[pt] OR "meta-analysis"[pt]) NOT ("practice guideline"[pt]', q)

    def test_allow_pubtypes(self):
        q = bd.build_query({"pubmed": {"journals": ["Ann Intern Med"], "allow_pubtypes": ["letter", "comment"]}}, 2018)
        self.assertNotIn('"letter"[pt]', q)
        self.assertNotIn('"comment"[pt]', q)
        self.assertIn('"editorial"[pt]', q)

    def test_raw_query_used(self):
        q = bd.build_query({"pubmed": {"query": '"Ophthalmology"[ta]'}}, 2020)
        self.assertTrue(q.startswith('("Ophthalmology"[ta]) AND'))

    def test_sources_file_is_valid(self):
        cfg = yaml.safe_load((ROOT / "sources.yaml").read_text())
        ids = [s["id"] for s in cfg["sources"]]
        self.assertEqual(len(ids), len(set(ids)))
        for s in cfg["sources"]:
            self.assertTrue({"id", "name", "abbr", "specialty"} <= s.keys(), s)
            self.assertTrue(s.get("pubmed") or s.get("rss"), s["id"])


class WebTests(unittest.TestCase):
    PAGE = """<html><body>
      <a href="/clinical-practice-guidelines">Clinical Practice Guidelines</a>
      <a href="/press-releases/new-guidelines-axspa">6/24/26New Guidelines Advance Treatment of
         Axial Spondyloarthritis in Adults and Youth</a>
      <a href="/press-releases/annual-meeting">9/1/26 Registration opens for the annual meeting</a>
      <a href="https://www.jacc.org/doi/10.1016/j.jacc.2026.07.003">Scientific Statements 2026 Clinical
         Considerations for the Care of the Tactical Athlete</a>
      <a href="/Guidelines/Home/GuidelineDetail/1158">AUG 2026 Practice guideline Pharmacologic Treatment
         for Migraine Prevention in Adults</a>
    </body></html>"""

    def test_press_release_links(self):
        page = {"url": "https://rheumatology.org/press-releases",
                "link_pattern": r"rheumatology\.org/press-releases/", "keywords": "guideline|guidance",
                "strip": r"^\d{1,2}/\d{1,2}/\d{2}"}
        items = bd.parse_web_links(self.PAGE, page["url"], page, re.compile("guideline", re.I))
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["date"], "2026-06-24")
        self.assertTrue(items[0]["title"].startswith("New Guidelines Advance Treatment of Axial"))
        self.assertEqual(items[0]["url"], "https://rheumatology.org/press-releases/new-guidelines-axspa")

    def test_doi_links_and_month_dates(self):
        page = {"url": "https://www.acc.org/guidelines", "link_pattern": r"jacc\.org/doi/", "keywords": ".",
                "strip": r"^Scientific Statements\s+(\d{4}\s+)?"}
        acc = bd.parse_web_links(self.PAGE, page["url"], page, re.compile("x"))
        self.assertEqual(acc[0]["doi"], "10.1016/j.jacc.2026.07.003")
        self.assertTrue(acc[0]["title"].startswith("Clinical Considerations"))
        page = {"url": "https://www.aan.com/practice/guidelines", "link_pattern": r"GuidelineDetail/\d+",
                "keywords": ".", "strip": r"^[A-Z]{3}\s+\d{4}\s+(Practice guideline)?"}
        aan = bd.parse_web_links(self.PAGE, page["url"], page, re.compile("x"))
        self.assertEqual(aan[0]["date"], "2026-08-01")
        self.assertEqual(aan[0]["title"], "Pharmacologic Treatment for Migraine Prevention in Adults")

    def test_page_meta(self):
        title, date = bd.page_meta('<meta property="og:title" content="ACR axSpA Guideline &amp; Summary">'
                                   '<meta property="article:published_time" content="2026-06-24T10:00:00Z">')
        self.assertEqual(title, "ACR axSpA Guideline & Summary")
        self.assertEqual(date, "2026-06-24")

    def test_best_link_picks_matching_guideline_page(self):
        body = ('<a href="/clinical-practice-guidelines">Clinical Practice Guidelines</a>'
                '<a href="/osteoarthritis-guideline">Osteoarthritis</a>'
                '<a href="/axial-spondyloarthritis-guideline">Read the guideline</a>')
        url = bd._best_link(body, "https://rheumatology.org/press-releases/x",
                            r"rheumatology\.org/(?!clinical-practice-guidelines)[a-z0-9-]*guideline",
                            "New Guidelines Advance Treatment of Axial Spondyloarthritis in Adults and Youth")
        self.assertEqual(url, "https://rheumatology.org/axial-spondyloarthritis-guideline")

    PR_BODY = ('<a href="/axial-spondyloarthritis-guideline">Axial Spondyloarthritis</a>'
               '<a href="/osteoarthritis-guideline">Osteoarthritis</a>'
               '<a href="/axial-spondyloarthritis-guideline#2026-axspa-guidelines-summary">'
               'View 2026 axSpA Guidelines Summary &rarr;</a>')
    ACR_TARGET = r"rheumatology\.org/(?!clinical-practice-guidelines)[a-z0-9-]*guideline[a-z0-9-]*/?(?:[?#].*)?$"

    def test_link_fragment_from_cta(self):
        frag = bd._link_fragment(self.PR_BODY, "https://rheumatology.org/press-releases/x",
                                 self.ACR_TARGET, "https://rheumatology.org/axial-spondyloarthritis-guideline")
        self.assertEqual(frag, "2026-axspa-guidelines-summary")

    LANDING = """<html><body>
      <a href="/lupus-guideline">Lupus</a>
      <a href="https://assets.contentstack.io/v3/assets/x/blt1/jia-summary-2015.pdf">Summary: 2015 JIA Guidelines</a>
      <a href="https://assets.contentstack.io/v3/assets/x/blt2/jia-appendix-2026.pdf">Appendix – PICO Questions 2026</a>
      <a href="https://assets.contentstack.io/v3/assets/x/blt3/jia-guidelines-summary-2026.pdf">Summary: 2026 JIA Guidelines Posted May 13, 2026</a>
      <a href="https://assets.contentstack.io/v3/assets/x/blt4/axspa-guideline-summary-2026.pdf">Summary: 2026 Axial SpA Guidelines Posted June 24, 2026</a>
      <a href="https://doi.org/10.1002/art.41042">2019 Update of the ACR Recommendations for JIA</a>
    </body></html>"""
    DOC_PAGE = {"document_link": r"assets\.contentstack\.io|doi\.org/10\.", "document_anchor": "summary"}

    def test_resolve_document_uses_slug_acronym(self):
        # "JIA" shares no token with "juvenile idiopathic arthritis"; the
        # landing-page slug supplies the acronym. The cross-linked axSpA
        # summary and the 2015/2019 documents must not win.
        item = {"title": "ACR Releases Updated Guidelines for Treatment of Juvenile Idiopathic Arthritis",
                "date": "2026-05-13"}
        doc = bd._resolve_document(self.LANDING, "https://rheumatology.org/juvenile-idiopathic-arthritis-guideline",
                                   item, self.DOC_PAGE)
        self.assertEqual(doc[0], "https://assets.contentstack.io/v3/assets/x/blt3/jia-guidelines-summary-2026.pdf")
        self.assertEqual(doc[1], "doc")
        self.assertIsNone(doc[2])

    def test_resolve_document_prefers_year_and_fragment(self):
        item = {"title": "New Guidelines Advance Treatment of Axial Spondyloarthritis in Adults and Youth",
                "date": "2026-06-24"}
        doc = bd._resolve_document(self.LANDING, "https://rheumatology.org/axial-spondyloarthritis-guideline",
                                   item, self.DOC_PAGE, frag="2026-axspa-guidelines-summary")
        self.assertEqual(doc[0], "https://assets.contentstack.io/v3/assets/x/blt4/axspa-guideline-summary-2026.pdf")

    def test_resolve_document_returns_doi_of_matching_article(self):
        body = ('<a href="https://doi.org/10.1002/acr2.11194">2021 ACR Vasculitis Guideline</a>'
                '<a href="https://assets.contentstack.io/v3/a/b/c/vasculitis-older.pdf">Summary: 2019 Vasculitis</a>')
        item = {"title": "ACR Releases Vasculitis Guideline", "date": "2021-08-02"}
        doc = bd._resolve_document(body, "https://rheumatology.org/vasculitis-guideline", item, self.DOC_PAGE)
        self.assertEqual(doc[1], "doi")
        self.assertEqual(doc[2], "10.1002/acr2.11194")

    def test_resolve_document_floor(self):
        # Nothing relevant on the page -> keep the landing-page URL.
        item = {"title": "ACR Releases Updated Lyme Disease Guideline", "date": "2026-01-01"}
        self.assertIsNone(bd._resolve_document(self.LANDING, "https://rheumatology.org/lyme-disease-guideline",
                                               item, self.DOC_PAGE))

    def test_find_indexed_dois(self):
        class FakeEU:
            def search(self, term, retmax):
                self.term = term
                return 1, ["999"]
            def fetch(self, ids):
                return [{"pmid": "999", "doi": "10.1016/j.jacc.2026.07.025",
                         "title": "Fifth Universal Definition of MI", "journal": "J Am Coll Cardiol",
                         "date": "2026-09-15", "mesh": [], "keywords": []}]
        found = bd.find_indexed_dois(FakeEU(), {"10.1016/j.jacc.2026.07.025", "10.1/not-indexed"})
        self.assertEqual(found["10.1016/j.jacc.2026.07.025"]["pmid"], "999")
        self.assertNotIn("10.1/not-indexed", found)

    def test_new_since_baseline(self):
        a = {"title": "Guideline A", "url": "https://x/a", "date": None}
        b = {"title": "Guideline B", "url": "https://x/b", "date": None}
        dated = {"title": "Guideline C", "url": "https://x/c", "date": "2026-01-02"}
        seen = {}
        # First sighting: baseline only, undated links are not published.
        self.assertEqual(bd.new_since_baseline([a, dated], "https://x", seen, {}, "2026-10-11"), [dated])
        self.assertEqual(seen["https://x"], ["https://x/a", "https://x/c"])
        # A link posted later is published, dated the day it was first seen.
        out = bd.new_since_baseline([a, b, dated], "https://x", seen, {}, "2026-10-12")
        self.assertEqual([(i["url"], i["date"]) for i in out], [("https://x/b", "2026-10-12"), ("https://x/c", "2026-01-02")])
        # And keeps that date on later runs.
        out = bd.new_since_baseline([a, b], "https://x", seen, {"https://x/b": "2026-10-12"}, "2026-11-01")
        self.assertEqual(out[0]["date"], "2026-10-12")

    def test_web_item_dropped_once_indexed(self):
        acr = {"id": "acr", "abbr": "ACR", "specialty": "Rheumatology"}
        records = {}
        bd.merge(records, {"pmid": None, "doi": None, "web": True, "journal": "ACR website",
                           "title": "New Guidelines Advance Treatment of Axial Spondyloarthritis in Adults and Youth",
                           "date": "2026-06-24", "url": "u", "mesh": [], "keywords": []}, acr)
        bd.merge(records, {"pmid": None, "doi": None, "web": True, "journal": "ACR website",
                           "title": "ACR Updates Osteoarthritis Guidance", "date": "2026-09-14",
                           "url": "v", "mesh": [], "keywords": []}, acr)
        bd.merge(records, {"pmid": "9", "doi": "10.1/axspa", "journal": "Arthritis Rheumatol",
                           "title": "2026 ACR Guideline for the Treatment of Axial Spondyloarthritis in Adults and Youth",
                           "date": "2026-11-01", "mesh": [], "keywords": []}, acr)
        self.assertEqual(bd.drop_superseded_web(records), 1)
        titles = {r["title"] for r in records.values()}
        self.assertIn("ACR Updates Osteoarthritis Guidance", titles)
        self.assertNotIn("New Guidelines Advance Treatment of Axial Spondyloarthritis in Adults and Youth", titles)


class TaggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tagger = bd.Tagger(yaml.safe_load((ROOT / "conditions.yaml").read_text()))

    def tag(self, title, mesh=()):
        return self.tagger.tag({"title": title, "mesh": list(mesh), "keywords": []})

    def test_title_and_mesh(self):
        names, specs = self.tag("2025 EULAR recommendations for the management of gout")
        self.assertEqual(names, ["Gout"])
        self.assertEqual(specs, ["Rheumatology"])
        names, _ = self.tag("Guideline on something", mesh=["Atrial Fibrillation"])
        self.assertIn("Atrial Fibrillation", names)

    def test_minor_mesh_ignored(self):
        # Minor-topic MeSH (comorbidities, passing mentions) must not tag.
        rec = {"title": "Guideline on something", "mesh": ["Atrial Fibrillation"],
               "mesh_major": [], "keywords": []}
        names, _ = self.tagger.tag(rec)
        self.assertNotIn("Atrial Fibrillation", names)
        rec["mesh_major"] = ["Atrial Fibrillation"]
        names, _ = self.tagger.tag(rec)
        self.assertIn("Atrial Fibrillation", names)

    def test_word_boundaries(self):
        names, _ = self.tag("Guidelines for shivering in hospital wards")
        self.assertNotIn("HIV", names)

    def test_all_patterns_compile(self):
        self.assertGreater(len(self.tagger.conditions), 100)


class MergeTests(unittest.TestCase):
    def test_merge_by_doi_and_finalize(self):
        sources = {"acr": {"id": "acr", "abbr": "ACR", "specialty": "Rheumatology"},
                   "who": {"id": "who", "abbr": "WHO", "specialty": "multi"}}
        records = {}
        base = {"pmid": "1", "doi": "10.1/x", "title": "Gout guideline", "journal": "J",
                "date": "2024-01-01", "mesh": [], "keywords": []}
        bd.merge(records, base, sources["acr"])
        bd.merge(records, dict(base, pmid=None, title="Gout guideline (feed)"), sources["who"])
        tagger = bd.Tagger([{"name": "Gout", "specialty": "Rheumatology", "match": ["gout"]}])
        prev = {"pmid-1": {"first_seen": "2024-02-02"}}
        out = bd.finalize(records, sources, tagger, prev, "2026-01-01")
        self.assertEqual(len(out), 1)
        g = out[0]
        self.assertEqual(g["sources"], ["acr", "who"])
        self.assertEqual(g["first_seen"], "2024-02-02")
        self.assertEqual(g["url"], "https://doi.org/10.1/x")
        self.assertEqual(g["specialties"], ["Rheumatology"])

    def test_untagged_goes_to_general(self):
        sources = {"who": {"id": "who", "abbr": "WHO", "specialty": "multi"}}
        records = {}
        bd.merge(records, {"pmid": "2", "doi": None, "title": "Guideline on things", "journal": "",
                           "date": None, "mesh": [], "keywords": []}, sources["who"])
        out = bd.finalize(records, sources, bd.Tagger([]), {}, "2026-01-01")
        self.assertEqual(out[0]["specialties"], ["General"])
        self.assertEqual(out[0]["conditions"], [])


if __name__ == "__main__":
    unittest.main()
