"""Profiling plugin for docker integration tests.

Activated by ``HERMES_DOCKER_TEST_PROFILE=1``. Instruments every
``subprocess.run`` call whose argv starts with ``docker`` to measure
wall-clock time, and collects per-test breakdowns so we can see exactly
which docker operations dominate the slow CI runs.

Outputs:
  - JSON report at ``$HERMES_DOCKER_PROFILE_OUT`` (default:
    ``docker-test-profile.json`` in the repo root).
  - Console summary on stderr at session end.

The plugin is a no-op when the env var is not set — zero overhead on
normal test runs.
"""
from __future__ import annotations

import json
import os
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pytest

_ACTIVE = bool(os.environ.get("HERMES_DOCKER_TEST_PROFILE"))

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class DockerCall:
    """One instrumented docker subprocess call."""

    argv: list[str]
    duration_s: float
    returncode: int
    timestamp: float  # monotonic


@dataclass
class TestProfile:
    """Per-test accumulation of docker call timings."""

    name: str
    calls: list[DockerCall] = field(default_factory=list)

    @property
    def total_docker_s(self) -> float:
        return sum(c.duration_s for c in self.calls)

    @property
    def call_count(self) -> int:
        return len(self.calls)

    def by_subcommand(self) -> dict[str, list[DockerCall]]:
        """Group calls by the first docker subcommand (run, exec, restart, ...)."""
        groups: dict[str, list[DockerCall]] = defaultdict(list)
        for c in self.calls:
            # argv[0] = "docker", argv[1] = subcommand
            sub = c.argv[1] if len(c.argv) > 1 else "?"
            groups[sub].append(c)
        return groups


# ---------------------------------------------------------------------------
# Session-level collector
# ---------------------------------------------------------------------------


