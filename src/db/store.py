"""SQLite run history for the Wikipedia video dashboard."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from src.config import PROJECT_ROOT, get_env

logger = logging.getLogger(__name__)

DB_PATH = Path(get_env("DB_PATH", str(PROJECT_ROOT / "runs.db")))

_db_lock = threading.RLock()


def _connect(timeout: float = 60.0) -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=timeout)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    conn.execute("PRAGMA busy_timeout=60000;")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=60000;")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                format TEXT NOT NULL,
                topic TEXT NOT NULL,
                wiki_title TEXT,
                wiki_url TEXT,
                run_date TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                started_at TEXT,
                finished_at TEXT,
                article_json TEXT,
                script_path TEXT,
                video_path TEXT,
                youtube_video_id TEXT,
                error_message TEXT,
                steps_log TEXT,
                upload_status TEXT NOT NULL DEFAULT 'none',
                upload_error TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS uploaded_topics (
                topic_norm TEXT PRIMARY KEY,
                topic_display TEXT NOT NULL,
                wiki_title TEXT,
                uploaded_at TEXT NOT NULL,
                run_id INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS topic_aliases (
                alias_norm TEXT PRIMARY KEY,
                canonical_norm TEXT NOT NULL,
                canonical_title TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        for column, decl in (
            ("format", "TEXT"),
            ("topic", "TEXT"),
            ("wiki_title", "TEXT"),
            ("wiki_url", "TEXT"),
            ("article_json", "TEXT"),
            ("upload_status", "TEXT NOT NULL DEFAULT 'none'"),
            ("upload_error", "TEXT"),
            ("dashboard_deleted", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _ensure_column(conn, "runs", column, decl)

        # Remove false/stale entries in uploaded_topics linked to runs that failed upload or were never uploaded
        conn.execute(
            """
            DELETE FROM uploaded_topics
            WHERE run_id IN (
                SELECT id FROM runs
                WHERE upload_status != 'uploaded'
                   OR youtube_video_id IS NULL
                   OR youtube_video_id = ''
                   OR youtube_video_id = 'skipped'
            )
            """
        )

        # Seed uploaded_topics only from runs that actually succeeded uploading to YouTube
        existing_runs = conn.execute(
            """
            SELECT id, topic, wiki_title, finished_at, started_at, run_date
            FROM runs
            WHERE upload_status = 'uploaded'
              AND youtube_video_id IS NOT NULL
              AND youtube_video_id != ''
              AND youtube_video_id != 'skipped'
              AND topic IS NOT NULL AND TRIM(topic) != ''
            """
        ).fetchall()
        for r in existing_runs:
            run_id = r[0]
            topic_str = str(r[1] or "").strip()
            wiki_title_str = str(r[2] or "").strip() or None
            ts = r[3] or r[4] or r[5] or datetime.now(timezone.utc).isoformat()
            norm_t = normalize_topic_key(topic_str)
            if norm_t:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO uploaded_topics (topic_norm, topic_display, wiki_title, uploaded_at, run_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (norm_t, topic_str, wiki_title_str, ts, run_id),
                )
            if wiki_title_str:
                norm_w = normalize_topic_key(wiki_title_str)
                if norm_w and norm_w != norm_t:
                    conn.execute(
                        """
                        INSERT OR IGNORE INTO uploaded_topics (topic_norm, topic_display, wiki_title, uploaded_at, run_id)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (norm_w, wiki_title_str, wiki_title_str, ts, run_id),
                    )
        conn.commit()


@contextmanager
def db(timeout: float = 60.0, retries: int = 5) -> Iterator[sqlite3.Connection]:
    with _db_lock:
        attempt = 0
        while True:
            conn = _connect(timeout=timeout)
            try:
                yield conn
                conn.commit()
                break
            except sqlite3.OperationalError as exc:
                conn.rollback()
                attempt += 1
                if "locked" in str(exc).lower() and attempt < retries:
                    logger.warning("Database locked; retrying transaction (attempt %d/%d)...", attempt, retries)
                    time.sleep(0.15 * attempt)
                    continue
                raise
            finally:
                conn.close()


