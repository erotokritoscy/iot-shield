"""Where the repository is, for the defaults that ship inside it.

Six modules need the repository root: three that default a *directory* to one shipping in the
repo — labelled corpora (``eval/corpus.py``), bench specs (``eval/bench.py``) and rule sets
(``detect/rules/loader.py``) — and three that default a training or conversion *input*
(``detect/meter/train.py``, ``detect/team3/train.py``, ``eval/fetch.py``).

Five of them counted ``__file__``'s parents for themselves, in **three spellings**:
``parents[3]`` in ``eval/``, ``parents[4]`` two packages deeper in ``detect/``, and
``parents[3].parent`` in ``eval/fetch.py``, which wants the workspace *above* the repo. That is
the whole problem: the index is a fact about where a module happens to sit, so it is right for
one file and wrong for its neighbour, and moving a file one package deeper silently changes what
it means. An index off by one yields a directory that does not exist rather than an error, so
every listing comes back empty and the subsystem blames its missing data.

So: counted once, at a fixed and stated depth. Every default built on it stays overridable by
environment variable, because *where files live* is a deployment property (the same
12-factor reasoning as ``IOTSHIELD_DATABASE_URL``) — this is only the fallback that makes a
clone of the repository work with no setup, and it is exactly the fallback that goes wrong in
a non-editable install, which is why the env overrides are not optional extras.
"""

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
"""``src/iotshield/_paths.py`` -> ``src/iotshield`` -> ``src`` -> the repository root."""


def configured_root(env: str, default: Path, root: Path | None = None) -> Path:
    """Where a subsystem's files live: explicit argument, else ``$env``, else the shipped default.

    Three steps in that order, and each earns its place. The **explicit argument** is the test
    and library seam, so nothing has to mutate the environment to point a call somewhere else.
    The **environment variable** is how a deployment says it, because *where files live* is a
    deployment property, not an operator one — the same 12-factor reasoning as
    ``IOTSHIELD_DATABASE_URL``, and the escape hatch that matters when ``REPO_ROOT`` guesses
    wrong. The **default** makes a clone work with no setup.

    Written once because ``corpus_root``, ``bench_root`` and ``rules_root`` were the same four
    lines three times, down to the ``.strip()`` that makes a variable set to whitespace mean
    "unset" rather than "the current directory".
    """
    if root is not None:
        return root
    configured = os.environ.get(env, "").strip()
    return Path(configured) if configured else default
