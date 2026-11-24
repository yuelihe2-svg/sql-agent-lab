"""Offline backup and restore for a standalone SQLite workspace."""
import argparse
from contextlib import closing, contextmanager
import json
from pathlib import Path
import re
import shutil
import sqlite3
from tempfile import TemporaryDirectory

from sql_agent_lab.macsql.data_sources import SourceRegistry
from sql_agent_lab.macsql.fetch_bird import sha256
from sql_agent_lab.macsql.task_lifecycle import LIVE_STATES, result_task_state, utc_now, write_json_atomic

_INDEX = "backup.json"
_RUN_FILES = ("manifest.json", "task.json", "trace.jsonl", "result.json", "evaluation-evidence.json")
_ID = r"[a-zA-Z0-9_-]{1,100}"
_FILE = re.compile(
    rf"workspace[.]sqlite|sources[.]json|databases/{_ID}[.]sqlite|"
    rf"workspace-knowledge/[0-9a-f]{{32}}|runs/{_ID}/(?:{'|'.join(re.escape(name) for name in _RUN_FILES)})"
)


@contextmanager
def _new_directory(destination):
    checkout = Path(__file__).resolve().parents[3]
    if destination == checkout or checkout in destination.parents:
        raise ValueError("Destination must be outside the checkout")
    if destination.exists() or destination.is_symlink():
        raise ValueError("Destination already exists; choose a new directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".sql-agent-stage-", dir=destination.parent) as temp:
        stage = Path(temp) / "contents"
        stage.mkdir()
        yield stage
        if destination.exists() or destination.is_symlink():
            raise ValueError("Destination already exists; choose a new directory")
        stage.rename(destination)


def _read_json(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _sqlite_snapshot(source, dest):
    if not source.is_file():
        raise ValueError("A required SQLite database is missing")
    with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as reader, \
            closing(sqlite3.connect(dest)) as writer:
        reader.backup(writer)
        if writer.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise ValueError("SQLite backup did not pass integrity checks")


def _is_link(path):
    return path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction())


def _safe_file(root, relative):
    if not isinstance(relative, str) or not _FILE.fullmatch(relative):
        raise ValueError("Backup contains an unsupported file path")
    path = root
    for component in relative.split("/"):
        path = path / component
        if _is_link(path):
            raise ValueError("Backup files must not use links")
    if not path.is_file():
        raise ValueError("A backup file is missing")
    return path


def create_backup(workspace, output, destination, *, sources=None):
    """Snapshot a stopped workspace, including all configured SQLite sources."""
    workspace, output, destination = (Path(p).resolve() for p in (workspace, output, destination))
    if not output.is_dir() or destination == output or output in destination.parents:
        raise ValueError("Choose an existing run directory and a separate backup destination")
    registry = SourceRegistry()
    if sources is not None:
        registry.load(sources)
    uploaded = output / "uploaded-sources.json"
    if uploaded.exists():
        registry.load(uploaded)
    databases = registry.databases
    with _new_directory(destination) as stage:
        dest_db = stage / "workspace.sqlite"
        _sqlite_snapshot(workspace, dest_db)
        with closing(sqlite3.connect(dest_db)) as conn:
            registered = {row[0] for row in conn.execute("SELECT id FROM data_source")}
            documents = list(conn.execute("SELECT storage_key, content_hash FROM knowledge_document"))
            conn.execute("UPDATE agent_run SET artifact_dir = NULL")
            conn.commit()
        if not registered <= databases.keys():
            raise ValueError("Source config is missing databases registered in the workspace")
        knowledge = workspace.with_name(workspace.stem + "-knowledge")
        entries = []
        for public in registry.catalog():
            key = public["id"]
            relative = f"databases/{key}.sqlite"
            dest = stage / relative
            dest.parent.mkdir(exist_ok=True)
            _sqlite_snapshot(databases[key], dest)
            entries.append({"id": key, "name": public["name"], "path": relative})
        write_json_atomic(stage / "sources.json", {"sources": entries})
        if _is_link(knowledge):
            raise ValueError("Knowledge directory must not be a link")
        for key, digest in documents:
            if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{32}", key):
                raise ValueError("Invalid knowledge storage key")
            source = knowledge / key
            if _is_link(source) or not source.is_file() or sha256(source) != digest:
                raise ValueError("A knowledge document is missing or does not match its record")
            dest = stage / "workspace-knowledge" / key
            dest.parent.mkdir(exist_ok=True)
            shutil.copyfile(source, dest)
        for run in output.iterdir():
            if not re.fullmatch(_ID, run.name) or not (run / "manifest.json").is_file():
                continue
            if _is_link(run):
                raise ValueError("A run directory must not be a link")
            for name in _RUN_FILES:
                source = run / name
                if source.exists() or source.is_symlink():
                    if _is_link(source) or not source.is_file():
                        raise ValueError("A run artifact is not a regular local file")
                    dest = stage / "runs" / run.name / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source, dest)
        files = {path.relative_to(stage).as_posix(): sha256(path)
                 for path in sorted(stage.rglob("*")) if path.is_file()}
        write_json_atomic(stage / _INDEX, {"version": 1, "created_at": utc_now(), "files": files})
    return len(files)


