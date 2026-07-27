"""Determinism: one private random stream per thing that is generated.

A committed corpus must be **fixed**. If its bytes drift, every benchmark silently measures a
different dataset and no two results are comparable — which is why the generated bundle is
written once and committed, exactly like the team3 artifact (ADR-0021 D5b).

The obvious way to get that is one seed per recording, drawing values in order. It works, and
it makes the corpus **ungrowable**: because the draws are sequential, inserting a device (or
even a channel) shifts every value after it, so adding one device rewrites the whole corpus
and invalidates every number already measured on it. The old generator had exactly this shape
— ``default_rng(recipe.seed)`` feeding a loop over channels.

So no stream is shared. Each one is derived from *its own name* — this scenario, this
recording, this device, this lane, this purpose — which buys the property that makes the
generator extensible at all:

======================================  ==========================================
Change                                  What moves
======================================  ==========================================
add a device to a recording             only that device's lines
add a lane to a scenario                only that lane's payload file
add a recording                         only that recording's files
reorder the devices in a recording      nothing (the builder sorts before rendering)
rename a device                         that device's lines (a rename *is* a new device)
======================================  ==========================================

The lane-scoping property pays off twice: it is what lets one recording be rendered as one
lane in one bundle and as three in another with the shared lane's payload **byte-identical**,
which is what makes a corroboration A/B apples-to-apples by construction rather than by hope.

Note "lane", not "format". The builder scopes streams by the renderer's *lane*, so re-rendering
a scenario in a second dialect of the same lane describes the same world — otherwise a
dialect-parity test could never be written, because the two bundles would disagree about what
happened rather than about how it was written down.
"""

import hashlib

import numpy as np

_SEPARATOR = "\x00"
"""Joins the name parts. A byte that cannot occur in an asset id or a format name, so
``("a", "bc")`` and ``("ab", "c")`` can never hash to the same stream."""

_WORDS = 4
_WORD_BYTES = 4


def stream(root_seed: int, *names: str) -> np.random.Generator:
    """A generator private to ``names``, stable across processes and machines.

    **Never** ``hash()``. Python randomizes string hashing per process unless
    ``PYTHONHASHSEED`` is pinned, so a seed derived from it would produce different data on
    every run — destroying the single property this module exists to provide, and doing it
    silently. ``blake2b`` is stable by definition, and cheap: this runs once per stream, not
    once per value.

    Mixed through ``SeedSequence`` rather than used as a raw seed so the digest's bits are
    spread across numpy's internal state; two names differing in one character then give
    unrelated streams instead of adjacent ones.
    """
    digest = hashlib.blake2b(
        _SEPARATOR.join(names).encode("utf-8"), digest_size=_WORDS * _WORD_BYTES
    ).digest()
    words = tuple(
        int.from_bytes(digest[index : index + _WORD_BYTES], "big")
        for index in range(0, len(digest), _WORD_BYTES)
    )
    return np.random.default_rng(np.random.SeedSequence((root_seed, *words)))
