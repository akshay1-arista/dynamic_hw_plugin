from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .audit import append_audit_event
from .config import (
    HAPY_BASE_BRANCHES,
    HAPY_GERRIT_REMOTE_NAME,
    HAPY_GIT_USER_EMAIL,
    HAPY_GIT_USER_NAME,
    HAPY_PRIVATE_BRANCH_REGISTRY_PATH,
    HAPY_REPO_ROOT,
    HAPY_TESTBED_CONFIG_ROOT,
    OUTPUTS_ROOT,
)
from .generator import GenerationError, resolve_run_root
from .models import (
    HapyCommitRequest,
    HapyCommitResult,
    HapyPrivateBranchDeleteRequest,
    HapyPrivateBranchDeleteResult,
    HapyPrivateBranchDeleteStatus,
    HapyPrivateBranchListResult,
    HapyPrivateBranchRecord,
    HapyPublishMetadata,
    RunMetadata,
    ValidationMessage,
)


class HapyRepoError(GenerationError):
    pass


def commit_run_to_hapy_repo(
    run_id: str,
    request: HapyCommitRequest,
    *,
    outputs_root: Path = OUTPUTS_ROOT,
    repo_root: Path | None = HAPY_REPO_ROOT,
    configs_root: Path | None = HAPY_TESTBED_CONFIG_ROOT,
    remote_name: str = HAPY_GERRIT_REMOTE_NAME,
    registry_path: Path = HAPY_PRIVATE_BRANCH_REGISTRY_PATH,
) -> HapyCommitResult:
    metadata, metadata_path = _load_run_metadata(run_id, outputs_root)
    run_root = metadata_path.parent
    repo_root, configs_root = _resolve_repo_paths(repo_root, configs_root)
    topology_path = run_root / metadata.topology_name
    if not topology_path.exists():
        raise HapyRepoError(f"Generated topology folder not found for run {run_id}")

    base_branch = request.base_branch
    if base_branch not in HAPY_BASE_BRANCHES:
        raise HapyRepoError(f"Unsupported base branch: {base_branch}")

    existing_publish = _find_publish(metadata)
    if existing_publish is not None:
        if existing_publish.base_branch != base_branch:
            raise HapyRepoError(
                f"Run {run_id} already has private branch {existing_publish.private_branch_name} "
                f"from base branch {existing_publish.base_branch}. Generate a new run to publish from {base_branch}."
            )
        return _build_commit_result(
            existing_publish,
            [
                ValidationMessage(
                    level="info",
                    message=(
                        f"Existing private branch {existing_publish.private_branch_name} already tracks "
                        f"{metadata.topology_name} from base branch {base_branch}."
                    ),
                )
            ],
        )

    repo_destination_relative_path, destination_relative_path = _resolve_destination_paths(
        repo_root,
        configs_root,
        metadata.reference_topology_id,
        metadata.topology_name,
    )
    private_branch_name = _build_private_branch_name(run_id)
    remote_url = _git(repo_root, "remote", "get-url", remote_name).stdout.strip()
    workspace_path = _prepare_workspace(
        run_root=run_root,
        repo_root=repo_root,
        remote_url=remote_url,
        remote_name=remote_name,
        base_branch=base_branch,
        private_branch_name=private_branch_name,
    )

    destination_path = workspace_path / repo_destination_relative_path
    if destination_path.exists():
        raise HapyRepoError(f"Destination already exists in isolated workspace: {repo_destination_relative_path}")

    destination_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(topology_path, destination_path)
    _git(workspace_path, "add", "--", str(repo_destination_relative_path))

    status_output = _git(workspace_path, "status", "--short", "--", str(repo_destination_relative_path)).stdout.strip()
    if not status_output:
        raise HapyRepoError(f"No staged changes found for {repo_destination_relative_path}")

    commit_message = f"VLDT-None: add topology {metadata.topology_name}"
    _git(workspace_path, "commit", "-m", commit_message)
    commit_sha = _git(workspace_path, "rev-parse", "HEAD").stdout.strip()
    now = _utc_now()

    publish_metadata = HapyPublishMetadata(
        run_id=run_id,
        topology_name=metadata.topology_name,
        reference_topology_id=metadata.reference_topology_id,
        repo_path=str(repo_root),
        destination_path=str(destination_path),
        destination_relative_path=str(destination_relative_path),
        base_branch=base_branch,
        private_branch_name=private_branch_name,
        commit_sha=commit_sha,
        commit_message=commit_message,
        remote_name=remote_name,
        workspace_path=str(workspace_path),
        created_by=request.requested_by,
        created_at=now,
        updated_at=now,
    )
    metadata.hapy_publishes.append(publish_metadata)
    metadata.updated_at = now
    _write_run_metadata(metadata_path, metadata)
    _upsert_registry_record(registry_path, publish_metadata)

    return _build_commit_result(
        publish_metadata,
        [
            ValidationMessage(
                level="info",
                message=(
                    f"Committed {destination_relative_path} on Gerrit private branch {private_branch_name} "
                    f"from base branch {base_branch}."
                ),
            )
        ],
    )


