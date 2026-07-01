"""
Python build framework — requirements.txt regex substitution, pyproject.toml (parsed
via stdlib tomllib to locate the entry, then applied as a targeted string replace so
formatting/comments are preserved — tomllib has no writer). `python -m compileall` for
the compile gate; `pytest -m "not integration"` for the unit test gate.
"""

import logging
import re
import subprocess
import sys
import tomllib
from pathlib import Path

from . import BuildFramework, BuildResult, DependencyBumpError, TestResult

logger = logging.getLogger(__name__)

# Matches "name[extras]==1.2.3", "name>=1.2.3", etc. (PEP 508 subset sufficient for
# requirements.txt lines and PEP 621 dependency strings).
_VERSION_LINE_RE = re.compile(
    r"^(?P<name>[A-Za-z0-9_.\-]+)\s*(?P<extras>\[[^\]]*\])?\s*"
    r"(?P<op>==|>=|<=|~=|!=|>|<)\s*(?P<ver>[^\s;#]+)"
)


class RequirementsError(DependencyBumpError):
    """Raised when requirements.txt / pyproject.toml cannot be parsed or the target
    dependency is not found."""


def _normalize(name: str) -> str:
    """PEP 503 normalization — '-', '_', '.' are equivalent and comparisons are case-insensitive."""
    return re.sub(r"[-_.]+", "-", name).lower()


class PythonFramework(BuildFramework):
    name = "python"

    @classmethod
    def detect(cls, repo_path: Path) -> bool:
        return any(
            (repo_path / fname).exists()
            for fname in ("requirements.txt", "pyproject.toml", "setup.py")
        )

    # ── bump_dependency ───────────────────────────────────────────────────────

    def bump_dependency(
        self, repo_path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        req_path = repo_path / "requirements.txt"
        if req_path.exists():
            self._bump_requirements_txt(req_path, component, old_version, new_version)
            self.manifest_file = "requirements.txt"
            return

        pyproject_path = repo_path / "pyproject.toml"
        if pyproject_path.exists():
            self._bump_pyproject_toml(pyproject_path, component, old_version, new_version)
            self.manifest_file = "pyproject.toml"
            return

        raise RequirementsError(
            f"Neither requirements.txt nor pyproject.toml found at {repo_path} — "
            f"cannot bump {component}."
        )

    def _bump_requirements_txt(
        self, path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        target = _normalize(component)
        found = False
        new_lines = []

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue

            match = _VERSION_LINE_RE.match(stripped)
            if match and _normalize(match.group("name")) == target:
                suffix = "\n" if line.endswith("\n") else ""
                extras = match.group("extras") or ""
                new_lines.append(f"{match.group('name')}{extras}=={new_version}{suffix}")
                found = True
                logger.info(
                    "requirements.txt: %s %s → %s", component, match.group("ver"), new_version
                )
            else:
                new_lines.append(line)

        if not found:
            raise RequirementsError(
                f"Dependency {component}@{old_version} not found in requirements.txt."
            )
        path.write_text("".join(new_lines), encoding="utf-8")

    def _bump_pyproject_toml(
        self, path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        raw = path.read_text(encoding="utf-8")
        try:
            data = tomllib.loads(raw)
        except tomllib.TOMLDecodeError as exc:
            raise RequirementsError(f"pyproject.toml could not be parsed: {exc}") from exc

        target = _normalize(component)

        candidates = list(data.get("project", {}).get("dependencies", []) or [])
        for group_deps in (data.get("project", {}).get("optional-dependencies", {}) or {}).values():
            candidates.extend(group_deps)

        for dep_str in candidates:
            match = _VERSION_LINE_RE.match(dep_str.strip())
            if match and _normalize(match.group("name")) == target and dep_str in raw:
                new_dep_str = dep_str.replace(match.group("ver"), new_version, 1)
                new_raw = raw.replace(dep_str, new_dep_str, 1)
                path.write_text(new_raw, encoding="utf-8")
                logger.info(
                    "pyproject.toml: %s %s → %s", component, match.group("ver"), new_version
                )
                return

        poetry_deps = data.get("tool", {}).get("poetry", {}).get("dependencies", {}) or {}
        for name, spec in poetry_deps.items():
            if _normalize(name) != target or not isinstance(spec, str):
                continue
            quoted = f'"{spec}"'
            if quoted in raw:
                new_raw = raw.replace(quoted, f'"{new_version}"', 1)
            elif spec in raw:
                new_raw = raw.replace(spec, new_version, 1)
            else:
                continue
            path.write_text(new_raw, encoding="utf-8")
            logger.info("pyproject.toml (poetry): %s %s → %s", component, spec, new_version)
            return

        raise RequirementsError(
            f"Dependency {component}@{old_version} not found in pyproject.toml."
        )

    # ── build / test_unit ─────────────────────────────────────────────────────

    def build(self, repo_path: Path) -> BuildResult:
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "compileall", ".", "-q",
                    "-x", r"(\.venv|venv|node_modules|\.git|build|dist)/",
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=60,
            )
        except FileNotFoundError:
            return BuildResult(success=False, output="ERROR: python interpreter not found.")
        except subprocess.TimeoutExpired:
            return BuildResult(success=False, output="ERROR: compileall timed out after 60 seconds.")

        if result.returncode == 0:
            return BuildResult(success=True, output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return BuildResult(success=False, output=output)

    def test_unit(self, repo_path: Path) -> TestResult:
        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "pytest", "-q",
                    "-m", "not integration", "--ignore=tests/integration",
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            return TestResult(
                status="ERROR",
                output="ERROR: pytest not found — must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return TestResult(status="ERROR", output="ERROR: pytest timed out after 600 seconds.")

        if result.returncode == 0:
            return TestResult(status="SUCCESS", output="")
        if result.returncode == 5:
            # pytest exit code 5 == "no tests collected"
            return TestResult(status="NO_TESTS_FOUND", output="")

        output = f"STDOUT:\n{result.stdout}"
        if result.stderr.strip():
            output += f"\n\nSTDERR:\n{result.stderr}"
        return TestResult(status="FAILED", output=output)
