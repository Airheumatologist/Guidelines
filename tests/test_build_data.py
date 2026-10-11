"""Offline tests for scripts/build_data.py (no network)."""

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
        self.assertIn('"2018/01/01"[dp]', q)
        self.assertIn("NOT (erratum[pt]", q)

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
