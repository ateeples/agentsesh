# AgentSesh Eval Harness

Controlled feedback loop to prove remediation works.

## The Loop

1. Agent runs a coding task with a "bad" CLAUDE.md (no tool discipline)
2. `sesh log` ingests the session transcript
3. `sesh fix --patch` generates CLAUDE.md fixes
4. Fixes are applied to the agent's CLAUDE.md
5. Agent runs the same task again
6. Grade delta proves (or disproves) the fix worked

## Usage

```bash
# Run the full eval loop
python3 eval/run_eval.py

# Run just one iteration
python3 eval/run_eval.py --once

# Compare results
python3 eval/run_eval.py --compare
```

## Structure

```
eval/
├── run_eval.py          # Main eval runner
├── tasks/               # Repeatable coding tasks
│   └── build_cli.md     # Task: build a simple CLI tool
├── workspace/           # Agent workspace (reset between runs)
├── configs/
│   ├── bad.md           # Intentionally bad CLAUDE.md
│   └── patched.md       # Auto-generated after sesh fix --patch
└── results/             # Session transcripts + grades
```
