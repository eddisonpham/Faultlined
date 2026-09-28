# 0001. Record architecture decisions

- **Status:** accepted
- **Date (UTC):** project start
- **Deciders:** project owner

## Context
Multiple agents work on this repository across sessions. Chat history is not durable, so decisions must be recorded where later agents will read them.

## Options considered
| Option | Pros | Cons |
|---|---|---|
| ADRs in `agents/decisions/` | Versioned with code, greppable, reviewable | Discipline required |
| Decisions only in commit messages / chat | No extra files | Lost, unreadable at scale |

## Decision
Use ADRs in `agents/decisions/`. Any change to architecture, technology, interfaces, benchmark methodology, or scope requires an ADR before implementation.

## Consequences
Agents must read relevant ADRs before changing related code. Reviews verify no orphan decisions.
