"""Accounts and access control: organizations, users, roles and login sessions (ADR-0039).

Deliberately outside the pipeline: no adapter, contract or detector knows a tenant exists.
The pipeline computes a run; storage stamps the run's organization onto every row it
writes; the APIs decide which organization a request may see. ``passwords`` is the only
cryptography here and uses the standard library alone.
"""

from iotshield.auth.passwords import hash_password, verify_password

__all__ = ["hash_password", "verify_password"]
