"""Log analysis utilities for engine health reporting.

Parses engine.log (JSON-Lines format) to produce per-account health
summaries and discrete automation cycle records used by the
/engine/account_health and /engine/account_cycles API endpoints.
"""

import json
import os
from datetime import datetime


def _default_log_path():
    data_dir = os.environ.get("BROWSER_ENGINE_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
    return os.path.join(data_dir, "engine.log")


def _read_log_lines(log_path=None):
    path = log_path or _default_log_path()
    if not os.path.exists(path):
        return []
    lines = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    lines.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception:
        pass
    return lines


def parse_account_health(target_account="ALL_EVENTS", login_data=None, log_path=None):
    """Parse engine.log for per-account health statistics.

    Args:
        target_account: Account email to filter on, or "ALL_EVENTS" for all.
        login_data:     List of user dicts from user_login_lookup.json.
        log_path:       Optional path override for engine.log.

    Returns:
        tuple: (summary dict, detailed list, valid_accounts list)
    """
    records = _read_log_lines(log_path)
    login_data = login_data or []

    valid_accounts = [u.get("username", "") for u in login_data if u.get("username")]

    # Aggregate per-account counters
    stats = {}  # account -> {successes, refusals, resets, events}

    for r in records:
        acct = r.get("account", "unknown")
        event = r.get("event", "")

        if target_account != "ALL_EVENTS" and acct != target_account.split("@")[0].lower():
            continue

        if acct not in stats:
            stats[acct] = {"successes": 0, "refusals": 0, "resets": 0, "events": []}

        if event == "SUCCESS":
            stats[acct]["successes"] += 1
        elif event == "REJECT":
            stats[acct]["refusals"] += 1
        elif event == "RESET":
            stats[acct]["resets"] += 1

        stats[acct]["events"].append(r)

    summary = {
        acct: {
            "successes": s["successes"],
            "refusals": s["refusals"],
            "resets": s["resets"],
            "total_events": len(s["events"]),
        }
        for acct, s in stats.items()
    }

    detailed = [
        {"account": acct, **s}
        for acct, s in stats.items()
    ]

    return summary, detailed, valid_accounts


def parse_engine_cycles(log_path=None):
    """Parse engine.log into discrete automation cycles.

    Each cycle spans from a START event to the next START event (exclusive),
    or the end of file.

    Args:
        log_path: Optional path override for engine.log.

    Returns:
        list: Dicts with keys start_idx, end_idx, account, ts, event_count.
    """
    records = _read_log_lines(log_path)
    if not records:
        return []

    cycles = []
    current_start = None
    current_start_idx = None

    for idx, r in enumerate(records):
        event = r.get("event", "")
        if event == "START":
            if current_start is not None:
                cycles.append({
                    "start_idx": current_start_idx,
                    "end_idx": idx - 1,
                    "account": current_start.get("account", "unknown"),
                    "ts": current_start.get("ts", ""),
                    "event_count": idx - current_start_idx,
                })
            current_start = r
            current_start_idx = idx

    # Close the last open cycle
    if current_start is not None:
        cycles.append({
            "start_idx": current_start_idx,
            "end_idx": len(records) - 1,
            "account": current_start.get("account", "unknown"),
            "ts": current_start.get("ts", ""),
            "event_count": len(records) - current_start_idx,
        })

    return cycles
