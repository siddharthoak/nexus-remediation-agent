"""
Unit tests for the agents/fixer/frameworks/ build framework abstraction.

bump_dependency() tests exercise real file I/O (no subprocess involved) so they are fully
deterministic. build()/test_unit() tests mock subprocess.run so they don't depend on which
build tools happen to be installed on the machine running the suite.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from frameworks import detect_framework
from frameworks.maven import MavenFramework, PomXMLError
from frameworks.gradle import GradleFramework, BuildGradleError
from frameworks.npm import NpmFramework, PackageJsonError
from frameworks.python_pip import PythonFramework, RequirementsError


@pytest.fixture
def repo_dir():
    tmpdir = tempfile.mkdtemp(prefix="test-framework-repo-")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _mock_completed(returncode=0, stdout="", stderr=""):
    result = MagicMock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


# ── detect_framework priority ordering ─────────────────────────────────────────

class TestDetectFramework:
    def test_maven_wins_over_npm_in_polyglot_repo(self, repo_dir):
        (repo_dir / "pom.xml").write_text("<project/>", encoding="utf-8")
        (repo_dir / "package.json").write_text("{}", encoding="utf-8")

        framework = detect_framework(repo_dir)
        assert framework.name == "maven"

    def test_gradle_wins_over_npm(self, repo_dir):
        (repo_dir / "build.gradle").write_text("", encoding="utf-8")
        (repo_dir / "package.json").write_text("{}", encoding="utf-8")

        framework = detect_framework(repo_dir)
        assert framework.name == "gradle"

    def test_npm_wins_over_python(self, repo_dir):
        (repo_dir / "package.json").write_text("{}", encoding="utf-8")
        (repo_dir / "requirements.txt").write_text("", encoding="utf-8")

        framework = detect_framework(repo_dir)
        assert framework.name == "npm"

    def test_python_detected_via_pyproject_toml(self, repo_dir):
        (repo_dir / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")

        framework = detect_framework(repo_dir)
        assert framework.name == "python"

    def test_no_framework_detected_returns_none(self, repo_dir):
        assert detect_framework(repo_dir) is None


# ── Maven ───────────────────────────────────────────────────────────────────────

SAMPLE_POM_XML = """\
<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <groupId>com.example</groupId>
  <artifactId>my-app</artifactId>
  <version>1.0.0</version>
  <properties>
    <spring.version>5.3.0</spring.version>
  </properties>
  <dependencies>
    <dependency>
      <groupId>org.example</groupId>
      <artifactId>log4j</artifactId>
      <version>1.2.17</version>
    </dependency>
    <dependency>
      <groupId>org.springframework</groupId>
      <artifactId>spring-core</artifactId>
      <version>${spring.version}</version>
    </dependency>
    <dependency>
      <groupId>com.example.bom</groupId>
      <artifactId>bom-managed-lib</artifactId>
    </dependency>
  </dependencies>
