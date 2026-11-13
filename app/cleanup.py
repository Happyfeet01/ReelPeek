"""Safe, periodic removal of cached media that has not been favorited."""
from __future__ import annotations

import argparse
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .database import Database

logger = logging.getLogger(__name__)


def prune_media(database: Database, data_root: Path, *, days: int = 14, dry_run: bool = False) -> int:
    """Delete cached media after N days, preserving favorites and history.

    Eligibility is based on downloaded_at, not updated_at. The database
    re-checks eligibility under a write lock before each deletion to prevent
    a concurrent favorite action from racing with the cleanup.
    """
    if days < 1:
        raise ValueError("days must be >= 1")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    candidates = database.expired_candidates(cutoff)
    if dry_run:
        return len(candidates)

    deleted = 0
    for item in candidates:
        try:
            if database.expire_if_eligible(item["id"], cutoff, data_root):
                deleted += 1
        except OSError:
            logger.exception("Cache cleanup failed for item %s; will retry", item["id"])
    return deleted


def main() -> None:
    parser = argparse.ArgumentParser(description="ReelPeek: expired cache preview/cleanup")
    parser.add_argument("--dry-run", action="store_true", help="Only count matching items")
    parser.add_argument("--days", type=int, default=int(os.getenv("RETENTION_DAYS", "14")))
    parser.add_argument("--data-dir", type=Path, default=Path(os.getenv("DATA_DIR", "/data")))
    args = parser.parse_args()
    db = Database(args.data_dir / "reelpeek.sqlite3")
    if not db.path.exists():
        parser.error(f"Database does not exist: {db.path}")
    if not args.dry_run:
        db.initialize()
    count = prune_media(db, args.data_dir, days=args.days, dry_run=args.dry_run)
    print(f"{'Would expire' if args.dry_run else 'Expired'} {count} cached items")


if __name__ == "__main__":
    main()
