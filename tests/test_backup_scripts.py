"""Runs scripts/backup.sh and scripts/restore.sh against the real test PostgreSQL (no Docker needed)."""

import os
import shutil
import stat
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from conftest import DB_URL, ROOT

pytestmark = pytest.mark.skipif(
    not (shutil.which("pg_dump") and shutil.which("pg_restore") and shutil.which("psql") and shutil.which("bash")),
    reason="PostgreSQL client tools are required",
)

_u = urlsplit(DB_URL)


@pytest.fixture
def env(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    return {
        **os.environ,
        "PG_EXEC": "env",  # run pg_dump & co. directly instead of through `docker compose exec`
        "PGHOST": _u.hostname if _u.hostname != "localhost" else "127.0.0.1",
        "PGPORT": str(_u.port or 5432),
        "PGPASSWORD": _u.password or "",
        "POSTGRES_USER": _u.username or "postgres",
        "POSTGRES_DB": _u.path.lstrip("/"),
        "BACKUP_DIR": str(tmp_path / "backups"),
        "SKIP_APP_CONTROL": "1",
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
    }


def run(script, *args, env, check=True):
    p = subprocess.run(["bash", str(ROOT / "scripts" / script), *args], env=env, capture_output=True, text=True)
    if check:
        assert p.returncode == 0, p.stderr
    return p


def psql(env, sql, db=None):
    out = subprocess.run(
        ["psql", "-d", db or env["POSTGRES_DB"], "-U", env["POSTGRES_USER"], "-qAt", "-c", sql],
        env=env, capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def seed_user(env, email="backup@example.com"):
    psql(env, f"INSERT INTO users (id, email, password_hash) VALUES (gen_random_uuid(), '{email}', 'x') ON CONFLICT DO NOTHING")


def test_backup_verify_and_restore_drill(env):
    psql(env, "TRUNCATE users, links, auth_tokens CASCADE")
    seed_user(env)
    run("backup.sh", env=env)

    backups = Path(env["BACKUP_DIR"])
    dumps = list(backups.glob("shortener-*.dump"))
    assert len(dumps) == 1 and (backups / f"{dumps[0].name}.sha256").exists()
    assert not list(backups.glob("*.partial"))
    assert oct(stat.S_IMODE(backups.stat().st_mode)) == "0o700"

    drill = run("restore.sh", "--check", str(dumps[0]), env=env)
    assert "RESTORE CHECK OK: users=1 links=0" in drill.stderr
    # the scratch database is always cleaned up
    assert psql(env, "SELECT count(*) FROM pg_database WHERE datname LIKE '%_restore_check'", db="postgres") == "0"


def test_destructive_restore_brings_data_back(env):
    psql(env, "TRUNCATE users, links, auth_tokens CASCADE")
    seed_user(env, "keep-me@example.com")
    run("backup.sh", env=env)
    dump = next(Path(env["BACKUP_DIR"]).glob("shortener-*.dump"))

    psql(env, "TRUNCATE users CASCADE")  # simulate disaster
    seed_user(env, "different@example.com")
    assert psql(env, "SELECT email FROM users") == "different@example.com"

    # refuses without confirmation
    p = subprocess.run(["bash", str(ROOT / "scripts/restore.sh"), str(dump)], env=env, input="nope\n",
                       capture_output=True, text=True)
    assert p.returncode != 0 and psql(env, "SELECT email FROM users") == "different@example.com"

    run("restore.sh", "--yes", str(dump), env=env)
    assert psql(env, "SELECT email FROM users") == "keep-me@example.com"


def test_corrupt_dump_is_rejected(env):
    psql(env, "TRUNCATE users, links, auth_tokens CASCADE")
    run("backup.sh", env=env)
    dump = next(Path(env["BACKUP_DIR"]).glob("shortener-*.dump"))
    with open(dump, "ab") as f:
        f.write(b"tampered")
    p = run("restore.sh", "--check", str(dump), env=env, check=False)
    assert p.returncode != 0 and "checksum mismatch" in p.stderr


def test_failed_dump_leaves_no_partial_files(env):
    env = {**env, "PG_EXEC": "false"}  # every pg_* invocation fails
    p = run("backup.sh", env=env, check=False)
    assert p.returncode != 0
    assert list(Path(env["BACKUP_DIR"]).glob("shortener-*")) == []


def test_old_local_backups_are_pruned(env):
    run("backup.sh", env=env)
    old = Path(env["BACKUP_DIR"]) / "shortener-20200101T000000Z.dump"
    old.write_bytes(b"old")
    t = time.time() - 40 * 86400
    os.utime(old, (t, t))
    run("backup.sh", env={**env, "BACKUP_KEEP_DAYS": "14"})
    assert not old.exists()
    assert len(list(Path(env["BACKUP_DIR"]).glob("shortener-*.dump"))) == 2  # the two fresh ones


def test_offsite_upload_and_remote_prune(env, tmp_path):
    calls = tmp_path / "rclone.calls"
    stub = tmp_path / "bin" / "rclone"
    stub.write_text(f'#!/bin/sh\necho "$@" >> {calls}\n')
    stub.chmod(0o755)
    run("backup.sh", env={**env, "BACKUP_RCLONE_REMOTE": "offsite:bucket/shortener", "BACKUP_REMOTE_KEEP_DAYS": "30"})
    log = calls.read_text()
    assert "copy" in log and "offsite:bucket/shortener/" in log and ".dump" in log
    assert "delete offsite:bucket/shortener/ --min-age 30d" in log


def test_offsite_failure_fails_the_backup(env, tmp_path):
    stub = tmp_path / "bin" / "rclone"
    stub.write_text("#!/bin/sh\nexit 1\n")
    stub.chmod(0o755)
    p = run("backup.sh", env={**env, "BACKUP_RCLONE_REMOTE": "offsite:x"}, check=False)
    assert p.returncode != 0 and "off-site upload failed" in p.stderr
