"""Pre-review rule implementations for Go source files.

Companion to ``checks.py`` (which holds Python / config rules). Kept in
its own module because the Go ecosystem has a different surface area
(error wrapping, panic, package layout, generated artefacts) and
lumping the rules together would obscure the language boundary.

Each rule is a pure function ``(RuleContext) -> list[RuleFinding]`` and
self-filters by file extension — when no ``.go`` files are in the diff
the rule returns immediately.

Scope (MVP, per project decision)
---------------------------------
This module ships three Go rules deliberately chosen to mirror the most
load-bearing Python rules:

- ``rule_go_silent_err``  — Go analogue of ``R-silent-except``: a
  swallowed ``err`` with no log / no wrap is the #1 source of silent
  outages in Go services.
- ``rule_go_sql_concat``  — Go analogue of
  ``R-sql-identifier-fstring``: ``fmt.Sprintf`` / ``+`` building SQL
  identifiers is a SQL-injection primitive.
- The scaffold-only fluff list in ``checks.py`` is extended to cover
  ``*.pb.go``, ``*_mock.go``, ``go.sum`` so a generated-artefact-only
  diff is blocked the same way it is for TS/Python.

A larger set of Go rules (defer rows.Close, http.DefaultClient ban,
bare panic, %v errors, WaitGroup.Add ordering, proto compatibility) is
intentionally deferred to "configuration requirements" — see
``docs/PRE_REVIEW_RULES_ROADMAP.md``. Those checks belong either in
``go vet`` / ``golangci-lint`` (which the project requires) or in a
future static-analysis pass; bolting half-baked regex checks onto this
engine would produce false positives that erode reviewer trust.
"""

from __future__ import annotations

import re

from .engine import RuleFinding


# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------


def _iter_go_sources(ctx) -> list[tuple[str, str]]:
    """Yield ``(rel_path, source)`` for every ``.go`` file in the diff.

    Test files (``*_test.go``) are included because the rules below are
    correctness rules, not style rules; a silent ``err`` in a test setup
    helper bites just as hard as one in production code. The previous
    project decision to skip Python ``tests/`` directories does NOT carry
    over to Go: in Go, table-driven tests sit next to the code they test
    and frequently call into production helpers, so blanket-skipping
    them would create a real blind spot.
    """
    out: list[tuple[str, str]] = []
    for rel in ctx.files_changed:
        if not rel.endswith(".go"):
            continue
        # Skip generated files — they are reviewed by the proto / mock
        # source they were generated from, not at the .go layer.
        if rel.endswith((".pb.go", "_grpc.pb.go", "_mock.go", "_gen.go")):
            continue
        src = ctx.read_text_safe(rel)
        if not src:
            continue
        out.append((rel, src))
    return out


def _line_at(src: str, byte_offset: int) -> int:
    """1-based line number of ``byte_offset`` in ``src``.

    Both rules below want a stable line number so the finding can be
    rendered as ``file.go:LINE`` in the report; computing it once via
    ``str.count`` is cheap and avoids per-rule re-implementation.
    """
    return src.count("\n", 0, byte_offset) + 1


# -----------------------------------------------------------------------------
# R-go-silent-err
# -----------------------------------------------------------------------------

# Matches the error-handling block opener `if err != nil {` (with optional
# whitespace and other common error variable names: err, e, retErr).
# Captures the body up to the matching closing brace at the same nesting
# level. We cannot do a perfect brace match with regex; instead we match
# the simple-body cases (no nested braces) which is exactly what silent
# handlers look like in practice.
_GO_ERR_HANDLER_RE = re.compile(
    r"if\s+(?:err|e|retErr|cerr|rerr|wErr)\s*!=\s*nil\s*\{(?P<body>[^{}]*)\}",
    re.MULTILINE,
)

