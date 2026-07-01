"""
FIX-01: Nexus IQ client — fetch and parse vulnerability scan reports.

IMPORTANT — ENDPOINT CONTRACT NOT YET CONFIRMED:
The Nexus IQ REST API endpoint paths and response field names below are based on
Sonatype's public documentation (https://help.sonatype.com/en/rest-api.html).
Before relying on this in production, validate:
  1. Which Nexus product the customer's scans use (Nexus IQ Server vs. Nexus Repository).
  2. The exact endpoint path for fetching policy violation / vulnerability reports.
  3. The exact JSON response shape (field names marked with FIXME below).
Run against a real Nexus IQ instance and compare the raw response to the parsing logic here.
"""

import os
from dataclasses import dataclass, field
from typing import Optional

import requests


class NexusAuthError(Exception):
    """Raised when the Nexus IQ server rejects our credentials (401/403)."""


class NexusReportParseError(Exception):
    """Raised when the API returns a response shape we cannot parse safely."""


@dataclass
class VulnerabilityFinding:
    component_name: str
    current_version: str
    recommended_version: str
    severity: str                   # critical | high | medium | low
    cve_ids: list = field(default_factory=list)


class NexusIQClient:
    """
    Authenticates to Nexus IQ Server and retrieves parsed vulnerability findings
    for a given application public ID.
    """

    def __init__(self, base_url: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or os.environ["NEXUS_IQ_ENDPOINT"]).rstrip("/")
        self._api_key = api_key or os.environ["NEXUS_IQ_API_KEY"]
        self._session = requests.Session()
        # Nexus IQ uses HTTP Basic auth with a service account username + API token.
        # FIXME: Confirm whether the customer's Nexus IQ uses basic auth (user:token)
        # or a Bearer token header. Adjust accordingly.
        self._session.auth = ("service-account", self._api_key)
        self._session.headers.update({"Accept": "application/json"})

    # ── Public API ────────────────────────────────────────────────────────────

    def get_vulnerability_report(self, application_public_id: str) -> list:
        """
        Fetch the latest policy violation report for `application_public_id` and
        return a list of VulnerabilityFinding objects.

        Returns an empty list if the report exists but contains no violations.

        FIXME: The endpoint path below is based on Nexus IQ REST API v2 docs.
        Confirm the exact path with the customer's Nexus IQ version before trusting.
        """
        report_id = self._get_latest_report_id(application_public_id)
        if report_id is None:
            return []

        # FIXME: Confirm endpoint. Documented path is:
        # GET /api/v2/applications/{applicationId}/reports/{reportId}/policy
        url = f"{self.base_url}/applications/{application_public_id}/reports/{report_id}/policy"
        response = self._get(url)
        return self._parse_policy_report(response.json())

    # ── Private helpers ───────────────────────────────────────────────────────

    def _get_latest_report_id(self, application_public_id: str) -> Optional[str]:
        """
        Retrieve the most recent completed scan report ID for this application.

        FIXME: Confirm endpoint. Documented path is:
        GET /api/v2/reports/applications/{applicationId}
        Returns a list of reports sorted by evaluation date descending.
        """
        url = f"{self.base_url}/reports/applications/{application_public_id}"
        response = self._get(url)
        reports = response.json()

        if not reports:
            return None

        # FIXME: Confirm field name. Docs suggest 'reportId' or 'reportHtmlUrl'.
        latest = reports[0]
        report_id = latest.get("reportId") or latest.get("reportHtmlUrl", "").split("/")[-1]
        if not report_id:
            raise NexusReportParseError(
                f"Cannot extract report ID from response: {latest!r}. "
                "Confirm response schema against Nexus IQ REST API docs."
            )
        return report_id

    def _get(self, url: str) -> requests.Response:
        """Execute a GET request with timeout and error handling."""
        try:
            response = self._session.get(url, timeout=30)
        except requests.RequestException as exc:
            raise NexusAuthError(f"Network error calling Nexus IQ: {exc}") from exc

        if response.status_code in (401, 403):
            raise NexusAuthError(
                f"Nexus IQ rejected credentials (HTTP {response.status_code}). "
                "Check NEXUS_IQ_API_KEY and the service account permissions."
            )
        response.raise_for_status()
        return response

    def _parse_policy_report(self, data: dict) -> list:
        """
        Parse a Nexus IQ policy report JSON blob into a list of VulnerabilityFinding.

        FIXME: The field names below are based on Nexus IQ documentation.
        They MUST be validated against a real API response before trusting.
        Key fields to confirm: 'components', component 'hash'/'packageUrl',
        'violations', violation 'policyName'/'severity', 'constraintViolations'.
        """
        if not isinstance(data, dict):
            raise NexusReportParseError(
                f"Expected a dict at root of policy report, got {type(data).__name__}. "
                "Confirm response schema."
            )

        # FIXME: Confirm top-level key. May be 'components' or 'aaData' depending on version.
        components = data.get("components", [])

        findings = []
        for component in components:
            violations = component.get("violations", [])
            if not violations:
                continue

            # FIXME: Confirm component coordinate field names.
            # Nexus IQ typically uses packageUrl (purl) format for Maven components.
            package_url = component.get("packageUrl", "")
            component_name, current_version = self._parse_package_url(package_url)

            # Determine highest severity across all violations on this component
            severity = self._highest_severity(violations)

            # FIXME: Confirm where recommended safe version lives.
            # It may be in violation.remediationRecommendations or a separate API call.
            recommended_version = self._extract_recommended_version(violations)

            cve_ids = self._extract_cve_ids(violations)

            findings.append(VulnerabilityFinding(
                component_name=component_name,
                current_version=current_version,
                recommended_version=recommended_version,
                severity=severity,
                cve_ids=cve_ids,
            ))

        return findings

    def _parse_package_url(self, package_url: str):
        """
        Extract component name and version from a package URL (purl).
        Maven purl format: pkg:maven/group.id/artifact-id@version
        FIXME: Confirm purl format used by this customer's Nexus IQ instance.
        """
        if not package_url:
            return "unknown-component", "unknown"
        try:
            # pkg:maven/org.example/my-lib@1.2.3
            after_type = package_url.split("/", 1)[1]
            name_version = after_type.rsplit("@", 1)
            name = name_version[0].replace("/", ":")
            version = name_version[1] if len(name_version) > 1 else "unknown"
            return name, version
        except (IndexError, ValueError):
            return package_url, "unknown"

    def _highest_severity(self, violations: list) -> str:
        order = {"critical": 4, "high": 3, "medium": 2, "low": 1}
        best = "low"
        for v in violations:
            # FIXME: Confirm field name — may be 'policyThreatLevel' or 'severity'.
            sev = v.get("severity", v.get("policyThreatLevel", "low")).lower()
            if order.get(sev, 0) > order.get(best, 0):
                best = sev
        return best

    def _extract_recommended_version(self, violations: list) -> str:
        """
        FIXME: Nexus IQ's remediation recommendation API may be a separate endpoint.
        GET /api/v2/components/remediation/application/{appId}?packageUrl={purl}
        For now, return a placeholder. Replace with real lookup once endpoint is confirmed.
        """
        for v in violations:
            recs = v.get("remediationRecommendations", {})
            if recs:
                # FIXME: Confirm nested structure of remediationRecommendations.
                return recs.get("recommendedVersion", "UNKNOWN — check Nexus IQ remediation API")
        return "UNKNOWN — check Nexus IQ remediation API"

    def _extract_cve_ids(self, violations: list) -> list:
        cve_ids = []
        for v in violations:
            # FIXME: Confirm field path. May be nested under constraintViolations[].cause.
            constraints = v.get("constraintViolations", [])
            for c in constraints:
                ref = c.get("reference", {})
                if ref.get("type", "") == "CVE":
                    cve_ids.append(ref.get("value", ""))
        return [c for c in cve_ids if c]