def publish_run_private_branch(
    run_id: str,
    request: HapyCommitRequest,
    *,
    outputs_root: Path = OUTPUTS_ROOT,
    repo_root: Path | None = HAPY_REPO_ROOT,
    configs_root: Path | None = HAPY_TESTBED_CONFIG_ROOT,
    remote_name: str = HAPY_GERRIT_REMOTE_NAME,
    registry_path: Path = HAPY_PRIVATE_BRANCH_REGISTRY_PATH,
) -> HapyCommitResult:
    commit_run_to_hapy_repo(
        run_id,
        request,
        outputs_root=outputs_root,
        repo_root=repo_root,
        configs_root=configs_root,
        remote_name=remote_name,
        registry_path=registry_path,
    )
    metadata, metadata_path = _load_run_metadata(run_id, outputs_root)
    _resolve_repo_paths(repo_root, configs_root)
    publish_metadata = _find_publish(metadata)
    if publish_metadata is None:
        raise HapyRepoError(f"No private branch metadata found for run {run_id}")
    if publish_metadata.base_branch != request.base_branch:
        raise HapyRepoError(
            f"Run {run_id} is already tied to base branch {publish_metadata.base_branch}, not {request.base_branch}."
        )
    if publish_metadata.private_branch_pushed:
        return _build_commit_result(
            publish_metadata,
            [
                ValidationMessage(
                    level="info",
                    message=f"Gerrit private branch {publish_metadata.private_branch_name} is already pushed.",
                )
            ],
        )
    if not publish_metadata.workspace_path:
        raise HapyRepoError(f"Workspace path is missing for {publish_metadata.private_branch_name}")

    workspace_path = Path(publish_metadata.workspace_path)
    if not workspace_path.exists():
        raise HapyRepoError(f"Publish workspace no longer exists: {workspace_path}")

    _git(workspace_path, "checkout", publish_metadata.private_branch_name)
    remote_branch_ref = f"refs/heads/{publish_metadata.private_branch_name}"
    _git(
        workspace_path,
        "push",
        "-u",
        remote_name,
        f"{publish_metadata.private_branch_name}:{remote_branch_ref}",
    )

    publish_metadata.remote_name = remote_name
    publish_metadata.remote_branch_ref = remote_branch_ref
    publish_metadata.private_branch_pushed = True
    publish_metadata.pushed_by = request.requested_by
    publish_metadata.fetch_command = (
        f"git fetch {remote_name} {remote_branch_ref} && "
        f"git checkout -b {publish_metadata.private_branch_name} FETCH_HEAD"
    )
    publish_metadata.updated_at = _utc_now()
    _replace_publish(metadata, publish_metadata)
    metadata.updated_at = publish_metadata.updated_at
    _write_run_metadata(metadata_path, metadata)
    _upsert_registry_record(registry_path, publish_metadata)
    if request.requested_by is not None:
        append_audit_event(
            action="private_branch_published",
            actor=request.requested_by,
            target_type="private_branch",
            target_id=publish_metadata.private_branch_name,
            summary=f"Pushed Gerrit private branch {publish_metadata.private_branch_name}.",
            details={
                "run_id": run_id,
                "base_branch": request.base_branch,
                "remote_name": remote_name,
                "remote_branch_ref": remote_branch_ref,
            },
        )

    return _build_commit_result(
        publish_metadata,
        [
            ValidationMessage(
                level="info",
                message=f"Pushed Gerrit private branch {publish_metadata.private_branch_name} to {remote_name}.",
            )
        ],
    )


