"""Migrate the local Docker PostgreSQL database (or a .dump file) to AWS RDS.

Usage:
    python scripts/push_local_to_rds.py
    python scripts/push_local_to_rds.py --dump-file path/to/backup.dump --yes
    python scripts/push_local_to_rds.py --local-url postgresql://postgres:postgres@localhost:3008/social_media_compliance_dev --rds-url ...
"""

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")
load_dotenv(ROOT / ".env.production", override=False)

DEFAULT_LOCAL_URL = os.getenv(
    "LOCAL_DATABASE_URL",
    "postgresql://postgres:postgres@localhost:3008/social_media_compliance_dev",
)
DEFAULT_RDS_URL = os.getenv(
    "RDS_DATABASE_URL",
    "postgresql://zenatech_admin:Zenatech101@zenatech-social-media-compliance.czg08ucmkhhn.ca-central-1.rds.amazonaws.com:5432/zenatech_social_media_compliance_system",
)


def mask_url(url: str) -> str:
    parsed = urlparse(url)
    if not parsed.password:
        return url
    port = f":{parsed.port}" if parsed.port else ""
    return parsed._replace(netloc=f"{parsed.username}:***@{parsed.hostname}{port}").geturl()


def with_sslmode(url: str, mode: str = "require") -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query.setdefault("sslmode", mode)
    return urlunparse(parsed._replace(query=urlencode(query)))


def dump_local_database(local_url: str, dump_path: Path, container: str = "social_media_db_local") -> None:
    """Create a custom-format pg_dump from local PostgreSQL (host pg_dump or Docker container)."""
    pg_dump = shutil.which("pg_dump")
    if pg_dump:
        cmd = [pg_dump, local_url, "--no-owner", "--no-acl", "-Fc", "-f", str(dump_path)]
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"pg_dump exited with code {res.returncode}")
        return

    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Neither pg_dump nor docker is available on PATH.")

    parsed = urlparse(local_url)
    db_name = (parsed.path or "/social_media_compliance_dev").lstrip("/")
    user = parsed.username or "postgres"
    container_dump = "/tmp/local_to_rds.dump"
    res = subprocess.run(
        [docker, "exec", container, "pg_dump", "-U", user, "-d", db_name, "--no-owner", "--no-acl", "-Fc", "-f", container_dump],
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker exec pg_dump exited with code {res.returncode}")
    subprocess.run([docker, "cp", f"{container}:{container_dump}", str(dump_path)], check=True)
    subprocess.run([docker, "exec", container, "rm", "-f", container_dump], check=False)


def restore_to_rds(dump_path: Path, rds_url: str) -> None:
    """Restore a custom-format dump file to AWS RDS with SSL required."""
    pg_restore = shutil.which("pg_restore")
    target = with_sslmode(rds_url, "require")
    if pg_restore:
        cmd = [pg_restore, "--clean", "--if-exists", "--no-owner", "--no-privileges", "-d", target, str(dump_path)]
        res = subprocess.run(cmd, check=False)
        if res.returncode != 0:
            raise RuntimeError(f"pg_restore exited with code {res.returncode}")
        return

    docker = shutil.which("docker")
    if not docker:
        raise RuntimeError("Neither pg_restore nor docker is available on PATH.")
    res = subprocess.run(
        [docker, "run", "--rm", "-i", "--network", "host", "postgres:17", "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-privileges", "-d", target],
        stdin=dump_path.open("rb"),
        check=False,
    )
    if res.returncode != 0:
        raise RuntimeError(f"docker pg_restore exited with code {res.returncode}")


def run_alembic_upgrade(rds_url: str) -> None:
    alembic = shutil.which("alembic")
    if not alembic:
        print("[!] alembic not found on PATH; skipping alembic upgrade head.")
        return
    env = {**os.environ, "DATABASE_URL": rds_url, "DATABASE_SSL": "require"}
    res = subprocess.run([alembic, "upgrade", "head"], cwd=str(ROOT), env=env, check=False)
    if res.returncode != 0:
        raise RuntimeError(f"alembic upgrade head exited with code {res.returncode}")


async def verify_rds(rds_url: str) -> None:
    conn = await asyncpg.connect(rds_url, ssl="require")
    try:
        tables = await conn.fetchval(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public'"
        )
        print(f"[OK] AWS RDS verified: {tables} public tables.")
    finally:
        await conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Push local PostgreSQL database (or .dump) to AWS RDS.")
    parser.add_argument("--local-url", default=DEFAULT_LOCAL_URL, help="Local PostgreSQL connection URL")
    parser.add_argument("--rds-url", default=DEFAULT_RDS_URL, help="Target AWS RDS PostgreSQL connection URL")
    parser.add_argument("--dump-file", type=Path, help="Optional existing .dump file to restore instead of dumping local DB")
    parser.add_argument("--skip-alembic", action="store_true", help="Skip alembic upgrade head after restore")
    parser.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    print(f"Source : {args.dump_file if args.dump_file else mask_url(args.local_url)}")
    print(f"Target : {mask_url(args.rds_url)}")
    if not args.yes:
        reply = input("Proceed with overwriting AWS RDS public schema? [y/N]: ").strip().lower()
        if reply not in ("y", "yes"):
            print("Aborted.")
            sys.exit(0)

    tmp_dir = Path(tempfile.mkdtemp(prefix="rds_push_"))
    try:
        dump_path = args.dump_file or (tmp_dir / "local_backup.dump")
        if not args.dump_file:
            print("[1/3] Dumping local database...")
            dump_local_database(args.local_url, dump_path)
        print("[2/3] Restoring dump to AWS RDS...")
        restore_to_rds(dump_path, args.rds_url)
        if not args.skip_alembic:
            print("[3/3] Running alembic upgrade head on AWS RDS...")
            run_alembic_upgrade(args.rds_url)
        asyncio.run(verify_rds(args.rds_url))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
