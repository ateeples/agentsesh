#!/usr/bin/env python3
"""Prompt-phrasing eval — test how review prompt wording affects bug detection.

Same buggy file, same model, different prompt phrasings.
Measures detection rate per prompt variant against known bug manifest.

Usage:
  python3 eval/run_phrasing_eval.py                # Run all variants
  python3 eval/run_phrasing_eval.py --variant 0    # Run single variant
  python3 eval/run_phrasing_eval.py --compare      # Compare saved results
  python3 eval/run_phrasing_eval.py --runs 3       # Multiple runs per variant for statistical significance
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path

EVAL_DIR = Path(__file__).parent
PHRASING_DIR = EVAL_DIR / "tasks" / "phrasing"
RESULTS_DIR = EVAL_DIR / "results" / "phrasing"
HARNESS_CONFIG = Path(os.environ.get("HARNESS_CONFIG", Path.home() / ".config" / "minimax-harness" / "config.json"))

# The prompt variants to test — from casual to surgical
PROMPT_VARIANTS = [
    {
        "name": "casual_read",
        "prompt": "Read this file and let me know what you think.",
    },
    {
        "name": "find_errors",
        "prompt": "Read this file to find errors.",
    },
    {
        "name": "study_for_bugs",
        "prompt": "Study this file carefully for bugs.",
    },
    {
        "name": "code_review",
        "prompt": "Review this code. What issues do you see?",
    },
    {
        "name": "audit_for_bugs",
        "prompt": "Audit this file for bugs. Check every method, every condition, every edge case.",
    },
    {
        "name": "adversarial",
        "prompt": (
            "You are a senior engineer reviewing this code before it ships to production. "
            "This code has bugs — find every single one. For each bug, state the line number, "
            "what's wrong, and what the fix is. Check: logic errors, off-by-one, race conditions, "
            "missing edge cases, incorrect return values, state corruption."
        ),
    },
    {
        "name": "decomposed",
        "prompt": (
            "Analyze this code by checking each of the following, one at a time:\n"
            "1. For every conditional/comparison, verify the operator direction is correct\n"
            "2. For every method, verify the return value matches the docstring contract\n"
            "3. For every state mutation, verify all related state is updated consistently\n"
            "4. For every method claiming thread-safety, verify locks are held\n"
            "5. For every edge case (empty, zero, None), verify the code handles it\n"
            "List every issue you find with line numbers."
        ),
    },
]


def load_minimax_config() -> dict:
    """Load MiniMax API config."""
    if HARNESS_CONFIG.exists():
        with open(HARNESS_CONFIG) as f:
            config = json.load(f)
    else:
        config = {}
    env_key = os.environ.get("MINIMAX_API_KEY")
    if env_key:
        config["api_key"] = env_key
    if not config.get("api_key"):
        print("Error: No MiniMax API key.", file=sys.stderr)
        sys.exit(1)
    return config


def call_minimax(prompt: str, system_prompt: str, config: dict) -> dict:
    """Call MiniMax API."""
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
        "max_tokens": 8192,
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
    except Exception as e:
        latency = int((time.monotonic() - start) * 1000)
        return {"content": "", "success": False, "error": str(e)[:500], "latency_ms": latency}

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


def score_response(response_text: str, bugs: list[dict]) -> dict:
    """Score a response against the bug manifest.

    For each known bug, check if the response mentions it by looking for
    keyword matches and line number references.
    """
    response_lower = response_text.lower()
    detected = []
    missed = []

    for bug in bugs:
        found = False

        # Check for line number mention
        line_mentioned = (
            f"line {bug['line']}" in response_lower
            or f"line:{bug['line']}" in response_lower
            or f"#{bug['line']}" in response_lower
        )

        # Check for keyword matches (need at least 2 keywords to count)
        keyword_hits = sum(1 for kw in bug["keywords"] if kw in response_lower)

        # A bug is "detected" if:
        # - Line number is mentioned AND at least 1 keyword matches, OR
        # - At least 3 keywords match (detected by description without line number)
        if (line_mentioned and keyword_hits >= 1) or keyword_hits >= 3:
            found = True

        if found:
            detected.append(bug["id"])
        else:
            missed.append(bug["id"])

    return {
        "detected": detected,
        "missed": missed,
        "detection_rate": len(detected) / len(bugs) if bugs else 0,
        "detected_count": len(detected),
        "total_bugs": len(bugs),
    }


def run_variant(variant_idx: int, config: dict, buggy_code: str, bugs: list[dict]) -> dict:
    """Run a single prompt variant."""
    variant = PROMPT_VARIANTS[variant_idx]

    print(f"\n  [{variant_idx}] {variant['name']}")
    print(f"      Prompt: {variant['prompt'][:80]}...")

    system_prompt = "You are a code reviewer. Analyze the code provided and report any issues you find."
    user_prompt = f"{variant['prompt']}\n\n```python\n{buggy_code}\n```"

    response = call_minimax(user_prompt, system_prompt, config)

    if not response["success"]:
        print(f"      Error: {response['error']}")
        return {"variant": variant["name"], "error": response["error"]}

    print(f"      Response: {response['completion_tokens']} tokens, {response['latency_ms']}ms")

    # Score
    score = score_response(response["content"], bugs)
    print(f"      Detected: {score['detected_count']}/{score['total_bugs']} bugs ({score['detection_rate']:.0%})")
    print(f"      Found: {score['detected']}")
    print(f"      Missed: {score['missed']}")

    return {
        "variant": variant["name"],
        "prompt": variant["prompt"],
        "detected": score["detected"],
        "missed": score["missed"],
        "detection_rate": score["detection_rate"],
        "detected_count": score["detected_count"],
        "total_bugs": score["total_bugs"],
        "completion_tokens": response["completion_tokens"],
        "latency_ms": response["latency_ms"],
        "response_length": len(response["content"]),
        "response_text": response["content"],
    }


def run_all(runs_per_variant: int = 1):
    """Run all prompt variants."""
    config = load_minimax_config()

    # Load buggy code and bug manifest
    buggy_code = (PHRASING_DIR / "buggy_cache.py").read_text()
    bugs_data = json.loads((PHRASING_DIR / "bugs.json").read_text())
    bugs = bugs_data["bugs"]

    print(f"\n{'='*60}")
    print(f"  PROMPT PHRASING EVAL")
    print(f"  {len(PROMPT_VARIANTS)} variants, {len(bugs)} known bugs, {runs_per_variant} run(s) each")
    print(f"{'='*60}")

    all_results = []

    for run_num in range(runs_per_variant):
        if runs_per_variant > 1:
            print(f"\n--- Run {run_num + 1}/{runs_per_variant} ---")

        for i in range(len(PROMPT_VARIANTS)):
            result = run_variant(i, config, buggy_code, bugs)
            result["run"] = run_num
            all_results.append(result)

    # Save results
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_file = RESULTS_DIR / f"phrasing-{timestamp}.json"
    result_file.write_text(json.dumps(all_results, indent=2))
    print(f"\n  Results saved to {result_file.name}")

    # Show comparison
    print_comparison(all_results)

    return all_results


def run_single(variant_idx: int):
    """Run a single variant."""
    config = load_minimax_config()
    buggy_code = (PHRASING_DIR / "buggy_cache.py").read_text()
    bugs_data = json.loads((PHRASING_DIR / "bugs.json").read_text())
    bugs = bugs_data["bugs"]

    result = run_variant(variant_idx, config, buggy_code, bugs)

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    result_file = RESULTS_DIR / f"single-{PROMPT_VARIANTS[variant_idx]['name']}-{timestamp}.json"
    result_file.write_text(json.dumps(result, indent=2))

    if result.get("response_text"):
        print(f"\n  Full response:\n")
        print(result["response_text"])


def print_comparison(results: list[dict]):
    """Print comparison table of all variants."""
    # Average results by variant if multiple runs
    variant_stats: dict[str, dict] = {}
    for r in results:
        if "error" in r:
            continue
        name = r["variant"]
        if name not in variant_stats:
            variant_stats[name] = {
                "rates": [],
                "counts": [],
                "tokens": [],
                "latencies": [],
                "all_detected": [],
            }
        variant_stats[name]["rates"].append(r["detection_rate"])
        variant_stats[name]["counts"].append(r["detected_count"])
        variant_stats[name]["tokens"].append(r.get("completion_tokens", 0))
        variant_stats[name]["latencies"].append(r.get("latency_ms", 0))
        variant_stats[name]["all_detected"].extend(r.get("detected", []))

    print(f"\n{'='*60}")
    print(f"  COMPARISON")
    print(f"{'='*60}")
    print()
    print(f"  {'Variant':<20s} {'Rate':>6s} {'Found':>6s} {'Tokens':>7s} {'Latency':>8s}")
    print(f"  {'─'*50}")

    sorted_variants = sorted(
        variant_stats.items(),
        key=lambda x: sum(x[1]["rates"]) / len(x[1]["rates"]),
        reverse=True,
    )

    for name, stats in sorted_variants:
        avg_rate = sum(stats["rates"]) / len(stats["rates"])
        avg_count = sum(stats["counts"]) / len(stats["counts"])
        avg_tokens = sum(stats["tokens"]) / len(stats["tokens"])
        avg_latency = sum(stats["latencies"]) / len(stats["latencies"])

        bar = "█" * int(avg_rate * 8)
        print(
            f"  {name:<20s} {avg_rate:>5.0%} {bar:<8s} "
            f"{avg_count:>4.1f}/8 {avg_tokens:>6.0f} {avg_latency:>7.0f}ms"
        )

    # Which bugs are hardest to find?
    print(f"\n  Bug detection frequency (across all variants):")
    bug_freq: dict[int, int] = {}
    total_runs = 0
    for r in results:
        if "error" not in r:
            total_runs += 1
            for bug_id in r.get("detected", []):
                bug_freq[bug_id] = bug_freq.get(bug_id, 0) + 1

    bugs_data = json.loads((PHRASING_DIR / "bugs.json").read_text())
    for bug in bugs_data["bugs"]:
        freq = bug_freq.get(bug["id"], 0)
        pct = freq / total_runs * 100 if total_runs else 0
        bar = "█" * int(pct / 12.5)
        print(f"    Bug {bug['id']}: {bar:<8s} {freq}/{total_runs} ({pct:.0f}%) — {bug['description'][:50]}")

    print()


def compare_saved():
    """Compare most recent saved results."""
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    result_files = sorted(RESULTS_DIR.glob("phrasing-*.json"), reverse=True)
    if not result_files:
        print("No phrasing eval results found. Run eval first.")
        return

    results = json.loads(result_files[0].read_text())
    print(f"  Loading: {result_files[0].name}")
    print_comparison(results)


def main():
    parser = argparse.ArgumentParser(
        description="Prompt-phrasing eval — test review prompt wording on bug detection",
    )
    parser.add_argument("--variant", type=int, help="Run single variant by index (0-6)")
    parser.add_argument("--compare", action="store_true", help="Compare saved results")
    parser.add_argument("--runs", type=int, default=1, help="Runs per variant (default: 1)")
    parser.add_argument("--list", action="store_true", help="List available variants")

    args = parser.parse_args()

    if args.list:
        for i, v in enumerate(PROMPT_VARIANTS):
            print(f"  [{i}] {v['name']}: {v['prompt'][:70]}...")
        return

    if args.compare:
        compare_saved()
    elif args.variant is not None:
        run_single(args.variant)
    else:
        run_all(runs_per_variant=args.runs)


if __name__ == "__main__":
    main()
