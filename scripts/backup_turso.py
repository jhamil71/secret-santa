"""Download a full copy of the hosted (Turso) database, pictures included, into a local SQLite file.

    TURSO_DATABASE_URL=... TURSO_AUTH_TOKEN=... python3 scripts/backup_turso.py

Writes backups/santa-<date>.db. To use a backup locally, copy it to data/santa.db (and unset the TURSO_ variables).
"""
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import dbconn  # noqa: E402


def main():
    if not dbconn.turso_configured():
        sys.exit("Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN first.")
    src = dbconn.connect("unused")
    out_dir = os.environ.get("BACKUP_DIR", "backups")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, time.strftime("santa-%Y-%m-%d_%H%M.db"))
    dst = sqlite3.connect(path)
    tables = src.execute("SELECT name, sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for t in tables:
        dst.execute(t["sql"])
    for t in tables:
        rows = src.execute(f'SELECT * FROM "{t["name"]}"').fetchall()
        if rows:
            marks = ",".join("?" * len(rows[0]))
            dst.executemany(f'INSERT INTO "{t["name"]}" VALUES ({marks})', [tuple(r) for r in rows])
        print(f'{t["name"]}: {len(rows)} rows')
    for t in src.execute("SELECT sql FROM sqlite_master WHERE type='index' AND sql IS NOT NULL").fetchall():
        dst.execute(t["sql"])
    dst.commit()
    dst.close()
    print("Backup written:", path)


if __name__ == "__main__":
    main()
