# Worklogs

One file per working session: what was decided, what was done, what is still
open. Named `YYYY-MM-DD-short-slug.md` using the date the session started.

Purpose is twofold. Day to day it is the handoff between sessions — anyone
picking the project back up reads the newest file and knows where things
stand. At the end it is raw material for the thesis: Chapter 3 needs the
design rationale and Chapter 6 needs every deviation from the proposal with
its justification, and both are far easier to write from dated notes than
from memory in May.

## Conventions

- **One file per session**, even a short one. A session that produced nothing
  but a decision is still worth a four-line file.
- **Write it at the end of the session**, not later. The value is the date.
- **Never edit an old worklog** except to fix a typo. If something recorded
  turns out to be wrong, say so in the current worklog and link back.
- **Decisions get recorded twice**: briefly here, and — if they override the
  proposal — properly in `docs/adr/`. The worklog says *that* a call was
  made; the ADR says why, with consequences.
- **Open items carry forward.** Each worklog's "Still open" section is the
  starting point for the next session's.

## Template

Copy `TEMPLATE.md` to start a new one.