def verify_backup(root):
    """Check the manifest against all portable snapshot files."""
    root = Path(root).resolve(strict=True)
    index = root / _INDEX
    if index.is_symlink():
        raise ValueError("Backup manifest must be a local file")
    manifest = _read_json(index)
    if not isinstance(manifest, dict) or manifest.get("version") != 1 or not isinstance(manifest.get("files"), dict):
        raise ValueError("Unrecognized backup manifest")
    files = manifest["files"]
    if not {"workspace.sqlite", "sources.json"} <= files.keys():
        raise ValueError("Backup lacks required workspace files")
    for relative, expected in files.items():
        if sha256(_safe_file(root, relative)) != expected:
            raise ValueError(f"Backup checksum mismatch: {relative}")
    _validate_sources(root, files)
    return files


def _validate_sources(root, files):
    source_config = _read_json(root / "sources.json")
    entries = source_config.get("sources") if isinstance(source_config, dict) else None
    if not isinstance(entries, list):
        raise ValueError("Backup source config is invalid")
    ids = set()
    for entry in entries:
        key = entry.get("id") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not re.fullmatch(_ID, key) or key in ids:
            raise ValueError("Backup source ID is invalid or duplicated")
        relative = f"databases/{key}.sqlite"
        if entry.get("path") != relative or relative not in files:
            raise ValueError("Backup source must point to a database inside the bundle")
        ids.add(key)


def _apply_later_deletions(target, source):
    """Retain deletions made after the snapshot, using trusted newer metadata."""
    path = Path(source).resolve(strict=True)
    lookup = """
        SELECT c.id, c.workspace_id, c.user_id, c.datasource_id, c.created_at, d.deleted_at
        FROM deleted_conversation d JOIN conversation c ON c.id = d.conversation_id
    """
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as newer:
        target.executemany("""
            INSERT OR IGNORE INTO deleted_conversation(conversation_id, deleted_at)
            SELECT id, ?6 FROM conversation WHERE id=?1 AND workspace_id=?2
                AND user_id=?3 AND datasource_id=?4 AND created_at=?5
        """, newer.execute(lookup))


def restore_backup(root, destination, *, deletions_from=None):
    """Restore a verified backup as a new workspace, with no historical tasks replayed."""
    root, destination = Path(root).resolve(strict=True), Path(destination).resolve()
    if destination == root or root in destination.parents:
        raise ValueError("Choose a restore directory outside the backup")
    files = verify_backup(root)
    with _new_directory(destination) as stage:
        for relative, expected in files.items():
            dest = stage / relative
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(_safe_file(root, relative), dest)
            if sha256(dest) != expected:
                raise ValueError(f"Backup changed during restore: {relative}")
        records = []
        for task_file in (stage / "runs").glob("*/task.json"):
            task = _read_json(task_file)
            if task["state"] in LIVE_STATES:
                result_path = task_file.with_name("result.json")
                result = _read_json(result_path) if result_path.is_file() else None
                final = result_task_state(result)
                task.update(state=final if final != "unknown" else "interrupted", stage="finished",
                            error=result.get("error") if final != "unknown" else "Task was interrupted in the restored backup.")
                write_json_atomic(task_file, task)
            records.append((task["state"], task.get("stage"), task.get("error"), task_file.parent.name))
        if deletions_from is not None:
            from .database import migrate
            migrate(stage / "workspace.sqlite")
        with closing(sqlite3.connect(stage / "workspace.sqlite")) as conn:
            if deletions_from is not None:
                _apply_later_deletions(conn, deletions_from)
            for state, status_stage, error, run_id in records:
                conn.execute("UPDATE agent_run SET state=?, stage=?, error=?, artifact_dir=? WHERE id=?",
                             (state, status_stage, error, str(destination / "runs" / run_id), run_id))
            conn.commit()
        (stage / "runs").mkdir(exist_ok=True)
    return len(files)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    backup = sub.add_parser("create", help="Snapshot a stopped local workspace")
    backup.add_argument("--workspace-db", required=True, type=Path)
    backup.add_argument("--output-dir", required=True, type=Path)
    backup.add_argument("--sources", type=Path)
    backup.add_argument("--to", required=True, type=Path)
    verify = sub.add_parser("verify")
    verify.add_argument("--backup", required=True, type=Path)
    restore = sub.add_parser("restore")
    restore.add_argument("--backup", required=True, type=Path)
    restore.add_argument("--to", required=True, type=Path)
    restore.add_argument("--deletions-from", type=Path,
                         help="Keep later deletions from the same workspace's newer SQLite metadata")
    args = parser.parse_args()
    try:
        if args.command == "create":
            count = create_backup(args.workspace_db, args.output_dir, args.to, sources=args.sources)
        elif args.command == "verify":
            count = len(verify_backup(args.backup))
        else:
            count = restore_backup(args.backup, args.to, deletions_from=args.deletions_from)
    except (OSError, sqlite3.Error, ValueError, TypeError, KeyError) as error:
        parser.exit(1, f"{error}\n")
    print(f"{args.command}: {count} files")


if __name__ == "__main__":
    main()
