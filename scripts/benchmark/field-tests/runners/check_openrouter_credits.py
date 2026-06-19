#!/usr/bin/env python3
"""check_openrouter_credits.py — preflight credit guard for OpenRouter (litellm:zai) lanes.

Polls OpenRouter's /api/v1/auth/key with the SAME key the litellm runner uses
(OPENROUTER_API_KEY). Catches a depleted balance LOUDLY before a run, instead of every
GLM cell silently DNF'ing at ~2s (a 402 looks identical to a throttle from outside).
On 2026-06-19 the GLM lane instant-rejected a full t4/t5 run for hours — it was empty
credits, not a throttle. This guard turns that into one clear "top up" message.

Exit codes: 0 = OK (or unlimited), 2 = DEPLETED, 3 = no key / cannot check.
Never prints the key. Uses only stdlib so it runs anywhere the benchmark does.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

AUTH_URL = "https://openrouter.ai/api/v1/auth/key"
KEY_ENV = "OPENROUTER_API_KEY"


def check(min_remaining: float = 0.05) -> int:
    key = os.environ.get(KEY_ENV, "").strip()
    if not key:
        print(f"[credits] {KEY_ENV} not in env — cannot check OpenRouter balance (skipping).")
        return 3
    req = urllib.request.Request(AUTH_URL, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8")).get("data", {})
    except urllib.error.HTTPError as exc:
        print(f"[credits] OpenRouter /auth/key HTTP {exc.code} — "
              + ("key invalid/expired" if exc.code in (401, 403) else "cannot verify balance"))
        return 2 if exc.code == 402 else 3
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        print(f"[credits] could not reach OpenRouter ({exc}); skipping guard.")
        return 3

    limit = data.get("limit")               # None == unlimited / no per-key cap
    usage = float(data.get("usage") or 0.0)
    free = data.get("is_free_tier")
    # OpenRouter reports the true available balance in `limit_remaining`; prefer it over
    # (limit - usage), which is wrong (usage is lifetime, the balance is topped up separately).
    raw_remaining = data.get("limit_remaining")
    if raw_remaining is None and limit is None:
        print(f"[credits] OpenRouter OK — usage ${usage:.4f}, no hard limit set "
              f"(is_free_tier={free}).")
        return 0
    remaining = float(raw_remaining) if raw_remaining is not None else float(limit) - usage
    lim_str = f"${float(limit):.2f}" if limit is not None else "none"
    print(f"[credits] OpenRouter — remaining ${remaining:.4f} "
          f"(usage ${usage:.4f}, limit {lim_str}, is_free_tier={free}).")
    if remaining <= min_remaining:
        print("[credits] DEPLETED — top up at https://openrouter.ai/credits before running "
              "OpenRouter (GLM / litellm:zai) lanes. They will otherwise all DNF at ~2s (402).")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(check())