# Tokens that indicate the handler is NOT silent: any logger / metric /
# wrapping / re-raise call. We accept generous matching because the goal
# is to avoid false positives, not to be exhaustive.
_GO_OBSERVABILITY_TOKENS = (
    "log.",
    "logger.",
    "slog.",
    "zap.",
    "zerolog.",
    "logrus.",
    "Errorf",          # fmt.Errorf with %w wrapping
    "errors.Wrap",     # github.com/pkg/errors (legacy but still seen)
    "errors.Join",
    "trace.",
    "span.",
    "metric",
    "Counter",
    "Histogram",
    "Observe",
    "Inc(",
    "Add(",
    "panic(",          # panic re-surface — not silent
    "t.Fatal",         # test failure surfacing
    "t.Error",
    "require.",
    "assert.",
)

# Body shapes that mean "definitely silent" — empty body, lone `_ = err`,
# or a bare `return` / `continue` / `break` with no observability call.
_GO_SILENT_BODY_RE = re.compile(
    r"""
    ^\s*$                                       # empty
  | ^\s*_\s*=\s*(?:err|e|retErr|cerr|rerr|wErr)\s*$  # explicit discard
  | ^\s*return\s*$                              # bare return
  | ^\s*return\s+nil\s*$                        # return nil with no wrap
  | ^\s*continue\s*$
  | ^\s*break\s*$
    """,
    re.VERBOSE | re.MULTILINE,
)


def rule_go_silent_err(ctx) -> list:
    """Block ``if err != nil {}`` with no log / no wrap / no metric.

    Go's idiomatic error handling makes silent swallows trivial to write
    by accident: a developer hits "early return" once, forgets to wrap
    or log, and the next outage is invisible until someone reads logs by
    hand. This rule mirrors ``R-silent-except`` for the Go side and
    treats violations as BLOCKER for the same reason — observability is
    a hard project rule (see ``docs/RESILIENCE_RULES.md`` R-2).

    Detection strategy: match common ``if err != nil { ... }`` blocks
    with non-nested bodies and check whether the body contains any
    observability token. Bodies with no observability AND a "trivially
    silent" shape (empty, ``_ = err``, bare ``return``/``return nil``,
    ``continue``, ``break``) are flagged. Anything more complex is left
    to senior review — the goal is high precision, not recall.
    """
    findings: list = []
    for rel, src in _iter_go_sources(ctx):
        for match in _GO_ERR_HANDLER_RE.finditer(src):
            body = match.group("body")

            # Comment-suppression escape hatch on the same line as the
            # `if err != nil {`, mirrors `# noqa: silent-except` in Python.
            block_lineno = _line_at(src, match.start())
            block_line = src.splitlines()[block_lineno - 1] if block_lineno - 1 < len(src.splitlines()) else ""
            if "nolint:silent-err" in block_line:
                continue

            if any(tok in body for tok in _GO_OBSERVABILITY_TOKENS):
                continue

            stripped_body = body.strip()
            if not _GO_SILENT_BODY_RE.search(stripped_body) and stripped_body != "":
                # Body has *some* content but no observability — leave to
                # senior review rather than risk false positive on
                # legitimate cleanup / state-mutation code.
                continue

            findings.append(
                RuleFinding(
                    rule_id="R-go-silent-err",
                    severity="blocker",
                    file=rel,
                    line=block_lineno,
                    category="observability",
                    summary=(
                        "`if err != nil` swallows the error with no log, "
                        "metric, or wrap"
                    ),
                    why=(
                        "An empty / bare-return error branch in a Go service "
                        "turns every upstream failure into silent degradation. "
                        "On a transient PG, NATS, or vendor-API blip the "
                        "service keeps running with stale state and no "
                        "operator signal — the worst-case failure mode for "
                        "any production industrial Go service. Project "
                        "resilience rule R-2 treats this as BLOCKER."
                    ),
                    fix=(
                        "Either (a) wrap with `fmt.Errorf(\"<context>: "
                        "%w\", err)` and return so the caller can decide, or "
                        "(b) if the call is best-effort, log at WARN with "
                        "`slog.Warn(\"<op> failed\", \"err\", err)`, "
                        "increment a `<op>_failures_total` counter, and "
                        "surface a `<op>_degraded` flag on /readyz. If the "
                        "handler really is correct, add `// nolint:silent-err` "
                        "on the `if err != nil {` line with a one-line "
                        "justification."
                    ),
                )
            )
    return findings


