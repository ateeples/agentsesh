#!/usr/bin/env python3
"""AgentSesh eval harness — controlled feedback loop for remediation testing.

Uses MiniMax M2.5 as the test agent. MiniMax describes tool usage step-by-step,
we parse that into a Claude Code-compatible transcript, and grade with sesh.

The loop:
  1. Call MiniMax with bad CLAUDE.md as system prompt
  2. Parse response into simulated tool calls
  3. Generate Claude Code .jsonl transcript
  4. Ingest and grade with sesh
  5. Generate patch with sesh fix --patch
  6. Re-run MiniMax with patched CLAUDE.md
  7. Compare grades

Usage:
  python3 eval/run_eval.py                    # Full loop: bad → patch → fixed → compare
  python3 eval/run_eval.py --once             # Single run with bad config
  python3 eval/run_eval.py --once --config bad
  python3 eval/run_eval.py --compare          # Compare most recent results
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).parent
RESULTS_DIR = EVAL_DIR / "results"
CONFIGS_DIR = EVAL_DIR / "configs"
TASKS_DIR = EVAL_DIR / "tasks"
TRANSCRIPTS_DIR = EVAL_DIR / "transcripts"

# MiniMax harness config — set MINIMAX_API_KEY env var or point HARNESS_CONFIG to your config.json
HARNESS_CONFIG = Path(os.environ.get("HARNESS_CONFIG", Path.home() / ".config" / "minimax-harness" / "config.json"))

_AGENT_SYSTEM_TEMPLATE = (
    'You are a coding agent that builds software by using tools. You have access to these tools:\n\n'
    '- **Read**: Read a file. Input: {{"file_path": "/path/to/file"}}\n'
    '- **Edit**: Edit a file (replace text). Input: {{"file_path": "/path", "old_string": "...", "new_string": "..."}}\n'
    '- **Write**: Create or overwrite a file. Input: {{"file_path": "/path", "content": "..."}}\n'
    '- **Bash**: Run a shell command. Input: {{"command": "..."}}\n'
    '- **Grep**: Search file contents. Input: {{"pattern": "...", "path": "/dir"}}\n'
    '- **Glob**: Find files by pattern. Input: {{"pattern": "**/*.py", "path": "/dir"}}\n\n'
    '## Your Instructions (CLAUDE.md)\n\n'
    '{claude_md}\n\n'
    '## IMPORTANT: Output Format\n\n'
    'You must describe EVERY action you take using this exact format. Each tool use must be a separate block:\n\n'
    '```tool\n'
    'TOOL: <tool_name>\n'
    'INPUT: <json object>\n'
    'RESULT: <what the tool would return — simulate realistic output>\n'
    'ERROR: <true or false>\n'
    '```\n\n'
    'Describe your thinking between tool blocks, but every file read, edit, write, or command MUST be a tool block.\n\n'
    'CRITICAL RULES:\n'
    '1. Each tool use MUST be its own separate ```tool block. NEVER combine multiple actions in one block.\n'
    '2. Work through the task step by step — one tool call at a time, like a real agent.\n'
    '3. Follow your CLAUDE.md instructions when choosing which tools to use.\n'
    '4. Be realistic — if your CLAUDE.md says to use bash for file operations, use Bash tool with commands like cat, grep, find, sed.\n'
    '5. If your CLAUDE.md does NOT say to read before editing, skip the Read and go straight to Write/Edit.\n'
    '6. Show 15-25 separate tool blocks for a complete implementation.\n'
    '7. Include some realistic errors (typos, wrong paths) — real agents make mistakes.\n'
    '8. Each INPUT must be valid JSON on a single line.\n\n'
    'Example of correct formatting (each action is its own block):\n\n'
    '```tool\n'
    'TOOL: Bash\n'
    'INPUT: {{"command": "cat existing_file.py"}}\n'
    'RESULT: # contents of file...\n'
    'ERROR: false\n'
    '```\n\n'
    'Then your thinking about what to do next...\n\n'
    '```tool\n'
    'TOOL: Write\n'
    'INPUT: {{"file_path": "new_file.py", "content": "print(\'hello\')"}}\n'
    'RESULT: File written successfully\n'
    'ERROR: false\n'
    '```\n'
)


def load_minimax_config() -> dict:
    """Load MiniMax API config."""
    if HARNESS_CONFIG.exists():
        with open(HARNESS_CONFIG) as f:
            config = json.load(f)
    else:
        config = {}

    # Env var override
    env_key = os.environ.get("MINIMAX_API_KEY")
    if env_key:
        config["api_key"] = env_key

    if not config.get("api_key"):
        print("Error: No MiniMax API key. Set MINIMAX_API_KEY or configure minimax-harness.", file=sys.stderr)
        sys.exit(1)

    return config


def call_minimax(prompt: str, system_prompt: str, config: dict) -> dict:
    """Call MiniMax API and return response."""
    api_key = config["api_key"]
    base_url = config.get("base_url", "https://api.minimax.io/v1")
    model = config.get("model", "MiniMax-M2.5-highspeed")

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": prompt},
    ]

    body = {
        "model": model,
        "messages": messages,
        "max_tokens": 16384,
        "temperature": 0.3,
        "stream": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    url = f"{base_url.rstrip('/')}/chat/completions"
    data = json.dumps(body).encode("utf-8")

    start = time.monotonic()
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=300) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8", errors="replace") if e.fp else str(e)
        latency = int((time.monotonic() - start) * 1000)
        return {"content": "", "success": False, "error": f"HTTP {e.code}: {error_body[:500]}",
                "latency_ms": latency, "prompt_tokens": 0, "completion_tokens": 0}
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        return {"content": "", "success": False, "error": str(e)[:500],
                "latency_ms": latency, "prompt_tokens": 0, "completion_tokens": 0}

    latency = int((time.monotonic() - start) * 1000)
    content = ""
    if result.get("choices"):
        content = result["choices"][0].get("message", {}).get("content", "")

    # Strip think tags
    content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()

    usage = result.get("usage", {})
    return {
        "content": content,
        "success": True,
        "error": None,
        "latency_ms": latency,
        "prompt_tokens": usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
    }


def parse_tool_blocks(response: str) -> list[dict]:
    """Parse tool blocks from MiniMax response into structured tool calls."""
    tool_calls = []

    # Match ```tool ... ``` blocks
    pattern = re.compile(
        r"```tool\s*\n(.*?)```",
        re.DOTALL,
    )

    for match in pattern.finditer(response):
        block = match.group(1).strip()

        tool_name = ""
        input_lines = []
        result_text = ""
        is_error = False
        current_field = None

        for line in block.split("\n"):
            stripped = line.strip()
            if stripped.startswith("TOOL:"):
                tool_name = stripped[5:].strip()
                current_field = "tool"
            elif stripped.startswith("INPUT:"):
                input_lines = [stripped[6:].strip()]
                current_field = "input"
            elif stripped.startswith("RESULT:"):
                result_text = stripped[7:].strip()
                current_field = "result"
            elif stripped.startswith("ERROR:"):
                is_error = stripped[6:].strip().lower() == "true"
                current_field = "error"
            elif current_field == "input":
                input_lines.append(line)
            elif current_field == "result":
                result_text += "\n" + line

        # Parse input JSON
        input_str = " ".join(l.strip() for l in input_lines)
        try:
            input_data = json.loads(input_str)
        except json.JSONDecodeError:
            # Try to extract something useful
            input_data = {"raw": input_str[:200]}

        if tool_name:
            tool_calls.append({
                "name": tool_name,
                "input": input_data,
                "output": result_text.strip(),
                "is_error": is_error,
            })

    return tool_calls


def generate_transcript(
    tool_calls: list[dict],
    task_prompt: str,
    model: str = "MiniMax-M2.5-highspeed",
) -> str:
    """Generate a Claude Code-compatible .jsonl transcript from parsed tool calls.

    This creates a synthetic transcript that AgentSesh's ClaudeCodeParser can parse.
    """
    lines = []
    base_time = datetime.now(timezone.utc)

    # User message with the task
    user_msg = {
        "type": "user",
        "message": {
            "role": "user",
            "content": task_prompt,
        },
        "timestamp": base_time.isoformat(),
    }
    lines.append(json.dumps(user_msg))

    # For each tool call, generate an assistant message with tool_use
    # followed by a user message with tool_result
    for i, tc in enumerate(tool_calls):
        tool_id = f"toolu_{uuid.uuid4().hex[:24]}"
        ts = (base_time.replace(second=i * 3 % 60, minute=i * 3 // 60)).isoformat()

        # Assistant message with tool_use
        assistant_msg = {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "model": model,
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": tc["name"],
                        "input": tc["input"],
                    }
                ],
            },
            "timestamp": ts,
        }
        lines.append(json.dumps(assistant_msg))

        # User message with tool_result
        result_content = tc.get("output", "OK")
        user_result = {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "is_error": tc.get("is_error", False),
                        "content": result_content,
                    }
                ],
            },
            "timestamp": ts,
        }
        lines.append(json.dumps(user_result))

    return "\n".join(lines) + "\n"


def ingest_and_grade(transcript_path: Path) -> dict:
    """Ingest a transcript and return grade info."""
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
        ["python3", "-m", "sesh", "--db", str(sesh_dir / "sesh.db"), "log", str(transcript_path)],
        cwd=str(sesh_root),
        capture_output=True,
        text=True,
    )
    print(f"  Ingest: {log_result.stdout.strip()}")
    if log_result.stderr.strip():
        print(f"  Ingest stderr: {log_result.stderr.strip()}")

    # Get the most recent session's grade
    reflect_result = subprocess.run(
        ["python3", "-m", "sesh", "--db", str(sesh_dir / "sesh.db"), "reflect", "--json"],
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
            "grade_notes": data.get("session", {}).get("grade_notes", ""),
        }
    except (json.JSONDecodeError, KeyError) as e:
        print(f"  Warning: Could not parse grade data: {e}", file=sys.stderr)
        if reflect_result.stderr:
            print(f"  reflect stderr: {reflect_result.stderr[:300]}", file=sys.stderr)
        return {"grade": "?", "score": 0, "patterns": []}


def generate_patch() -> str:
    """Run sesh fix --patch and return the CLAUDE.md patch."""
    sesh_root = EVAL_DIR.parent
    sesh_dir = sesh_root / ".sesh"

    result = subprocess.run(
        ["python3", "-m", "sesh", "--db", str(sesh_dir / "sesh.db"), "fix", "--patch"],
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

    eliminated = set(bad.get('patterns', [])) - set(patched.get('patterns', []))
    if eliminated:
        print(f"\n  Patterns ELIMINATED by patch: {', '.join(eliminated)}")

    remaining = set(bad.get('patterns', [])) & set(patched.get('patterns', []))
    if remaining:
        print(f"  Patterns REMAINING after patch: {', '.join(remaining)}")

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


def run_single(config_name: str, task_name: str) -> dict:
    """Run a single eval iteration with MiniMax."""
    run_id = datetime.now().strftime("%Y%m%d-%H%M%S")

    print(f"\n  Config: {config_name}")
    print(f"  Task:   {task_name}")
    print(f"  Run ID: {run_id}")

    # Load configs
    mm_config = load_minimax_config()
    model = mm_config.get("model", "MiniMax-M2.5-highspeed")
    print(f"  Model:  {model}")

    config_path = CONFIGS_DIR / f"{config_name}.md"
    if not config_path.exists():
        print(f"Error: Config {config_path} not found", file=sys.stderr)
        sys.exit(1)
    claude_md = config_path.read_text()

    task_path = TASKS_DIR / f"{task_name}.md"
    if not task_path.exists():
        print(f"Error: Task {task_path} not found", file=sys.stderr)
        sys.exit(1)
    task_prompt = task_path.read_text()

    # Build system prompt with CLAUDE.md injected
    system_prompt = _AGENT_SYSTEM_TEMPLATE.format(claude_md=claude_md)

    # Call MiniMax
    print(f"\n  Calling MiniMax...")
    start = time.time()
    response = call_minimax(task_prompt, system_prompt, mm_config)
    duration = time.time() - start

    if not response["success"]:
        print(f"  Error: {response['error']}")
        return save_result(
            {"run_id": run_id, "model": model, "duration_seconds": round(duration, 1),
             "prompt_tokens": 0, "completion_tokens": 0},
            {"grade": "?", "score": 0, "patterns": []},
            config_name,
        )

    print(f"  Response: {response['completion_tokens']} tokens, {response['latency_ms']}ms")

    # Parse tool blocks
    tool_calls = parse_tool_blocks(response["content"])
    print(f"  Parsed {len(tool_calls)} tool calls from response")

    if len(tool_calls) < 3:
        print(f"  Warning: Only {len(tool_calls)} tool calls parsed. Response may not have followed format.")
        # Show first 500 chars of response for debugging
        print(f"  Response preview: {response['content'][:500]}")

    # Generate transcript
    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    transcript_path = TRANSCRIPTS_DIR / f"{config_name}-{run_id}.jsonl"
    transcript_content = generate_transcript(tool_calls, task_prompt, model=model)
    transcript_path.write_text(transcript_content)
    print(f"  Transcript: {transcript_path.name}")

    # Ingest and grade
    grade_info = ingest_and_grade(transcript_path)
    print(f"  Grade: {grade_info['grade']} (score: {grade_info['score']})")
    print(f"  Patterns: {', '.join(grade_info.get('patterns', [])) or 'none'}")

    run_meta = {
        "run_id": run_id,
        "model": model,
        "duration_seconds": round(duration, 1),
        "prompt_tokens": response["prompt_tokens"],
        "completion_tokens": response["completion_tokens"],
        "parsed_tool_calls": len(tool_calls),
    }

    return save_result(run_meta, grade_info, config_name)


def run_full_loop(task_name: str):
    """Run the full eval loop: bad → patch → patched → compare."""
    print("\n" + "=" * 60)
    print("  PHASE 1: Run with bad config")
    print("=" * 60)

    bad_result = run_single("bad", task_name)

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

    run_single("patched", task_name)

    # Compare
    print("\n" + "=" * 60)
    print("  PHASE 4: Compare results")
    print("=" * 60)

    compare_results()


def main():
    parser = argparse.ArgumentParser(
        description="AgentSesh eval harness — test remediation with MiniMax",
    )
    parser.add_argument("--once", action="store_true",
                        help="Run a single iteration (no patch/re-run)")
    parser.add_argument("--compare", action="store_true",
                        help="Compare most recent bad vs patched results")
    parser.add_argument("--config", default="bad",
                        help="Config to use for --once (default: bad)")
    parser.add_argument("--task", default="build_cli",
                        help="Task to run (default: build_cli)")

    args = parser.parse_args()

    if args.compare:
        compare_results()
    elif args.once:
        run_single(args.config, args.task)
    else:
        run_full_loop(args.task)


if __name__ == "__main__":
    main()