def list_private_branches(
    registry_path: Path = HAPY_PRIVATE_BRANCH_REGISTRY_PATH,
) -> HapyPrivateBranchListResult:
    records = _load_registry_records(registry_path)
    records.sort(key=lambda item: (item.updated_at, item.created_at, item.private_branch_name), reverse=True)
    return HapyPrivateBranchListResult(branches=records)


def delete_private_branches(
    request: HapyPrivateBranchDeleteRequest,
    *,
    outputs_root: Path = OUTPUTS_ROOT,
    registry_path: Path = HAPY_PRIVATE_BRANCH_REGISTRY_PATH,
) -> HapyPrivateBranchDeleteResult:
    records = _load_registry_records(registry_path)
    if request.delete_all:
        targets = {record.private_branch_name for record in records}
    else:
        targets = set(request.private_branch_names)

    missing = sorted(targets - {record.private_branch_name for record in records})
    if missing:
        raise HapyRepoError(f"Unknown private branch names: {', '.join(missing)}")

    results: list[HapyPrivateBranchDeleteStatus] = []
    remaining = [record for record in records if record.private_branch_name not in targets]
    for record in records:
        if record.private_branch_name not in targets:
            continue
        results.append(
            _delete_private_branch_record(
                record,
                outputs_root=outputs_root,
                actor=request.requested_by,
            )
        )

    _replace_registry_records(registry_path, remaining)
    for result in results:
        result.registry_removed = result.success

    deleted_count = sum(1 for result in results if result.success)
    return HapyPrivateBranchDeleteResult(
        results=results,
        messages=[
            ValidationMessage(
                level="info",
                message=f"Deleted {deleted_count} Gerrit private branch{'es' if deleted_count != 1 else ''}.",
            )
        ],
    )


def _resolve_repo_paths(repo_root: Path | None, configs_root: Path | None) -> tuple[Path, Path]:
    if repo_root is None:
        raise HapyRepoError("HAPY_REPO_ROOT is not configured")
    if configs_root is None:
        raise HapyRepoError("HAPY_TESTBED_CONFIG_ROOT is not configured")

    repo_root = repo_root.resolve()
    configs_root = configs_root.resolve()
    if not repo_root.exists():
        raise HapyRepoError(f"Hapy repo path does not exist: {repo_root}")
    if not (repo_root / ".git").exists():
        raise HapyRepoError(f"Hapy repo path is not a git repository: {repo_root}")
    if not configs_root.exists():
        raise HapyRepoError(f"Hapy testbed configs path does not exist: {configs_root}")
    if repo_root not in configs_root.parents and configs_root != repo_root:
        raise HapyRepoError("Hapy testbed configs path must be inside the configured Hapy repo")
    return repo_root, configs_root


def _resolve_destination_paths(
    repo_root: Path,
    configs_root: Path,
    reference_topology_id: str,
    topology_name: str,
) -> tuple[Path, Path]:
    reference_path = Path(reference_topology_id)
    destination_relative_path = Path(topology_name)
    if str(reference_path.parent) != ".":
        destination_relative_path = reference_path.parent / destination_relative_path

    repo_configs_relative_path = configs_root.relative_to(repo_root)
    repo_destination_relative_path = repo_configs_relative_path / destination_relative_path
    return repo_destination_relative_path, destination_relative_path


