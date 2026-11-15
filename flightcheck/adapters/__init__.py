"""Data-source adapters. Each returns a Flight for (ident, date, origin)."""

from .aeroapi import AeroAPIAdapter
from .schedule import ScheduleAdapter

__all__ = ["AeroAPIAdapter", "ScheduleAdapter"]
