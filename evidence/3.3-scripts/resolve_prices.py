"""Resolve the Bedrock pricing page's Anthropic price placeholders the way its widget does.
Scratch tool, not part of the repo.

    python3 -I resolve_prices.py <page html> <price-data dir> <table title> <region> <model>...

The page's tables hold placeholders {priceOf!<service>/<file>!<key>[!opt]} in each widget's
data-pricing-markup. The widget script (b0.p.awsstatic.com/client-side-script/static/js/
main.bundle.js) fetches <endpoint>/<service>/USD/current/<file>.json for each token path and
shows regions[<selected region>][<key>]["price"].
"""

import gzip
import html
import json
import os
import re
import sys

page, data_dir, title, region, models = (
    sys.argv[1],
    sys.argv[2],
    sys.argv[3],
    sys.argv[4],
    sys.argv[5:],
)
text = open(page, encoding="utf-8", errors="replace").read()

markups = [html.unescape(m) for m in re.findall(r'data-pricing-markup="(.*?)"', text, flags=re.S)]
index, title = int(title.split(":", 1)[0]), title.split(":", 1)[1]
markup = markups[index]
assert " ".join(re.sub(r"<[^>]+>", " ", markup).split()).startswith(title), "wrong widget"
print(f"{len(markups)} pricing widgets; widget {index} is titled {title!r}")

header = [
    " ".join(re.sub(r"<[^>]+>", " ", c).split())
    for c in re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.findall(r"<tr\b.*?</tr>", markup, flags=re.S)[0], flags=re.S)
]
print("columns:", header)

FILES = {
    "bedrockfoundationmodels": "bedrockfoundationmodels.json",
    "cHVibGljLzQ4ZmRhYjdmLTgyNmMtNDQxMi1hMGZmLWE3NDcyM2VmZjEzNS81Nw": "geo-marketplace.json",
    "cHVibGljLzljYzM1NWVlLTRjNjYtNDk0OC04MTg0LWVjMDFkYjBlNGNmMy8zMg": "global-marketplace.json",
}
data = {}
for file_key, name in FILES.items():
    with open(os.path.join(data_dir, name), "rb") as f:
        raw = f.read()
    if raw[:2] == b"\x1f\x8b":  # served gzip-encoded; curl ran without --compressed
        raw = gzip.decompress(raw)
    data[file_key] = json.loads(raw)
    regions = sorted(data[file_key].get("regions", {}))
    print(f"{name}: top-level keys {sorted(data[file_key])}; {len(regions)} regions;"
          f" has {region!r}: {region in regions}")

TOKEN = re.compile(r"\{priceOf!(?P<service>[^/!]+)/(?P<file>[^!]+)!(?P<key>[^!}]+)(?P<mods>(?:![a-z=0-9]+)*)\}")
for row in re.findall(r"<tr\b.*?</tr>", markup, flags=re.S):
    cells = re.findall(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", row, flags=re.S)
    name = " ".join(re.sub(r"<[^>]+>", " ", cells[0]).split())
    if name not in models:
        continue
    print(f"\n== {name}")
    for column, cell in zip(header[1:3], cells[1:3]):
        m = TOKEN.search(cell)
        if not m:
            print(f"   {column}: no placeholder, cell {cell!r}")
            continue
        entry = data[m["file"]]["regions"].get(region, {}).get(m["key"])
        print(f"   {column}: file {m['file'][:24]}..., key {m['key']}, modifiers {m['mods'] or '-'}")
        print(f"      entry: {json.dumps(entry, sort_keys=True)}")
