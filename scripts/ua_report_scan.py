#!/usr/bin/env python3
"""Scan a UA crawl report for user-visible defects."""
import json
import re
import sys

path = sys.argv[1] if len(sys.argv) > 1 else "/out/report.json"
data = json.load(open(path, encoding="utf-8"))

TEMPLATE = re.compile(r"\{[^}]*\}")
LITERALS = re.compile(r"\b(None|False|True|nan)\b")
ERRWORD = re.compile(r"\b(error|exception|traceback)\b", re.I)
MARK = re.compile(r"(\\u[0-9a-fA-F]{4}|\x60\x60\x60)")

flags = []
for item in data:
    for text in item["texts"]:
        where = (item["kind"], item["value"])
        if LITERALS.search(text):
            flags.append((where, "literal", text[:70]))
        if ERRWORD.search(text):
            flags.append((where, "errword", text[:70]))
        if len(text) > 3800:
            flags.append((where, "long", len(text)))
        if TEMPLATE.search(text):
            flags.append((where, "template", text[:70]))
        if MARK.search(text):
            flags.append((where, "escape", text[:70]))

seen = set()
for where, kind, sample in flags:
    key = (kind, str(sample)[:45])
    if key in seen:
        continue
    seen.add(key)
    print(where, kind, "|", sample)

print("screens", len(data), "flags", len(flags), "unique", len(seen))
