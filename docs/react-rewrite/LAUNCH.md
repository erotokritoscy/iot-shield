# Launching the rewrite session (for Andreas)

The implementing session never reads this file; it reads `brief.md`. This is your
pre-flight, the one thing to paste, and the morning-review checklist.

## Pre-flight (the run dies at 2am on any of these)

- [ ] **Branch.** `react-dashboard` already exists (cut from `week2-productization` at
      `42cd35a`) and is checked out — the agent works directly on it, no branching of its
      own. Verify before launch: `git branch --show-current` → `react-dashboard`.
- [ ] **Permissions.** `/goal` does not change permission mode: in default mode the
      session blocks at its first un-allowlisted command and waits for you. Start the
      session in a mode that can run unattended (e.g. `claude --dangerously-skip-permissions`,
      or auto-accept edits + a permissive allowlist you trust).
- [ ] **Docker Desktop running** — the packaging phase does `docker compose up`.
- [ ] **Node** ≥ 20 with npm — verified on this machine 2026-07-28: node v22.19.0,
      npm 11.13.0. Nothing to do unless the environment changed.
- [ ] **Claude Code ≥ 2.1.139** (the `/goal` feature), hooks not disabled.
- [ ] Start the session **in the `iotshield/` directory** (paths in the brief and goal
      are repo-relative).

## The one thing to paste — the goal

Paste this as the session's very first input, with no message before it. With `/goal`,
the condition itself is the first turn's directive, so the loop is active from the
agent's first action — which is why the bootstrap instructions (read the brief first,
audit the inventory, stay on the branch) live inside the goal text rather than in a
separate opening message.

```
/goal The React dashboard rewrite is complete per docs/react-rewrite/brief.md. Before
any implementation: read that brief in full — its §2 autonomy protocol governs this
session (nobody is watching; decide-and-log, never wait for approval) — then follow its
§0 read order, audit its §6 parity inventory against the dashboard source (it is a
floor, not a ceiling) recording additions in docs/react-rewrite/PARITY.md, and verify
the already-created react-dashboard branch is checked out: all work happens there, never
on another branch. Completion means: every row of PARITY.md (including added rows) has
status done, blocked, or contested — no todo rows; every done item was individually
verified with evidence surfaced in this conversation (test output, request bodies,
screenshots) and cited in PARITY.md; every blocked or contested item has a DEVIATIONS.md
entry — an honestly reported blocker satisfies this goal, a silently skipped or faked
item does not; `npm run check` in web/ exits 0 with output shown; `uv run pytest` and
scripts/check.sh pass with output shown; `docker compose up` serves the new dashboard on
:8080 and a screenshot walkthrough of Monitor, Operate, and Evaluate against demo data
has been shown in this conversation; the ADR and README/CLAUDE.md/compose updates
required by brief.md §10 exist; all work is committed on the react-dashboard branch with
nothing left uncommitted. If fundamentally blocked, or after 150 turns, stop and post a
complete status summary (PARITY.md state plus DEVIATIONS.md) instead of continuing.
```

## Morning review

1. **`DEVIATIONS.md`** — the index of everything that needs your eyes: deviations from
   the default decisions, blocked/contested parity items, surprising judgment calls.
2. **`PARITY.md`** — scan for `blocked` / `contested` / remaining `todo` rows.
3. **Screenshots** — `docs/react-rewrite/screenshots/` (also in the transcript): this is
   your deferred design review. Token-level restyling is cheap to request now.
4. **The branch** — `git log --oneline react-dashboard` (slice commits name parity IDs),
   then diff against the base.
5. **Run it** — `docker compose up`, open :8080, walk Triage → Compose → Evaluate
   yourself against the demo data.
6. Decide: merge, iterate in a follow-up session, or discuss.
