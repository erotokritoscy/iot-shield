"""Where the repository is, for the defaults that ship inside it.

Three subsystems default a directory to one that ships in the repo: labelled corpora
(``eval/corpus.py``), bench specs (``eval/bench.py``) and rule sets
(``detect/rules/loader.py``). Each needs the repository root, and each derived it by counting
``__file__``'s parents — which is correct only for the file doing the counting. A module one
package deeper needs a different index, and an index that is off by one yields a directory
that does not exist rather than an error: every listing comes back empty and the subsystem
blames its missing data. Two copies already existed at the same depth; the third would have
been the first at a different one.

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