# -----------------------------------------------------------------------------
# R-go-sql-concat
# -----------------------------------------------------------------------------

# Match a SQL keyword that introduces an identifier position followed by
# a Go format placeholder (`%s`, `%v`, `%q`) inside an `fmt.Sprintf` /
# `fmt.Errorf` literal, OR followed by a string concatenation `"..." + x`.
_SQL_KEYWORD_GO = r"(?:FROM|JOIN|INTO|UPDATE|TABLE)"

# fmt.Sprintf("...FROM %s ...", x) — placeholder right after a SQL keyword.
_GO_SQL_FMT_RE = re.compile(
    rf"""fmt\.(?:Sprintf|Errorf)\(\s*["`][^"`]*\b{_SQL_KEYWORD_GO}\s+%[svq]""",
    re.IGNORECASE,
)

# "... FROM " + tableName — FROM appears at the tail of a quoted literal,
# the closing quote is what we land on, then `+ ident` follows.
_GO_SQL_CONCAT_RE = re.compile(
    rf"""\b{_SQL_KEYWORD_GO}\s+["`]\s*\+\s*[A-Za-z_][A-Za-z0-9_.\{{\}}]*""",
    re.IGNORECASE,
)


def rule_go_sql_concat(ctx) -> list:
    """Block string-formatted SQL identifiers in Go.

    Go's ``database/sql`` makes parameterised values trivial (``$1``,
    ``$2``); identifiers are the trap. ``fmt.Sprintf("SELECT ... FROM
    %s", table)`` is a SQL-injection primitive even when ``table`` looks
    like it comes from a whitelist — whitelists drift, Unicode
    homoglyphs sneak past ``\\w`` checks, and the only safe form is
    explicit identifier quoting (``pgx.Identifier{table}.Sanitize()`` or
    constant-time lookup against a Go map).

    Severity BLOCKER, category security. Mirrors
    ``R-sql-identifier-fstring`` on the Python side.
    """
    findings: list = []
    for rel, src in _iter_go_sources(ctx):
        for lineno, line in enumerate(src.splitlines(), start=1):
            if _GO_SQL_FMT_RE.search(line) or _GO_SQL_CONCAT_RE.search(line):
                findings.append(
                    RuleFinding(
                        rule_id="R-go-sql-concat",
                        severity="blocker",
                        file=rel,
                        line=lineno,
                        category="security",
                        summary=(
                            "SQL identifier built via fmt.Sprintf / "
                            "string concatenation"
                        ),
                        why=(
                            "Building a table / column / schema name into "
                            "SQL with `fmt.Sprintf` or `+` is a SQL-injection "
                            "primitive. Whitelists and regex guards drift as "
                            "the schema grows and miss Unicode homoglyphs; "
                            "this class of bug is one of the top causes of "
                            "auth-bypass incidents in production Go services."
                        ),
                        fix=(
                            "Use `pgx.Identifier{name}.Sanitize()` (pgx) or "
                            "`pq.QuoteIdentifier(name)` (lib/pq) for "
                            "PostgreSQL identifiers. For ClickHouse, look up "
                            "the table from a typed `map[Domain]Table` "
                            "constant — never accept the identifier from "
                            "user input. Bind values (not identifiers) via "
                            "`$1`, `$2` placeholders."
                        ),
                    )
                )
    return findings


__all__ = [
    "rule_go_silent_err",
    "rule_go_sql_concat",
]
