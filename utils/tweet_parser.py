from __future__ import annotations

import re


def extract_boundaries(group_item_title: str) -> list[int]:
    """Extracts numeric range boundaries from labels like '<40', '115-139', or '240+'."""
    title = (group_item_title or "").strip().lower()
    if not title:
        return []

    boundaries: set[int] = set()

    # Formats like "115-139".
    match = re.search(r"(\d+)\s*[-–]\s*(\d+)", title)
    if match:
        boundaries.add(int(match.group(1)))
        boundaries.add(int(match.group(2)))

    # Formats like "<40", "<=40".
    for value in re.findall(r"<\s*=*\s*(\d+)", title):
        boundaries.add(int(value))

    # Formats like ">240", ">=240".
    for value in re.findall(r">\s*=*\s*(\d+)", title):
        boundaries.add(int(value))

    # Formats like "240+".
    for value in re.findall(r"(\d+)\s*\+", title):
        boundaries.add(int(value))

    # Fallback for single numeric threshold labels.
    if not boundaries:
        numbers = [int(x) for x in re.findall(r"\d+", title)]
        boundaries.update(numbers)

    return sorted(boundaries)


def min_distance_to_boundaries(tweet_count: int, boundaries: list[int]) -> int:
    """Returns absolute distance to the closest boundary, or large value if none exist."""
    if not boundaries:
        return 10_000
    return min(abs(tweet_count - boundary) for boundary in boundaries)
