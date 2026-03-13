#!/usr/bin/env python3
"""AgentSesh eval harness — controlled feedback loop for remediation testing.

The loop:
  1. Reset workspace
  2. Copy CLAUDE.md (bad or patched) into workspace
  3. Run Claude Code on the task (non-interactive)
  4. Find the session transcript
  5. Ingest with `sesh log`
  6. Grade with `sesh reflect`
  7. Generate patch with `sesh fix --patch`
  8. Optionally apply patch and re-run

Usage:
  python3 eval/run_eval.py                    # Full loop: bad run → patch → fixed run → compare
  python3 eval/run_eval.py --once             # Single run with current config
  python3 eval/run_eval.py --once --config bad # Single run with bad config
  python3 eval/run_eval.py --compare          # Compare most recent bad vs patched results
  python3 eval/run_eval.py --model haiku      # Use a specific model (default: sonnet)
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).parent
WORKSPACE = EVAL_DIR / "workspace"
RESULTS_DIR = EVAL_DIR / "results"
CONFIGS_DIR = EVAL_DIR / "configs"
TASKS_DIR = EVAL_DIR / "tasks"

# Where sesh stores its database
SESH_DIR = EVAL_DIR.parent / ".sesh"


def reset_workspace():
    """Clean the workspace directory for a fresh run."""
    if WORKSPACE.exists():
        # Preserve .claude/ dir if it exists (has session transcripts)
        shutil.rmtree(WORKSPACE)
    WORKSPACE.mkdir(parents=True, exist_ok=True)


def setup_workspace(config_name: str, task_name: str):
    """Set up workspace with CLAUDE.md and task prompt."""
    reset_workspace()

    # Copy CLAUDE.md
    config_src = CONFIGS_DIR / f"{config_name}.md"
    if not config_src.exists():
        print(f"Error: Config {config_src} not found", file=sys.stderr)
        sys.exit(1)

    claude_md = WORKSPACE / "CLAUDE.md"
    shutil.copy2(config_src, claude_md)

    # Read task prompt
    task_src = TASKS_DIR / f"{task_name}.md"
    if not task_src.exists():
        print(f"Error: Task {task_src} not found", file=sys.stderr)
        sys.exit(1)

    return task_src.read_text()


def run_agent(task_prompt: str, model: str = "sonnet", max_turns: int = 50) -> dict:
    """Run Claude Code on the task and return run metadata."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"\n{'='*60}")
    print(f"  Running agent (model: {model}, run_id: {run_id})")
    print(f"{'='*60}\n")

    # Build the claude command
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "--model", model,
        "--max-turns", str(max_turns),
        "-p", task_prompt,
    ]

    start_time = time.time()

    result = subprocess.run(
        cmd,
        cwd=str(WORKSPACE),
        capture_output=True,
        text=True,
        timeout=600,  # 10 min max
    )

    duration = time.time() - start_time

    return {
        "run_id": run_id,
        "model": model,
        "exit_code": result.returncode,
        "duration_seconds": round(duration, 1),
        "stdout_lines": len(result.stdout.splitlines()) if result.stdout else 0,
        "stderr_preview": result.stderr[:500] if result.stderr else "",
    }


def find_session_transcript() -> Path | None:
    """Find the most recent session transcript in the workspace.

    Claude Code stores transcripts in ~/.claude/projects/<project-hash>/
    """
    # Claude Code stores sessions under ~/.claude/projects/
    claude_dir = Path.home() / ".claude" / "projects"
    if not claude_dir.exists():
        return None

    # Find the most recent .jsonl file across all project dirs
    newest = None
    newest_mtime = 0

    for jsonl in claude_dir.rglob("*.jsonl"):
        mtime = jsonl.stat().st_mtime
        if mtime > newest_mtime:
            newest = jsonl
            newest_mtime = mtime

    # Only return if modified in the last 5 minutes (likely our session)
    if newest and (time.time() - newest_mtime) < 300:
        return newest
    return None


