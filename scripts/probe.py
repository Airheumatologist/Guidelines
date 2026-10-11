#!/usr/bin/env python3
"""Discovery probe: report how reachable society guideline pages and the
reference site are from the Actions runner, and what links they expose."""
import re, sys, urllib.request, urllib.parse
from html.parser import HTMLParser

UA = "Mozilla/5.0 (compatible; GuidelinesWebApp/1.0; +https://github.com/Airheumatologist/Guidelines)"

class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self._href = None; self._text = []
    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._href = dict(attrs).get("href"); self._text = []
    def handle_data(self, data):
        if self._href is not None: self._text.append(data)
    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((self._href, re.sub(r"\s+", " ", "".join(self._text)).strip()))
            self._href = None

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.status, r.headers.get("content-type", ""), r.read().decode("utf-8", "replace")

def probe(url, show=8, kw=r"guideline|recommendation|statement|consensus|practice"):
    try:
        status, ctype, body = get(url)
    except Exception as e:
        print(f"FAIL {url}: {e}"); return None
    p = Links(); p.feed(body)
    hits = [(urllib.parse.urljoin(url, h), t) for h, t in p.links if h and re.search(kw, t, re.I) and len(t) > 20]
    print(f"OK   {url} status={status} bytes={len(body)} anchors={len(p.links)} guideline-anchors={len(hits)}")
    for h, t in hits[:show]:
        print(f"       - {t[:100]}  <{h[:120]}>")
    return body, p.links

if __name__ == "__main__":
    print("=== reference site ===")
    for u in ["https://updatesinmedicine.com/", "https://updatesinmedicine.com/robots.txt",
              "https://updatesinmedicine.com/sitemap.xml", "https://updatesinmedicine.com/guidelines"]:
        r = probe(u, show=0)
        if r:
            body, links = r
            if u.endswith(".txt") or u.endswith(".xml"):
                print("\n".join("       " + l for l in body.splitlines()[:60]))
            else:
                seen = []
                for h, t in links:
                    full = urllib.parse.urljoin(u, h or "")
                    if "updatesinmedicine.com" in full and full not in seen:
                        seen.append(full)
                for s in seen[:120]:
                    print("       link:", s)
                scripts = re.findall(r'<script[^>]+src="([^"]+)"', body)
                print("       scripts:", scripts[:10])
                print("       body-head:", re.sub(r"\s+", " ", body[:1500]))
    print("\n=== society pages ===")
    for u in [l.strip() for l in open(sys.argv[1]) if l.strip() and not l.startswith("#")]:
        probe(u)