def _build_private_branch_name(run_id: str) -> str:
    cleaned_run = _slugify(run_id) or "run"
    return f"hw_topo_gen_private_{cleaned_run}"


def _prepare_workspace(
    *,
    run_root: Path,
    repo_root: Path,
    remote_url: str,
    remote_name: str,
    base_branch: str,
    private_branch_name: str,
) -> Path:
    workspace_root = run_root / "hapy_publish"
    workspace_path = workspace_root / f"{_slugify(base_branch)}-{_slugify(private_branch_name)}"
    if workspace_path.exists():
        shutil.rmtree(workspace_path)
    workspace_root.mkdir(parents=True, exist_ok=True)

    _git_with_cwd(run_root, "git", "clone", str(repo_root), str(workspace_path))
    if remote_name == "origin":
        _git(workspace_path, "remote", "set-url", "origin", remote_url)
    else:
        _git(workspace_path, "remote", "rename", "origin", "source")
        _git(workspace_path, "remote", "add", remote_name, remote_url)
    _configure_git_identity(repo_root, workspace_path)
    _git(workspace_path, "fetch", remote_name, base_branch)
    _git(workspace_path, "checkout", "-B", private_branch_name, f"{remote_name}/{base_branch}")
    return workspace_path


def _configure_git_identity(
    source_repo: Path,
    target_repo: Path,
) -> None:
    name, email = _resolve_git_identity(source_repo)
    _git(target_repo, "config", "user.name", name)
    _git(target_repo, "config", "user.email", email)


def _resolve_git_identity(
    source_repo: Path,
) -> tuple[str, str]:
    env_name = HAPY_GIT_USER_NAME.strip()
    env_email = HAPY_GIT_USER_EMAIL.strip()
    if env_name or env_email:
        if not env_name or not env_email:
            raise HapyRepoError(
                "Git service identity is incomplete. Set both HAPY_GIT_USER_NAME and "
                "HAPY_GIT_USER_EMAIL, or configure user.name and user.email in the Hapy repo."
            )
        return env_name, env_email

    repo_name = _git_config_get(source_repo, "user.name")
    repo_email = _git_config_get(source_repo, "user.email")
    if repo_name and repo_email:
        return repo_name, repo_email
    if repo_name or repo_email:
        raise HapyRepoError(
            "Git service identity is incomplete in the configured Hapy repo. Set both "
            "user.name and user.email there, or configure HAPY_GIT_USER_NAME and "
            "HAPY_GIT_USER_EMAIL."
        )
    raise HapyRepoError(
        "Git service identity is not configured. Set HAPY_GIT_USER_NAME and "
        "HAPY_GIT_USER_EMAIL, or configure user.name and user.email in the Hapy repo."
    )


