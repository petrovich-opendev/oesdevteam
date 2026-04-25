"""Namespace-scoped reviewer configuration.

A namespace (``namespaces/<env>/<domain>/``) declares its squad
composition in ``roles.yaml`` — the single source of truth for which
Senior Reviewers run on its features. Without a ``roles.yaml`` the
controller falls back to the default ``REVIEWER_ROLES`` from
``squad.py``; this preserves backward-compatibility for legacy
namespaces that were never explicitly configured.

Why namespace-scoped config (not file-ext heuristic)
----------------------------------------------------
The previous design switched ``SENIOR_BACKEND → SENIOR_GO`` based on
file extensions in the diff. That works for "obvious" diffs but
breaks down on hybrid namespaces (Go service with a Python ETL helper,
or Python project that adds a single ``.go`` script): the heuristic
either over- or under-applies. ``roles.yaml`` is declarative — the
maintainer of a namespace tells the platform exactly which reviewers
their codebase needs, and that decision survives every PR.

Schema
------
::

    # namespaces/<env>/<domain>/roles.yaml
    reviewers:
      - senior_go
      - senior_frontend
      - senior_data
      - senior_sre
      - business_expert
      - senior_domain_logic

Unknown role names are rejected (loud failure during pipeline start
beats silent under-review). An empty list is rejected — squad with
zero reviewers cannot block anything.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from ..models import AgentRole

logger = logging.getLogger("oesdevteam.namespace_config")


# Filename for namespace squad composition. Lives next to features.json
# so a maintainer skimming the namespace root sees it immediately.
ROLES_FILENAME = "roles.yaml"

# Filename for namespace-wide terminology rules. One edit propagates to
# every reviewer in the squad — the alternative (per-prompt hardcode)
# drifts the moment one prompt is updated and another is forgotten.
TERMINOLOGY_FILENAME = "terminology.md"

# Cap on terminology block size. The block is prepended to every
# reviewer's system prompt; a runaway file would inflate context for
# every review call. Industrial domains with bilingual ban/preferred
# tables fit comfortably under this cap.
_TERMINOLOGY_MAX_CHARS = 6_000


def load_terminology(project_dir: Path) -> str:
    """Return namespace-wide terminology rules to prepend to every reviewer.

    Args:
        project_dir: Namespace root.

    Returns:
        Contents of ``terminology.md`` (≤ ``_TERMINOLOGY_MAX_CHARS``),
        or empty string if the file is absent / empty / unreadable. The
        caller is expected to skip injection when this returns empty.
    """
    path = (project_dir / TERMINOLOGY_FILENAME).resolve()
    if not path.exists():
        logger.info(
            "namespace_config: no %s in %s — reviewers run without "
            "terminology injection",
            TERMINOLOGY_FILENAME,
            project_dir,
        )
        return ""
    try:
        content = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(
            "namespace_config: failed to read %s: %s — reviewers run "
            "without terminology injection",
            path,
            exc,
        )
        return ""
    if not content:
        return ""
    if len(content) > _TERMINOLOGY_MAX_CHARS:
        logger.warning(
            "namespace_config: %s is %d chars, trimming to %d "
            "(consider splitting per-section files referenced from the main doc)",
            path,
            len(content),
            _TERMINOLOGY_MAX_CHARS,
        )
        content = content[:_TERMINOLOGY_MAX_CHARS]
    logger.info(
        "namespace_config: loaded %d-char terminology block from %s",
        len(content),
        path,
    )
    return content


def load_namespace_roles(project_dir: Path) -> tuple[AgentRole, ...] | None:
    """Return the squad composition declared in ``roles.yaml``.

    Args:
        project_dir: Namespace root.

    Returns:
        ``None`` when the file is absent — the caller should fall back
        to the default ``REVIEWER_ROLES``. A non-empty tuple of
        ``AgentRole``s otherwise.

    Raises:
        ValueError: when the file is present but malformed (non-YAML,
            missing ``reviewers`` key, empty list, unknown role name,
            duplicate role). Loud failure beats silent degradation —
            an unparseable squad config is operator error and the
            pipeline must not silently fall back to defaults.
    """
    path = (project_dir / ROLES_FILENAME).resolve()
    if not path.exists():
        logger.info(
            "namespace_config: no %s in %s — using default REVIEWER_ROLES",
            ROLES_FILENAME,
            project_dir,
        )
        return None

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"failed to read {path}: {exc}") from exc

    try:
        data: Any = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValueError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(data, dict):
        raise ValueError(
            f"{path}: top-level YAML must be a mapping, got {type(data).__name__}"
        )

    raw_roles = data.get("reviewers")
    if raw_roles is None:
        raise ValueError(f"{path}: missing required key 'reviewers'")
    if not isinstance(raw_roles, list):
        raise ValueError(
            f"{path}: 'reviewers' must be a list, got {type(raw_roles).__name__}"
        )
    if not raw_roles:
        raise ValueError(
            f"{path}: 'reviewers' is empty — a squad with zero reviewers "
            "cannot block anything; remove the file to use the default squad"
        )

    valid_values = {r.value for r in AgentRole}
    resolved: list[AgentRole] = []
    seen: set[str] = set()
    for entry in raw_roles:
        if not isinstance(entry, str):
            raise ValueError(
                f"{path}: every reviewer entry must be a string, got "
                f"{type(entry).__name__} ({entry!r})"
            )
        name = entry.strip()
        if not name:
            raise ValueError(f"{path}: empty reviewer name in list")
        if name in seen:
            raise ValueError(
                f"{path}: duplicate reviewer {name!r} — each role may "
                "appear at most once"
            )
        if name not in valid_values:
            allowed = ", ".join(sorted(valid_values))
            raise ValueError(
                f"{path}: unknown reviewer {name!r}. Allowed: {allowed}"
            )
        resolved.append(AgentRole(name))
        seen.add(name)

    logger.info(
        "namespace_config: loaded %d reviewers from %s: %s",
        len(resolved),
        path,
        [r.value for r in resolved],
    )
    return tuple(resolved)
