#!/usr/bin/env python3
"""Generate/check schema count maximums from the shared runtime limit."""

import argparse
import json
from pathlib import Path

from limits import MAX_COUNT


ROOT = Path(__file__).resolve().parents[1]


def schema_count_nodes():
    run = ROOT / "schemas" / "run.json"
    canonical = ROOT / "schemas" / "canonical.json"
    return ((run, ("properties", "count")),
            (canonical, ("properties", "spec", "properties", "count")))


def generate(check=False):
    stale = []
    for path, keys in schema_count_nodes():
        document = json.loads(path.read_text())
        node = document
        for key in keys:
            node = node[key]
        if node.get("maximum") != MAX_COUNT:
            stale.append(path)
            if not check:
                node["maximum"] = MAX_COUNT
                path.write_text(json.dumps(document, indent=2) + "\n")
    if check and stale:
        names = ", ".join(str(path.relative_to(ROOT)) for path in stale)
        raise SystemExit(f"schema count maximum is stale: {names}; run app/generate_schemas.py")
    return stale


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    generate(check=args.check)


if __name__ == "__main__":
    main()