def create_run(topic: str, fmt: str, run_date: str, *, status: str = "running") -> int:
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cur = conn.execute(
            """
            INSERT INTO runs (
                format, topic, run_date, status, started_at, steps_log, upload_status
            )
            VALUES (?, ?, ?, ?, ?, '[]', 'none')
            """,
            (fmt.lower(), topic, run_date, status, now),
        )
        return int(cur.lastrowid)


def update_run(run_id: int, **fields: Any) -> None:
    if not fields:
        return
    columns = ", ".join(f"{key} = ?" for key in fields)
    values = list(fields.values()) + [run_id]
    with db() as conn:
        conn.execute(f"UPDATE runs SET {columns} WHERE id = ?", values)


def append_step_log(run_id: int, step: str, detail: str = "") -> None:
    with db() as conn:
        row = conn.execute("SELECT steps_log FROM runs WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return
        log: list[dict[str, str]] = json.loads(row["steps_log"] or "[]")
        log.append(
            {
                "step": step,
                "detail": detail,
                "at": datetime.now(timezone.utc).isoformat(),
            }
        )
        conn.execute(
            "UPDATE runs SET steps_log = ? WHERE id = ?",
            (json.dumps(log), run_id),
        )


def finish_run(run_id: int, status: str, error_message: str | None = None) -> None:
    update_run(
        run_id,
        status=status,
        finished_at=datetime.now(timezone.utc).isoformat(),
        error_message=error_message,
    )


def stop_run(run_id: int, reason: str = "Stopped by user") -> None:
    now = datetime.now(timezone.utc).isoformat()
    update_run(
        run_id,
        status="stopped",
        finished_at=now,
        error_message=reason,
    )
    append_step_log(run_id, "stopped", reason)


def reset_run_for_retry(run_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    update_run(
        run_id,
        status="running",
        started_at=now,
        finished_at=None,
        error_message=None,
        upload_status="none",
        upload_error=None,
    )
    append_step_log(run_id, "retry", "Generation retry initiated")


def queue_run_for_retry(run_id: int) -> None:
    now = datetime.now(timezone.utc).isoformat()
    update_run(
        run_id,
        status="queued",
        started_at=now,
        finished_at=None,
        error_message=None,
        upload_status="none",
        upload_error=None,
    )
    append_step_log(run_id, "queued", "Generation retry queued")


def list_failed_runs(limit: int = 500) -> list[dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE status IN ('failed', 'stopped')
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]


def set_upload_status(
    run_id: int,
    upload_status: str,
    *,
    youtube_video_id: str | None = None,
    upload_error: str | None = None,
) -> None:
    fields: dict[str, Any] = {
        "upload_status": upload_status,
        "upload_error": upload_error,
    }
    if youtube_video_id is not None:
        fields["youtube_video_id"] = youtube_video_id
    update_run(run_id, **fields)


def fail_orphaned_runs(
    error_message: str = "Interrupted by app restart",
) -> list[int]:
    now = datetime.now(timezone.utc).isoformat()
    failed_ids: list[int] = []
    with db() as conn:
        rows = conn.execute("SELECT id FROM runs WHERE status IN ('running', 'queued')").fetchall()
        ids = [int(row["id"]) for row in rows]
        if ids:
            conn.execute(
                """
                UPDATE runs
                SET status = 'failed',
                    finished_at = ?,
                    error_message = ?
                WHERE status IN ('running', 'queued')
                """,
                (now, error_message),
            )
            failed_ids.extend(ids)

        upload_rows = conn.execute(
            "SELECT id FROM runs WHERE upload_status = 'uploading'"
        ).fetchall()
        upload_ids = [int(row["id"]) for row in upload_rows]
        if upload_ids:
            conn.execute(
                """
                UPDATE runs
                SET upload_status = 'failed',
                    upload_error = ?
                WHERE upload_status = 'uploading'
                """,
                (error_message,),
            )
            failed_ids.extend(upload_ids)

        for run_id in sorted(set(failed_ids)):
            row = conn.execute(
                "SELECT steps_log FROM runs WHERE id = ?", (run_id,)
            ).fetchone()
            if not row:
                continue
            log: list[dict[str, str]] = json.loads(row["steps_log"] or "[]")
            log.append(
                {
                    "step": "interrupted",
                    "detail": error_message,
                    "at": now,
                }
            )
            conn.execute(
                "UPDATE runs SET steps_log = ? WHERE id = ?",
                (json.dumps(log), run_id),
            )
    return sorted(set(failed_ids))


def get_run(run_id: int) -> dict[str, Any] | None:
    with db() as conn:
        row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        return dict(row) if row else None


def delete_run(run_id: int) -> bool:
    with db() as conn:
        cur = conn.execute("DELETE FROM runs WHERE id = ?", (run_id,))
        return cur.rowcount > 0


def mark_run_dashboard_deleted(run_id: int) -> bool:
    """Mark a run as removed from the dashboard view after automatic deletion."""
    with db() as conn:
        cur = conn.execute(
            "UPDATE runs SET dashboard_deleted = 1 WHERE id = ?",
            (run_id,),
        )
        return cur.rowcount > 0


def list_runs(
    fmt: str | None = None,
    run_date: str | None = None,
    limit: int = 200,
    include_dashboard_deleted: bool = False,
) -> list[dict[str, Any]]:
    query = "SELECT * FROM runs"
    clauses: list[str] = []
    params: list[Any] = []
    if not include_dashboard_deleted:
        clauses.append("(dashboard_deleted = 0 OR dashboard_deleted IS NULL)")
    if fmt:
        clauses.append("format = ?")
        params.append(fmt.lower())
    if run_date:
        clauses.append("run_date = ?")
        params.append(run_date)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY run_date DESC, id DESC LIMIT ?"
    params.append(limit)
    with db() as conn:
        rows = conn.execute(query, params).fetchall()
        return [dict(row) for row in rows]


def list_run_dates() -> list[str]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT run_date FROM runs
            WHERE (dashboard_deleted = 0 OR dashboard_deleted IS NULL)
            ORDER BY run_date DESC
            """
        ).fetchall()
        return [row[0] for row in rows]


def list_failed_uploads(limit: int = 10) -> list[dict[str, Any]]:
    """Runs whose YouTube upload failed and still need a retry (capped to limit, default 10)."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE upload_status = 'failed'
              AND status = 'success'
              AND video_path IS NOT NULL
              AND video_path != ''
            ORDER BY id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_failed_uploads() -> int:
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE upload_status = 'failed'
              AND status = 'success'
              AND video_path IS NOT NULL
              AND video_path != ''
            """
        ).fetchone()
        return int(row[0])


def list_recent_failed_runs(hours: int = 24, limit: int = 20) -> list[dict[str, Any]]:
    """Runs whose video generation failed or was stopped within the last `hours` (default 24h)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db() as conn:
        rows = conn.execute(
            """
            SELECT * FROM runs
            WHERE status IN ('failed', 'stopped')
              AND (started_at >= ? OR finished_at >= ?)
            ORDER BY id ASC
            LIMIT ?
            """,
            (cutoff, cutoff, limit),
        ).fetchall()
        return [dict(row) for row in rows]


def count_recent_failed_runs(hours: int = 24) -> int:
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) FROM runs
            WHERE status IN ('failed', 'stopped')
              AND (started_at >= ? OR finished_at >= ?)
            """,
            (cutoff, cutoff),
        ).fetchone()
        return int(row[0]) if row else 0


def count_runs_today() -> dict[str, int]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with db() as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ?", (today,)
        ).fetchone()[0]
        failed = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ? AND status = 'failed'",
            (today,),
        ).fetchone()[0]
        success = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE run_date = ? AND status = 'success'",
            (today,),
        ).fetchone()[0]
        running = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'running'", ()
        ).fetchone()[0]
        queued = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE status = 'queued'", ()
        ).fetchone()[0]
        uploading = conn.execute(
            "SELECT COUNT(*) FROM runs WHERE upload_status = 'uploading'", ()
        ).fetchone()[0]
    return {
        "today_total": total,
        "today_success": success,
        "today_failed": failed,
        "running": running,
        "queued": queued,
        "uploading": uploading,
    }


def normalize_topic_key(topic: str) -> str:
    """Normalize topic for collision-free comparison (casing, punctuation, spacing, parentheticals, acronym dots)."""
    if not topic:
        return ""
    # Strip Wikipedia URL prefix if present
    t = re.sub(r"^https?://[^/]+/wiki/", "", topic.strip())
    # Strip parenthetical qualifiers (e.g. "Silk Road (trade network)" -> "Silk Road")
    stripped = re.sub(r"\s*\([^)]*\)", "", t).strip()
    if not stripped:
        stripped = t
    # Normalize acronym dots (e.g. "R.E.M." -> "REM", "U.S.A." -> "USA")
    acronym_cleaned = re.sub(r"(?<=\b[a-zA-Z])\.(?=[a-zA-Z](\.|\b))", "", stripped)
    acronym_cleaned = re.sub(r"\.", "", acronym_cleaned)
    cleaned = re.sub(r"[^\w\s]", " ", acronym_cleaned.lower().strip())
    return re.sub(r"\s+", " ", cleaned).strip()


def record_uploaded_topic(
    topic: str,
    wiki_title: str | None = None,
    run_id: int | None = None,
) -> None:
    """Persist an uploaded topic and its canonical wiki title to prevent repeats."""
    now = datetime.now(timezone.utc).isoformat()
    norm_topic = normalize_topic_key(topic)
    if not norm_topic:
        return

    with db() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO uploaded_topics (topic_norm, topic_display, wiki_title, uploaded_at, run_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (norm_topic, topic.strip(), (wiki_title or "").strip() or None, now, run_id),
        )
        if wiki_title:
            norm_wiki = normalize_topic_key(wiki_title)
            if norm_wiki and norm_wiki != norm_topic:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO uploaded_topics (topic_norm, topic_display, wiki_title, uploaded_at, run_id)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (norm_wiki, wiki_title.strip(), wiki_title.strip(), now, run_id),
                )


def get_uploaded_topics(exclude_run_id: int | None = None) -> set[str]:
    """Return set of normalized topic keys and wiki titles that have been successfully uploaded to YouTube."""
    with db() as conn:
        seen: set[str] = set()
        # 1. From uploaded_topics table
        if exclude_run_id is not None:
            rows = conn.execute(
                "SELECT topic_norm, wiki_title FROM uploaded_topics WHERE run_id != ? OR run_id IS NULL",
                (exclude_run_id,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT topic_norm, wiki_title FROM uploaded_topics").fetchall()
        for r in rows:
            if r[0]:
                seen.add(r[0])
            if r[1]:
                norm_w = normalize_topic_key(r[1])
                if norm_w:
                    seen.add(norm_w)

        # 2. From runs table: ONLY runs that were ACTUALLY uploaded to YouTube
        query = """
            SELECT topic, wiki_title FROM runs
            WHERE upload_status = 'uploaded'
              AND youtube_video_id IS NOT NULL
              AND youtube_video_id != ''
              AND youtube_video_id != 'skipped'
              AND topic IS NOT NULL AND TRIM(topic) != ''
        """
        params: list[Any] = []
        if exclude_run_id is not None:
            query += " AND id != ?"
            params.append(exclude_run_id)

        runs = conn.execute(query, params).fetchall()
        for r in runs:
            norm_t = normalize_topic_key(r[0])
            if norm_t:
                seen.add(norm_t)
            if r[1]:
                norm_w = normalize_topic_key(r[1])
                if norm_w:
                    seen.add(norm_w)

        # 3. Expand seen with topic aliases (if canonical is uploaded, alias is also marked uploaded)
        alias_rows = conn.execute("SELECT alias_norm, canonical_norm FROM topic_aliases").fetchall()
        for a_norm, c_norm in alias_rows:
            if c_norm in seen:
                seen.add(a_norm)
            elif a_norm in seen:
                seen.add(c_norm)

        return seen


def record_topic_alias(alias: str, canonical_title: str) -> None:
    """Record a known alias/redirect from an alias topic to its canonical Wikipedia title."""
    alias_norm = normalize_topic_key(alias)
    canonical_norm = normalize_topic_key(canonical_title)
    if not alias_norm or not canonical_norm or alias_norm == canonical_norm:
        return
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO topic_aliases (alias_norm, canonical_norm, canonical_title, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(alias_norm) DO UPDATE SET
                canonical_norm = excluded.canonical_norm,
                canonical_title = excluded.canonical_title,
                created_at = excluded.created_at
            """,
            (alias_norm, canonical_norm, canonical_title.strip(), now),
        )


