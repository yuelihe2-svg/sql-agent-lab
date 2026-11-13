"""Study one BIRD question using an already-downloaded local Ollama model.

No model search or cloud client is started. Native Ollama requests explicitly
set context length and disable thinking; token counts are local-server usage.
"""

import argparse
from dataclasses import asdict
import json
import os
from pathlib import Path
import time
from uuid import uuid4

import httpx

from .context import ContextPolicy
from .dataset import load_bird_jsonl, score_sql
from .fetch_bird import sha256
from .metadata import load_metadata
from .tools import SQLiteTools, ToolLimits
from .reference_core import JsonlTrace, ModelReply, ROLES, WorkflowConfig
from .reference_baseline import ReferenceMacSQLAgent


class ModelResponseError(ValueError):
    """A received but unusable reply, retaining actual local usage for auditing."""

    def __init__(self, message, *, body, usage, code):
        super().__init__(message)
        self.code, self.raw_usage = code, usage
        self.response = body.get('message', {}).get('content', '')
        self.done_reason = body.get('done_reason')


class LocalOllamaModel:
    """Local-only adapter; requires an existing GGUF model, never pulls weights."""

    def __init__(self, client, model, context_window=8192, *, num_threads=None, cpu_only=False):
        if client.base_url.host not in {"127.0.0.1", "::1", "localhost"}:
            raise ValueError("The local adapter requires a loopback server")
        self.client, self.model, self.context_window = client, model, context_window
        if num_threads is not None and (not isinstance(num_threads, int) or num_threads < 1):
            raise ValueError("num_threads must be a positive integer")
        self.num_threads, self.cpu_only = num_threads, cpu_only
        response = client.get("/api/tags")
        response.raise_for_status()
        candidates = [m for m in response.json()["models"] if m["name"] == model]
        if (len(candidates) != 1 or candidates[0].get("details", {}).get("format") != "gguf"
                or model.endswith((":cloud", "-cloud"))):
            raise ValueError("Choose an already-downloaded local GGUF model")
        self.metadata = candidates[0]
        response = client.post("/api/show", json={"model": model})
        response.raise_for_status()
        if response.json().get("remote_model") or response.json().get("remote_host"):
            raise ValueError("Remote models are not allowed in the local example")

    def __call__(self, *, role, model, messages, max_output_tokens):
        if model != self.model:
            raise ValueError("This single-model pilot only accepts the validated local model")
        print(f"Calling local {role} ...", flush=True)
        options = {"num_ctx": self.context_window, "num_predict": max_output_tokens,
                   "temperature": 0, "seed": 123}
        if self.num_threads is not None:
            options["num_thread"] = self.num_threads
        if self.cpu_only:
            options["num_gpu"] = 0
        response_format = 'json' if role == 'selector' or (messages and messages[-1]['content'].startswith(('SEMANTIC_REVIEW_V1', 'SEMANTIC_REVIEW_V2', 'SEMANTIC_REVIEW_V3', 'SEMANTIC_REVIEW_V4', 'SEMANTIC_REVIEW_V5', 'SEMANTIC_REVIEW_V6', 'SEMANTIC_REVIEW_V7', 'SEMANTIC_REVIEW_V8', 'OUTPUT_REQUIREMENTS_V1', 'OUTPUT_REQUIREMENTS_V2', 'OUTPUT_REQUIREMENTS_V3'))) else None
        request = {
            "model": model, "messages": messages, "format": response_format, "stream": False,
            "think": False, "keep_alive": "5m",
            "options": options,
        }
        if response_format is None:
            request.pop('format')
        response = self.client.post("/api/chat", json=request)
        response.raise_for_status()
        body = response.json()
        usage = {key: body[key] for key in (
            "prompt_eval_count", "eval_count", "total_duration", "load_duration",
            "prompt_eval_duration", "eval_duration", "prompt_eval_cached_count") if key in body}
        usage['response_format_contract'] = response_format or 'text'
        if not body.get("done") or body.get("done_reason") != "stop":
            raise ModelResponseError("Local model did not complete its response", body=body, usage=usage,
                                     code='output_truncated' if body.get('done_reason') == 'length' else 'incomplete_response')
        if not all(key in usage for key in ("prompt_eval_count", "eval_count")):
            raise ModelResponseError("Local server did not return token counts", body=body, usage=usage,
                                     code='missing_usage')
        print(f"Completed {role}: {usage['prompt_eval_count']} input / {usage['eval_count']} output tokens", flush=True)
        return ModelReply(body["message"]["content"], usage["prompt_eval_count"], usage["eval_count"],
                          response_id="local-" + uuid4().hex, simulated=False, raw_usage=usage)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--question-id", type=int, default=4)
    parser.add_argument("--model", default="qwen3:4b-instruct")
    parser.add_argument("--metadata", type=Path, help="Optional sourced column documentation JSON")
    parser.add_argument("--staging-report", type=Path, help="Verified per-job database staging record")
    parser.add_argument("--context-window", type=int, default=8192)
    parser.add_argument("--input-budget", type=int, default=6500)
    parser.add_argument("--port", type=int, default=11434, help="Local Ollama port; host remains loopback")
    parser.add_argument("--num-threads", type=int, help="CPU threads per inference request")
    parser.add_argument("--cpu-only", action="store_true", help="Explicitly set model GPU offload to zero")
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--schema-retrieval", action="store_true",
                        help="Add bounded FTS5 schema hints to the existing role prompts")
    parser.add_argument("--value-lookup", action="store_true",
                        help="Check up to two explicit question literals against read-only data")
    parser.add_argument("--retrieval-top-k", type=int, default=8)
    parser.add_argument('--semantic-review', action='store_true', help='Optional experimental audit: up to two extra model calls per question')
    parser.add_argument('--semantic-output-tokens', type=int, default=1600,
                        help='Separate output limit for the semantic audit only (default: 1600)')
    parser.add_argument('--no-output-type-check', dest='output_type_check', action='store_false',
                        help='Disable output type diagnostics for controlled comparisons')
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535 or args.request_timeout <= 0:
        parser.error("Use an unprivileged port and a positive request timeout")
    if args.value_lookup and not args.schema_retrieval:
        parser.error("--value-lookup requires --schema-retrieval")
    if not 1 <= args.retrieval_top_k <= 12:
        parser.error("--retrieval-top-k must be between 1 and 12")
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ["LANGCHAIN_TRACING_V2"] = "false"
    output, data = args.output_dir.resolve(), args.data_dir.resolve()
    source_root = Path(__file__).resolve().parents[3]
    if output == source_root or source_root in output.parents:
        parser.error("Keep traces outside the source checkout")
    if output.exists() and any(output.iterdir()):
        parser.error("Use a new empty output directory")
    output.mkdir(parents=True, exist_ok=True)
    lock = json.loads((data / "source-lock.json").read_text())
    if sha256(data / "mini_dev_sqlite.jsonl") != lock["annotations_sha256"]:
        raise ValueError("Dataset checksum mismatch")
    databases = {db: data / entry["path"] for db, entry in lock["databases"].items()}
    question, reference = load_bird_jsonl(data / "mini_dev_sqlite.jsonl", databases,
                                         question_ids=[args.question_id])[0]
    path = databases[question.db_id]
    before = sha256(path)
    if before != lock["databases"][question.db_id]["sha256"]:
        raise ValueError("Database checksum mismatch")
    staging = None
    if args.staging_report:
        staging = json.loads(args.staging_report.read_text(encoding='utf8'))
        if (Path(staging['data_dir']).resolve() != data or
            Path(staging['database_path']).resolve() != path.resolve() or
            staging['sha256'] != before or staging['question_id'] != str(args.question_id)):
            raise ValueError('Staging report does not match the actual question/database')
    tools = SQLiteTools({question.db_id: path}, ToolLimits(timeout_seconds=30),
                        metadata=load_metadata(args.metadata) if args.metadata else None)
    trace = JsonlTrace(output / "trace.jsonl")
    # The request destination is fixed, proxy environment variables are ignored,
    # and redirects are disabled. There is no fallback to a cloud endpoint.
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False,
                      follow_redirects=False, timeout=args.request_timeout) as client:
        model = LocalOllamaModel(client, args.model, context_window=args.context_window,
                                 num_threads=args.num_threads, cpu_only=args.cpu_only)
        policy = ContextPolicy(input_budget=args.input_budget, context_window=args.context_window)
        config = WorkflowConfig(tools, model, context=policy, trace=trace, max_repairs=1,
                                schema_retrieval=args.schema_retrieval,
                                value_lookup=args.value_lookup,
                                retrieval_top_k=args.retrieval_top_k, semantic_review=args.semantic_review,
                                semantic_output_tokens=args.semantic_output_tokens,
                                output_type_check=args.output_type_check)
        agent = ReferenceMacSQLAgent({role: args.model for role in ROLES}, config=config)
        manifest = {"mode": "local_inference", "cloud_api_calls": 0,
                    "workflow": "reference", "question": asdict(question),
                    "dataset_revision": lock["hf_revision"], "model": model.metadata,
                    "database_path": str(path.resolve()), "database_staging": staging,
                    "context_window": args.context_window, "input_byte_budget": args.input_budget,
                    "metadata_path": str(args.metadata.resolve()) if args.metadata else None,
                    "metadata_sha256": sha256(args.metadata) if args.metadata else None,
                    "output_limit": policy.output_reserve, "complex_output_limit": config.complex_output_tokens, "max_schema_refreshes": 1,
                    "selector_output_contract": "macsql_reference_decisions_v1",
                    "max_selection_retries": 0,
                    "max_schema_pages": 0,
                    "max_plan_retries": 0,
                    "max_semantic_reviews": int(args.semantic_review),
                    "max_requirements_extractions": int(args.semantic_review),
                    "requirements_output_tokens": 800,
                    "max_semantic_revisions": int(args.semantic_review),
                    "sql_validation": "sqlite_explain_strict_dqs_v1",
                    "plan_contract": "short_plan_single_sql_v2",
                    "semantic_review_contract": "atomic_outputs_suggestion_only_v7" if args.semantic_review else "disabled",
                    "semantic_output_tokens": config.semantic_output_tokens,
                    "semantic_review": args.semantic_review,
                    "output_type_check": args.output_type_check,
                    "schema_retrieval": args.schema_retrieval,
                    "value_lookup": args.value_lookup,
                    "retrieval_top_k": args.retrieval_top_k,
                    "port": args.port, "cpu_only": args.cpu_only, "num_threads": args.num_threads,
                    "request_timeout_seconds": args.request_timeout,
                    "thinking": False, "max_repairs": config.max_repairs, "sql_timeout_seconds": 30,
                    "agentopt_proxy_accounting_verified": False}
        (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        start = time.monotonic()
        try:
            sql = agent.run(question)
            feedback = tools.compile_sql_readonly(question.db_id, sql)
            if feedback['status'] == 'ok':
                feedback = tools.execute_sql_readonly(question.db_id, sql)
                score = score_sql(tools, reference, sql)
            else:
                # A rejected SQL identifier must not regain credit via SQLite's
                # legacy double-quoted-string fallback during external scoring.
                score = 0.0
            result = {"status": "completed" if feedback['status'] == 'ok' else "failed",
                      "sql": sql, "feedback": feedback, "score": score}
            if feedback['status'] != 'ok':
                result.update(error_type='SQLExecutionError', error=feedback.get('message', 'SQL execution failed'))
        except Exception as error:
            result = {"status": "failed", "error_type": type(error).__name__, "error": str(error)}
        recorded = [json.loads(line) for line in (output/'trace.jsonl').read_text(encoding='utf8').splitlines()]
        finished = next((e for e in reversed(recorded) if e['event']=='workflow_completed'), {})
        result['output_type_check'] = finished.get('output_type_check', {'status': 'unverified'})
        result['semantic_status'] = finished.get('semantic_status', 'not_completed')
        result['semantic_metrics'] = finished.get('semantic_metrics', {})
        if result['status']=='completed' and result['output_type_check']['status']=='mismatch':
            result['status']='needs_review'
        result.update(elapsed_seconds=time.monotonic() - start, cloud_api_calls=0,
                      fault_injected=False, database_unchanged=sha256(path) == before)
        (output / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=False), flush=True)
        if result["status"] in ("failed", "needs_review"):
            raise SystemExit(1)
    return result


if __name__ == "__main__":
    main()
