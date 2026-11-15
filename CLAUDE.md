# HyperCut: notes for Claude

Coding style: follow Andrej Karpathy-inspired guidelines (https://github.com/forrestchang/andrej-karpathy-skills): think before coding, simplest working change, touch only what the task needs, define how you'll verify it.

- Stdlib first: server is `http.server`, HTTP is `urllib`. Don't add a dependency for a few lines.
- `pytest --cov=hypercut --cov-fail-under=100` must stay green. No `pragma: no cover`, no network in tests.
- Before claiming a pipeline change works: `python scripts/live_test.py` and `python scripts/e2e.py`.
- Read `docs/ARCHITECTURE.md` and `.claude/skills/hypercut/SKILL.md` (known traps) before changing timing or ffmpeg code.