# ── Factory ───────────────────────────────────────────────────────────────────

def make_vulnerability_source():
    """
    Return the appropriate vulnerability source based on DEPLOYMENT_MODE — same
    switch used by common/tracking_store.py's make_tracking_store() and
    common/knowledge_store.py's make_knowledge_store().

    DEPLOYMENT_MODE=azure (or NEXUS_IQ_ENDPOINT set without explicit mode) → NexusIQClient
    DEPLOYMENT_MODE=local (or no NEXUS_IQ_ENDPOINT set)                    → ScanReportClient

    ScanReportClient reads pre-generated Trivy/Grype/OWASP JSON reports from
    SCAN_REPORT_PATH instead of calling a live Nexus IQ Server — useful for local
    testing against a repo that doesn't have Nexus IQ access. Both expose the same
    get_vulnerability_report(app_id) -> list[VulnerabilityFinding] interface, so
    callers do not need to branch on which one they got.
    """
    # Imported lazily to avoid a circular import: scan_report_client.py imports
    # VulnerabilityFinding from this module at module level.
    from scan_report_client import ScanReportClient

    mode = os.environ.get("DEPLOYMENT_MODE", "")
    use_nexus = mode == "azure" or (not mode and bool(os.environ.get("NEXUS_IQ_ENDPOINT")))
    if use_nexus:
        return NexusIQClient()
    return ScanReportClient()
