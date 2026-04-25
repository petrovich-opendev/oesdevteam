"""Audit Gate — hooks into FeatureController pipeline.

Called after verification passes, before git commit.
Blocks task completion if HIGH severity findings are detected.

Usage standalone:
    python -m security.audit_gate /path/to/project
    python -m security.audit_gate /path/to/project --files src/app.py src/db.py

Usage from FeatureController:
    from security.audit_gate import security_gate
    blocked, report = await security_gate(project_dir, files_changed=["src/app.py"])
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from security.security_audit import ScanReport, scan, scan_files

logger = logging.getLogger("oesdevteam.security.gate")


async def security_gate(
    project_dir: str | Path,
    files_changed: list[str] | None = None,
) -> tuple[bool, ScanReport]:
    """Run security scan and return (is_blocked, report).

    Args:
        project_dir: Root directory of the project
        files_changed: If provided, scan only these files (faster).
                       If None, scan entire project directory.

    Returns:
        (blocked: bool, report: ScanReport)
        blocked=True means HIGH severity findings exist → do not proceed
    """
    project_dir = Path(project_dir).resolve()

    if files_changed:
        logger.info("Security gate: scanning %d changed files", len(files_changed))
        report = await scan_files(files_changed, project_dir)
    else:
        logger.info("Security gate: full scan of %s", project_dir)
        report = await scan(project_dir)

    report.save()

    if report.has_blockers:
        logger.warning(
            "SECURITY GATE BLOCKED: %d HIGH findings in %s",
            report.high_count, project_dir,
        )
        for f in report.findings:
            if f.severity == "HIGH":
                logger.warning(
                    "  [%s] %s:%d — %s (%s)",
                    f.tool, f.file, f.line, f.description[:100], f.rule_id,
                )
    else:
        logger.info(
            "Security gate PASSED: %d findings (HIGH: %d, MEDIUM: %d)",
            len(report.findings), report.high_count, report.medium_count,
        )

    return report.has_blockers, report


# === CLI entrypoint ===

async def _main():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <project_dir> [--files file1 file2 ...]")
        sys.exit(1)

    project_dir = sys.argv[1]
    files = None

    if "--files" in sys.argv:
        idx = sys.argv.index("--files")
        files = sys.argv[idx + 1:]

    blocked, report = await security_gate(project_dir, files_changed=files)

    print(f"\n{'=' * 50}")
    print(f"Security Gate: {'BLOCKED' if blocked else 'PASSED'}")
    print(f"Target: {report.target}")
    print(f"Findings: {len(report.findings)} (HIGH: {report.high_count}, MEDIUM: {report.medium_count})")
    if report.errors:
        print(f"Errors:")
        for e in report.errors:
            print(f"  - {e}")

    sys.exit(2 if blocked else 0)


if __name__ == "__main__":
    asyncio.run(_main())
