#!/usr/bin/env python
"""Run every offline devtools test file in its own interpreter and total the results.

Usage:  python -B -I devtools/run_offline_tests.py [project-root]

Each ``test_shipstation_*.py`` file is launched as a separate process because several
of them read the project root from ``sys.argv[1]`` and rewrite ``sys.argv`` at import
time, so they cannot be collected together with ``unittest discover``.

These are *offline* tests: they parse source files and exercise extracted functions.
They do NOT start Odoo.  Real ORM/security/view behaviour is covered by the Odoo
``TransactionCase`` suite in ``shipstation_connector/tests`` (run with ``odoo-bin
--test-enable --test-tags /shipstation_connector``).
"""
import pathlib
import re
import subprocess
import sys

HERE = pathlib.Path(__file__).resolve().parent
PROJECT = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else HERE.parent

RAN = re.compile(r"^Ran (\d+) tests? in", re.M)
FAILED = re.compile(r"^FAILED \((.*)\)", re.M)
SKIPPED = re.compile(r"skipped=(\d+)")


def argument_for(path):
    """Return the CLI argument list a test file expects (they were written in different styles)."""
    text = path.read_text(encoding="utf-8-sig")
    if "module_dir = sys.argv[1]" in text or "module_dir=sys.argv[1]" in text:
        return [str(PROJECT / "shipstation_connector")]  # expects the addon directory
    if "sys.argv[1]" in text:
        return [str(PROJECT)]  # expects the repository root
    return []  # locates the addon itself via __file__; an extra argv would be read as a test name


def main():
    files = sorted(HERE.glob("test_shipstation_*.py"))
    totals = {"ran": 0, "bad": 0, "skipped": 0, "files_bad": []}
    for path in files:
        proc = subprocess.run(
            [sys.executable, "-B", str(path), *argument_for(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )
        output = proc.stdout + proc.stderr
        ran = RAN.search(output)
        n = int(ran.group(1)) if ran else 0
        failed = FAILED.search(output)
        bad = sum(int(x) for x in re.findall(r"=(\d+)", re.sub(r"skipped=\d+", "", failed.group(1)))) if failed else 0
        skipped = sum(int(x) for x in SKIPPED.findall(failed.group(1))) if failed else 0
        if proc.returncode != 0 and not failed:
            bad = max(bad, 1)  # crashed before unittest could report
        totals["ran"] += n
        totals["bad"] += bad
        totals["skipped"] += skipped
        status = "OK  " if proc.returncode == 0 else "FAIL"
        print(f"{status} {path.name:48s} ran={n:3d} bad={bad} skipped={skipped}")
        if proc.returncode != 0:
            totals["files_bad"].append(path.name)
            print("\n".join("     | " + line for line in output.strip().splitlines()[-25:]))
    print("-" * 78)
    print(f"TOTAL ran={totals['ran']} failed_or_errored={totals['bad']} skipped={totals['skipped']} "
          f"files_failing={len(totals['files_bad'])}/{len(files)}")
    return 1 if totals["files_bad"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
