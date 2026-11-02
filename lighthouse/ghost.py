"""Ghost-job signals. Scaffold: flags, never hides — the user decides.

Current signal: posting age. Planned signals:
  - posting reappears with a new id but same fingerprint (perpetual repost)
  - posting vanished from the board (closed) -> mark inactive
  - vague/evergreen title ("Talent Pool", "General Application")
  - optional Claude pass over the description for evergreen/pipeline language
"""

from __future__ import annotations

from datetime import datetime, timezone

EVERGREEN_HINTS = ("talent pool", "talent community", "general application", "future opportunities")


def ghost_reasons(title: str, posted_at: datetime | None, max_age_days: int) -> list[str]:
    reasons = []
    if posted_at is not None:
        age = (datetime.now(timezone.utc) - posted_at.astimezone(timezone.utc)).days
        if age > max_age_days:
            reasons.append(f"open {age} days")
    t = title.lower()
    if any(h in t for h in EVERGREEN_HINTS):
        reasons.append("evergreen/pipeline title")
    return reasons
