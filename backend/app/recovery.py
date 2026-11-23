"""Idempotent local startup recovery for Jobs and artifact publication manifests."""

from __future__ import annotations

import os
import re
import stat
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

from app.artifacts import ArtifactPublicationService
from app.database import Database, utc_now
from app.jobs import JobService


_FINDING_CODE = re.compile(r"^[a-z][a-z0-9_]{0,79}$")


class StartupRecovery:
    def __init__(self, database: Database, jobs: JobService, artifacts: ArtifactPublicationService):
        self.database = database
        self.jobs = jobs
        self.artifacts = artifacts

    def _record_finding(self, relative_path: str, finding_code: str) -> None:
        normalized = Path(relative_path).as_posix()
        if (not normalized or normalized.startswith("/") or "\\" in normalized or
            any(part in {"", ".", ".."} for part in normalized.split("/")) or
            not _FINDING_CODE.fullmatch(finding_code)):
            raise ValueError("Unsafe recovery finding.")
        with closing(self.database.connect()) as connection:
            connection.execute(
                "INSERT OR IGNORE INTO recovery_findings(id,relative_path,finding_code,created_at,status) VALUES(?,?,?,?,'OPEN')",
                (str(uuid.uuid4()), normalized, finding_code, utc_now()),
            )

    def _reconcile_jobs(self) -> dict[str, int]:
        stamp = utc_now()
        interrupted_jobs = 0
        interrupted_attempts = 0
        inconsistent_terminal = []
        with closing(self.database.connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                rows = connection.execute(
                    """SELECT j.id,j.status,
                              EXISTS(SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.status='RUNNING') AS has_running
                       FROM jobs j
                       WHERE j.status IN ('RUNNING','CANCEL_REQUESTED')
                          OR (j.status='QUEUED' AND EXISTS(SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.status='RUNNING'))
                       ORDER BY j.created_at,j.id"""
                ).fetchall()
                for job in rows:
                    interrupted_attempts += connection.execute(
                        """UPDATE job_attempts SET status='INTERRUPTED',ended_at=?,error_code='process_interrupted',retryable=1
                           WHERE job_id=? AND status='RUNNING'""",
                        (stamp, job["id"]),
                    ).rowcount
                    changed = connection.execute(
                        """UPDATE jobs SET status='INTERRUPTED',cancel_requested=0,lock_version=lock_version+1,updated_at=?
                           WHERE id=? AND status=?""",
                        (stamp, job["id"], job["status"]),
                    ).rowcount
                    interrupted_jobs += changed
                inconsistent_terminal = connection.execute(
                    """SELECT j.id FROM jobs j WHERE j.status IN ('CANCELLED','READY','FAILED','INTERRUPTED')
                         AND EXISTS(SELECT 1 FROM job_attempts a WHERE a.job_id=j.id AND a.status='RUNNING')"""
                ).fetchall()
                for job in inconsistent_terminal:
                    interrupted_attempts += connection.execute(
                        """UPDATE job_attempts SET status='INTERRUPTED',ended_at=?,error_code='process_interrupted',retryable=1
                           WHERE job_id=? AND status='RUNNING'""",
                        (stamp, job["id"]),
                    ).rowcount
                    interrupted_jobs += connection.execute(
                        """UPDATE jobs SET status='INTERRUPTED',cancel_requested=0,lock_version=lock_version+1,updated_at=?
                           WHERE id=? AND status IN ('CANCELLED','READY','FAILED')""",
                        (stamp, job["id"]),
                    ).rowcount
                connection.commit()
            except BaseException:
                connection.rollback()
                raise
        for row in inconsistent_terminal:
            self._record_finding(f"jobs/{row['id']}", "terminal_job_has_running_attempt")
        return {"jobs_interrupted": interrupted_jobs, "attempts_interrupted": interrupted_attempts,
                "terminal_inconsistencies_reconciled": len(inconsistent_terminal)}

    def _manifest_paths(self) -> set[str]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute("SELECT staging_relative_path,managed_relative_path FROM artifacts").fetchall()
        return {path for row in rows for path in (row["staging_relative_path"], row["managed_relative_path"])}

    def _scan_artifact_tree(self, root: Path, known_paths: set[str]) -> int:
        findings = 0
        if not root.exists():
            return findings

        def visit(directory: Path) -> None:
            nonlocal findings
            try:
                info = directory.lstat()
                if directory.is_symlink() or not directory.is_dir():
                    self._record_finding(directory.relative_to(self.database.data_root).as_posix(), "unsafe_artifact_directory")
                    findings += 1
                    return
                if getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    self._record_finding(directory.relative_to(self.database.data_root).as_posix(), "unsafe_artifact_directory")
                    findings += 1
                    return
                entries = list(os.scandir(directory))
            except OSError:
                self._record_finding(directory.relative_to(self.database.data_root).as_posix(), "artifact_inventory_unavailable")
                findings += 1
                return
            for entry in entries:
                path = Path(entry.path)
                relative = path.relative_to(self.database.data_root).as_posix()
                try:
                    entry_info = path.lstat()
                except OSError:
                    self._record_finding(relative, "artifact_inventory_unavailable")
                    findings += 1
                    continue
                reparse = getattr(entry_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
                if reparse or path.is_symlink():
                    self._record_finding(relative, "unsafe_artifact_path")
                    findings += 1
                elif path.is_dir():
                    visit(path)
                elif path.is_file() and relative not in known_paths:
                    self._record_finding(relative, "orphan_file")
                    findings += 1
                elif not path.is_file():
                    self._record_finding(relative, "unsafe_artifact_path")
                    findings += 1

        visit(root)
        return findings

    def _inventory_orphans(self, known_paths: set[str]) -> int:
        findings = 0
        staging = self.database.data_root / "staging" / "artifacts"
        findings += self._scan_artifact_tree(staging, known_paths)
        projects_root = self.database.data_root / "projects"
        if projects_root.exists():
            try:
                root_info = projects_root.lstat()
                if projects_root.is_symlink() or getattr(root_info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
                    self._record_finding("projects", "unsafe_artifact_directory")
                    return findings + 1
                project_dirs = list(os.scandir(projects_root))
            except OSError:
                self._record_finding("projects", "artifact_inventory_unavailable")
                return findings + 1
            for project_entry in project_dirs:
                project_path = Path(project_entry.path)
                if project_path.is_symlink():
                    self._record_finding(project_path.relative_to(self.database.data_root).as_posix(), "unsafe_artifact_directory")
                    findings += 1
                    continue
                if not project_path.is_dir():
                    continue
                artifacts_dir = project_path / "artifacts"
                findings += self._scan_artifact_tree(artifacts_dir, known_paths)
        return findings

    def reconcile(self) -> dict[str, Any]:
        """Run before any worker may claim a Job; safe to call repeatedly."""
        job_report = self._reconcile_jobs()
        artifact_report = self.artifacts.reconcile_publications()
        inventory_findings = self._inventory_orphans(self._manifest_paths())
        for relative_path, finding_code in artifact_report["findings"]:
            self._record_finding(relative_path, finding_code)
        if artifact_report["startup_blocked"]:
            raise RuntimeError("Startup recovery cannot validate an existing READY artifact; worker claims remain disabled.")
        report = {**job_report, **{key: value for key, value in artifact_report.items() if key != "findings"},
                  "inventory_findings": inventory_findings,
                  "open_findings": self._open_finding_count()}
        self.jobs._allow_claims_after_recovery()
        return report

    def _open_finding_count(self) -> int:
        with closing(self.database.connect()) as connection:
            return connection.execute("SELECT COUNT(*) FROM recovery_findings WHERE status='OPEN'").fetchone()[0]