class ProfileCollector:
    """Singleton accumulator shared across the pytest session."""

    def __init__(self) -> None:
        self.tests: dict[str, TestProfile] = {}
        self.current: Optional[TestProfile] = None
        self._original_run: Any = None
        self._patched = False

    def start_test(self, name: str) -> None:
        self.current = TestProfile(name=name)
        self.tests[name] = self.current

    def end_test(self) -> None:
        self.current = None

    def record(self, call: DockerCall) -> None:
        if self.current is not None:
            self.current.calls.append(call)

    def install_patch(self) -> None:
        """Monkey-patch subprocess.run to capture docker call timings."""
        if self._patched:
            return
        import subprocess

        self._original_run = subprocess.run
        collector = self

        def timed_run(*args: Any, **kwargs: Any) -> Any:
            argv = list(args[0]) if args and args[0] else kwargs.get("args", [])
            is_docker = bool(argv) and argv[0] == "docker"
            if not is_docker:
                return collector._original_run(*args, **kwargs)
            t0 = time.monotonic()
            result = collector._original_run(*args, **kwargs)
            elapsed = time.monotonic() - t0
            rc = getattr(result, "returncode", -1)
            call = DockerCall(
                argv=[str(a) for a in argv],
                duration_s=round(elapsed, 4),
                returncode=rc,
                timestamp=t0,
            )
            collector.record(call)
            return result

        subprocess.run = timed_run
        self._patched = True

    def uninstall_patch(self) -> None:
        if not self._patched or self._original_run is None:
            return
        import subprocess

        subprocess.run = self._original_run
        self._patched = False

    def write_report(self, out_path: Path) -> None:
        """Write the JSON report."""
        report: dict[str, Any] = {
            "tests": [],
            "summary": {},
        }
        all_docker_time = 0.0
        all_call_count = 0
        subcmd_totals: dict[str, float] = defaultdict(float)
        subcmd_counts: dict[str, int] = defaultdict(int)

        for name, tp in sorted(
            self.tests.items(), key=lambda x: x[1].total_docker_s, reverse=True
        ):
            by_sub = tp.by_subcommand()
            test_entry: dict[str, Any] = {
                "name": name,
                "total_docker_s": round(tp.total_docker_s, 3),
                "call_count": tp.call_count,
                "by_subcommand": {
                    sub: {
                        "count": len(calls),
                        "total_s": round(sum(c.duration_s for c in calls), 3),
                        "avg_s": round(
                            sum(c.duration_s for c in calls) / len(calls), 3
                        )
                        if calls
                        else 0,
                        "max_s": round(max(c.duration_s for c in calls), 3)
                        if calls
                        else 0,
                    }
                    for sub, calls in sorted(
                        by_sub.items(),
                        key=lambda x: sum(c.duration_s for c in x[1]),
                        reverse=True,
                    )
                },
                "calls": [
                    {
                        "argv": " ".join(c.argv[:8]),  # truncate long argv
                        "duration_s": c.duration_s,
                        "returncode": c.returncode,
                    }
                    for c in sorted(tp.calls, key=lambda x: x.duration_s, reverse=True)
                ],
            }
            report["tests"].append(test_entry)
            all_docker_time += tp.total_docker_s
            all_call_count += tp.call_count
            for sub, calls in by_sub.items():
                subcmd_totals[sub] += sum(c.duration_s for c in calls)
                subcmd_counts[sub] += len(calls)

        report["summary"] = {
            "total_tests": len(self.tests),
            "total_docker_s": round(all_docker_time, 3),
            "total_calls": all_call_count,
            "by_subcommand": {
                sub: {
                    "count": subcmd_counts[sub],
                    "total_s": round(subcmd_totals[sub], 3),
                    "avg_s": round(subcmd_totals[sub] / subcmd_counts[sub], 3)
                    if subcmd_counts[sub]
                    else 0,
                }
                for sub in sorted(
                    subcmd_totals, key=lambda s: subcmd_totals[s], reverse=True
                )
            },
        }

        out_path.write_text(json.dumps(report, indent=2) + "\n")

    def print_summary(self) -> None:
        """Print a human-readable summary to stderr."""
        if not self.tests:
            print("\n[docker-profile] No tests profiled.", file=sys.stderr)
            return

        print("\n" + "=" * 72, file=sys.stderr)
        print("[docker-profile] Docker operation timing breakdown", file=sys.stderr)
        print("=" * 72, file=sys.stderr)

        # Summary by subcommand
        subcmd_totals: dict[str, float] = defaultdict(float)
        subcmd_counts: dict[str, int] = defaultdict(int)
        for tp in self.tests.values():
            for sub, calls in tp.by_subcommand().items():
                subcmd_totals[sub] += sum(c.duration_s for c in calls)
                subcmd_counts[sub] += len(calls)

        total = sum(subcmd_totals.values())
        print(
            f"\n  Total docker time: {total:.1f}s across {sum(subcmd_counts.values())} calls\n",
            file=sys.stderr,
        )
        print(
            f"  {'Subcommand':<15} {'Calls':>8} {'Total':>10} {'Avg':>8} {'%':>6}",
            file=sys.stderr,
        )
        print(
            f"  {'─' * 15} {'─' * 8} {'─' * 10} {'─' * 8} {'─' * 6}",
            file=sys.stderr,
        )
        for sub in sorted(subcmd_totals, key=lambda s: subcmd_totals[s], reverse=True):
            t = subcmd_totals[sub]
            n = subcmd_counts[sub]
            pct = (t / total * 100) if total else 0
            print(
                f"  {sub:<15} {n:>8} {t:>9.1f}s {t / n:>7.2f}s {pct:>5.1f}%",
                file=sys.stderr,
            )

        # Top 10 slowest tests
        print(
            f"\n  Top 10 slowest tests (by docker operation time):\n",
            file=sys.stderr,
        )
        sorted_tests = sorted(
            self.tests.values(), key=lambda t: t.total_docker_s, reverse=True
        )
        for i, tp in enumerate(sorted_tests[:10], 1):
            print(
                f"  {i:>2}. {tp.total_docker_s:>6.1f}s  {tp.call_count:>3} calls  {tp.name}",
                file=sys.stderr,
            )
            by_sub = tp.by_subcommand()
            for sub, calls in sorted(
                by_sub.items(),
                key=lambda x: sum(c.duration_s for c in x[1]),
                reverse=True,
            ):
                t = sum(c.duration_s for c in calls)
                if t < 0.1:
                    continue
                print(
                    f"       {sub:<13} {t:>5.1f}s ({len(calls)} calls)",
                    file=sys.stderr,
                )

        # Slowest individual calls
        all_calls: list[tuple[str, DockerCall]] = []
        for tp in self.tests.values():
            for c in tp.calls:
                all_calls.append((tp.name, c))
        all_calls.sort(key=lambda x: x[1].duration_s, reverse=True)
        if all_calls:
            print(
                f"\n  Top 10 slowest individual docker calls:\n",
                file=sys.stderr,
            )
            for i, (test_name, c) in enumerate(all_calls[:10], 1):
                argv_short = " ".join(c.argv[:6])
                if len(c.argv) > 6:
                    argv_short += " ..."
                print(
                    f"  {i:>2}. {c.duration_s:>6.1f}s  {argv_short}",
                    file=sys.stderr,
                )
                print(f"      test: {test_name}", file=sys.stderr)

        print("\n" + "=" * 72, file=sys.stderr)


# ---------------------------------------------------------------------------
# Plugin hooks
# ---------------------------------------------------------------------------

_collector: Optional[ProfileCollector] = None


def _get_collector() -> ProfileCollector:
    global _collector
    if _collector is None:
        _collector = ProfileCollector()
    return _collector


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    """Wrap each test call to track per-test docker operations."""
    if not _ACTIVE:
        yield
        return
    collector = _get_collector()
    collector.start_test(item.nodeid)
    yield
    collector.end_test()


def pytest_sessionstart(session):
    """Install the subprocess.run patch at session start."""
    if not _ACTIVE:
        return
    collector = _get_collector()
    collector.install_patch()


def pytest_sessionfinish(session, exitstatus):
    """Write the report and print the summary at session end."""
    if not _ACTIVE:
        return
    collector = _get_collector()
    collector.uninstall_patch()

    out = os.environ.get(
        "HERMES_DOCKER_PROFILE_OUT",
        str(Path.cwd() / "docker-test-profile.json"),
    )
    out_path = Path(out)
    collector.write_report(out_path)
    collector.print_summary()
    print(f"\n[docker-profile] Report written to {out_path}", file=sys.stderr)
