"""Explicit builder for Cinema Visual Style Vector v2.

Run: python -m app.style_index build
"""
from __future__ import annotations

import argparse
import json

from app.config import load_settings
from app.db import Database
from app.style_vector.embedding import BailianStyleEmbedder, persist_usage
from app.style_vector.index import StyleVectorIndex


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or inspect the versioned style-vector index")
    parser.add_argument("command", choices=("build", "health"))
    args = parser.parse_args()
    settings = load_settings(prefer_private_cloud=True)
    db = Database(settings.data_dir / "storyboard.sqlite3")
    embedder = BailianStyleEmbedder(settings) if args.command == "build" else None
    index = StyleVectorIndex(db, embedder)
    try:
        result = index.build() if args.command == "build" else index.health()
    finally:
        usage = embedder.drain_usage() if embedder else []
        persist_usage(db, usage)
        if embedder:
            embedder.close()
    print(json.dumps({"result": result, "requests": len(usage),
                      "estimated_list_price_cny": round(sum(item.get("estimated_list_price_cny") or 0
                                                             for item in usage), 6)},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