def _git_config_get(repo_root: Path, key: str) -> str:
    completed = subprocess.run(
        ["git", "config", "--get", key],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _find_publish(
    metadata: RunMetadata,
) -> HapyPublishMetadata | None:
    for publish in metadata.hapy_publishes:
        return publish
    return None


def _replace_publish(metadata: RunMetadata, updated_publish: HapyPublishMetadata) -> None:
    metadata.hapy_publishes = [
        updated_publish if publish.private_branch_name == updated_publish.private_branch_name else publish
        for publish in metadata.hapy_publishes
    ]


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "git command failed"
        raise HapyRepoError(f"git {' '.join(args)} failed: {error_text}")
    return completed


def _git_with_cwd(cwd: Path, *command: str) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        list(command),
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        error_text = completed.stderr.strip() or completed.stdout.strip() or "command failed"
        raise HapyRepoError(f"{' '.join(command)} failed: {error_text}")
    return completed


def _load_run_metadata(run_id: str, outputs_root: Path) -> tuple[RunMetadata, Path]:
    metadata_path = resolve_run_root(run_id, outputs_root) / "run_metadata.json"
    if not metadata_path.exists():
        raise HapyRepoError(f"Run metadata not found for {run_id}")
    with metadata_path.open() as fh:
        return RunMetadata.model_validate(json.load(fh)), metadata_path


def _write_run_metadata(path: Path, metadata: RunMetadata) -> None:
    with path.open("w") as fh:
        json.dump(metadata.model_dump(mode="json"), fh, indent=2)
        fh.write("\n")


@contextmanager
def _locked_registry(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as fh:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
        fh.seek(0)
        raw = fh.read().strip()
        try:
            data = json.loads(raw) if raw else []
        except json.JSONDecodeError as error:
            raise HapyRepoError(f"Invalid private branch registry JSON at {path}") from error
        yield data, fh
        fh.seek(0)
        fh.truncate()
        json.dump(data, fh, indent=2)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


def _upsert_registry_record(path: Path, publish_metadata: HapyPublishMetadata) -> None:
    record = HapyPrivateBranchRecord(
        run_id=publish_metadata.run_id,
        topology_name=publish_metadata.topology_name,
        reference_topology_id=publish_metadata.reference_topology_id,
        repo_path=publish_metadata.repo_path,
        destination_path=publish_metadata.destination_path,
        destination_relative_path=publish_metadata.destination_relative_path,
        base_branch=publish_metadata.base_branch,
        private_branch_name=publish_metadata.private_branch_name,
        commit_sha=publish_metadata.commit_sha,
        commit_message=publish_metadata.commit_message,
        private_branch_pushed=publish_metadata.private_branch_pushed,
        remote_name=publish_metadata.remote_name,
        remote_branch_ref=publish_metadata.remote_branch_ref,
        fetch_command=publish_metadata.fetch_command,
        workspace_path=publish_metadata.workspace_path,
        created_by=publish_metadata.created_by,
        pushed_by=publish_metadata.pushed_by,
        created_at=publish_metadata.created_at,
        updated_at=publish_metadata.updated_at,
    )
    with _locked_registry(path) as (data, _fh):
        records = [HapyPrivateBranchRecord.model_validate(item) for item in data]
        updated = False
        for index, existing in enumerate(records):
            if existing.private_branch_name == record.private_branch_name:
                records[index] = record
                updated = True
                break
        if not updated:
            records.append(record)
        data[:] = [item.model_dump(mode="json") for item in records]


def _load_registry_records(path: Path) -> list[HapyPrivateBranchRecord]:
    if not path.exists():
        return []
    with _locked_registry(path) as (data, _fh):
        return [HapyPrivateBranchRecord.model_validate(item) for item in data]


def _build_commit_result(
    publish_metadata: HapyPublishMetadata,
    messages: list[ValidationMessage],
) -> HapyCommitResult:
    return HapyCommitResult(
        run_id=publish_metadata.run_id,
        topology_name=publish_metadata.topology_name,
        reference_topology_id=publish_metadata.reference_topology_id,
        repo_path=publish_metadata.repo_path,
        destination_path=publish_metadata.destination_path,
        destination_relative_path=publish_metadata.destination_relative_path,
        base_branch=publish_metadata.base_branch,
        private_branch_name=publish_metadata.private_branch_name,
        commit_sha=publish_metadata.commit_sha,
        commit_message=publish_metadata.commit_message,
        private_branch_pushed=publish_metadata.private_branch_pushed,
        remote_name=publish_metadata.remote_name,
        remote_branch_ref=publish_metadata.remote_branch_ref,
        fetch_command=publish_metadata.fetch_command,
        created_by=publish_metadata.created_by,
        pushed_by=publish_metadata.pushed_by,
        created_at=publish_metadata.created_at,
        updated_at=publish_metadata.updated_at,
        messages=messages,
    )


def _slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(value)).strip(".-")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _replace_registry_records(path: Path, records: list[HapyPrivateBranchRecord]) -> None:
    with _locked_registry(path) as (data, _fh):
        data[:] = [record.model_dump(mode="json") for record in records]


def _delete_private_branch_record(
    record: HapyPrivateBranchRecord,
    *,
    outputs_root: Path,
    actor,
) -> HapyPrivateBranchDeleteStatus:
    deleted_local_paths: list[str] = []
    messages: list[ValidationMessage] = []

    repo_root = Path(record.repo_path).resolve()
    if _delete_local_branch_if_present(repo_root, record.private_branch_name, record.base_branch):
        deleted_local_paths.append(str(repo_root))

    workspace_path = Path(record.workspace_path).resolve() if record.workspace_path else None
    if workspace_path and workspace_path.exists():
        if _delete_local_branch_if_present(workspace_path, record.private_branch_name, record.base_branch):
            deleted_local_paths.append(str(workspace_path))

    deleted_remote = _delete_remote_branch_if_present(repo_root, record.remote_name, record.private_branch_name)
    _remove_publish_from_run_metadata(record.run_id, record.private_branch_name, outputs_root)

    append_audit_event(
        action="private_branch_deleted",
        actor=actor,
        target_type="private_branch",
        target_id=record.private_branch_name,
        summary=f"Deleted Gerrit private branch {record.private_branch_name}.",
        details={
            "run_id": record.run_id,
            "deleted_local_paths": deleted_local_paths,
            "deleted_remote": deleted_remote,
            "remote_name": record.remote_name,
        },
    )
    if not deleted_local_paths and not deleted_remote:
        messages.append(
            ValidationMessage(
                level="warning",
                message=f"No local or remote refs were found for {record.private_branch_name}; registry entry was removed.",
            )
        )
    return HapyPrivateBranchDeleteStatus(
        private_branch_name=record.private_branch_name,
        run_id=record.run_id,
        deleted_local_paths=deleted_local_paths,
        deleted_remote=deleted_remote,
        success=True,
        messages=messages,
    )


def _delete_local_branch_if_present(repo_root: Path, branch_name: str, fallback_branch: str) -> bool:
    if not repo_root.exists() or not (repo_root / ".git").exists():
        return False
    if not _git_ref_exists(repo_root, f"refs/heads/{branch_name}"):
        return False
    current_branch = _git(repo_root, "branch", "--show-current").stdout.strip()
    if current_branch == branch_name:
        if _git_ref_exists(repo_root, f"refs/heads/{fallback_branch}"):
            _git(repo_root, "checkout", fallback_branch)
        else:
            _git(repo_root, "checkout", "--detach")
    _git(repo_root, "branch", "-D", branch_name)
    return True


def _delete_remote_branch_if_present(repo_root: Path, remote_name: str, branch_name: str) -> bool:
    if not repo_root.exists() or not (repo_root / ".git").exists():
        return False
    completed = subprocess.run(
        ["git", "ls-remote", "--exit-code", "--heads", remote_name, branch_name],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        return False
    _git(repo_root, "push", remote_name, "--delete", branch_name)
    return True


def _git_ref_exists(repo_root: Path, ref_name: str) -> bool:
    completed = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref_name],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.returncode == 0


def _remove_publish_from_run_metadata(run_id: str, private_branch_name: str, outputs_root: Path) -> None:
    try:
        metadata, metadata_path = _load_run_metadata(run_id, outputs_root)
    except HapyRepoError:
        return
    metadata.hapy_publishes = [
        publish
        for publish in metadata.hapy_publishes
        if publish.private_branch_name != private_branch_name
    ]
    metadata.updated_at = _utc_now()
    _write_run_metadata(metadata_path, metadata)
