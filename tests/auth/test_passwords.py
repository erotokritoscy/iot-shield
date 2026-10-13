"""Password hashing: salted, self-describing, and refusing anything malformed."""

from iotshield.auth.passwords import DUMMY_HASH, hash_password, verify_password


def test_a_hash_verifies_its_own_password_and_no_other() -> None:
    stored = hash_password("correct horse battery")
    assert verify_password("correct horse battery", stored)
    assert not verify_password("correct horse batterY", stored)
    assert not verify_password("", stored)


def test_the_same_password_hashes_differently_each_time() -> None:
    # A per-hash salt: two users with one password do not share a stored value.
    assert hash_password("same password") != hash_password("same password")


def test_the_stored_form_carries_its_own_parameters() -> None:
    scheme, n, r, p, salt, digest = hash_password("x" * 8).split("$")
    assert (scheme, int(r), int(p)) == ("scrypt", 8, 1)
    assert int(n) >= 2**14 and salt and digest


def test_a_hash_made_with_other_parameters_still_verifies() -> None:
    # Raising the work factor later must not lock existing users out: verify reads the
    # parameters from the stored value rather than assuming today's.
    import base64
    import hashlib

    salt = b"0123456789abcdef"
    digest = hashlib.scrypt(b"old password", salt=salt, n=2**14, r=8, p=1, dklen=32)
    stored = "$".join(
        [
            "scrypt",
            str(2**14),
            "8",
            "1",
            base64.b64encode(salt).decode(),
            base64.b64encode(digest).decode(),
        ]
    )
    assert verify_password("old password", stored)


def test_malformed_stored_values_never_match() -> None:
    for stored in ("", "plaintext", "bcrypt$1$2$3$4$5", "scrypt$x$8$1$AAAA$AAAA", "scrypt$1$2"):
        assert not verify_password("anything", stored)


def test_the_dummy_hash_matches_nothing_a_user_could_type() -> None:
    assert not verify_password("", DUMMY_HASH)
    assert not verify_password("password", DUMMY_HASH)
