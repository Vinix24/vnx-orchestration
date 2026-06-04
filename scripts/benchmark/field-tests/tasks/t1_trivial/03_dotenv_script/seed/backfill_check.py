"""backfill_check — verifies Supabase connection vars are present and prints summary.

Runs as CLI: `python3 backfill_check.py`. Requires SUPABASE_URL and SUPABASE_KEY
in the environment. The seed version of this script does NOT call load_dotenv(),
so it fails when .env exists but is not shell-sourced. Worker must add it.
"""
from __future__ import annotations

import os
import sys


def main() -> int:
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_KEY")

    if not supabase_url:
        raise RuntimeError("missing env var SUPABASE_URL (.env not loaded?)")
    if not supabase_key:
        raise RuntimeError("missing env var SUPABASE_KEY (.env not loaded?)")

    print(f"OK: connected to {supabase_url} (key prefix: {supabase_key[:8]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
