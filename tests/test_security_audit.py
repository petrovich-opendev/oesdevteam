"""Tests for ``security.security_audit`` Go-toolchain phases.

The semgrep / bandit phases are exercised end-to-end by the standalone
audit_gate runs in CI; here we focus on the new behaviour added when the
DevTeam pipeline started supporting industrial Go services:

- ``_find_go_mod_root`` — Go tools require running from the module root.
  A naive ``cwd=<changed file's dir>`` reports zero findings on a real
  project and silently masks every issue. The walk must be correct.
- ``scan_files`` dispatch — Python files trigger semgrep+bandit only;
  Go files trigger gosec+govulncheck only; .proto files trigger
  buf breaking. No misfires across language boundaries.
- ``run_govulncheck`` / ``run_buf_breaking`` — when the binary is absent
  we surface a helpful error rather than crashing. When present we parse
  the JSON-lines output into ``Finding`` objects with the right severity.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Ensure project root on sys.path for "from security..." imports.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from security import security_audit


# ---------------------------------------------------------------------------
# _find_go_mod_root
# ---------------------------------------------------------------------------


class TestFindGoModRoot:
    def test_returns_dir_containing_go_mod(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        sub = tmp_path / "internal" / "store"
        sub.mkdir(parents=True)

        assert security_audit._find_go_mod_root(sub) == tmp_path

    def test_walks_up_from_file_path(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        sub = tmp_path / "cmd" / "svc"
        sub.mkdir(parents=True)
        file = sub / "main.go"
        file.write_text("package main\n")

        # File path (not directory) is also acceptable.
        assert security_audit._find_go_mod_root(file) == tmp_path

    def test_returns_none_when_no_go_mod(self, tmp_path: Path):
        sub = tmp_path / "src"
        sub.mkdir()
        (sub / "main.go").write_text("package main\n")

        assert security_audit._find_go_mod_root(sub) is None

    def test_picks_nearest_module_in_multi_module_repo(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/outer\n")
        inner = tmp_path / "tools" / "x"
        inner.mkdir(parents=True)
        (inner / "go.mod").write_text("module example.com/inner\n")
        deep = inner / "internal" / "y"
        deep.mkdir(parents=True)

        # The nearest go.mod wins — deeper module overrides outer.
        assert security_audit._find_go_mod_root(deep) == inner


# ---------------------------------------------------------------------------
# scan_files dispatch — language boundaries
# ---------------------------------------------------------------------------


class _StubResult:
    """Helper: return predictable (findings, errors) for any tool function."""

    def __init__(self, tool: str):
        self.tool = tool
        self.calls: list[Path] = []

    async def __call__(self, target):
        self.calls.append(Path(target))
        return [], []


class TestScanFilesDispatch:
    """Each language only invokes the tools that apply to it."""

    @pytest.mark.asyncio
    async def test_python_only_diff_runs_no_go_tools(self, tmp_path: Path):
        py = tmp_path / "app.py"
        py.write_text("print('hi')\n")

        semgrep, bandit, gosec, govuln, buf = (
            _StubResult("semgrep"),
            _StubResult("bandit"),
            _StubResult("gosec"),
            _StubResult("govulncheck"),
            _StubResult("buf"),
        )
        with patch.object(security_audit, "run_semgrep", semgrep), \
             patch.object(security_audit, "run_bandit", bandit), \
             patch.object(security_audit, "run_gosec", gosec), \
             patch.object(security_audit, "run_govulncheck", govuln), \
             patch.object(security_audit, "run_buf_breaking", buf):
            await security_audit.scan_files(["app.py"], tmp_path)

        assert len(semgrep.calls) == 1
        assert len(bandit.calls) == 1
        assert gosec.calls == []
        assert govuln.calls == []
        assert buf.calls == []

    @pytest.mark.asyncio
    async def test_go_diff_runs_no_python_tools_and_uses_module_root(self, tmp_path: Path):
        # Real go.mod at the module root; changed file is several levels deep.
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        deep = tmp_path / "internal" / "store"
        deep.mkdir(parents=True)
        gofile = deep / "users.go"
        gofile.write_text("package store\n")

        semgrep, bandit, gosec, govuln, buf = (
            _StubResult("semgrep"),
            _StubResult("bandit"),
            _StubResult("gosec"),
            _StubResult("govulncheck"),
            _StubResult("buf"),
        )
        with patch.object(security_audit, "run_semgrep", semgrep), \
             patch.object(security_audit, "run_bandit", bandit), \
             patch.object(security_audit, "run_gosec", gosec), \
             patch.object(security_audit, "run_govulncheck", govuln), \
             patch.object(security_audit, "run_buf_breaking", buf):
            await security_audit.scan_files(["internal/store/users.go"], tmp_path)

        assert semgrep.calls == []
        assert bandit.calls == []
        # Both Go tools must be called from the module root, not the file's dir.
        assert gosec.calls == [tmp_path]
        assert govuln.calls == [tmp_path]
        assert buf.calls == []

    @pytest.mark.asyncio
    async def test_proto_diff_dispatches_buf_to_module_root(self, tmp_path: Path):
        api = tmp_path / "api" / "fleet" / "v1"
        api.mkdir(parents=True)
        (api / "buf.yaml").write_text("version: v1\n")
        proto = api / "fleet.proto"
        proto.write_text("syntax = \"proto3\";\n")

        buf = _StubResult("buf")
        with patch.object(security_audit, "run_buf_breaking", buf), \
             patch.object(security_audit, "run_semgrep", _StubResult("semgrep")), \
             patch.object(security_audit, "run_bandit", _StubResult("bandit")):
            await security_audit.scan_files(["api/fleet/v1/fleet.proto"], tmp_path)

        assert buf.calls == [api]

    @pytest.mark.asyncio
    async def test_go_module_dedupe_runs_each_tool_once_per_root(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        for sub in ("internal/a/x.go", "internal/b/y.go", "cmd/svc/main.go"):
            p = tmp_path / sub
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("package x\n")

        gosec = _StubResult("gosec")
        govuln = _StubResult("govulncheck")
        with patch.object(security_audit, "run_gosec", gosec), \
             patch.object(security_audit, "run_govulncheck", govuln), \
             patch.object(security_audit, "run_semgrep", _StubResult("semgrep")), \
             patch.object(security_audit, "run_bandit", _StubResult("bandit")):
            await security_audit.scan_files(
                ["internal/a/x.go", "internal/b/y.go", "cmd/svc/main.go"],
                tmp_path,
            )

        # All three files share a single module root — exactly one run per tool.
        assert gosec.calls == [tmp_path]
        assert govuln.calls == [tmp_path]


# ---------------------------------------------------------------------------
# Binary discovery — graceful absence
# ---------------------------------------------------------------------------


class TestBinaryAbsence:
    @pytest.mark.asyncio
    async def test_govulncheck_missing_returns_diagnostic_not_crash(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        with patch.object(security_audit, "_find_go_tool", return_value=None):
            findings, errors = await security_audit.run_govulncheck(tmp_path)

        assert findings == []
        assert len(errors) == 1
        assert "govulncheck" in errors[0]
        assert "install" in errors[0].lower()

    @pytest.mark.asyncio
    async def test_buf_missing_returns_diagnostic_not_crash(self, tmp_path: Path):
        api = tmp_path / "api"
        api.mkdir()
        (api / "buf.yaml").write_text("version: v1\n")
        with patch.object(security_audit, "_find_go_tool", return_value=None):
            findings, errors = await security_audit.run_buf_breaking(tmp_path)

        assert findings == []
        assert len(errors) == 1
        assert "buf" in errors[0]

    @pytest.mark.asyncio
    async def test_buf_no_modules_skips_silently(self, tmp_path: Path):
        # No buf.yaml anywhere — nothing to compare, no error.
        with patch.object(security_audit, "_find_go_tool", return_value="/fake/buf"):
            findings, errors = await security_audit.run_buf_breaking(tmp_path)

        assert findings == []
        assert errors == []

    @pytest.mark.asyncio
    async def test_govulncheck_no_go_mod_returns_diagnostic(self, tmp_path: Path):
        # Binary exists but the target has no module — clear error message.
        with patch.object(security_audit, "_find_go_tool", return_value="/fake/govulncheck"):
            findings, errors = await security_audit.run_govulncheck(tmp_path)

        assert findings == []
        assert len(errors) == 1
        assert "go.mod" in errors[0]


# ---------------------------------------------------------------------------
# Output parsing — hand-crafted JSON-lines payloads
# ---------------------------------------------------------------------------


class _FakeProc:
    def __init__(self, stdout: bytes = b"", stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, self._stderr


def _patched_subprocess(stdout: bytes, stderr: bytes = b"", returncode: int = 0):
    async def _create(*args, **kwargs):
        return _FakeProc(stdout, stderr, returncode)
    return _create


class TestOutputParsing:
    @pytest.mark.asyncio
    async def test_govulncheck_parses_finding_with_call_trace(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        payload = "\n".join([
            json.dumps({"config": {"protocol_version": "v1.0.0"}}),
            json.dumps({
                "finding": {
                    "osv": "GO-2024-1234",
                    "trace": [{
                        "module": "example.com/x/internal/store",
                        "function": "store.Insert",
                        "position": {
                            "filename": "/work/internal/store/users.go",
                            "line": 42,
                        },
                    }],
                },
            }),
            # Advisory without a trace — must be skipped (informational only).
            json.dumps({"finding": {"osv": "GO-2024-9999"}}),
        ]).encode()

        with patch.object(security_audit, "_find_go_tool", return_value="/fake/govulncheck"), \
             patch("asyncio.create_subprocess_exec", _patched_subprocess(payload)):
            findings, errors = await security_audit.run_govulncheck(tmp_path)

        assert errors == []
        assert len(findings) == 1
        f = findings[0]
        assert f.tool == "govulncheck"
        assert f.severity == "HIGH"
        assert f.rule_id == "GO-2024-1234"
        assert f.file == "/work/internal/store/users.go"
        assert f.line == 42

    @pytest.mark.asyncio
    async def test_govulncheck_dedupes_duplicate_osv(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        trace = [{"function": "f", "position": {"filename": "/a.go", "line": 1}}]
        payload = "\n".join([
            json.dumps({"finding": {"osv": "GO-X", "trace": trace}}),
            json.dumps({"finding": {"osv": "GO-X", "trace": trace}}),
            json.dumps({"finding": {"osv": "GO-Y", "trace": trace}}),
        ]).encode()

        with patch.object(security_audit, "_find_go_tool", return_value="/fake/govulncheck"), \
             patch("asyncio.create_subprocess_exec", _patched_subprocess(payload)):
            findings, _ = await security_audit.run_govulncheck(tmp_path)

        ids = sorted(f.rule_id for f in findings)
        assert ids == ["GO-X", "GO-Y"]

    @pytest.mark.asyncio
    async def test_buf_breaking_parses_violations(self, tmp_path: Path):
        api = tmp_path / "api"
        api.mkdir()
        (api / "buf.yaml").write_text("version: v1\n")
        payload = "\n".join([
            json.dumps({
                "path": "api/fleet/v1/fleet.proto",
                "start_line": 17,
                "type": "FIELD_NO_DELETE",
                "message": "Previously present field \"id\" on message \"Truck\" was deleted.",
            }),
            json.dumps({
                "path": "api/fleet/v1/fleet.proto",
                "start_line": 33,
                "type": "RPC_NO_DELETE",
                "message": "Previously present RPC \"GetTruck\" on service \"Fleet\" was deleted.",
            }),
        ]).encode()

        with patch.object(security_audit, "_find_go_tool", return_value="/fake/buf"), \
             patch("asyncio.create_subprocess_exec", _patched_subprocess(payload, returncode=100)):
            findings, errors = await security_audit.run_buf_breaking(tmp_path)

        assert errors == []
        assert len(findings) == 2
        assert {f.rule_id for f in findings} == {"FIELD_NO_DELETE", "RPC_NO_DELETE"}
        assert all(f.severity == "HIGH" for f in findings)
        assert all(f.tool == "buf-breaking" for f in findings)

    @pytest.mark.asyncio
    async def test_buf_breaking_clean_exit_zero_findings(self, tmp_path: Path):
        api = tmp_path / "api"
        api.mkdir()
        (api / "buf.yaml").write_text("version: v1\n")
        with patch.object(security_audit, "_find_go_tool", return_value="/fake/buf"), \
             patch("asyncio.create_subprocess_exec", _patched_subprocess(b"", returncode=0)):
            findings, errors = await security_audit.run_buf_breaking(tmp_path)

        assert findings == []
        assert errors == []


# ---------------------------------------------------------------------------
# scan() — full-tree dispatch only invokes Go phases when go.mod exists
# ---------------------------------------------------------------------------


class TestFullScan:
    @pytest.mark.asyncio
    async def test_pure_python_tree_skips_all_go_phases(self, tmp_path: Path):
        (tmp_path / "app.py").write_text("print('hi')\n")

        gosec = _StubResult("gosec")
        govuln = _StubResult("govulncheck")
        buf = _StubResult("buf")
        with patch.object(security_audit, "run_semgrep", _StubResult("semgrep")), \
             patch.object(security_audit, "run_bandit", _StubResult("bandit")), \
             patch.object(security_audit, "run_gosec", gosec), \
             patch.object(security_audit, "run_govulncheck", govuln), \
             patch.object(security_audit, "run_buf_breaking", buf):
            await security_audit.scan(tmp_path)

        assert gosec.calls == []
        assert govuln.calls == []
        assert buf.calls == []

    @pytest.mark.asyncio
    async def test_go_tree_invokes_both_gosec_and_govulncheck(self, tmp_path: Path):
        (tmp_path / "go.mod").write_text("module example.com/x\n")
        (tmp_path / "main.go").write_text("package main\n")

        gosec = _StubResult("gosec")
        govuln = _StubResult("govulncheck")
        with patch.object(security_audit, "run_semgrep", _StubResult("semgrep")), \
             patch.object(security_audit, "run_bandit", _StubResult("bandit")), \
             patch.object(security_audit, "run_gosec", gosec), \
             patch.object(security_audit, "run_govulncheck", govuln), \
             patch.object(security_audit, "run_buf_breaking", _StubResult("buf")):
            await security_audit.scan(tmp_path)

        assert len(gosec.calls) == 1
        assert len(govuln.calls) == 1

    @pytest.mark.asyncio
    async def test_proto_present_invokes_buf_breaking(self, tmp_path: Path):
        api = tmp_path / "api"
        api.mkdir()
        (api / "buf.yaml").write_text("version: v1\n")
        (api / "x.proto").write_text("syntax = \"proto3\";\n")

        buf = _StubResult("buf")
        with patch.object(security_audit, "run_semgrep", _StubResult("semgrep")), \
             patch.object(security_audit, "run_bandit", _StubResult("bandit")), \
             patch.object(security_audit, "run_buf_breaking", buf):
            await security_audit.scan(tmp_path)

        assert len(buf.calls) == 1


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
