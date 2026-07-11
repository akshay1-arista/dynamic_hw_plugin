import json
import subprocess
from pathlib import Path

from app.hapy_repo import (
    commit_run_to_hapy_repo,
    delete_private_branches,
    list_private_branches,
    publish_run_private_branch,
)
from app.models import ActorIdentity, HapyCommitRequest, HapyPrivateBranchDeleteRequest


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _git_dir(git_dir: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", f"--git-dir={git_dir}", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _init_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    remote_root = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", str(remote_root)], check=True, capture_output=True, text=True)

    repo_root = tmp_path / "velocloud.src"
    repo_root.mkdir()
    _git(repo_root, "init")
    _git(repo_root, "checkout", "-B", "master")
    _git(repo_root, "config", "user.name", "Test User")
    _git(repo_root, "config", "user.email", "test@example.com")
    (repo_root / "README.md").write_text("seed\n")
    (repo_root / "hapy" / "hapy" / "testbed" / "configs").mkdir(parents=True)
    _git(repo_root, "add", "README.md", "hapy")
    _git(repo_root, "commit", "-m", "seed")
    _git(repo_root, "remote", "add", "origin", str(remote_root))
    _git(repo_root, "push", "-u", "origin", "master")
    _git(repo_root, "branch", "release_6.4", "master")
    _git(repo_root, "push", "origin", "release_6.4")
    return repo_root, remote_root, repo_root / "hapy" / "hapy" / "testbed" / "configs"


def _build_run_outputs(tmp_path: Path, *, reference_topology_id: str) -> tuple[Path, str]:
    outputs_root = tmp_path / "outputs"
    run_id = "run123"
    run_root = outputs_root / f"{run_id}-abc123"
    topology_root = run_root / "nested-topology"
    topology_root.mkdir(parents=True)
    (topology_root / "config.json").write_text(json.dumps({"testbed": {"name": "nested-topology"}}))
    (topology_root / "characteristics.json").write_text("{}")
    (run_root / "run_metadata.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "topology_name": "nested-topology",
                "reference_topology_id": reference_topology_id,
                "mappings": [],
            }
        )
    )
    return outputs_root, run_id


def test_commit_run_to_hapy_repo_places_nested_reference_under_parent_folder(tmp_path):
    repo_root, _remote_root, configs_root = _init_repo(tmp_path)
    outputs_root, run_id = _build_run_outputs(tmp_path, reference_topology_id="5-site-cluster/spirent")
    registry_path = tmp_path / "hapy_private_branches.json"

    result = commit_run_to_hapy_repo(
        run_id,
        HapyCommitRequest(base_branch="release_6.4"),
        outputs_root=outputs_root,
        repo_root=repo_root,
        configs_root=configs_root,
        remote_name="origin",
        registry_path=registry_path,
    )

    expected_relative = Path("5-site-cluster") / "nested-topology"
    assert result.destination_relative_path == str(expected_relative)
    assert Path(result.destination_path, "config.json").exists()
    assert result.base_branch == "release_6.4"
    assert result.private_branch_name == "hw_topo_gen_private_run123"
    assert list_private_branches(registry_path).branches[0].private_branch_name == result.private_branch_name


def test_publish_run_private_branch_pushes_only_private_branch_ref(tmp_path):
    repo_root, remote_root, configs_root = _init_repo(tmp_path)
    outputs_root, run_id = _build_run_outputs(tmp_path, reference_topology_id="3-site")
    registry_path = tmp_path / "hapy_private_branches.json"
    pushed = publish_run_private_branch(
        run_id,
        HapyCommitRequest(base_branch="release_6.4"),
        outputs_root=outputs_root,
        repo_root=repo_root,
        configs_root=configs_root,
        remote_name="origin",
        registry_path=registry_path,
    )

    expected_ref = "refs/heads/hw_topo_gen_private_run123"
    assert pushed.private_branch_pushed is True
    assert pushed.remote_branch_ref == expected_ref
    assert expected_ref in pushed.fetch_command
    assert _git_dir(remote_root, "rev-parse", expected_ref) == pushed.commit_sha


def test_delete_private_branches_removes_local_remote_and_registry_entries(tmp_path):
    repo_root, remote_root, configs_root = _init_repo(tmp_path)
    outputs_root, run_id = _build_run_outputs(tmp_path, reference_topology_id="3-site")
    registry_path = tmp_path / "hapy_private_branches.json"
    pushed = publish_run_private_branch(
        run_id,
        HapyCommitRequest(base_branch="release_6.4"),
        outputs_root=outputs_root,
        repo_root=repo_root,
        configs_root=configs_root,
        remote_name="origin",
        registry_path=registry_path,
    )
    _git(repo_root, "fetch", "origin", pushed.private_branch_name)
    _git(repo_root, "checkout", "-B", pushed.private_branch_name, f"origin/{pushed.private_branch_name}")
    _git(repo_root, "checkout", "master")

    result = delete_private_branches(
        HapyPrivateBranchDeleteRequest(
            private_branch_names=[pushed.private_branch_name],
            requested_by=ActorIdentity(name="Test User", email="test@example.com"),
        ),
        outputs_root=outputs_root,
        registry_path=registry_path,
    )

    assert result.results[0].private_branch_name == pushed.private_branch_name
    assert result.results[0].deleted_remote is True
    assert str(repo_root) in result.results[0].deleted_local_paths
    assert list_private_branches(registry_path).branches == []
    assert _git(repo_root, "branch", "--list", pushed.private_branch_name) == ""
    remote_heads = _git_dir(remote_root, "for-each-ref", "--format=%(refname)", "refs/heads")
    assert pushed.remote_branch_ref not in remote_heads
