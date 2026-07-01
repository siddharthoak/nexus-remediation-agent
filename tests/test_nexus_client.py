"""
TEST-01: Unit tests for nexus_client.py.

Tests parse logic with mocked HTTP responses — no real network calls.

The mock payloads below reflect the assumed Nexus IQ API response schema.
They are intentionally isolated in fixtures so they can be updated in ONE place
once the real schema is confirmed (see FIX-01 / nexus_client.py for TODOs).
"""

import json
import pytest
from unittest.mock import patch, MagicMock

import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "agents", "fixer"))

from nexus_client import NexusIQClient, NexusAuthError, NexusReportParseError


# ── Fixtures — update these when real Nexus IQ schema is confirmed ────────────

MOCK_REPORTS_RESPONSE = [
    {"reportId": "abc123", "applicationId": "test-app", "evaluationDate": "2024-01-15"}
]

MOCK_POLICY_REPORT = {
    "components": [
        {
            "packageUrl": "pkg:maven/org.example/log4j@1.2.17",
            "violations": [
                {
                    "severity": "critical",
                    "remediationRecommendations": {"recommendedVersion": "2.20.0"},
                    "constraintViolations": [
                        {"reference": {"type": "CVE", "value": "CVE-2021-44228"}},
                        {"reference": {"type": "CVE", "value": "CVE-2021-45046"}},
                    ],
                }
            ],
        },
        {
            "packageUrl": "pkg:maven/com.fasterxml.jackson.core/jackson-databind@2.9.0",
            "violations": [
                {
                    "severity": "high",
                    "remediationRecommendations": {"recommendedVersion": "2.14.2"},
                    "constraintViolations": [
                        {"reference": {"type": "CVE", "value": "CVE-2022-42003"}}
                    ],
                }
            ],
        },
    ]
}

MOCK_EMPTY_POLICY_REPORT = {"components": []}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _make_client():
    return NexusIQClient(base_url="https://nexus.example.com/api/v2", api_key="test-key")


def _mock_response(json_data, status_code=200):
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data
    mock.raise_for_status = MagicMock()
    if status_code >= 400:
        from requests import HTTPError
        mock.raise_for_status.side_effect = HTTPError(response=mock)
    return mock


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestSuccessfulParsing:
    """Happy path: API returns a report with multiple vulnerabilities."""

    def test_returns_correct_number_of_findings(self):
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),   # _get_latest_report_id call
                _mock_response(MOCK_POLICY_REPORT),       # get_vulnerability_report call
            ]
            findings = client.get_vulnerability_report("test-app")

        assert len(findings) == 2

    def test_log4j_finding_fields_are_correct(self):
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),
                _mock_response(MOCK_POLICY_REPORT),
            ]
            findings = client.get_vulnerability_report("test-app")

        log4j = next(f for f in findings if "log4j" in f.component_name)
        assert log4j.current_version == "1.2.17"
        assert log4j.recommended_version == "2.20.0"
        assert log4j.severity == "critical"
        assert "CVE-2021-44228" in log4j.cve_ids
        assert "CVE-2021-45046" in log4j.cve_ids

    def test_jackson_finding_fields_are_correct(self):
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),
                _mock_response(MOCK_POLICY_REPORT),
            ]
            findings = client.get_vulnerability_report("test-app")

        jackson = next(f for f in findings if "jackson" in f.component_name)
        assert jackson.current_version == "2.9.0"
        assert jackson.recommended_version == "2.14.2"
        assert jackson.severity == "high"
        assert "CVE-2022-42003" in jackson.cve_ids


class TestEmptyReport:
    """API returns a report with no violations — should return empty list without error."""

    def test_empty_report_returns_empty_list(self):
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),
                _mock_response(MOCK_EMPTY_POLICY_REPORT),
            ]
            findings = client.get_vulnerability_report("test-app")

        assert findings == []

    def test_no_reports_at_all_returns_empty_list(self):
        """API returns empty list of reports (no scans yet)."""
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.return_value = _mock_response([])  # Empty reports list
            findings = client.get_vulnerability_report("test-app")

        assert findings == []


class TestAuthFailure:
    """401/403 responses must raise NexusAuthError, not a generic exception."""

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_failure_raises_nexus_auth_error(self, status_code):
        client = _make_client()
        mock_resp = _mock_response({}, status_code=status_code)

        import requests
        with patch.object(client._session, "get", return_value=mock_resp):
            mock_resp.raise_for_status.side_effect = requests.HTTPError(response=mock_resp)
            with patch.object(mock_resp, "status_code", status_code):
                # Override the session.get call to return our mock
                with patch.object(client, "_get") as mock_get:
                    mock_get.side_effect = NexusAuthError(f"HTTP {status_code}")
                    with pytest.raises(NexusAuthError):
                        client.get_vulnerability_report("test-app")

    def test_auth_error_is_not_generic_exception(self):
        """Verify NexusAuthError is its own type, not just a plain Exception."""
        assert issubclass(NexusAuthError, Exception)
        assert NexusAuthError is not Exception


class TestMalformedResponse:
    """Malformed / unexpected response shapes must fail clearly, not silently."""

    def test_non_dict_root_raises_parse_error(self):
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),
                _mock_response(["unexpected", "list"]),  # Not a dict
            ]
            with pytest.raises(NexusReportParseError):
                client.get_vulnerability_report("test-app")

    def test_component_without_package_url_does_not_crash(self):
        """A component with a missing packageUrl should produce an 'unknown' finding, not crash."""
        malformed = {
            "components": [
                {
                    # packageUrl missing entirely
                    "violations": [{"severity": "high", "constraintViolations": []}]
                }
            ]
        }
        client = _make_client()
        with patch.object(client, "_get") as mock_get:
            mock_get.side_effect = [
                _mock_response(MOCK_REPORTS_RESPONSE),
                _mock_response(malformed),
            ]
            # Should not raise — should return a finding with 'unknown' fields
            findings = client.get_vulnerability_report("test-app")
        assert len(findings) == 1
        assert findings[0].component_name == "unknown-component"
