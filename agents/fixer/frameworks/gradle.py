"""
Gradle build framework — regex-based bump on build.gradle / build.gradle.kts, falling
back to the version catalog (gradle/libs.versions.toml) when the version string is not
declared inline. Uses the repo's own `./gradlew` wrapper; Gradle itself is not installed
in the Fixer image.
"""

import logging
import re
import subprocess
from pathlib import Path

from . import BuildFramework, BuildResult, DependencyBumpError, TestResult

logger = logging.getLogger(__name__)

_TEST_SOURCE_LANGS = ("java", "kotlin", "groovy")


class BuildGradleError(DependencyBumpError):
    """Raised when build.gradle(.kts) / the version catalog cannot be parsed or the
    target dependency is not found."""


class GradleFramework(BuildFramework):
    name = "gradle"

    @classmethod
    def detect(cls, repo_path: Path) -> bool:
        return (repo_path / "build.gradle").exists() or (repo_path / "build.gradle.kts").exists()

    # ── bump_dependency ───────────────────────────────────────────────────────

    def bump_dependency(
        self, repo_path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        """
        Strategy A: look for a literal 'group:artifact:old_version' (or double-quoted)
        declaration directly in build.gradle / build.gradle.kts and replace the version.

        Strategy B (fallback): look up old_version in gradle/libs.versions.toml. Only
        replaces if the version string is unambiguous (appears exactly once) — refuses
        to guess which catalog entry corresponds to `component` otherwise.
        """
        group_id, _, artifact_id = component.rpartition(":")
        if not group_id:
            artifact_id = component

        build_file = None
        for candidate in ("build.gradle", "build.gradle.kts"):
            path = repo_path / candidate
            if path.exists():
                build_file = path
                break

        if build_file is not None:
            text = build_file.read_text(encoding="utf-8")
            if group_id:
                pattern = re.compile(
                    r"(['\"])" + re.escape(f"{group_id}:{artifact_id}:{old_version}") + r"\1"
                )
            else:
                pattern = re.compile(
                    r"(['\"])[\w.\-]+:" + re.escape(f"{artifact_id}:{old_version}") + r"\1"
                )
            new_text, count = pattern.subn(
                lambda m: m.group(0).replace(old_version, new_version), text
            )
            if count > 0:
                build_file.write_text(new_text, encoding="utf-8")
                self.manifest_file = build_file.name
                logger.info(
                    "%s: %s %s → %s", build_file.name, component, old_version, new_version
                )
                return

        catalog = repo_path / "gradle" / "libs.versions.toml"
        if catalog.exists():
            text = catalog.read_text(encoding="utf-8")
            pattern = re.compile(r'(=\s*")' + re.escape(old_version) + r'(")')
            matches = list(pattern.finditer(text))
            if len(matches) == 1:
                new_text = pattern.sub(r"\g<1>" + new_version + r"\g<2>", text)
                catalog.write_text(new_text, encoding="utf-8")
                self.manifest_file = "gradle/libs.versions.toml"
                logger.info(
                    "gradle/libs.versions.toml: %s %s → %s", component, old_version, new_version
                )
                return
            if len(matches) > 1:
                raise BuildGradleError(
                    f"Version {old_version} appears {len(matches)} times in "
                    f"gradle/libs.versions.toml — ambiguous which entry corresponds to "
                    f"{component}. Refusing to guess."
                )

        raise BuildGradleError(
            f"Dependency {component}@{old_version} not found in build.gradle(.kts) "
            "or gradle/libs.versions.toml."
        )

    # ── build / test_unit ─────────────────────────────────────────────────────

    def build(self, repo_path: Path) -> BuildResult:
        gradlew = repo_path / "gradlew"
        if not gradlew.exists():
            return BuildResult(
                success=False,
                output="ERROR: gradlew wrapper not found in repository root — cannot run a Gradle build.",
            )
        try:
            result = subprocess.run(
                [str(gradlew), "classes", "-q"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except PermissionError:
            return BuildResult(
                success=False,
                output="ERROR: gradlew could not be executed — check wrapper permissions (chmod +x gradlew).",
            )
        except subprocess.TimeoutExpired:
            return BuildResult(success=False, output="ERROR: ./gradlew classes timed out after 300 seconds.")

        if result.returncode == 0:
            return BuildResult(success=True, output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return BuildResult(success=False, output=output)

    def test_unit(self, repo_path: Path) -> TestResult:
        gradlew = repo_path / "gradlew"
        if not gradlew.exists():
            return TestResult(
                status="ERROR",
                output="ERROR: gradlew wrapper not found in repository root.",
            )

        has_tests = False
        for lang in _TEST_SOURCE_LANGS:
            test_dir = repo_path / "src" / "test" / lang
            if test_dir.exists() and (
                any(test_dir.rglob("*Test.*")) or any(test_dir.rglob("*Tests.*"))
            ):
                has_tests = True
                break
        if not has_tests:
            return TestResult(status="NO_TESTS_FOUND", output="")

        try:
            result = subprocess.run(
                [str(gradlew), "test", "-q"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except PermissionError:
            return TestResult(
                status="ERROR",
                output="ERROR: gradlew could not be executed — check wrapper permissions (chmod +x gradlew).",
            )
        except subprocess.TimeoutExpired:
            return TestResult(status="ERROR", output="ERROR: ./gradlew test timed out after 600 seconds.")

        if result.returncode == 0:
            return TestResult(status="SUCCESS", output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return TestResult(status="FAILED", output=output)
