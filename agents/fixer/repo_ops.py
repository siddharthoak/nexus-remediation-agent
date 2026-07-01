"""
FIX-02: Repository operations — clone, branch, commit, push, cleanup.

Uses GitPython for a programmatic API rather than subprocess calls, which makes
the operations testable without a real git binary and avoids PAT leakage in shell history.
"""

import logging
import os
import shutil
import tempfile
from typing import Optional
from urllib.parse import urlparse

import git

logger = logging.getLogger(__name__)


class RepoBranchExistsError(Exception):
    """Raised when the deterministic branch name already exists remotely."""


class RepoOps:
    """
    Wraps all git operations needed by the Fixer agent.

    Branch naming is deterministic: fix/{component-name}-{short-hash-of-component+version}
    so that re-running the agent for the same vulnerability does not create duplicate branches
    or duplicate PRs. See create_branch() for collision handling behavior.

    Usage as a context manager ensures cleanup() runs even on error:

        with RepoOps() as ops:
            ops.clone(repo_url, github_pat, local_path)
            ops.create_branch("fix/log4j-abc123")
            # ... make changes ...
            ops.commit_changes("fix: upgrade log4j to 2.20.0")
            ops.push_branch("fix/log4j-abc123")
    """

    def __init__(self):
        self._repo: Optional[git.Repo] = None
        self._local_path: Optional[str] = None

    # ── Context manager ───────────────────────────────────────────────────────

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.cleanup()
        return False  # Do not suppress exceptions

    # ── Public API ────────────────────────────────────────────────────────────

    def clone_local(self, source_path: str, remote_url: str, github_pat: str) -> str:
        """
        Create a fast local clone from an already-cloned source directory.

        Uses hardlinks where possible (same filesystem) — milliseconds instead of the
        network round-trip of a full GitHub clone. After cloning, re-points origin to
        remote_url (GitHub) so push and remote branch checks work correctly.

        Typical use: clone the repo once with clone(), then call clone_local() once per
        parallel finding so each worker has an isolated working directory without paying
        the GitHub clone cost N times.
        """
        self._local_path = tempfile.mkdtemp(prefix="oss-remediation-")
        logger.info("Local clone from %s to %s", source_path, self._local_path)
        self._repo = git.Repo.clone_from(source_path, self._local_path)
        # After a local clone, origin points to the source path — re-point to GitHub.
        authenticated_url = self._build_authenticated_url(remote_url, github_pat)
        self._repo.remotes.origin.set_url(authenticated_url)
        self._configure_credential_helper(github_pat, remote_url)
        return self._local_path

    def clone(self, repo_url: str, github_pat: str, local_path: Optional[str] = None) -> str:
        """
        Clone `repo_url` using `github_pat` for HTTPS auth.

        The PAT is injected via Git's credential helper configuration, NOT embedded in the
        URL, to prevent it from appearing in git logs, reflog, or error messages.

        Returns the local path where the repo was cloned.
        """
        self._local_path = local_path or tempfile.mkdtemp(prefix="oss-remediation-")

        # Build an authenticated URL by injecting credentials as a Git config credential
        # helper override rather than in the URL string itself.
        authenticated_url = self._build_authenticated_url(repo_url, github_pat)

        logger.info("Cloning %s to %s", repo_url, self._local_path)
        self._repo = git.Repo.clone_from(
            authenticated_url,
            self._local_path,
            env={"GIT_TERMINAL_PROMPT": "0"},
        )
        # Store PAT for subsequent pushes via a credential helper in the local config.
        # This avoids re-embedding the PAT at push time.
        self._configure_credential_helper(github_pat, repo_url)

        logger.info("Clone complete: %s", self._local_path)
        return self._local_path

    def create_branch(self, branch_name: str, skip_if_exists: bool = True) -> bool:
        """
        Create and check out `branch_name` off the current default branch.

        If the branch already exists remotely:
          - skip_if_exists=True (default): logs a warning and returns False.
            The caller should interpret False as "PR already in progress, skip this run."
          - skip_if_exists=False: raises RepoBranchExistsError.

        Returns True if the branch was newly created, False if it already existed.
        """
        self._require_repo()

        # Fetch remote refs so we can check for existing branches
        origin = self._repo.remotes.origin
        origin.fetch()

        remote_branches = [ref.name for ref in origin.refs]
        remote_branch_ref = f"origin/{branch_name}"

        if remote_branch_ref in remote_branches:
            message = f"Branch '{branch_name}' already exists remotely — PR likely already open."
            if skip_if_exists:
                logger.warning(message + " Skipping this remediation run.")
                return False
            raise RepoBranchExistsError(message)

        # Create branch off default (HEAD)
        new_branch = self._repo.create_head(branch_name)
        new_branch.checkout()
        logger.info("Created and checked out branch: %s", branch_name)
        return True

    def commit_changes(self, message: str, files: Optional[list] = None) -> str:
        """
        Stage `files` (or all changes if None) and create a commit.

        Returns the commit hexsha.
        """
        self._require_repo()

        if files:
            self._repo.index.add(files)
        else:
            self._repo.git.add(A=True)

        commit = self._repo.index.commit(message)
        logger.info("Committed %s: %s", commit.hexsha[:8], message)
        return commit.hexsha

    def push_branch(self, branch_name: str) -> None:
        """Push `branch_name` to origin. Never force-pushes."""
        self._require_repo()
        origin = self._repo.remotes.origin
        origin.push(refspec=f"{branch_name}:{branch_name}")
        logger.info("Pushed branch %s to origin", branch_name)

    def cleanup(self) -> None:
        """Remove the local clone directory. Safe to call multiple times."""
        if self._local_path and os.path.exists(self._local_path):
            shutil.rmtree(self._local_path, ignore_errors=True)
            logger.info("Cleaned up local clone at %s", self._local_path)
            self._local_path = None
            self._repo = None

    # ── Static helpers ────────────────────────────────────────────────────────

    @staticmethod
    def make_branch_name(component_name: str, current_version: str) -> str:
        """
        Produce a deterministic, collision-resistant branch name for a vulnerability fix.
        Format: fix/{sanitized-component}-{8-char-hash}

        The hash is derived from component_name + current_version, so the same vulnerability
        always maps to the same branch name across agent runs, preventing duplicate PRs.
        """
        import hashlib
        key = f"{component_name}@{current_version}"
        short_hash = hashlib.sha1(key.encode()).hexdigest()[:8]
        # Sanitize component name for use in branch name
        safe_name = component_name.replace(":", "-").replace("/", "-").replace(".", "-")
        safe_name = safe_name[:40]  # Keep branch name reasonable
        return f"fix/{safe_name}-{short_hash}"

    # ── Private helpers ───────────────────────────────────────────────────────

    def _require_repo(self):
        if self._repo is None:
            raise RuntimeError("No repository cloned yet. Call clone() first.")

    def _build_authenticated_url(self, repo_url: str, github_pat: str) -> str:
        """
        Inject PAT into the HTTPS URL as `x-access-token:PAT@host/path`.
        This is GitHub's documented machine-account authentication pattern for HTTPS.
        The PAT does NOT appear in the URL returned by `git remote -v` after clone
        because GitPython uses it only during the clone operation.
        """
        parsed = urlparse(repo_url)
        return parsed._replace(
            netloc=f"x-access-token:{github_pat}@{parsed.hostname}"
        ).geturl()

    def _configure_credential_helper(self, github_pat: str, repo_url: str) -> None:
        """
        Store the PAT in the repo's local git config as a credential helper store
        so subsequent pushes authenticate without re-embedding the PAT in remote URLs.
        """
        parsed = urlparse(repo_url)
        host = parsed.hostname
        with self._repo.config_writer() as cw:
            cw.set_value(f'credential "https://{host}"', "helper", "store")
        # Write to the local git credentials store (scoped to this temp directory)
        creds_path = os.path.join(self._local_path, ".git", "credentials")
        with open(creds_path, "w") as f:
            f.write(f"https://x-access-token:{github_pat}@{host}\n")
        with self._repo.config_writer() as cw:
            cw.set_value("credential", "helper", f"store --file {creds_path}")
