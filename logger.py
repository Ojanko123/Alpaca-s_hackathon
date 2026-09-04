"""
Decision logger.

Writes a structured, human-readable log entry every time the agent
makes a decision (enter, exit, stop, hold, halt). This is the raw
material you'll draw the one-page write-up, slides, and social posts
from - don't rely on memory later in the week.

Two outputs:
- logs/decisions.jsonl  (machine-readable, one JSON object per line)
- logs/decisions.md     (human-readable running log, easy to skim)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

JSONL_PATH = LOG_DIR / "decisions.jsonl"
MD_PATH = LOG_DIR / "decisions.md"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "agent.log"),
    ],
)


def log_decision(
    pair: str,
    action: str,
    zscore: float,
    reason: str,
    equity: float | None = None,
    drawdown_pct: float | None = None,
    extra: dict | None = None,
) -> None:
    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "pair": pair,
        "action": action,
        "zscore": round(zscore, 3) if zscore is not None else None,
        "reason": reason,
        "equity": equity,
        "drawdown_pct": round(drawdown_pct, 4) if drawdown_pct is not None else None,
        "extra": extra or {},
    }

    with open(JSONL_PATH, "a") as f:
        f.write(json.dumps(entry) + "\n")

    with open(MD_PATH, "a") as f:
        f.write(
            f"- **{entry['timestamp']}** | `{pair}` | **{action}** | "
            f"z={entry['zscore']} | {reason}"
            + (f" | equity=${equity:,.2f}" if equity is not None else "")
            + (f" | drawdown={drawdown_pct:.2%}" if drawdown_pct is not None else "")
            + "\n"
        )


def daily_summary_header(day_number: int, date_str: str) -> None:
    with open(MD_PATH, "a") as f:
        f.write(f"\n## Day {day_number} - {date_str}\n\n")
