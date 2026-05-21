from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCENARIOS_ROOT = PROJECT_ROOT / "benchmarks" / "scenarios"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "benchmarks" / "output"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run TSADAI benchmark campaigns one after the other."
    )
    parser.add_argument(
        "--scenarios-root",
        default=str(DEFAULT_SCENARIOS_ROOT),
        help="Directory containing benchmark campaign folders.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
        help="Directory where benchmark outputs are written.",
    )
    parser.add_argument(
        "--campaign",
        action="append",
        default=[],
        help=(
            "Campaign folder name to run. Can be used multiple times. "
            "Defaults to every campaign folder in sorted order."
        ),
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Validate all selected scenarios without starting nodes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the commands that would run, without executing them.",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue with later campaigns when one campaign fails.",
    )
    args = parser.parse_args()

    scenarios_root = Path(args.scenarios_root)
    campaigns = select_campaigns(scenarios_root, args.campaign)
    if not campaigns:
        raise SystemExit(f"ERROR: no campaigns found in {scenarios_root}")

    print("Selected campaigns:", flush=True)
    for campaign in campaigns:
        print(f"  - {campaign.name}", flush=True)

    failures: list[tuple[Path, int]] = []
    started_at = time.perf_counter()
    for campaign in campaigns:
        command = benchmark_command(
            campaign=campaign,
            output_dir=Path(args.output_dir),
            validate_only=args.validate_only,
        )
        print("", flush=True)
        print(f"=== {campaign.name} ===", flush=True)
        print(format_command(command), flush=True)

        if args.dry_run:
            continue

        completed = subprocess.run(command, cwd=PROJECT_ROOT)
        if completed.returncode != 0:
            failures.append((campaign, completed.returncode))
            if not args.continue_on_error:
                break

    elapsed_s = time.perf_counter() - started_at
    if args.dry_run:
        print("", flush=True)
        print("Dry run complete.", flush=True)
        return

    print("", flush=True)
    print(f"Benchmark batch finished in {elapsed_s:.1f}s.", flush=True)
    if failures:
        for campaign, returncode in failures:
            print(
                f"FAILED: {campaign.name} exited with status {returncode}",
                flush=True,
            )
        raise SystemExit(1)


def select_campaigns(scenarios_root: Path, requested: list[str]) -> list[Path]:
    if not scenarios_root.exists():
        raise SystemExit(f"ERROR: scenarios root does not exist: {scenarios_root}")

    if requested:
        campaigns = []
        for name in requested:
            campaign = scenarios_root / name
            if not campaign.is_dir():
                raise SystemExit(f"ERROR: campaign not found: {campaign}")
            campaigns.append(campaign)
        return campaigns

    return sorted(path for path in scenarios_root.iterdir() if path.is_dir())


def benchmark_command(
    campaign: Path,
    output_dir: Path,
    validate_only: bool,
) -> list[str]:
    command = [
        sys.executable,
        "-m",
        "benchmarks.run_benchmark",
        "--scenario",
        str(campaign),
        "--output-dir",
        str(output_dir),
    ]
    if validate_only:
        command.append("--validate-only")
    return command


def format_command(command: list[str]) -> str:
    return " ".join(quote_arg(part) for part in command)


def quote_arg(value: str) -> str:
    if not value or any(char.isspace() for char in value):
        return f'"{value}"'
    return value


if __name__ == "__main__":
    main()
