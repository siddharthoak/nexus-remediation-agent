"""
ReleaseFetcher — collects migration-relevant text for a (component, from_version, to_version) tuple.

Sources (tried in order; partial results are fine):
  1. OSV.dev API — CVE details, affected/fixed versions, references
  2. GitHub Releases API — release notes for each version in the upgrade range
  3. GitHub Advisory Database — additional security context

No LLM calls here — this returns raw text that the Knowledge Agent feeds to Claude.
"""

import logging
import os
from typing import Optional

import requests

logger = logging.getLogger(__name__)

OSV_API    = "https://api.osv.dev/v1/query"
GITHUB_API = "https://api.github.com"

# Maps Maven groupId:artifactId to GitHub owner/repo for release note fetching.
# Absence is not an error — GitHub notes are skipped for unlisted deps.
MAVEN_TO_GITHUB: dict = {
    "org.apache.logging.log4j:log4j-core":              "apache/logging-log4j2",
    "org.apache.logging.log4j:log4j-api":               "apache/logging-log4j2",
    "org.apache.logging.log4j:log4j":                   "apache/logging-log4j2",
    "org.apache.log4j:log4j":                           "apache/log4j",
    "org.springframework.boot:spring-boot":             "spring-projects/spring-boot",
    "org.springframework:spring-core":                  "spring-projects/spring-framework",
    "org.springframework:spring-web":                   "spring-projects/spring-framework",
    "org.springframework:spring-webmvc":                "spring-projects/spring-framework",
    "commons-collections:commons-collections":          "apache/commons-collections",
    "org.apache.commons:commons-collections4":          "apache/commons-collections",
    "commons-codec:commons-codec":                      "apache/commons-codec",
    "org.apache.commons:commons-lang3":                 "apache/commons-lang",
    "com.fasterxml.jackson.core:jackson-databind":      "FasterXML/jackson-databind",
    "com.google.guava:guava":                           "google/guava",
    "org.yaml:snakeyaml":                               "snakeyaml/snakeyaml",
    "ch.qos.logback:logback-classic":                   "qos-ch/logback",
    "org.apache.struts:struts2-core":                   "apache/struts",
    "org.apache.httpcomponents:httpclient":             "apache/httpcomponents-client",
    "commons-fileupload:commons-fileupload":            "apache/commons-fileupload",
    "org.apache.commons:commons-compress":              "apache/commons-compress",
    "com.h2database:h2":                                "h2database/h2database",
    "org.bouncycastle:bcprov-jdk15on":                  "bcgit/bc-java",
}


class ReleaseFetcher:
    def __init__(self, github_pat: Optional[str] = None):
        self._gh_pat = github_pat or os.environ.get("GITHUB_PAT", "")
        self._gh_headers = {
            "Authorization":        f"Bearer {self._gh_pat}",
            "Accept":               "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def fetch(
        self,
        component_name: str,
        from_version: str,
        to_version: str,
        cve_ids: Optional[list] = None,
    ) -> str:
        """
        Returns a concatenated text blob with all fetched context.
        Empty string if nothing could be fetched (caller handles gracefully).
        """
        parts = []

        osv_text = self._fetch_osv(component_name, cve_ids or [])
        if osv_text:
            parts.append("=== OSV Vulnerability Data ===\n" + osv_text)

        gh_repo = MAVEN_TO_GITHUB.get(component_name)
        if gh_repo:
            gh_text = self._fetch_github_releases(gh_repo, from_version, to_version)
            if gh_text:
                parts.append(f"=== GitHub Release Notes ({gh_repo}) ===\n" + gh_text)

        return "\n\n".join(parts)

    # ── OSV.dev ───────────────────────────────────────────────────────────────

    def _fetch_osv(self, component_name: str, cve_ids: list) -> str:
        results = []

        for cve_id in cve_ids[:3]:
            try:
                resp = requests.get(
                    f"https://api.osv.dev/v1/vulns/{cve_id}",
                    timeout=15,
                )
                if resp.status_code == 200:
                    results.append(self._format_osv_vuln(resp.json()))
            except Exception as exc:
                logger.debug("OSV lookup failed for %s: %s", cve_id, exc)

        parts = component_name.split(":")
        if len(parts) == 2:
            try:
                resp = requests.post(
                    OSV_API,
                    json={"package": {"name": component_name, "ecosystem": "Maven"}},
                    timeout=15,
                )
                if resp.status_code == 200:
                    for vuln in resp.json().get("vulns", [])[:5]:
                        text = self._format_osv_vuln(vuln)
                        if text not in results:
                            results.append(text)
            except Exception as exc:
                logger.debug("OSV package query failed for %s: %s", component_name, exc)

        return "\n---\n".join(results)

    def _format_osv_vuln(self, vuln: dict) -> str:
        lines = [f"ID: {vuln.get('id', '?')}"]
        if vuln.get("summary"):
            lines.append(f"Summary: {vuln['summary']}")
        details = vuln.get("details", "")
        if details:
            lines.append(f"Details: {details[:1500]}")
        fixed = []
        for affected in vuln.get("affected", []):
            for rng in affected.get("ranges", []):
                for ev in rng.get("events", []):
                    if "fixed" in ev:
                        fixed.append(ev["fixed"])
        if fixed:
            lines.append(f"Fixed in: {', '.join(set(fixed))}")
        refs = [r.get("url", "") for r in vuln.get("references", [])[:3] if r.get("url")]
        if refs:
            lines.append(f"References: {' | '.join(refs)}")
        return "\n".join(lines)

    # ── GitHub Releases ───────────────────────────────────────────────────────

    def _fetch_github_releases(self, gh_repo: str, from_version: str, to_version: str) -> str:
        try:
            url  = f"{GITHUB_API}/repos/{gh_repo}/releases?per_page=30"
            resp = requests.get(url, headers=self._gh_headers, timeout=15)
            if resp.status_code != 200:
                logger.debug("GitHub releases API returned %d for %s", resp.status_code, gh_repo)
                return ""
        except Exception as exc:
            logger.debug("GitHub releases fetch failed for %s: %s", gh_repo, exc)
            return ""

        releases   = resp.json()
        from_major = _parse_major(from_version)
        to_major   = _parse_major(to_version)
        relevant   = []

        for release in releases:
            tag       = release.get("tag_name", "")
            ver       = tag.lstrip("v").lstrip("release-")
            rel_major = _parse_major(ver)
            if from_major <= rel_major <= to_major:
                body = (release.get("body") or "").strip()
                if body:
                    relevant.append(f"Release {tag}:\n{body[:1500]}")

        return "\n\n".join(relevant[:10])


def _parse_major(version: str) -> int:
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return -1
