"""Cheap change detection for the dashboard.

Instead of the browser asking every few seconds, the server watches a small
fingerprint of the database and pushes a message when it actually changes.
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Event, Job, LibraryItem, Setting


def fingerprint(session: Session) -> str:
    """Short string that changes whenever something a dashboard shows changed.

    Derived from the data itself, so no call site can forget to announce a change.
    """
    jobs_count, jobs_updated = session.execute(
        select(func.count(Job.id), func.max(Job.updated_at))
    ).one()
    events_max = session.scalar(select(func.max(Event.id)))
    library_indexed = session.scalar(select(func.max(LibraryItem.indexed_at)))
    library_count = session.scalar(select(func.count(LibraryItem.id)))
    settings_updated = session.scalar(select(func.max(Setting.updated_at)))
    parts = [jobs_count, jobs_updated, events_max, library_count, library_indexed, settings_updated]
    return "|".join("" if part is None else str(part) for part in parts)
