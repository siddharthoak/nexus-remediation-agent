"""
Build framework abstraction — lets the Fixer's tool-use loop call `run_build` and
`run_unit_tests` without knowing whether the target repo is Maven, Gradle, npm, or Python.

`detect_framework(repo_path)` inspects the repository root and returns the first
`BuildFramework` implementation whose `detect()` classmethod matches. Priority order
matters for polyglot repos (e.g. a Java project with a `package.json` for frontend
tooling must resolve as Maven, not npm):

    1. pom.xml                                     → Maven
    2. build.gradle / build.gradle.kts             → Gradle
    3. package.json                                → npm
    4. requirements.txt / pyproject.toml / setup.py → Python

Returns None if no supported build file is found. Callers degrade gracefully in that
case (see code_fixer.py) rather than raising — CI remains the fallback verification.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


# ── Result types ──────────────────────────────────────────────────────────────

@dataclass
class BuildResult:
    success: bool
    output: str = ""  # empty on success; compiler/build output on failure


@dataclass
class TestResult:
    __test__ = False  # tells pytest this is not a test class despite the name

    status: str  # "SUCCESS" | "NO_TESTS_FOUND" | "FAILED" | "ERROR"
    output: str = ""  # empty on SUCCESS/NO_TESTS_FOUND; test output otherwise


# ── Exceptions ────────────────────────────────────────────────────────────────

class DependencyBumpError(Exception):
    """
    Raised when a framework's bump_dependency() cannot locate or update the target
    dependency in its manifest. Framework modules raise a specific subclass; callers
    may catch this base class generically.
    """


# ── Interface ─────────────────────────────────────────────────────────────────

class BuildFramework(ABC):
    """
    All framework implementations must satisfy this interface. One instance is created
    per fix attempt (see detect_framework()); `manifest_file` is populated as a side
    effect of `bump_dependency()` so callers know which file to include in the PR's
    files-changed list without hardcoding a manifest name per framework.
    """

    name: str = "unknown"

    def __init__(self):
        self.manifest_file: str = ""

    @classmethod
    @abstractmethod
    def detect(cls, repo_path: Path) -> bool:
        """True if this framework owns this repo."""

    @abstractmethod
    def bump_dependency(
        self, repo_path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        """
        Bump `component` from old_version to new_version in this framework's manifest.
        Sets self.manifest_file to the relative path actually modified.
        Raises a DependencyBumpError subclass if the dependency cannot be found.
        """

    @abstractmethod
    def build(self, repo_path: Path) -> BuildResult:
        """Compile / typecheck the repository. No tests are executed."""

    @abstractmethod
    def test_unit(self, repo_path: Path) -> TestResult:
        """Run unit tests only. Integration tests are structurally excluded."""


# ── Detection ─────────────────────────────────────────────────────────────────

def detect_framework(repo_path: Path) -> Optional[BuildFramework]:
    """
    Try each supported framework in priority order and return the first match,
    or None if the repository does not match any supported build file.
    """
    # Imported lazily to avoid a module-load cycle (each framework module does not
    # need to import from this package's submodules at import time).
    from .maven import MavenFramework
    from .gradle import GradleFramework
    from .npm import NpmFramework
    from .python_pip import PythonFramework

    for framework_cls in (MavenFramework, GradleFramework, NpmFramework, PythonFramework):
        if framework_cls.detect(repo_path):
            logger.info("detect_framework: %s detected at %s", framework_cls.name, repo_path)
            return framework_cls()

    logger.warning(
        "detect_framework: no supported build file found at %s — "
        "degraded mode (no compile/test gate, CI is the fallback).",
        repo_path,
    )
    return None
