"""Fetch pinned official BIRD annotations and SQLite seed files, never run images.

All downloaded data, image metadata and archives stay outside this checkout.
Only the exact regular tar member opt/dii/seed.sqlite is extracted to a caller-
controlled path. No image entrypoint or dataset-provided code is executed.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
import shutil
import tarfile

import httpx

REVISION = "5b256b5a9c17478f06ed6912612e9a14d17065c8"
REPOSITORY = "3036197201/bird-mini-dev-sqlite"
HF_BASE = f"https://huggingface.co/datasets/birdsql/bird_mini_dev/resolve/{REVISION}"
REGISTRY = f"https://registry-1.docker.io/v2/{REPOSITORY}"
ACCEPT = ", ".join(("application/vnd.docker.distribution.manifest.list.v2+json",
                     "application/vnd.docker.distribution.manifest.v2+json",
                     "application/vnd.oci.image.index.v1+json",
                     "application/vnd.oci.image.manifest.v1+json"))


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def fetch(client, url, path, expected=None, headers=None):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if expected is not None and sha256(path) != expected.removeprefix("sha256:"):
            raise ValueError(f"Existing file has wrong hash: {path}")
        return
    part = path.with_suffix(path.suffix + ".part")
    with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        with part.open("wb") as handle:
            for chunk in response.iter_bytes(1024 * 1024):
                handle.write(chunk)
    if expected is not None and sha256(part) != expected.removeprefix("sha256:"):
        raise ValueError(f"Downloaded file has wrong hash: {path}")
    part.replace(path)


def extract_seed(archive, target):
    with tarfile.open(archive, "r:gz") as tar:
        matches = [m for m in tar.getmembers() if m.name.lstrip("./") == "opt/dii/seed.sqlite"]
        if len(matches) != 1 or not matches[0].isfile() or matches[0].size > 2_000_000_000:
            raise ValueError("Expected one bounded, regular SQLite seed file")
        with tar.extractfile(matches[0]) as source, target.open("xb") as destination:
            shutil.copyfileobj(source, destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.data_dir.resolve()
    source_root = Path(__file__).resolve().parents[3]
    if root == source_root or source_root in root.parents:
        parser.error("Data must stay outside the source checkout")
    root.mkdir(parents=True, exist_ok=True)
    with httpx.Client(timeout=120, follow_redirects=True) as client:
        fetch(client, HF_BASE + "/data/manifest.json", root / "upstream-manifest.json")
        manifest = json.loads((root / "upstream-manifest.json").read_text())
        item = manifest["files"]["sqlite"]
        fetch(client, HF_BASE + "/" + item["path"], root / "mini_dev_sqlite.jsonl", item["sha256"])
        fetch(client, HF_BASE + "/README.md", root / "UPSTREAM-README.md")
        rows = [json.loads(line) for line in (root / "mini_dev_sqlite.jsonl").read_text(encoding="utf-8").splitlines()]
        if len(rows) != 500 or {r["question_id"] for r in rows} != set(range(500)):
            raise ValueError("Unexpected dataset IDs or size")
        db_ids = sorted({r["db_id"] for r in rows})
        if any(not re.fullmatch(r"[a-z0-9_]+", db) for db in db_ids):
            raise ValueError("Unexpected database identifier")
        tags_path = root / "docker-tags.json"
        fetch(client, f"https://hub.docker.com/v2/repositories/{REPOSITORY}/tags?page_size=100", tags_path)
        tags = json.loads(tags_path.read_text())["results"]
        response = client.get(f"https://auth.docker.io/token?service=registry.docker.io&scope=repository:{REPOSITORY}:pull")
        response.raise_for_status()
        headers = {"Authorization": "Bearer " + response.json()["token"], "Accept": ACCEPT}
        lock = {"hf_repository": "birdsql/bird_mini_dev", "hf_revision": REVISION,
                "annotations_sha256": sha256(root / "mini_dev_sqlite.jsonl"),
                "license": "CC-BY-SA-4.0", "database_repository": REPOSITORY, "databases": {}}
        (root / "databases").mkdir(exist_ok=True)
        for db in db_ids:
            candidates = [t for t in tags if t["name"].startswith(f"harbor-minidev1024-r1-{db}-")]
            if len(candidates) != 1:
                raise ValueError(f"Expected one matching official image tag for {db}")
            tag = candidates[0]
            meta = root / "image-metadata" / db
            fetch(client, f"{REGISTRY}/manifests/{tag['digest']}", meta / "index.json", tag["digest"], headers)
            index = json.loads((meta / "index.json").read_text())
            amd = next(m for m in index["manifests"] if m["platform"].get("architecture") == "amd64")
            fetch(client, f"{REGISTRY}/manifests/{amd['digest']}", meta / "manifest.json", amd["digest"], headers)
            image = json.loads((meta / "manifest.json").read_text())
            fetch(client, f"{REGISTRY}/blobs/{image['config']['digest']}", meta / "config.json", image["config"]["digest"], headers)
            config = json.loads((meta / "config.json").read_text())
            history = [h for h in config["history"] if not h.get("empty_layer", False)]
            if len(history) != len(image["layers"]):
                raise ValueError("Image history and layer counts disagree")
            seeds = [layer for h, layer in zip(history, image["layers"])
                     if h.get("created_by", "").startswith("COPY seed.sqlite /opt/dii/seed.sqlite")]
            if len(seeds) != 1 or seeds[0] != image["layers"][-1]:
                raise ValueError("Expected a final seed-only data layer")
            layer = seeds[0]
            archive = root / "layers" / (layer["digest"].split(":")[1] + ".tar.gz")
            print(f"Fetching {db}: {layer['size'] / 1e6:.1f} MB compressed", flush=True)
            fetch(client, f"{REGISTRY}/blobs/{layer['digest']}", archive, layer["digest"], headers)
            target = root / "databases" / f"{db}.sqlite"
            if not target.exists():
                extract_seed(archive, target)
            digest = sha256(target)
            tag_hash = tag["name"].removeprefix(f"harbor-minidev1024-r1-{db}-").split("-")[0]
            if not digest.startswith(tag_hash):
                raise ValueError(f"SQLite hash does not match official image tag: {db}")
            lock["databases"][db] = {"path": f"databases/{db}.sqlite", "sha256": digest,
                                      "bytes": target.stat().st_size, "image_tag": tag["name"],
                                      "image_index_digest": tag["digest"], "amd64_manifest_digest": amd["digest"],
                                      "data_layer_digest": layer["digest"]}
            (root / "source-lock.json").write_text(json.dumps(lock, indent=2), encoding="utf-8")
        print(f"Verified {len(rows)} questions and {len(lock['databases'])} SQLite databases", flush=True)


if __name__ == "__main__":
    main()
