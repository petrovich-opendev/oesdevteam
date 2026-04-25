"""Security Scanner — Semgrep + Bandit + Go-toolchain runner.

Scans a target directory, parses JSON output, returns structured findings.
Can be used standalone or as part of FeatureController pipeline.

Phases dispatched by language present in the target tree (or in the
``files_changed`` list when called from the pipeline):

- semgrep + bandit on Python
- gosec + govulncheck on each Go module (one ``go.mod`` root per run)
- buf breaking on each protobuf module (one ``buf.yaml`` per run)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

logger = logging.getLogger("oesdevteam.security")

CONFIG_PATH = Path(os.environ.get(
    "SECURITY_CONFIG",
    Path(__file__).parent / "config.yaml",
))


@dataclass
class Finding:
    tool: str
    severity: str
    confidence: str
    file: str
    line: int
    description: str
    rule_id: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ScanReport:
    timestamp: str
    target: str
    findings: list[Finding]
    errors: list[str]

    @property
    def high_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "HIGH")

    @property
    def medium_count(self) -> int:
        return sum(1 for f in self.findings if f.severity == "MEDIUM")

    @property
    def has_blockers(self) -> bool:
        config = _load_config()
        block_on = config.get("scanner", {}).get("block_on", ["HIGH"])
        return any(f.severity in block_on for f in self.findings)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "target": self.target,
            "summary": {
                "total": len(self.findings),
                "high": self.high_count,
                "medium": self.medium_count,
                "has_blockers": self.has_blockers,
            },
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.errors,
        }

    def save(self, output_dir: str | Path | None = None) -> Path:
        config = _load_config()
        if output_dir is None:
            output_dir = Path(config.get("reports", {}).get(
                "output_dir", "security/audit_reports",
            ))
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        ts = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d_%H%M%S")
        target_name = Path(self.target).name or "scan"
        filename = f"{ts}_{target_name}_security.json"
        path = output_dir / filename
        path.write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False))
        logger.info("Report saved: %s", path)
        return path


def _find_binary(name: str) -> str:
    """Find binary in current venv or PATH."""
    venv_bin = Path(sys.executable).parent / name
    if venv_bin.exists():
        return str(venv_bin)
    found = shutil.which(name)
    if found:
        return found
    raise FileNotFoundError(f"{name}: binary not found in venv or PATH")


def _load_config() -> dict:
    if CONFIG_PATH.exists():
        return yaml.safe_load(CONFIG_PATH.read_text()) or {}
    return {}


def _build_exclude_args(config: dict) -> list[str]:
    exclude_paths = config.get("scanner", {}).get("exclude_paths", [])
    args = []
    for p in exclude_paths:
        args.extend(["--exclude", p])
    return args


async def run_semgrep(target_dir: str | Path) -> tuple[list[Finding], list[str]]:
    """Run semgrep scan on target directory. Returns (findings, errors)."""
    config = _load_config()
    semgrep_config = config.get("semgrep", {}).get("config", "auto")
    timeout = config.get("semgrep", {}).get("timeout", 300)
    min_severity = config.get("scanner", {}).get("min_severity", "MEDIUM")

    severity_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2, "CRITICAL": 3, "ERROR": 3}
    min_level = severity_order.get(min_severity, 1)

    exclude_args = _build_exclude_args(config)
    semgrep_bin = config.get("semgrep", {}).get("binary") or _find_binary("semgrep")
    cmd = [
        semgrep_bin, "scan",
        f"--config={semgrep_config}",
        "--json",
        "--quiet",
        *exclude_args,
        str(target_dir),
    ]

    logger.info("Running semgrep on %s", target_dir)
    findings = []
    errors = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if stderr:
            stderr_text = stderr.decode(errors="replace").strip()
            if stderr_text:
                logger.debug("semgrep stderr: %s", stderr_text[:500])

        if stdout:
            data = json.loads(stdout.decode(errors="replace"))
            for result in data.get("results", []):
                severity = result.get("extra", {}).get("severity", "MEDIUM").upper()
                if severity_order.get(severity, 1) >= min_level:
                    findings.append(Finding(
                        tool="semgrep",
                        severity=severity,
                        confidence="HIGH",
                        file=result.get("path", ""),
                        line=result.get("start", {}).get("line", 0),
                        description=result.get("extra", {}).get("message", ""),
                        rule_id=result.get("check_id", ""),
                    ))

            for err in data.get("errors", []):
                errors.append(f"semgrep: {err.get('message', str(err))}")

    except asyncio.TimeoutError:
        errors.append(f"semgrep: timeout after {timeout}s")
        logger.error("semgrep timeout after %ds", timeout)
    except json.JSONDecodeError as e:
        errors.append(f"semgrep: failed to parse output — {e}")
    except FileNotFoundError:
        errors.append("semgrep: binary not found — install with 'pip install semgrep'")
    except Exception as e:
        errors.append(f"semgrep: {e}")

    logger.info("semgrep: %d findings", len(findings))
    return findings, errors


async def run_bandit(target_dir: str | Path) -> tuple[list[Finding], list[str]]:
    """Run bandit scan on target directory. Returns (findings, errors)."""
    config = _load_config()
    timeout = config.get("bandit", {}).get("timeout", 120)
    min_confidence = config.get("bandit", {}).get("min_confidence", "MEDIUM")
    min_severity = config.get("scanner", {}).get("min_severity", "MEDIUM")

    severity_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    min_sev_level = severity_order.get(min_severity, 1)
    min_conf_level = severity_order.get(min_confidence, 1)

    exclude_paths = config.get("scanner", {}).get("exclude_paths", [])
    exclude_arg = ",".join(exclude_paths) if exclude_paths else ""

    bandit_bin = config.get("bandit", {}).get("binary") or _find_binary("bandit")
    cmd = [
        bandit_bin, "-r", str(target_dir),
        "-f", "json",
        "--quiet",
    ]
    if exclude_arg:
        cmd.extend(["--exclude", exclude_arg])

    logger.info("Running bandit on %s", target_dir)
    findings = []
    errors = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        if stdout:
            data = json.loads(stdout.decode(errors="replace"))
            for result in data.get("results", []):
                severity = result.get("issue_severity", "MEDIUM").upper()
                confidence = result.get("issue_confidence", "MEDIUM").upper()

                if (severity_order.get(severity, 1) >= min_sev_level
                        and severity_order.get(confidence, 1) >= min_conf_level):
                    findings.append(Finding(
                        tool="bandit",
                        severity=severity,
                        confidence=confidence,
                        file=result.get("filename", ""),
                        line=result.get("line_number", 0),
                        description=result.get("issue_text", ""),
                        rule_id=result.get("test_id", ""),
                    ))

            for err in data.get("errors", []):
                errors.append(f"bandit: {err}")

    except asyncio.TimeoutError:
        errors.append(f"bandit: timeout after {timeout}s")
        logger.error("bandit timeout after %ds", timeout)
    except json.JSONDecodeError as e:
        errors.append(f"bandit: failed to parse output — {e}")
    except FileNotFoundError:
        errors.append("bandit: binary not found — install with 'pip install bandit'")
    except Exception as e:
        errors.append(f"bandit: {e}")

    logger.info("bandit: %d findings", len(findings))
    return findings, errors


def _find_go_tool(name: str, config_section: dict) -> str | None:
    """Locate a Go-installed binary. Order: config override → PATH → $(go env GOPATH)/bin."""
    explicit = config_section.get("binary")
    if explicit:
        return explicit
    try:
        return _find_binary(name)
    except FileNotFoundError:
        pass
    import subprocess
    try:
        gopath = subprocess.check_output(
            ["go", "env", "GOPATH"], text=True, timeout=5,
        ).strip()
        candidate = Path(gopath) / "bin" / name
        if candidate.exists():
            return str(candidate)
    except Exception:
        pass
    return None


def _find_go_mod_root(start: Path) -> Path | None:
    """Walk upwards from ``start`` until a ``go.mod`` is found.

    Go tools (gosec, govulncheck, buf) operate on a module — the ``./...``
    expansion silently does nothing when CWD is below the module root and
    the file is in a sibling tree. Without this walk the scan reports
    "0 findings" on a real codebase, which is worse than a hard error.
    """
    start = start if start.is_dir() else start.parent
    for candidate in (start, *start.parents):
        if (candidate / "go.mod").exists():
            return candidate
    return None


async def run_gosec(target_dir: str | Path) -> tuple[list[Finding], list[str]]:
    """Run gosec scan on a Go project directory. Returns (findings, errors).

    Always invoked from the nearest ``go.mod`` root (gosec's package-loader
    requires it). The target may be a subdirectory; we walk up to find the
    module root, then pass ``./...`` from there.
    """
    config = _load_config()
    timeout = config.get("gosec", {}).get("timeout", 120)
    min_severity = config.get("scanner", {}).get("min_severity", "MEDIUM")

    severity_map = {"LOW": "LOW", "MEDIUM": "MEDIUM", "HIGH": "HIGH"}
    severity_order = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    min_level = severity_order.get(min_severity, 1)

    gosec_bin = _find_go_tool("gosec", config.get("gosec", {}))
    if not gosec_bin:
        return [], ["gosec: binary not found — install with 'go install github.com/securego/gosec/v2/cmd/gosec@latest'"]

    target_dir = Path(target_dir)
    module_root = _find_go_mod_root(target_dir)
    if module_root is None:
        return [], [f"gosec: no go.mod found at or above {target_dir}"]

    cmd = [gosec_bin, "-fmt", "json", "-quiet", "./..."]

    logger.info("Running gosec on %s (module root %s)", target_dir, module_root)
    findings: list[Finding] = []
    errors: list[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(module_root),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        raw = stdout.decode(errors="replace")
        if not raw.strip():
            return findings, errors

        data = json.loads(raw)
        for issue in data.get("Issues", []):
            severity = severity_map.get(issue.get("severity", "MEDIUM").upper(), "MEDIUM")
            if severity_order.get(severity, 1) >= min_level:
                findings.append(Finding(
                    tool="gosec",
                    severity=severity,
                    confidence=issue.get("confidence", "MEDIUM").upper(),
                    file=issue.get("file", ""),
                    line=int(issue.get("line", 0) or 0),
                    description=issue.get("details", ""),
                    rule_id=issue.get("rule_id", ""),
                ))

    except asyncio.TimeoutError:
        errors.append(f"gosec: timeout after {timeout}s")
        logger.error("gosec timeout after %ds", timeout)
    except json.JSONDecodeError as e:
        errors.append(f"gosec: failed to parse output — {e}")
    except FileNotFoundError:
        errors.append("gosec: binary not found")
    except Exception as e:
        errors.append(f"gosec: {e}")

    logger.info("gosec: %d findings", len(findings))
    return findings, errors


async def run_govulncheck(target_dir: str | Path) -> tuple[list[Finding], list[str]]:
    """Run govulncheck against a Go module. Returns (findings, errors).

    govulncheck cross-references the call graph against the Go vulnerability
    database (vuln.go.dev). Every reachable known CVE is reported as HIGH —
    these are exploitable in this binary, not theoretical.

    Output format: streaming JSON-lines with ``finding`` records that carry
    a trace of frames pointing at the call site.
    """
    config = _load_config()
    timeout = config.get("govulncheck", {}).get("timeout", 180)

    bin_path = _find_go_tool("govulncheck", config.get("govulncheck", {}))
    if not bin_path:
        return [], ["govulncheck: binary not found — install with 'go install golang.org/x/vuln/cmd/govulncheck@latest'"]

    target_dir = Path(target_dir)
    module_root = _find_go_mod_root(target_dir)
    if module_root is None:
        return [], [f"govulncheck: no go.mod found at or above {target_dir}"]

    cmd = [bin_path, "-format", "json", "./..."]

    logger.info("Running govulncheck on %s (module root %s)", target_dir, module_root)
    findings: list[Finding] = []
    errors: list[str] = []

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(module_root),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

        raw = stdout.decode(errors="replace")
        # govulncheck emits one JSON object per line.
        seen_osv: dict[str, Finding] = {}
        for line in raw.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            finding = msg.get("finding")
            if not finding:
                continue
            osv = finding.get("osv", "")
            # We only want findings with a concrete call-site trace
            # (advisories without traces are "imported but not called" —
            # informational, not exploitable).
            trace = finding.get("trace") or []
            if not osv or not trace:
                continue
            top = trace[0] or {}
            file_path = top.get("position", {}).get("filename", "") or ""
            line_no = int(top.get("position", {}).get("line", 0) or 0)
            # Dedupe: one Finding per OSV id, keep first call-site.
            if osv in seen_osv:
                continue
            seen_osv[osv] = Finding(
                tool="govulncheck",
                severity="HIGH",
                confidence="HIGH",
                file=file_path,
                line=line_no,
                description=f"reachable vulnerability {osv} via {top.get('function', '')}",
                rule_id=osv,
            )
        findings.extend(seen_osv.values())

        if stderr:
            stderr_text = stderr.decode(errors="replace").strip()
            if stderr_text and proc.returncode not in (0, 3):
                # Exit 3 = vulnerabilities found (expected); other non-zero
                # codes mean the scan itself failed.
                errors.append(f"govulncheck: {stderr_text[:500]}")

    except asyncio.TimeoutError:
        errors.append(f"govulncheck: timeout after {timeout}s")
        logger.error("govulncheck timeout after %ds", timeout)
    except FileNotFoundError:
        errors.append("govulncheck: binary not found")
    except Exception as e:
        errors.append(f"govulncheck: {e}")

    logger.info("govulncheck: %d findings", len(findings))
    return findings, errors


async def run_buf_breaking(
    target_dir: str | Path,
    base_ref: str = "main",
) -> tuple[list[Finding], list[str]]:
    """Run ``buf breaking`` against ``base_ref`` for every buf module under target.

    Protobuf is a public contract; an unreviewed breaking change silently
    wedges every downstream consumer at deploy time. Run this whenever
    ``.proto`` files appear in the diff.
    """
    config = _load_config()
    timeout = config.get("buf", {}).get("timeout", 120)
    base_ref = config.get("buf", {}).get("base_ref", base_ref)

    bin_path = _find_go_tool("buf", config.get("buf", {}))
    if not bin_path:
        return [], ["buf: binary not found — install per https://buf.build/docs/installation"]

    target_dir = Path(target_dir).resolve()
    # buf operates per-module: any directory containing buf.yaml is a module.
    module_dirs: list[Path] = []
    for buf_yaml in target_dir.rglob("buf.yaml"):
        module_dirs.append(buf_yaml.parent)
    if not module_dirs:
        return [], []

    findings: list[Finding] = []
    errors: list[str] = []

    for module_dir in module_dirs:
        cmd = [
            bin_path, "breaking",
            "--against", f".git#branch={base_ref}",
            "--error-format", "json",
        ]
        logger.info("Running buf breaking on %s (against %s)", module_dir, base_ref)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(module_dir),
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)

            raw = stdout.decode(errors="replace")
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # buf json error format: path, start_line, type, message
                findings.append(Finding(
                    tool="buf-breaking",
                    severity="HIGH",
                    confidence="HIGH",
                    file=msg.get("path", ""),
                    line=int(msg.get("start_line", 0) or 0),
                    description=msg.get("message", ""),
                    rule_id=msg.get("type", "BUF_BREAKING"),
                ))

            if stderr:
                stderr_text = stderr.decode(errors="replace").strip()
                # buf prints a banner on stderr even on success; only log
                # when the binary itself failed to execute the comparison
                # (e.g. "no commits", missing branch).
                if stderr_text and proc.returncode != 0 and not raw.strip():
                    errors.append(f"buf-breaking ({module_dir.name}): {stderr_text[:500]}")

        except asyncio.TimeoutError:
            errors.append(f"buf-breaking ({module_dir.name}): timeout after {timeout}s")
            logger.error("buf-breaking timeout on %s", module_dir)
        except FileNotFoundError:
            errors.append("buf: binary not found")
        except Exception as e:
            errors.append(f"buf-breaking ({module_dir.name}): {e}")

    logger.info("buf-breaking: %d findings across %d module(s)", len(findings), len(module_dirs))
    return findings, errors


def _has_go_files(target_dir: Path) -> bool:
    """Return True if the directory tree contains any .go source files."""
    return any(target_dir.rglob("*.go"))


def _has_proto_files(target_dir: Path) -> bool:
    """Return True if the directory tree contains any .proto files."""
    return any(target_dir.rglob("*.proto"))


async def scan(target_dir: str | Path) -> ScanReport:
    """Run full security scan on target directory.

    Tools used:
    - semgrep   (all languages)
    - bandit    (Python files only)
    - gosec     (Go module — when go.mod present)
    - govulncheck (Go module — when go.mod present)
    - buf breaking (each buf.yaml module — when .proto files present)
    """
    target_dir = Path(target_dir).resolve()
    if not target_dir.exists():
        return ScanReport(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            target=str(target_dir),
            findings=[],
            errors=[f"Target directory does not exist: {target_dir}"],
        )

    scan_tasks = [run_semgrep(target_dir), run_bandit(target_dir)]
    if _has_go_files(target_dir):
        scan_tasks.append(run_gosec(target_dir))
        scan_tasks.append(run_govulncheck(target_dir))
    if _has_proto_files(target_dir):
        scan_tasks.append(run_buf_breaking(target_dir))

    results = await asyncio.gather(*scan_tasks)

    all_findings: list[Finding] = []
    all_errors: list[str] = []
    for findings, errs in results:
        all_findings.extend(findings)
        all_errors.extend(errs)

    return ScanReport(
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
        target=str(target_dir),
        findings=all_findings,
        errors=all_errors,
    )


async def scan_files(files: list[str], base_dir: str | Path) -> ScanReport:
    """Scan specific changed files. Used by FeatureController.

    - Python files: semgrep + bandit per file.
    - Go files: gosec + govulncheck per detected ``go.mod`` module root
      (one run per module, not per file — Go tools operate on packages).
    - .proto files: buf breaking against the configured base ref, per
      detected ``buf.yaml`` module root.
    """
    base_dir = Path(base_dir).resolve()
    python_files = [f for f in files if f.endswith(".py")]
    go_files = [f for f in files if f.endswith(".go")]
    proto_files = [f for f in files if f.endswith(".proto")]

    if not python_files and not go_files and not proto_files:
        return ScanReport(
            timestamp=datetime.now(tz=timezone.utc).isoformat(),
            target=str(base_dir),
            findings=[],
            errors=[],
        )

    all_findings: list[Finding] = []
    all_errors: list[str] = []

    gather_tasks = []

    # Per-file semgrep + bandit for Python.
    for f in python_files:
        fpath = Path(f) if Path(f).is_absolute() else base_dir / f
        if fpath.exists():
            gather_tasks.append(run_semgrep(fpath))
            gather_tasks.append(run_bandit(fpath))

    # gosec/govulncheck operate on Go modules; collect unique module roots.
    if go_files:
        module_roots: set[Path] = set()
        for f in go_files:
            fpath = Path(f) if Path(f).is_absolute() else base_dir / f
            if not fpath.exists():
                continue
            root = _find_go_mod_root(fpath.parent)
            if root is not None:
                module_roots.add(root)
        for root in module_roots:
            gather_tasks.append(run_gosec(root))
            gather_tasks.append(run_govulncheck(root))

    # buf breaking: walk up from each .proto to find buf.yaml module roots.
    if proto_files:
        buf_roots: set[Path] = set()
        for f in proto_files:
            fpath = Path(f) if Path(f).is_absolute() else base_dir / f
            cursor = fpath.parent if fpath.exists() else (base_dir / f).parent
            for candidate in (cursor, *cursor.parents):
                if (candidate / "buf.yaml").exists():
                    buf_roots.add(candidate)
                    break
                if candidate == base_dir:
                    break
        for root in buf_roots:
            gather_tasks.append(run_buf_breaking(root))

    if gather_tasks:
        results = await asyncio.gather(*gather_tasks)
        for findings, errs in results:
            all_findings.extend(findings)
            all_errors.extend(errs)

    return ScanReport(
        timestamp=datetime.now(tz=timezone.utc).isoformat(),
        target=str(base_dir),
        findings=all_findings,
        errors=all_errors,
    )


# === CLI entrypoint ===

async def _main():
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <target_directory>")
        sys.exit(1)

    target = sys.argv[1]
    report = await scan(target)
    report_path = report.save()

    print(f"\n{'=' * 50}")
    print(f"Security Scan: {report.target}")
    print(f"Findings: {len(report.findings)} (HIGH: {report.high_count}, MEDIUM: {report.medium_count})")
    if report.errors:
        print(f"Errors: {len(report.errors)}")
        for e in report.errors:
            print(f"  - {e}")
    if report.has_blockers:
        print("BLOCKED: HIGH severity findings detected")
        sys.exit(2)
    print(f"Report: {report_path}")


if __name__ == "__main__":
    asyncio.run(_main())
