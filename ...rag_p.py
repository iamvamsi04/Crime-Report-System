"""Remove retired financial and support storage, preserving populated legacy databases first."""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
import logging
import sqlite3
from uuid import uuid4

from config import settings

RETIRED_TABLES = ('billing_case_events', 'billing_cases', 'billing_disputes', 'payments', 'credit_notes')


def remove_retired_tables(conn, backup_dir=None):
    existing = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    retired = [name for name in RETIRED_TABLES if name in existing]
    if not retired:
        return None
    conn.execute('BEGIN IMMEDIATE')
    backup = None
    try:
        if any(conn.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] for table in retired):
            path = Path(conn.execute('PRAGMA database_list').fetchone()[2])
            if not path.is_file():
                raise ValueError('Cannot remove populated legacy tables without a file backup.')
            folder = Path(backup_dir) if backup_dir else settings.ROOT.parent / 'previous_versions/database_backups'
            folder.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
            backup = folder / f'{path.stem}-before-table-cleanup-{stamp}-{uuid4().hex[:8]}.db'
            # A separate read connection can snapshot the committed database while
            # this connection holds the reserved write lock, before any drops.
            with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro', uri=True)) as source:
                with closing(sqlite3.connect(backup)) as destination:
                    source.backup(destination)
                    if destination.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                        raise ValueError('Legacy backup failed its integrity check.')
            logging.getLogger(__name__).warning('Retired records preserved in %s', backup)
        for table in retired:
            conn.execute(f'DROP TABLE {table}')
        if conn.execute('PRAGMA foreign_key_check').fetchall():
            raise ValueError('Table cleanup would leave broken references.')
        conn.commit()
        return str(backup) if backup else None
    except BaseException:
        conn.rollback()
        raise


def remove_views(conn):
    """Drop stored query definitions atomically; underlying records are preserved."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='view'")]
        for name in names:
            quoted = '"' + name.replace('"', '""') + '"'
            conn.execute('DROP VIEW ' + quoted)
        conn.commit()
        return names
    except BaseException:
        conn.rollback()
        raise
