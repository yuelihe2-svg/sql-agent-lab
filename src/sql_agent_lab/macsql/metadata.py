"""Optional, provenance-carrying column documentation; never execute source DDL."""

import argparse
import json
from pathlib import Path
import re

from .fetch_bird import HF_BASE, REVISION, fetch, sha256


def parse_schema_docs(schemas, source):
    """Parse the pinned BIRD baseline's backtick-quoted DDL comments as text.

    Published examples are kept as text, not evaluated as Python. They do not
    define an exhaustive enumeration or verify that a value exists locally.
    """
    result = {}
    for db, ddl in schemas.items():
        table = None
        result[db] = {}
        for line in ddl.splitlines():
            match = re.match(r"\s*CREATE TABLE `([^`]+)`", line)
            if match:
                table = match[1]
                result[db][table] = {}
                continue
            match = re.match(r"\s*`([^`]+)`.*?--\s*(.*)$", line)
            if table and match:
                column, comment = match.groups()
                description, _, examples = comment.partition("example:")
                result[db][table][column] = {
                    "description": description.rstrip("; "),
                    "documented_examples": examples.strip(), "source": source,
                }
    return result


def load_metadata(path):
    """Read a sidecar with databases→tables→columns, optional enum meanings."""
    path = Path(path)
    body = json.loads(path.read_text(encoding="utf-8"))
    return body["databases"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.output_dir.resolve()
    checkout = Path(__file__).resolve().parents[3]
    if root == checkout or checkout in root.parents:
        parser.error("Keep downloaded documentation outside the source checkout")
    import httpx
    source = HF_BASE + "/baseline_inference/schemas/sqlite.json"
    raw = root / "upstream-schema.json"
    with httpx.Client(timeout=60, follow_redirects=True) as client:
        fetch(client, source, raw)
    body = {"revision": REVISION, "source": source, "source_sha256": sha256(raw),
            "databases": parse_schema_docs(json.loads(raw.read_text(encoding="utf-8")), source)}
    target = root / "column-metadata.json"
    if target.exists():
        raise ValueError("Use a new metadata directory; never overwrite edited documentation")
    target.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
    print(target)


if __name__ == "__main__":
    main()
