"""Offline compiler/check/search for the cinema visual knowledge source."""
import argparse
import json
from collections import Counter

from app.style_direct.library import (ARTIFACT, SOURCE_ROOT, build, read_source, runtime_library, search)


def summary(library):
    return {"version": library.version, "source_hash": library.source_hash,
            "terms": len(library.terms), "recipes": len(library.recipes), "knowledge": len(library.knowledge),
            "categories": dict(Counter(recipe.category for recipe in library.recipes)),
            "network_requests": 0, "embedding_requests": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("build", "check"):
        child = sub.add_parser(command)
        child.add_argument("--source", default=str(SOURCE_ROOT))
    child = sub.add_parser("search")
    child.add_argument("query")
    child.add_argument("--section", choices=["all", "terms", "recipes", "knowledge"], default="all")
    child.add_argument("--limit", type=int, default=8)
    args = parser.parse_args()
    if args.command == "build":
        result = summary(build(args.source))
    elif args.command == "check":
        source = read_source(args.source)
        compiled = runtime_library()
        result = {**summary(source), "artifact": str(ARTIFACT), "ready": source.source_hash == compiled.source_hash}
        if not result["ready"]:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            raise SystemExit(1)
    else:
        result = {"results": search(args.query, section=args.section, limit=args.limit), "network_requests": 0}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
