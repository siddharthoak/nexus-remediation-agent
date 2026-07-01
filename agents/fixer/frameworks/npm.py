"""
npm build framework — package.json JSON parse, `npm ci` + `npm run build`, `npm test`.
Covers Node, React, and Angular repos (the build script differs by project; the test
command shape is the same across all of them).
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path

from . import BuildFramework, BuildResult, DependencyBumpError, TestResult

logger = logging.getLogger(__name__)

_DEP_SECTIONS = ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies")
_RANGE_PREFIX_RE = re.compile(r"^([\^~>=<]*)")


class PackageJsonError(DependencyBumpError):
    """Raised when package.json cannot be parsed or the target dependency is not found."""


class NpmFramework(BuildFramework):
    name = "npm"

    @classmethod
    def detect(cls, repo_path: Path) -> bool:
        return (repo_path / "package.json").exists()

    # ── bump_dependency ───────────────────────────────────────────────────────

    def bump_dependency(
        self, repo_path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        """
        Bump `component`'s entry across dependencies/devDependencies/peerDependencies/
        optionalDependencies, preserving any semver range prefix (^, ~, >=, ...).
        """
        pkg_path = repo_path / "package.json"
        if not pkg_path.exists():
            raise PackageJsonError(f"package.json not found at {pkg_path}")

        data = json.loads(pkg_path.read_text(encoding="utf-8"))

        found = False
        for section in _DEP_SECTIONS:
            deps = data.get(section)
            if not isinstance(deps, dict) or component not in deps:
                continue

            raw_spec = deps[component]
            prefix_match = _RANGE_PREFIX_RE.match(raw_spec)
            prefix = prefix_match.group(1) if prefix_match else ""
            deps[component] = f"{prefix}{new_version}"
            found = True
            logger.info(
                "package.json: %s %s → %s (%s)", component, raw_spec, deps[component], section
            )
            break

        if not found:
            raise PackageJsonError(f"Dependency {component}@{old_version} not found in package.json.")

        pkg_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        self.manifest_file = "package.json"

    # ── build / test_unit ─────────────────────────────────────────────────────

    def _has_script(self, repo_path: Path, script_name: str) -> bool:
        try:
            data = json.loads((repo_path / "package.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return bool(data.get("scripts", {}).get(script_name))

    def build(self, repo_path: Path) -> BuildResult:
        try:
            install = subprocess.run(
                ["npm", "ci", "--no-audit", "--no-fund"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=240,
            )
        except FileNotFoundError:
            return BuildResult(
                success=False,
                output="ERROR: npm not found — Node.js/npm must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return BuildResult(success=False, output="ERROR: npm ci timed out after 240 seconds.")

        if install.returncode != 0:
            output = f"npm ci FAILED:\nSTDERR:\n{install.stderr}"
            if install.stdout.strip():
                output += f"\n\nSTDOUT:\n{install.stdout}"
            return BuildResult(success=False, output=output)

        has_build_script = self._has_script(repo_path, "build")
        has_tsconfig = (repo_path / "tsconfig.json").exists()

        if not has_build_script and not has_tsconfig:
            # Nothing to build or typecheck — no-op success rather than an unnecessary
            # `npx tsc` invocation, which would otherwise attempt a network install.
            return BuildResult(success=True, output="")

        try:
            if has_build_script:
                result = subprocess.run(
                    ["npm", "run", "build", "--if-present"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
            else:
                result = subprocess.run(
                    ["npx", "tsc", "--noEmit"],
                    cwd=str(repo_path),
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
        except FileNotFoundError:
            return BuildResult(
                success=False,
                output="ERROR: npm/npx not found — Node.js/npm must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return BuildResult(success=False, output="ERROR: npm build timed out after 300 seconds.")

        if result.returncode == 0:
            return BuildResult(success=True, output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return BuildResult(success=False, output=output)

    def test_unit(self, repo_path: Path) -> TestResult:
        if not self._has_script(repo_path, "test"):
            return TestResult(status="NO_TESTS_FOUND", output="")

        try:
            result = subprocess.run(
                ["npm", "test", "--", "--testPathIgnorePatterns=integration", "--watchAll=false"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=600,
                env={**os.environ, "CI": "true"},
            )
        except FileNotFoundError:
            return TestResult(
                status="ERROR",
                output="ERROR: npm not found — Node.js/npm must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return TestResult(status="ERROR", output="ERROR: npm test timed out after 600 seconds.")

        if result.returncode == 0:
            return TestResult(status="SUCCESS", output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return TestResult(status="FAILED", output=output)
