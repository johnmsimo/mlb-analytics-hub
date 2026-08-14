#!/usr/bin/env python3
"""Run a command with bounded retries for Fly.io machine lease conflicts only."""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from typing import TextIO


def is_retryable_lease_conflict(output: str) -> bool:
    """Return True only for Fly's known transient machine-lease collision."""
    normalized = output.lower()
    return (
        "failed to acquire lease" in normalized
        and "lease currently held" in normalized
    )


def run_with_lease_retry(
    command: Sequence[str],
    *,
    attempts: int = 4,
    base_delay_seconds: float = 15,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    sleeper: Callable[[float], None] = time.sleep,
    stream: TextIO = sys.stdout,
) -> int:
    """Run command, retrying only retryable lease conflicts with linear backoff."""
    if not command:
        raise ValueError("command must not be empty")
    if attempts < 1:
        raise ValueError("attempts must be at least 1")
    if base_delay_seconds < 0:
        raise ValueError("base_delay_seconds must be non-negative")

    for attempt in range(1, attempts + 1):
        try:
            completed = runner(
                list(command),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                check=False,
            )
        except OSError as exc:
            stream.write(f"Unable to execute {command[0]}: {exc}\n")
            stream.flush()
            return 127

        output = completed.stdout or ""
        if output:
            stream.write(output)
            if not output.endswith("\n"):
                stream.write("\n")
            stream.flush()

        if completed.returncode == 0:
            return 0

        if not is_retryable_lease_conflict(output) or attempt == attempts:
            return completed.returncode

        delay = base_delay_seconds * attempt
        stream.write(
            "Transient Fly.io machine lease conflict; "
            f"retrying in {delay:g}s ({attempt + 1}/{attempts}).\n"
        )
        stream.flush()
        sleeper(delay)

    raise AssertionError("retry loop exhausted without returning")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Retry a command only when Fly.io reports a machine lease conflict."
    )
    parser.add_argument("--attempts", type=int, default=4)
    parser.add_argument("--base-delay", type=float, default=15)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)

    if args.command and args.command[0] == "--":
        args.command = args.command[1:]
    if not args.command:
        parser.error("a command is required after --")
    if args.attempts < 1:
        parser.error("--attempts must be at least 1")
    if args.base_delay < 0:
        parser.error("--base-delay must be non-negative")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run_with_lease_retry(
        args.command,
        attempts=args.attempts,
        base_delay_seconds=args.base_delay,
    )


if __name__ == "__main__":
    raise SystemExit(main())
