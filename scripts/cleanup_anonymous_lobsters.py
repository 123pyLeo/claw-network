#!/usr/bin/env python3
"""Soft-delete anonymous lobsters that have been inactive for 30+ days.

An "anonymous lobster" is one with owner_id IS NULL — i.e., it was created
via the auto-register flow but never bound to a phone owner. These accumulate
indefinitely (mass registration, abandoned installs, attack noise) and have
no protection against unbounded growth.

Run as a daily cron job:
    0 4 * * *  /home/.venv/bin/python /home/claw-network-release/scripts/cleanup_anonymous_lobsters.py

Idempotent. Safe to re-run. Logs results to stdout (cron will mail them).
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Make sure we can find the server package no matter where this script lives
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.store import get_conn, utc_now  # noqa: E402

INACTIVE_DAYS = int(os.environ.get("CLEANUP_INACTIVE_DAYS", "30"))


def main() -> int:
    cutoff_dt = datetime.now(timezone.utc) - timedelta(days=INACTIVE_DAYS)
    cutoff = cutoff_dt.isoformat()
    now = utc_now()

    with get_conn() as conn:
        # Find candidates: anonymous (no owner) AND not already soft-deleted
        # AND last_seen_at older than cutoff (or last_seen_at IS NULL AND
        # created_at older than cutoff — they never connected at all)
        rows = conn.execute(
            """
            SELECT id, claw_id, name, runtime_id, last_seen_at, created_at
            FROM lobsters
            WHERE deleted_at IS NULL
              AND (owner_id IS NULL OR owner_id = '')
              AND is_official = 0
              AND (
                  (last_seen_at IS NOT NULL AND last_seen_at < ?)
                  OR
                  (last_seen_at IS NULL AND created_at < ?)
              )
            """,
            (cutoff, cutoff),
        ).fetchall()

        print(
            f"[{now}] cleanup_anonymous_lobsters: found "
            f"{len(rows)} candidates older than {INACTIVE_DAYS} days"
        )
        if not rows:
            return 0

        for r in rows:
            print(
                f"  soft-deleting {r['claw_id']:14} name={r['name']:30} "
                f"last_seen={r['last_seen_at']}  created={r['created_at']}"
            )

        conn.execute(
            """
            UPDATE lobsters
            SET deleted_at = ?, updated_at = ?
            WHERE deleted_at IS NULL
              AND (owner_id IS NULL OR owner_id = '')
              AND is_official = 0
              AND (
                  (last_seen_at IS NOT NULL AND last_seen_at < ?)
                  OR
                  (last_seen_at IS NULL AND created_at < ?)
              )
            """,
            (now, now, cutoff, cutoff),
        )

    print(f"[{now}] cleanup_anonymous_lobsters: soft-deleted {len(rows)} lobsters")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
