"""
Maven build framework — pom.xml XML parsing, `mvn compile` and `mvn test` (Surefire only).

Detected first in priority order: a pom.xml at the repo root always resolves as Maven,
even in a polyglot repo that also has a package.json for frontend tooling.
"""

import logging
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

from . import BuildFramework, BuildResult, DependencyBumpError, TestResult

logger = logging.getLogger(__name__)

_POM_NS_URI = "http://maven.apache.org/POM/4.0.0"


class PomXMLError(DependencyBumpError):
    """Raised when pom.xml cannot be parsed or the target dependency is not found."""


def _is_unit_test_file(name: str) -> bool:
    if name.endswith("IT.java") or name.endswith("IntegrationTest.java"):
        return False
    return name.endswith("Test.java") or name.endswith("Tests.java")


class MavenFramework(BuildFramework):
    name = "maven"

    @classmethod
    def detect(cls, repo_path: Path) -> bool:
        return (repo_path / "pom.xml").exists()

    # ── bump_dependency ───────────────────────────────────────────────────────

    def bump_dependency(
        self, repo_path: Path, component: str, old_version: str, new_version: str
    ) -> None:
        """
        Bump a dependency version in pom.xml using an XML parser (never string-replace).

        Handles three real-world patterns found in customer repos:
          1. Standard literal <version>X.Y.Z</version>
          2. Maven property reference <version>${spring.version}</version> — updates the
             property in <properties>, or inlines the version if the property is not found.
          3. BOM-managed dependency (no <version> element) — adds an explicit <version>
             override so the new version is pinned regardless of the BOM.

        Supports both namespaced pom.xml files (xmlns="http://maven.apache.org/POM/4.0.0")
        and bare files without a namespace declaration (some legacy / generated poms).
        """
        pom_path = repo_path / "pom.xml"
        if not pom_path.exists():
            raise PomXMLError(f"pom.xml not found at {pom_path}")

        tree = ET.parse(str(pom_path))
        root = tree.getroot()

        ET.register_namespace("", _POM_NS_URI)

        if root.tag.startswith(f"{{{_POM_NS_URI}}}"):
            ns = {"m": _POM_NS_URI}
            dep_xpath = ".//m:dependency"
            tag = lambda t: f"m:{t}"  # noqa: E731
            subtag = lambda t: f"{{{_POM_NS_URI}}}{t}"  # noqa: E731
            prop_xpath = lambda name: f"./m:properties/m:{name}"  # noqa: E731
        else:
            ns = {}
            dep_xpath = ".//dependency"
            tag = lambda t: t  # noqa: E731
            subtag = lambda t: t  # noqa: E731
            prop_xpath = lambda name: f"./properties/{name}"  # noqa: E731

        parts = component.split(":")
        artifact_id = parts[-1]
        group_id = parts[0] if len(parts) > 1 else None

        found = False
        for dep in root.findall(dep_xpath, ns):
            aid_el = dep.find(tag("artifactId"), ns)
            gid_el = dep.find(tag("groupId"), ns)
            ver_el = dep.find(tag("version"), ns)

            if aid_el is None:
                continue

            aid_match = aid_el.text == artifact_id
            gid_match = group_id is None or (gid_el is not None and gid_el.text == group_id)
            if not (aid_match and gid_match):
                continue

            if ver_el is None:
                # BOM-managed — add an explicit version override.
                ET.SubElement(dep, subtag("version")).text = new_version
                found = True
                logger.info(
                    "pom.xml: %s added explicit version %s (was BOM-managed)",
                    component, new_version,
                )
                break

            ver_text = ver_el.text or ""

            if ver_text.startswith("${") and ver_text.endswith("}"):
                # Maven property reference — update the property value in <properties>.
                prop_name = ver_text[2:-1]
                prop_el = root.find(prop_xpath(prop_name), ns)
                if prop_el is not None:
                    logger.info(
                        "pom.xml: property %s %s → %s",
                        prop_name, prop_el.text, new_version,
                    )
                    prop_el.text = new_version
                else:
                    # Property not found — inline the version directly.
                    logger.info(
                        "pom.xml: %s inlining version (property %s not found)",
                        component, prop_name,
                    )
                    ver_el.text = new_version
                found = True
                break

            if ver_text == old_version:
                ver_el.text = new_version
                found = True
                logger.info("pom.xml: %s %s → %s", component, old_version, new_version)
                break

        if not found:
            raise PomXMLError(f"Dependency {component}@{old_version} not found in pom.xml.")

        tree.write(str(pom_path), xml_declaration=True, encoding="utf-8")
        self.manifest_file = "pom.xml"

    # ── build / test_unit ─────────────────────────────────────────────────────

    def build(self, repo_path: Path) -> BuildResult:
        try:
            result = subprocess.run(
                ["mvn", "compile", "-q", "--batch-mode"],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=300,
            )
        except FileNotFoundError:
            return BuildResult(
                success=False,
                output="ERROR: mvn not found — Maven must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return BuildResult(success=False, output="ERROR: mvn compile timed out after 300 seconds.")

        if result.returncode == 0:
            return BuildResult(success=True, output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return BuildResult(success=False, output=output)

    def test_unit(self, repo_path: Path) -> TestResult:
        test_root = repo_path / "src" / "test" / "java"
        has_tests = test_root.exists() and any(
            _is_unit_test_file(f.name) for f in test_root.rglob("*.java")
        )
        if not has_tests:
            return TestResult(status="NO_TESTS_FOUND", output="")

        try:
            result = subprocess.run(
                [
                    "mvn", "test", "-q", "--batch-mode",
                    "-Dexclude=**/*IT.java,**/*IntegrationTest.java",
                ],
                cwd=str(repo_path),
                capture_output=True,
                text=True,
                timeout=600,
            )
        except FileNotFoundError:
            return TestResult(
                status="ERROR",
                output="ERROR: mvn not found — Maven must be installed in the container image.",
            )
        except subprocess.TimeoutExpired:
            return TestResult(status="ERROR", output="ERROR: mvn test timed out after 600 seconds.")

        if result.returncode == 0:
            return TestResult(status="SUCCESS", output="")

        output = f"STDERR:\n{result.stderr}"
        if result.stdout.strip():
            output += f"\n\nSTDOUT:\n{result.stdout}"
        return TestResult(status="FAILED", output=output)
