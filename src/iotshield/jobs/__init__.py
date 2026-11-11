"""Saved, schedulable jobs (ADR-0050): a chain of pulls, run again on demand or on a schedule.

``model`` is what a job is (windows, schedule), ``run`` checks one and runs one execution
through the same code ``POST /runs`` uses, and ``worker`` is the compose service that queues
due jobs and runs them. Storage is ``storage/jobs.py``; the routes are ``api/jobs.py``.
"""
