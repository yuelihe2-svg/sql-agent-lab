"""Fetch only pinned public assets for a fixed HPC pilot; no model inference."""

import argparse
import json
from pathlib import Path
import platform
import subprocess

import httpx

from sql_agent_lab.macsql.fetch_bird import fetch, extract_seed, sha256, HF_BASE, REGISTRY
from sql_agent_lab.macsql.dataset import load_bird_jsonl, score_sql
from sql_agent_lab.macsql.metadata import load_metadata
from sql_agent_lab.macsql.tools import SQLiteTools, ToolLimits
from sql_agent_lab.macsql.context import ContextPolicy
from sql_agent_lab.macsql.reference_baseline import ReferenceMacSQLAgent, _messages, schema_text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root.resolve()
    bundle = root / "bundle"
    deployment = json.loads((bundle / "deployment.json").read_text())
    model_manifest = bundle / "model-manifest.json"
    if sha256(model_manifest) != deployment["model_manifest_sha256"]:
        raise ValueError("Model manifest differs from the Windows baseline")
    data = root / "data"
    data.mkdir(exist_ok=True)
    lock = json.loads((bundle / "source-lock.json").read_text())
    (data / "source-lock.json").write_bytes((bundle / "source-lock.json").read_bytes())
    with httpx.Client(timeout=180, follow_redirects=True) as client:
        manifest = json.loads(model_manifest.read_text())
        for item in [manifest["config"], *manifest["layers"]]:
            digest = item["digest"]
            print("Model asset", digest, item["size"], flush=True)
            fetch(client, f"https://registry.ollama.ai/v2/library/qwen3/blobs/{digest}",
                  root / "models" / "blobs" / digest.replace(":", "-"), digest)
        target = root / "models/manifests/registry.ollama.ai/library/qwen3/4b-instruct"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(model_manifest.read_bytes())
        fetch(client, HF_BASE + "/data/mini_dev_sqlite.jsonl", data / "mini_dev_sqlite.jsonl",
              lock["annotations_sha256"])
        # The annotation path is pinned in the deployment bundle, not inferred
        # from a mutable latest release. File content is SHA-256 verified.
        token = client.get("https://auth.docker.io/token", params={
            "service": "registry.docker.io", "scope": "repository:3036197201/bird-mini-dev-sqlite:pull"})
        token.raise_for_status()
        headers = {"Authorization": "Bearer " + token.json()["token"]}
        (data / "databases").mkdir(exist_ok=True)
        for db in deployment["databases"]:
            entry = lock["databases"][db]
            digest = entry["data_layer_digest"]
            archive = root / "downloads" / (db + ".tar.gz")
            print("Database asset", db, flush=True)
            fetch(client, f"{REGISTRY}/blobs/{digest}", archive, digest, headers)
            target = data / entry["path"]
            if not target.exists():
                extract_seed(archive, target)
            if sha256(target) != entry["sha256"]:
                raise ValueError(f"Database mismatch: {db}")
    databases = {db: data / lock["databases"][db]["path"] for db in deployment["databases"]}
    tools = SQLiteTools(databases, ToolLimits(timeout_seconds=30),
                        metadata=load_metadata(bundle / "column-metadata.json"))
    integrity_tools = SQLiteTools(databases, ToolLimits(timeout_seconds=300))
    dataset = load_bird_jsonl(data / "mini_dev_sqlite.jsonl", databases,
                             question_ids=deployment["question_ids"])
    policy = ContextPolicy(input_budget=14000, context_window=16384)
    checks = []
    for question, reference in dataset:
        print("Checking", question.question_id, question.db_id, flush=True)
        schema = tools.load_schema(question.db_id, enrich=True)
        with integrity_tools.connection(question.db_id) as conn:
            integrity = conn.execute("PRAGMA quick_check").fetchone()[0]
        score = score_sql(tools, reference, reference.sql)
        decomposer_prompt = ReferenceMacSQLAgent._decomposer_prompt(
            None, question.question, question.evidence, schema_text(schema))
        selector_expected = policy.count_tokens(_messages(decomposer_prompt)) > policy.input_budget
        checks.append({"question_id": question.question_id, "db_id": question.db_id,
                       "integrity": integrity, "scorer_self_check": score,
                       "selector_expected": selector_expected})
        if integrity != "ok" or score != 1:
            raise ValueError("Pilot preflight failed")
    report = {"mode": "no_model_preflight", "model_calls": 0, "checks": checks,
              "python": platform.python_version(), "model_manifest_sha256": sha256(model_manifest)}
    (root / "preflight.json").write_text(json.dumps(report, indent=2))
    subprocess.run([str(root / "env/bin/python"), "-m", "pip", "check"], check=True)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    main()
