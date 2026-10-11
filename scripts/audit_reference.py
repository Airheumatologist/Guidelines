#!/usr/bin/env python3
"""One-off coverage audit: list the guidelines shown on the reference site's
guideline pages and report which ones are missing from site/data/guidelines.json.
Reads the reference pages only for this comparison; nothing from them is
ingested into the app."""
import json, re, sys, urllib.request

UA = "Mozilla/5.0 (compatible; GuidelinesWebApp-audit/1.0; +https://github.com/Airheumatologist/Guidelines)"
BASE = "https://updatesinmedicine.com"

def get(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "replace")

def rsc_text(body):
    """Concatenate Next.js flight payload chunks (self.__next_f.push([1,"..."]))."""
    parts = re.findall(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)', body)
    return "".join(json.loads('"' + p + '"') for p in parts)

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "discover"
    if mode == "discover":
        body = get(BASE + "/guidelines/rheumatology")
        txt = rsc_text(body)
        print("html bytes", len(body), "rsc bytes", len(txt))
        for m in list(re.finditer(r"(?i)spondyl", txt))[:4]:
            print("---- context ----")
            print(txt[max(0, m.start() - 700): m.end() + 700])
        keys = re.findall(r'"([a-zA-Z_]{3,30})":', txt)
        from collections import Counter
        print("---- common keys ----")
        print(Counter(keys).most_common(60))