def ingest_and_grade(transcript: Path) -> dict:
    """Ingest a transcript and return grade info."""
    # Ensure sesh is initialized
    sesh_root = EVAL_DIR.parent
    sesh_dir = sesh_root / ".sesh"
    if not sesh_dir.exists():
        subprocess.run(
            ["python3", "-m", "sesh", "init"],
            cwd=str(sesh_root),
            capture_output=True,
        )

    # Ingest
    log_result = subprocess.run(
        ["python3", "-m", "sesh", "log", str(transcript), "--db", str(sesh_dir / "sesh.db")],
        cwd=str(sesh_root),
        capture_output=True,
        text=True,
    )
    print(f"  Ingest: {log_result.stdout.strip()}")

    # Get the most recent session's grade
    reflect_result = subprocess.run(
        ["python3", "-m", "sesh", "reflect", "--json", "--db", str(sesh_dir / "sesh.db")],
        cwd=str(sesh_root),
        capture_output=True,
        text=True,
    )

    try:
        data = json.loads(reflect_result.stdout)
        return {
            "session_id": data.get("session", {}).get("id", "unknown"),
            "grade": data.get("session", {}).get("grade", "?"),
            "score": data.get("session", {}).get("score", 0),
            "tool_calls": data.get("session", {}).get("tool_call_count", 0),
            "errors": data.get("session", {}).get("error_count", 0),
            "patterns": [p.get("type", "") for p in data.get("patterns", [])],
        }
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Warning: Could not parse grade data: {e}", file=sys.stderr)
        return {"grade": "?", "score": 0, "patterns": []}


