"""Encrypting the credentials an API data source stores (ADR-0049 D2).

A database connection's password lives in the environment (ADR-0042) because the deployment
declares it. An API source's credentials are typed by an org admin, so they have to live in
the database — and a database dump, a backup or a ``SELECT *`` must not hand them out. They
are therefore stored as a Fernet token (AES-128-CBC + HMAC-SHA256) under a key only the
server holds, ``IOTSHIELD_SECRET_KEY``.

No key, no API sources: saving one is refused rather than stored in the clear, and reading one
says the server is not configured rather than failing obscurely. Generate a key with
``python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"``.
Rotating it makes every stored credential unreadable (each such source then asks for its
credentials again); that is recorded in the ADR rather than solved with a key ring.
"""

import json
import os
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

SECRET_KEY_ENV: Final = "IOTSHIELD_SECRET_KEY"


class SecretsUnavailable(Exception):
    """The server cannot encrypt or decrypt credentials. Safe to show: names no secret."""


def _fernet() -> Fernet:
    key = os.environ.get(SECRET_KEY_ENV, "").strip()
    if not key:
        raise SecretsUnavailable(
            f"the server has no {SECRET_KEY_ENV}, so it cannot store credentials"
        )
    try:
        return Fernet(key.encode())
    except ValueError as exc:
        raise SecretsUnavailable(f"the server's {SECRET_KEY_ENV} is not a valid key") from exc


def seal(values: dict[str, str]) -> str:
    """Encrypt a small mapping of credential fields into one opaque token."""
    return _fernet().encrypt(json.dumps(values, sort_keys=True).encode()).decode()


def unseal(token: str) -> dict[str, str]:
    try:
        raw = _fernet().decrypt(token.encode())
    except InvalidToken as exc:
        raise SecretsUnavailable(
            "stored credentials cannot be read with the server's current key; enter them again"
        ) from exc
    values = json.loads(raw)
    if not isinstance(values, dict):  # pragma: no cover - only seal() writes these
        raise SecretsUnavailable("stored credentials are malformed; enter them again")
    return {str(key): str(value) for key, value in values.items()}