</project>
"""


class TestMavenFramework:
    def test_detect_requires_pom_xml(self, repo_dir):
        assert MavenFramework.detect(repo_dir) is False
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        assert MavenFramework.detect(repo_dir) is True

    def test_bump_literal_version(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        fw.bump_dependency(repo_dir, "org.example:log4j", "1.2.17", "2.20.0")

        content = (repo_dir / "pom.xml").read_text()
        assert "<version>2.20.0</version>" in content
        assert fw.manifest_file == "pom.xml"

    def test_bump_property_reference(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        fw.bump_dependency(repo_dir, "org.springframework:spring-core", "5.3.0", "6.1.0")

        content = (repo_dir / "pom.xml").read_text()
        assert "<spring.version>6.1.0</spring.version>" in content

    def test_bump_bom_managed_adds_explicit_version(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        fw.bump_dependency(repo_dir, "com.example.bom:bom-managed-lib", "1.0.0", "2.0.0")

        content = (repo_dir / "pom.xml").read_text()
        assert "<version>2.0.0</version>" in content

    def test_missing_dependency_raises(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        with pytest.raises(PomXMLError):
            fw.bump_dependency(repo_dir, "org.nonexistent:library", "1.0.0", "2.0.0")

    def test_test_unit_no_tests_found_when_no_test_dir(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        result = fw.test_unit(repo_dir)
        assert result.status == "NO_TESTS_FOUND"

    def test_test_unit_excludes_integration_tests(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        test_dir = repo_dir / "src" / "test" / "java"
        test_dir.mkdir(parents=True)
        (test_dir / "FooIT.java").write_text("class FooIT {}", encoding="utf-8")
        (test_dir / "BarIntegrationTest.java").write_text("class BarIntegrationTest {}", encoding="utf-8")

        fw = MavenFramework()
        # Only IT/IntegrationTest files exist — none are unit tests, so NO_TESTS_FOUND
        # without ever invoking `mvn test`.
        with patch("frameworks.maven.subprocess.run") as mock_run:
            result = fw.test_unit(repo_dir)
            mock_run.assert_not_called()
        assert result.status == "NO_TESTS_FOUND"

    def test_build_success(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        with patch("frameworks.maven.subprocess.run", return_value=_mock_completed(returncode=0)):
            result = fw.build(repo_dir)
        assert result.success is True

    def test_build_failure_surfaces_stderr(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        with patch(
            "frameworks.maven.subprocess.run",
            return_value=_mock_completed(returncode=1, stderr="cannot find symbol: method foo()"),
        ):
            result = fw.build(repo_dir)
        assert result.success is False
        assert "cannot find symbol" in result.output

    def test_build_missing_mvn_degrades_gracefully(self, repo_dir):
        (repo_dir / "pom.xml").write_text(SAMPLE_POM_XML, encoding="utf-8")
        fw = MavenFramework()
        with patch("frameworks.maven.subprocess.run", side_effect=FileNotFoundError):
            result = fw.build(repo_dir)
        assert result.success is False
        assert "mvn not found" in result.output


# ── Gradle ────────────────────────────────────────────────────────────────────

class TestGradleFramework:
    def test_bump_literal_declaration(self, repo_dir):
        (repo_dir / "build.gradle").write_text(
            "dependencies {\n    implementation 'org.example:log4j:1.2.17'\n}\n",
            encoding="utf-8",
        )
        fw = GradleFramework()
        fw.bump_dependency(repo_dir, "org.example:log4j", "1.2.17", "2.20.0")

        content = (repo_dir / "build.gradle").read_text()
        assert "org.example:log4j:2.20.0" in content
        assert fw.manifest_file == "build.gradle"

    def test_bump_via_version_catalog_when_unambiguous(self, repo_dir):
        (repo_dir / "build.gradle").write_text(
            "dependencies {\n    implementation libs.log4j\n}\n", encoding="utf-8"
        )
        catalog_dir = repo_dir / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(
            '[versions]\nlog4j = "1.2.17"\n', encoding="utf-8"
        )
        fw = GradleFramework()
        fw.bump_dependency(repo_dir, "org.example:log4j", "1.2.17", "2.20.0")

        content = (catalog_dir / "libs.versions.toml").read_text()
        assert '"2.20.0"' in content
        assert fw.manifest_file == "gradle/libs.versions.toml"

    def test_bump_refuses_ambiguous_catalog_match(self, repo_dir):
        (repo_dir / "build.gradle").write_text("dependencies {}\n", encoding="utf-8")
        catalog_dir = repo_dir / "gradle"
        catalog_dir.mkdir()
        (catalog_dir / "libs.versions.toml").write_text(
            '[versions]\nlog4j = "1.2.17"\nguava = "1.2.17"\n', encoding="utf-8"
        )
        fw = GradleFramework()
        with pytest.raises(BuildGradleError):
            fw.bump_dependency(repo_dir, "org.example:log4j", "1.2.17", "2.20.0")

    def test_missing_dependency_raises(self, repo_dir):
        (repo_dir / "build.gradle").write_text("dependencies {}\n", encoding="utf-8")
        fw = GradleFramework()
        with pytest.raises(BuildGradleError):
            fw.bump_dependency(repo_dir, "org.example:log4j", "1.2.17", "2.20.0")

    def test_build_degrades_gracefully_when_gradlew_absent(self, repo_dir):
        (repo_dir / "build.gradle").write_text("", encoding="utf-8")
        fw = GradleFramework()
        result = fw.build(repo_dir)
        assert result.success is False
        assert "gradlew" in result.output


# ── npm ───────────────────────────────────────────────────────────────────────

SAMPLE_PACKAGE_JSON = {
    "name": "my-app",
    "scripts": {"build": "tsc", "test": "jest"},
    "dependencies": {"lodash": "^4.17.15"},
    "devDependencies": {"typescript": "~5.0.0"},
}


class TestNpmFramework:
    def test_bump_preserves_range_prefix(self, repo_dir):
        (repo_dir / "package.json").write_text(json.dumps(SAMPLE_PACKAGE_JSON), encoding="utf-8")
        fw = NpmFramework()
        fw.bump_dependency(repo_dir, "lodash", "4.17.15", "4.17.21")

        data = json.loads((repo_dir / "package.json").read_text())
        assert data["dependencies"]["lodash"] == "^4.17.21"
        assert fw.manifest_file == "package.json"

    def test_bump_dev_dependency(self, repo_dir):
        (repo_dir / "package.json").write_text(json.dumps(SAMPLE_PACKAGE_JSON), encoding="utf-8")
        fw = NpmFramework()
        fw.bump_dependency(repo_dir, "typescript", "5.0.0", "5.4.0")

        data = json.loads((repo_dir / "package.json").read_text())
        assert data["devDependencies"]["typescript"] == "~5.4.0"

    def test_missing_dependency_raises(self, repo_dir):
        (repo_dir / "package.json").write_text(json.dumps(SAMPLE_PACKAGE_JSON), encoding="utf-8")
        fw = NpmFramework()
        with pytest.raises(PackageJsonError):
            fw.bump_dependency(repo_dir, "nonexistent-pkg", "1.0.0", "2.0.0")

    def test_test_unit_no_tests_found_when_no_test_script(self, repo_dir):
        pkg = {**SAMPLE_PACKAGE_JSON, "scripts": {"build": "tsc"}}
        (repo_dir / "package.json").write_text(json.dumps(pkg), encoding="utf-8")
        fw = NpmFramework()
        with patch("frameworks.npm.subprocess.run") as mock_run:
            result = fw.test_unit(repo_dir)
            mock_run.assert_not_called()
        assert result.status == "NO_TESTS_FOUND"

    def test_test_unit_success(self, repo_dir):
        (repo_dir / "package.json").write_text(json.dumps(SAMPLE_PACKAGE_JSON), encoding="utf-8")
        fw = NpmFramework()
        with patch("frameworks.npm.subprocess.run", return_value=_mock_completed(returncode=0)):
            result = fw.test_unit(repo_dir)
        assert result.status == "SUCCESS"


# ── Python ────────────────────────────────────────────────────────────────────

class TestPythonFramework:
    def test_bump_requirements_txt_pinned(self, repo_dir):
        (repo_dir / "requirements.txt").write_text(
            "flask==2.0.0\nrequests>=2.25.0\n# a comment\n", encoding="utf-8"
        )
        fw = PythonFramework()
        fw.bump_dependency(repo_dir, "flask", "2.0.0", "3.0.0")

        content = (repo_dir / "requirements.txt").read_text()
        assert "flask==3.0.0" in content
        assert "requests>=2.25.0" in content  # untouched
        assert "# a comment" in content
        assert fw.manifest_file == "requirements.txt"

    def test_bump_requirements_txt_case_and_dash_insensitive(self, repo_dir):
        (repo_dir / "requirements.txt").write_text("Flask_Login==0.6.0\n", encoding="utf-8")
        fw = PythonFramework()
        fw.bump_dependency(repo_dir, "flask-login", "0.6.0", "0.6.3")

        content = (repo_dir / "requirements.txt").read_text()
        assert "==0.6.3" in content

    def test_bump_requirements_txt_missing_raises(self, repo_dir):
        (repo_dir / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        fw = PythonFramework()
        with pytest.raises(RequirementsError):
            fw.bump_dependency(repo_dir, "nonexistent", "1.0.0", "2.0.0")

    def test_bump_pyproject_toml_project_dependencies(self, repo_dir):
        (repo_dir / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\ndependencies = ["flask==2.0.0", "requests>=2.25.0"]\n',
            encoding="utf-8",
        )
        fw = PythonFramework()
        fw.bump_dependency(repo_dir, "flask", "2.0.0", "3.0.0")

        content = (repo_dir / "pyproject.toml").read_text()
        assert "flask==3.0.0" in content
        assert "requests>=2.25.0" in content
        assert fw.manifest_file == "pyproject.toml"

    def test_test_unit_no_tests_collected_maps_to_no_tests_found(self, repo_dir):
        (repo_dir / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        fw = PythonFramework()
        with patch("frameworks.python_pip.subprocess.run", return_value=_mock_completed(returncode=5)):
            result = fw.test_unit(repo_dir)
        assert result.status == "NO_TESTS_FOUND"

    def test_test_unit_failure_surfaces_output(self, repo_dir):
        (repo_dir / "requirements.txt").write_text("flask==2.0.0\n", encoding="utf-8")
        fw = PythonFramework()
        with patch(
            "frameworks.python_pip.subprocess.run",
            return_value=_mock_completed(returncode=1, stdout="FAILED tests/test_foo.py::test_bar"),
        ):
            result = fw.test_unit(repo_dir)
        assert result.status == "FAILED"
        assert "test_foo.py" in result.output
