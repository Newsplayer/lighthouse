"""Job sources. Each source turns one board's public feed into a list of Job objects.

To add an ATS (Lever, Ashby, Workable, SmartRecruiters...), write a class with
`name` and `fetch(board) -> list[Job]`, then register it in SOURCES.
"""

from .greenhouse import GreenhouseSource

SOURCES = {
    GreenhouseSource.name: GreenhouseSource,
}
