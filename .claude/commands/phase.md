---
description: Run a numbered phase prompt from agents/prompts/ (e.g. /phase 01)
argument-hint: <phase number, e.g. 01>
---
Read `CLAUDE.md`. Then find the file in `agents/prompts/` whose name starts with `$ARGUMENTS-` and execute it exactly,
including its acceptance criteria and its final commit step. If no file matches, list the available prompts and stop.
