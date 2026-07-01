"""
Unit tests for scan_report_client.py — the local-mode (DEPLOYMENT_MODE=local)
counterpart to NexusIQClient. Uses small representative Trivy/Grype/OWASP JSON
fixtures written to a temp directory — no real scanner CLI or network calls.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from scan_report_client import ScanReportClient, ScanReportError

MOCK_TRIVY_REPORT = {
    "Results": [
        {
            "Vulnerabilities": [
                {
                    "VulnerabilityID": "CVE-2021-44228",
                    "PkgName": "log4j-core",
                    "PkgIdentifier": {
                        "PURL": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"
                    },
                    "InstalledVersion": "2.14.1",
                    "FixedVersion": "2.20.0",
                    "Severity": "CRITICAL",
                },
                {
                    "VulnerabilityID": "CVE-2021-45046",
                    "PkgName": "log4j-core",
                    "PkgIdentifier": {
                        "PURL": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1"
                    },
                    "InstalledVersion": "2.14.1",
                    "FixedVersion": "2.20.0",
                    "Severity": "CRITICAL",
                },
            ]
        }
    ]
}

MOCK_GRYPE_REPORT = {
    "matches": [
        {
            "vulnerability": {
                "id": "CVE-2015-6420",
                "severity": "High",
                "fix": {"versions": ["3.2.2"], "state": "fixed"},
            },
            "artifact": {
                "name": "commons-collections",
                "version": "3.2.1",
                "type": "java-archive",
                "purl": "pkg:maven/commons-collections/commons-collections@3.2.1",
            },
        },
        # Same component as the Trivy fixture above — should merge, not duplicate.
        {
            "vulnerability": {
                "id": "CVE-2021-45105",
                "severity": "Critical",
                "fix": {"versions": [], "state": "not-fixed"},
            },
            "artifact": {
                "name": "log4j-core",
                "version": "2.14.1",
                "type": "java-archive",
                "purl": "pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1",
            },
        },
    ]
}

MOCK_OWASP_REPORT = {
    "dependencies": [
        {
            "packages": [{"id": "pkg:maven/mysql/mysql-connector-java@5.1.35"}],
            "vulnerabilities": [
                {"name": "CVE-2018-3258", "severity": "HIGH", "cvssv3": {"baseScore": 8.5}}
            ],
        },
        {
            # No vulnerabilities — must be skipped, not turned into an empty finding.
            "packages": [{"id": "pkg:maven/com.example/clean-lib@1.0.0"}],
            "vulnerabilities": [],
        },
    ]
}


@pytest.fixture
def report_dir():
    tmpdir = tempfile.mkdtemp(prefix="test-scan-reports-")
    yield Path(tmpdir)
    shutil.rmtree(tmpdir, ignore_errors=True)


def _write(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


class TestTrivyOnly:
    def test_parses_findings_and_merges_cve_ids(self, report_dir):
        _write(report_dir / "trivy-report.json", MOCK_TRIVY_REPORT)
        client = ScanReportClient(str(report_dir))
        findings = client.get_vulnerability_report()

        assert len(findings) == 1
        log4j = findings[0]
        assert log4j.component_name == "org.apache.logging.log4j:log4j-core"
        assert log4j.current_version == "2.14.1"
        assert log4j.recommended_version == "2.20.0"
        assert log4j.severity == "critical"
        assert set(log4j.cve_ids) == {"CVE-2021-44228", "CVE-2021-45046"}


class TestGrypeMergesWithTrivy:
    def test_same_component_merges_cves_without_duplicating(self, report_dir):
        _write(report_dir / "trivy-report.json", MOCK_TRIVY_REPORT)
        _write(report_dir / "grype-report.json", MOCK_GRYPE_REPORT)
        client = ScanReportClient(str(report_dir))
        findings = client.get_vulnerability_report()

        # log4j-core appears in both reports at the same version — must merge into one.
        log4j_matches = [f for f in findings if "log4j-core" in f.component_name]
        assert len(log4j_matches) == 1
        log4j = log4j_matches[0]
        assert "CVE-2021-44228" in log4j.cve_ids
        assert "CVE-2021-45046" in log4j.cve_ids
        assert "CVE-2021-45105" in log4j.cve_ids  # only in Grype
        # Trivy's concrete fix version must win over Grype's empty fix.versions.
        assert log4j.recommended_version == "2.20.0"

    def test_grype_only_component_is_included(self, report_dir):
        _write(report_dir / "trivy-report.json", MOCK_TRIVY_REPORT)
        _write(report_dir / "grype-report.json", MOCK_GRYPE_REPORT)
        client = ScanReportClient(str(report_dir))
        findings = client.get_vulnerability_report()

        commons = next(f for f in findings if "commons-collections" in f.component_name)
        assert commons.current_version == "3.2.1"
        assert commons.recommended_version == "3.2.2"
        assert commons.severity == "high"


class TestOwaspFallback:
    def test_owasp_report_parsed_from_nested_path(self, report_dir):
        _write(
            report_dir / "dependency-check-report" / "dependency-check-report.json",
            MOCK_OWASP_REPORT,
        )
        client = ScanReportClient(str(report_dir))
        findings = client.get_vulnerability_report()

        assert len(findings) == 1
        mysql = findings[0]
        assert mysql.component_name == "mysql:mysql-connector-java"
        assert mysql.current_version == "5.1.35"
        assert "CVE-2018-3258" in mysql.cve_ids

    def test_dependency_with_no_vulnerabilities_is_skipped(self, report_dir):
        _write(
            report_dir / "dependency-check-report" / "dependency-check-report.json",
            MOCK_OWASP_REPORT,
        )
        client = ScanReportClient(str(report_dir))
        findings = client.get_vulnerability_report()

        assert not any("clean-lib" in f.component_name for f in findings)


class TestNoReportsFound:
    def test_missing_all_reports_raises_scan_report_error(self, report_dir):
        client = ScanReportClient(str(report_dir))
        with pytest.raises(ScanReportError):
            client.get_vulnerability_report()

    def test_malformed_json_is_skipped_not_crashed(self, report_dir):
        (report_dir / "trivy-report.json").write_text("{not valid json", encoding="utf-8")
        _write(report_dir / "grype-report.json", MOCK_GRYPE_REPORT)
        client = ScanReportClient(str(report_dir))

        # Malformed Trivy report is logged and skipped; Grype findings still load.
        findings = client.get_vulnerability_report()
        assert len(findings) == 2


class TestReportPathFromEnv:
    def test_uses_scan_report_path_env_var_when_no_arg_given(self, report_dir, monkeypatch):
        _write(report_dir / "trivy-report.json", MOCK_TRIVY_REPORT)
        monkeypatch.setenv("SCAN_REPORT_PATH", str(report_dir))

        client = ScanReportClient()
        findings = client.get_vulnerability_report()
        assert len(findings) == 1
