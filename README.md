# Robotics ML Infrastructure Platform (project name TBD)

An ML infrastructure platform for robotics. The specific problem is selected in scaffolding phase 01
and recorded in `agents/spec/problem.md`.

This repository is driven by coding agents (Claude Code) working from persistent context in `agents/`.

## Getting started (human)

```bash
git init && git add -A && git commit -m "chore: initial scaffold"
pip install pre-commit && pre-commit install        # secret scanning + hygiene hooks
cp .env.example .env                                 # then fill in HF_KEY locally; .env is gitignored
python scripts/check_repo_hygiene.py                 # should print OK
claude                                               # start Claude Code in this directory
```

Then run the phase prompts in order, each in a **fresh session** (`/clear` between):

```text
/phase 01     industry research + problem selection      (you approve the problem)
/phase 02     requirements + technology evaluation
/phase 03     architecture                               (you review)
/phase 04     repo + tooling scaffold
/phase 05     minimal vertical slice
/phase 06     benchmark + observability foundations
/phase 07     repo-wide review + handoff
```

Or paste the file contents from `agents/prompts/` directly. See `agents/prompts/README.md`.

## Things only you can do

- Keep real credentials in `.env` or your shell only. Never paste them into chat, files, or commits.
- Tell the agent about your actual hardware if detection is ambiguous (GPU model, VRAM, RAM, disk, OS).
- Approve the problem selection (after `/phase 01`) and the architecture (after `/phase 03`).
