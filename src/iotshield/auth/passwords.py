"""Password hashing with the standard library's scrypt — no new dependency (ADR-0039).

The stored form is self-describing, ``scrypt$<n>$<r>$<p>$<salt-b64>$<hash-b64>``, so the
work factor can be raised later without invalidating existing hashes: ``verify`` reads the
parameters a hash was made with rather than assuming today's.
"""

import base64
import hashlib
import hmac
import secrets

# OWASP's scrypt floor (N=2^17, r=8, p=1) costs ~128 MiB per hash; 2^15 is 32 MiB, which
# keeps a login under ~100 ms on a laptop and leaves headroom on the API containers. The
# parameters travel with each hash, so raising them later is a one-line change.
_N = 2**15
_R = 8
_P = 1
_DKLEN = 32
_MAXMEM = 64 * 1024 * 1024
_SCHEME = "scrypt"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _derive(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=n, r=r, p=p, maxmem=_MAXMEM, dklen=_DKLEN
    )


def hash_password(password: str) -> str:
    """A salted scrypt hash in the self-describing stored form."""
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, _N, _R, _P)
    return f"{_SCHEME}${_N}${_R}${_P}${_b64(salt)}${_b64(digest)}"


def verify_password(password: str, stored: str) -> bool:
    """Whether ``password`` matches ``stored``. A malformed stored value never matches."""
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != _SCHEME:
            return False
        expected = base64.b64decode(digest)
        actual = _derive(password, base64.b64decode(salt), int(n), int(r), int(p))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# Verified against when the username does not exist, so a login for an unknown user costs
# the same scrypt as a wrong password and response time does not reveal which usernames
# are registered.
DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
