"""BIRD acquisition/extraction and metadata-only subset selection contracts."""

import io
import tarfile
import json
import hashlib
import sqlite3
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor

import pytest

pytest.importorskip("langgraph")

from sql_agent_lab.macsql.fetch_bird import extract_seed
from sql_agent_lab.macsql.preflight_bird import select_questions
from sql_agent_lab.macsql.hpc.local_database import stage_question, cleanup_staged, choose_local_base


def test_subset_is_stable_under_input_order_and_gold_changes():
    rows = [{"question_id": db * 10 + i, "db_id": f"db_{db:02}",
             "difficulty": level, "SQL": "SELECT secret"}
            for db in range(11)
            for i, level in enumerate(["simple", "moderate", "moderate", "challenging"])]
    chosen = select_questions(rows)
    altered = [{**r, "SQL": "SELECT other_secret"} for r in reversed(rows)]
    assert [r["question_id"] for r in chosen] == [r["question_id"] for r in select_questions(altered)]
    assert len(chosen) == 24
    assert all(sum(r["db_id"] == f"db_{db:02}" for r in chosen) >= 2 for db in range(11))


def test_seed_extraction_ignores_unrelated_paths_and_never_executes_archive(tmp_path):
    archive = tmp_path / "layer.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in [("../../outside.txt", b"ignore"),
                              ("opt/dii/seed.sqlite", b"SQLite format 3\x00fixture")]:
            item = tarfile.TarInfo(name)
            item.size = len(content)
            tar.addfile(item, io.BytesIO(content))
    target = tmp_path / "db.sqlite"
    extract_seed(archive, target)
    assert target.read_bytes() == b"SQLite format 3\x00fixture"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["db.sqlite", "layer.tar.gz"]
    with pytest.raises(FileExistsError):
        extract_seed(archive, target)


def test_seed_extraction_rejects_symlinks(tmp_path):
    archive = tmp_path / "layer.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        item = tarfile.TarInfo("opt/dii/seed.sqlite")
        item.type = tarfile.SYMTYPE
        item.linkname = "../../outside"
        tar.addfile(item)
    with pytest.raises(ValueError, match="regular"):
        extract_seed(archive, tmp_path / "db.sqlite")


@pytest.fixture
def staged_seed(tmp_path):
    data=tmp_path/'source';(data/'databases').mkdir(parents=True)
    db=data/'databases/cards.sqlite'
    conn=sqlite3.connect(db)
    conn.execute('CREATE TABLE cards(id INTEGER PRIMARY KEY, label TEXT)')
    conn.execute("INSERT INTO cards VALUES(1, 'one')")
    conn.commit();conn.execute('PRAGMA journal_mode=WAL');conn.close()
    annotations=json.dumps({'question_id':1,'db_id':'cards','question':'Count cards','SQL':'SELECT COUNT(*) FROM cards'})+'\n'
    (data/'mini_dev_sqlite.jsonl').write_text(annotations,encoding='utf8')
    lock={'annotations_sha256':hashlib.sha256((data/'mini_dev_sqlite.jsonl').read_bytes()).hexdigest(),
          'databases':{'cards':{'path':'databases/cards.sqlite','sha256':hashlib.sha256(db.read_bytes()).hexdigest()}}}
    (data/'source-lock.json').write_text(json.dumps(lock),encoding='utf8')
    local=tmp_path/'local';local.mkdir()
    return data,db,local


def test_parallel_staging_has_independent_wal_files_and_same_answers(staged_seed):
    data,source,local=staged_seed
    before=source.read_bytes()
    def worker(_):
        report=stage_question(data,1,local,require_local=False)
        target=__import__('pathlib').Path(report['database_path'])
        assert target!=source and not target.samefile(source)
        with closing(sqlite3.connect(target.as_uri()+'?mode=ro',uri=True)) as c:
            for _ in range(20):assert c.execute('SELECT COUNT(*) FROM cards').fetchone()==(1,)
        assert target.read_bytes()==before
        return report
    with ThreadPoolExecutor(max_workers=8) as pool:reports=list(pool.map(worker,range(8)))
    assert len({r['data_dir'] for r in reports})==8
    assert source.read_bytes()==before
    for report in reports:assert cleanup_staged(report)
    assert not list(local.iterdir())


def test_staging_rejects_hash_mismatch_and_does_not_leave_copy(staged_seed):
    data,source,local=staged_seed
    lock=json.loads((data/'source-lock.json').read_text());lock['databases']['cards']['sha256']='0'*64
    (data/'source-lock.json').write_text(json.dumps(lock))
    with pytest.raises(ValueError,match='checksum'):stage_question(data,1,local,require_local=False)
    assert source.exists() and not list(local.iterdir())


def test_staging_never_discards_a_nonempty_wal(staged_seed):
    data,source,local=staged_seed
    wal=source.with_name(source.name+'-wal');wal.write_bytes(b'potential committed frames')
    with pytest.raises(ValueError,match='Nonempty source WAL'):stage_question(data,1,local,require_local=False)
    assert wal.read_bytes()==b'potential committed frames' and not list(local.iterdir())


def test_cleanup_requires_the_exact_owned_directory(staged_seed):
    data,source,local=staged_seed
    report=stage_question(data,1,local,require_local=False)
    with pytest.raises(ValueError,match='token'):cleanup_staged({**report,'token':'wrong'})
    with pytest.raises(ValueError,match='outside'):cleanup_staged({**report,'root':str(local)})
    assert source.exists()
    assert cleanup_staged(report)
    assert not cleanup_staged(report)


def test_explicit_shared_temp_directory_is_rejected(monkeypatch,tmp_path):
    monkeypatch.setattr('sql_agent_lab.macsql.hpc.local_database.filesystem_type',lambda p:'nfs4')
    with pytest.raises(ValueError,match='node-local'):choose_local_base({'MACSQL_LOCAL_TMPDIR':str(tmp_path)})