def resolve_topic_alias(alias: str) -> str | None:
    """Return the canonical title for an alias if known, or None."""
    alias_norm = normalize_topic_key(alias)
    if not alias_norm:
        return None
    with db() as conn:
        row = conn.execute(
            "SELECT canonical_title FROM topic_aliases WHERE alias_norm = ?",
            (alias_norm,),
        ).fetchone()
        return str(row[0]) if row else None


def is_topic_uploaded(topic: str, exclude_run_id: int | None = None) -> bool:
    """Check if a topic or title was previously uploaded to YouTube."""
    norm = normalize_topic_key(topic)
    if not norm:
        return False
    return norm in get_uploaded_topics(exclude_run_id=exclude_run_id)


def get_covered_topics(exclude_run_id: int | None = None) -> set[str]:
    """Return set of normalized topic keys and wiki titles that have been covered:
    either already uploaded, completed successfully (video rendered), or actively running/queued.
    """
    seen: set[str] = set()
    # 1. From uploaded topics
    seen.update(get_uploaded_topics(exclude_run_id=exclude_run_id))

    # 2. From runs table: successful runs, or active runs (running/queued)
    with db() as conn:
        query = """
            SELECT topic, wiki_title FROM runs
            WHERE (dashboard_deleted = 0 OR dashboard_deleted IS NULL)
              AND (
                  status IN ('success', 'running', 'queued')
                  OR upload_status IN ('uploaded', 'uploading')
              )
              AND topic IS NOT NULL AND TRIM(topic) != ''
        """
        params: list[Any] = []
        if exclude_run_id is not None:
            query += " AND id != ?"
            params.append(exclude_run_id)

        runs = conn.execute(query, params).fetchall()
        for r in runs:
            norm_t = normalize_topic_key(r[0])
            if norm_t:
                seen.add(norm_t)
            if r[1]:
                norm_w = normalize_topic_key(r[1])
                if norm_w:
                    seen.add(norm_w)

        # 3. Expand with aliases
        alias_rows = conn.execute("SELECT alias_norm, canonical_norm FROM topic_aliases").fetchall()
        for a_norm, c_norm in alias_rows:
            if c_norm in seen:
                seen.add(a_norm)
            elif a_norm in seen:
                seen.add(c_norm)

    return seen


def is_topic_covered(topic: str, exclude_run_id: int | None = None) -> bool:
    """Check if a topic or title has already been covered (uploaded or locally rendered/active)."""
    norm = normalize_topic_key(topic)
    if not norm:
        return False
    return norm in get_covered_topics(exclude_run_id=exclude_run_id)


def list_uploaded_topics(limit: int = 100) -> list[dict[str, Any]]:
    """Return most recently uploaded topics."""
    with db() as conn:
        rows = conn.execute(
            """
            SELECT topic_display, wiki_title, uploaded_at, run_id
            FROM uploaded_topics
            ORDER BY uploaded_at DESC, rowid DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

