"""Rank stats: counters showing whether the candidate pool is big enough."""

from .report import build_report
from .store import RankStats

__all__ = ["RankStats", "build_report"]
