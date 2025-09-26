"""Celery task registration entrypoint used by the worker container."""

from .app import celery

__all__ = ["celery"]
