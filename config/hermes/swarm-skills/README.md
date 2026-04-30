# Swarm-skills (queen broadcast directory)

This directory is the queen's broadcast channel to the N mm-agents. Files
here are pulled by every node at the start of every cycle and merged into
each node's local Hermes skills.

**Who writes here:** the operator (during synthesis sessions, after reading
the `hermes-reports` branch) or an orchestrator agent on their behalf.

**Who reads here:** every mm-agent, automatically, on every cycle.

**Format:** standard Hermes skills layout (one skill per subdirectory with
a `SKILL.md` describing trigger conditions + actions).

**Initial state:** empty. After 48h of pilot reports, the first synthesis
pass writes the first skill here.

---

## How skills get promoted

The collaborative-ensemble protocol promotes successful node-level
patterns into fleet-wide skills via a three-step pipeline:

1. **Propose.** Any mm-agent that finds a fix/recipe that worked on
   their node writes a proposal to `~/.hermes/proposals/<skill>.md`.
   The proposal MUST include: trigger condition, exact commands,
   evidence (which node, what error it fixed, timestamp), and any
   env vars / image tags required.
2. **Corroborate.** Peers reading the forum can try the recipe on
   their own node next cycle. If it works, they reply with a
   `topic:"answer"` post citing the proposal id and confirm.
3. **Promote.** When >=3 distinct nodes have used the pattern
   successfully (proposal author + 2 corroborations counts), the
   queen (or `scripts/hermes/skill_promotion.py` running periodically
   on master) writes the proposal as
   `config/hermes/swarm-skills/<skill>.md`, commits, and pushes.
   Workers `git pull` it on their next cycle and rsync it into
   `~/.hermes/skills/mm-swarm/`.

The operator's master orchestrator may draft skill proposals directly
when forum convergence is observed, accelerating promotion for
high-signal patterns.