def generate_patch() -> str:
    """Run sesh fix --patch and return the CLAUDE.md patch."""
    sesh_root = EVAL_DIR.parent
    sesh_dir = sesh_root / ".sesh"

    result = subprocess.run(
        ["python3", "-m", "sesh", "fix", "--patch", "--db", str(sesh_dir / "sesh.db")],
        cwd=str(sesh_root),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def create_patched_config(patch: str):
    """Create a patched CLAUDE.md by combining bad config with sesh fix patch."""
    bad_config = (CONFIGS_DIR / "bad.md").read_text()
    patched = bad_config + "\n\n" + patch
    patched_path = CONFIGS_DIR / "patched.md"
    patched_path.write_text(patched)
    print(f"  Patched config written to {patched_path}")
    return patched_path


def save_result(run_meta: dict, grade_info: dict, config_name: str):
    """Save run results to a JSON file."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    result = {
        "timestamp": datetime.now().isoformat(),
        "config": config_name,
        **run_meta,
        **grade_info,
    }

    result_file = RESULTS_DIR / f"{config_name}-{run_meta['run_id']}.json"
    result_file.write_text(json.dumps(result, indent=2))
    print(f"  Result saved to {result_file.name}")
    return result


def compare_results():
    """Compare the most recent bad vs patched results."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    bad_results = sorted(RESULTS_DIR.glob("bad-*.json"), reverse=True)
    patched_results = sorted(RESULTS_DIR.glob("patched-*.json"), reverse=True)

    if not bad_results:
        print("No bad-agent results found. Run eval first.")
        return

    if not patched_results:
        print("No patched-agent results found. Run full eval loop first.")
        return

    bad = json.loads(bad_results[0].read_text())
    patched = json.loads(patched_results[0].read_text())

    print(f"\n{'='*60}")
    print(f"  EVAL COMPARISON")
    print(f"{'='*60}")
    print()
    print(f"  {'':20s} {'BAD':>10s}  {'PATCHED':>10s}  {'DELTA':>10s}")
    print(f"  {'─'*55}")
    print(f"  {'Grade':20s} {bad['grade']:>10s}  {patched['grade']:>10s}  {'':>10s}")

    score_delta = patched['score'] - bad['score']
    sign = "+" if score_delta > 0 else ""
    print(f"  {'Score':20s} {bad['score']:>10d}  {patched['score']:>10d}  {sign}{score_delta:>9d}")

    tc_delta = patched.get('tool_calls', 0) - bad.get('tool_calls', 0)
    sign = "+" if tc_delta > 0 else ""
    print(f"  {'Tool calls':20s} {bad.get('tool_calls', 0):>10d}  {patched.get('tool_calls', 0):>10d}  {sign}{tc_delta:>9d}")

    err_delta = patched.get('errors', 0) - bad.get('errors', 0)
    sign = "+" if err_delta > 0 else ""
    print(f"  {'Errors':20s} {bad.get('errors', 0):>10d}  {patched.get('errors', 0):>10d}  {sign}{err_delta:>9d}")

    dur_delta = patched.get('duration_seconds', 0) - bad.get('duration_seconds', 0)
    sign = "+" if dur_delta > 0 else ""
    print(f"  {'Duration (s)':20s} {bad.get('duration_seconds', 0):>10.1f}  {patched.get('duration_seconds', 0):>10.1f}  {sign}{dur_delta:>9.1f}")

    print()
    print(f"  Bad patterns:     {', '.join(bad.get('patterns', [])) or 'none'}")
    print(f"  Patched patterns: {', '.join(patched.get('patterns', [])) or 'none'}")

    # Patterns eliminated
    eliminated = set(bad.get('patterns', [])) - set(patched.get('patterns', []))
    if eliminated:
        print(f"\n  Patterns ELIMINATED by patch: {', '.join(eliminated)}")

    # Patterns remaining
    remaining = set(bad.get('patterns', [])) & set(patched.get('patterns', []))
    if remaining:
        print(f"  Patterns REMAINING after patch: {', '.join(remaining)}")

    # New patterns (shouldn't happen, but track it)
    new = set(patched.get('patterns', [])) - set(bad.get('patterns', []))
    if new:
        print(f"  Patterns NEW in patched: {', '.join(new)}")

    print()

    if score_delta > 0:
        print(f"  RESULT: Remediation improved score by {score_delta} points.")
    elif score_delta == 0:
        print(f"  RESULT: No score change. Remediation had no measurable effect.")
    else:
        print(f"  RESULT: Score decreased by {abs(score_delta)} points. Investigate.")

    print()


def run_single(config_name: str, task_name: str, model: str) -> dict:
    """Run a single eval iteration."""
    print(f"\n  Config: {config_name}")
    print(f"  Task:   {task_name}")
    print(f"  Model:  {model}")

    # Setup
    task_prompt = setup_workspace(config_name, task_name)

    # Run agent
    run_meta = run_agent(task_prompt, model=model)

    if run_meta["exit_code"] != 0:
        print(f"\n  Agent exited with code {run_meta['exit_code']}")
        if run_meta["stderr_preview"]:
            print(f"  stderr: {run_meta['stderr_preview'][:200]}")

    # Find transcript
    transcript = find_session_transcript()
    if not transcript:
        print("  Warning: No session transcript found. Cannot grade.")
        result = save_result(run_meta, {"grade": "?", "score": 0, "patterns": []}, config_name)
        return result

    print(f"  Transcript: {transcript.name}")

    # Ingest and grade
    grade_info = ingest_and_grade(transcript)
    print(f"  Grade: {grade_info['grade']} (score: {grade_info['score']})")
    print(f"  Patterns: {', '.join(grade_info.get('patterns', [])) or 'none'}")

    # Save
    result = save_result(run_meta, grade_info, config_name)
    return result


def run_full_loop(task_name: str, model: str):
    """Run the full eval loop: bad → patch → patched → compare."""
    print("\n" + "=" * 60)
    print("  PHASE 1: Run with bad config")
    print("=" * 60)

    bad_result = run_single("bad", task_name, model)

    if bad_result.get("grade") == "?":
        print("\n  Cannot continue without a graded session. Aborting.")
        return

    # Generate patch
    print("\n" + "=" * 60)
    print("  PHASE 2: Generate remediation patch")
    print("=" * 60)

    patch = generate_patch()
    if not patch or "No CLAUDE.md" in patch:
        print("\n  No patch generated (clean session or no patterns). Nothing to fix.")
        return

    print(f"\n  Patch preview ({len(patch)} chars):")
    for line in patch.split("\n")[:10]:
        print(f"    {line}")
    if patch.count("\n") > 10:
        print(f"    ... ({patch.count(chr(10)) - 10} more lines)")

    create_patched_config(patch)

    print("\n" + "=" * 60)
    print("  PHASE 3: Run with patched config")
    print("=" * 60)

    patched_result = run_single("patched", task_name, model)

    # Compare
    print("\n" + "=" * 60)
    print("  PHASE 4: Compare results")
    print("=" * 60)

    compare_results()


def main():
    parser = argparse.ArgumentParser(
        description="AgentSesh eval harness — test remediation effectiveness",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration (no patch/re-run)")
    parser.add_argument("--compare", action="store_true",
                        help="Compare most recent bad vs patched results")
    parser.add_argument("--config", default="bad",
                        help="Config to use for --once (default: bad)")
    parser.add_argument("--task", default="build_cli",
                        help="Task to run (default: build_cli)")
    parser.add_argument("--model", default="sonnet",
                        help="Model to use (default: sonnet)")

    args = parser.parse_args()

    if args.compare:
        compare_results()
    elif args.once:
        run_single(args.config, args.task, args.model)
    else:
        run_full_loop(args.task, args.model)


if __name__ == "__main__":
    main()
