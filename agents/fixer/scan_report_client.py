"""
Scan report client — parses Trivy, Grype, and OWASP Dependency-Check JSON reports into
VulnerabilityFinding objects. This is the local-mode / non-Azure counterpart to
NexusIQClient: same VulnerabilityFinding shape, same get_vulnerability_report(app_id)
signature, so main.py needs no branching beyond the make_vulnerability_source() factory
in nexus_client.py.

Report path is configured via SCAN_REPORT_PATH env var (a directory containing one or
more of: trivy-report.json, grype-report.json, dependency-check-report/dependency-check-report.json).
When multiple reports are present, findings are merged and deduplicated by
(component_name, current_version); Trivy's fix version wins over Grype's if both are
present, since Trivy was the first one merged in.
"""

import json
import logging
import os
from pathlib import Path
from typing import Optional

from nexus_client import VulnerabilityFinding

logger = logging.getLogger(__name__)


class ScanReportError(Exception):
    """Raised when no readable report files are found at the configured path."""


class ScanReportClient:
    """
    Reads vulnerability findings from scanner JSON reports on the local filesystem —
    the DEPLOYMENT_MODE=local counterpart to NexusIQClient.
    """

    TRIVY_FILE = "trivy-report.json"
    GRYPE_FILE = "grype-report.json"
    # The GitHub Actions artifact preserves directory structure, so the OWASP report
    # lands at <report_dir>/dependency-check-report/dependency-check-report.json.
    OWASP_FILE = "dependency-check-report/dependency-check-report.json"

    def __init__(self, report_dir: Optional[str] = None):
        self._report_dir = Path(report_dir or os.environ["SCAN_REPORT_PATH"])

    # ── Public API ────────────────────────────────────────────────────────────

    def get_vulnerability_report(self, _app_id: str = "") -> list:
        """
        Return a list of VulnerabilityFinding objects merged from all available reports.

        `_app_id` is accepted for API compatibility with NexusIQClient but is unused —
        the report path is fixed at construction time via SCAN_REPORT_PATH.
        """
        findings: dict = {}  # (component_name, current_version) → VulnerabilityFinding

        trivy_path = self._report_dir / self.TRIVY_FILE
        grype_path = self._report_dir / self.GRYPE_FILE
        owasp_path = self._report_dir / self.OWASP_FILE

        if trivy_path.exists():
            for f in self._parse_trivy(trivy_path):
                key = (f.component_name, f.current_version)
                findings.setdefault(key, f)
        else:
            logger.debug("Trivy report not found at %s", trivy_path)

        if grype_path.exists():
            for f in self._parse_grype(grype_path):
                key = (f.component_name, f.current_version)
                existing = findings.get(key)
                if existing:
                    for cve in f.cve_ids:
                        if cve not in existing.cve_ids:
                            existing.cve_ids.append(cve)
                    if existing.recommended_version.startswith("UNKNOWN") and \
                       not f.recommended_version.startswith("UNKNOWN"):
                        existing.recommended_version = f.recommended_version
                else:
                    findings[key] = f
        else:
            logger.debug("Grype report not found at %s", grype_path)

        if owasp_path.exists():
            for f in self._parse_owasp(owasp_path):
                key = (f.component_name, f.current_version)
                findings.setdefault(key, f)
        else:
            logger.debug("OWASP report not found at %s", owasp_path)

        if not findings:
            raise ScanReportError(
                f"No readable report files found in {self._report_dir}. "
                f"Expected one or more of: {self.TRIVY_FILE}, {self.GRYPE_FILE}, {self.OWASP_FILE}."
            )

        result = list(findings.values())
        logger.info("Loaded %d unique findings from %s", len(result), self._report_dir)
        return result

    # ── Trivy parser ──────────────────────────────────────────────────────────

    def _parse_trivy(self, path: Path) -> list:
        """
        Trivy JSON format:
          { "Results": [ { "Vulnerabilities": [ {
              "VulnerabilityID": "CVE-...",
              "PkgName": "log4j-core",
              "PkgIdentifier": {"PURL": "pkg:maven/..."},
              "InstalledVersion": "2.14.1",
              "FixedVersion": "2.20.0",
              "Severity": "CRITICAL"
          } ] } ] }
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse Trivy report: %s", exc)
            return []

        findings: dict = {}
        for result in data.get("Results", []):
            for vuln in result.get("Vulnerabilities") or []:
                cve_id = vuln.get("VulnerabilityID", "")
                severity = vuln.get("Severity", "UNKNOWN").lower()
                fixed_ver = vuln.get("FixedVersion", "")
                installed = vuln.get("InstalledVersion", "unknown")

                purl = (vuln.get("PkgIdentifier") or {}).get("PURL", "")
                name = self._name_from_purl(purl) or vuln.get("PkgName", "unknown")

                key = (name, installed)
                if key not in findings:
                    findings[key] = VulnerabilityFinding(
                        component_name=name,
                        current_version=installed,
                        recommended_version=fixed_ver or "UNKNOWN — check Trivy FixedVersion",
                        severity=severity,
                        cve_ids=[cve_id] if cve_id else [],
                    )
                else:
                    if cve_id and cve_id not in findings[key].cve_ids:
                        findings[key].cve_ids.append(cve_id)

        return list(findings.values())

    # ── Grype parser ──────────────────────────────────────────────────────────

    def _parse_grype(self, path: Path) -> list:
        """
        Grype JSON format:
          { "matches": [ {
              "vulnerability": {
                  "id": "CVE-...",
                  "severity": "Critical",
                  "fix": { "versions": ["2.20.0"], "state": "fixed" }
              },
              "artifact": {
                  "name": "log4j-core",
                  "version": "2.14.1",
                  "type": "java-archive",
                  "purl": "pkg:maven/..."
              }
          } ] }
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse Grype report: %s", exc)
            return []

        findings: dict = {}
        for match in data.get("matches", []):
            vuln = match.get("vulnerability", {})
            artifact = match.get("artifact", {})

            cve_id = vuln.get("id", "")
            severity = vuln.get("severity", "UNKNOWN").lower()
            fix_info = vuln.get("fix", {})
            fix_vers = fix_info.get("versions", [])
            fixed_ver = fix_vers[0] if fix_vers else "UNKNOWN — check Grype fix.versions"

            purl = artifact.get("purl", "")
            name = self._name_from_purl(purl) or artifact.get("name", "unknown")
            installed = artifact.get("version", "unknown")

            key = (name, installed)
            if key not in findings:
                findings[key] = VulnerabilityFinding(
                    component_name=name,
                    current_version=installed,
                    recommended_version=fixed_ver,
                    severity=severity,
                    cve_ids=[cve_id] if cve_id else [],
                )
            else:
                if cve_id and cve_id not in findings[key].cve_ids:
                    findings[key].cve_ids.append(cve_id)

        return list(findings.values())

    # ── OWASP Dependency-Check parser ─────────────────────────────────────────

    def _parse_owasp(self, path: Path) -> list:
        """
        OWASP DC JSON format:
          { "dependencies": [ {
              "packages": [{"id": "pkg:maven/...@version"}],
              "vulnerabilities": [{
                  "name": "CVE-...",
                  "severity": "CRITICAL",
                  "cvssv3": {"baseScore": 10.0}
              }]
          } ] }
        """
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not parse OWASP report: %s", exc)
            return []

        findings: dict = {}
        for dep in data.get("dependencies", []):
            vulns = dep.get("vulnerabilities", [])
            if not vulns:
                continue

            packages = dep.get("packages", [])
            purl = packages[0].get("id", "") if packages else ""
            name, installed = self._parse_purl(purl)

            severity = self._highest_severity([v.get("severity", "low") for v in vulns])
            cve_ids = [v["name"] for v in vulns if v.get("name", "").startswith("CVE")]

            key = (name, installed)
            if key not in findings:
                findings[key] = VulnerabilityFinding(
                    component_name=name,
                    current_version=installed,
                    recommended_version="UNKNOWN — check NVD or scanner for safe version",
                    severity=severity,
                    cve_ids=cve_ids,
                )

        return list(findings.values())

    # ── Helpers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _name_from_purl(purl: str) -> str:
        """
        Extract a Maven groupId:artifactId name from a package URL.
        pkg:maven/org.apache.logging.log4j/log4j-core@2.14.1
        → org.apache.logging.log4j:log4j-core
        """
        if not purl or not purl.startswith("pkg:maven/"):
            return ""
        try:
            after_type = purl[len("pkg:maven/"):]
            name_part = after_type.split("@")[0]
            return name_part.replace("/", ":")
        except (IndexError, ValueError):
            return ""

    @staticmethod
    def _parse_purl(purl: str):
        """Extract (component_name, version) from a purl."""
        if not purl:
            return "unknown-component", "unknown"
        try:
            after_type = purl.split("/", 1)[1] if "/" in purl else purl
            name_version = after_type.rsplit("@", 1)
            name = name_version[0].replace("/", ":")
            version = name_version[1] if len(name_version) > 1 else "unknown"
            return name, version
        except (IndexError, ValueError):
            return purl, "unknown"

    @staticmethod
    def _highest_severity(severities: list) -> str:
        order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        best = "low"
        for s in severities:
            sev = s.lower()
            if order.get(sev, 0) > order.get(best, 0):
                best = sev
        return best
