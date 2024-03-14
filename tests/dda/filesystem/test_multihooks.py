from pathlib import Path
from unittest.mock import patch

import pytest

from dagshub.streaming import DagsHubFilesystem, uninstall_hooks, get_mounted_filesystems
from dagshub.streaming.errors import FilesystemAlreadyMountedError
from tests.mocks.repo_api import MockRepoAPI


@pytest.fixture
def username():
    return "user"


@pytest.fixture
def repo_1_name():
    return "repo1"


@pytest.fixture
def repo_2_name():
    return "repo2"


@pytest.fixture
def repo_1(username, repo_1_name) -> MockRepoAPI:
    repo = MockRepoAPI(f"{username}/{repo_1_name}")
    repo.add_repo_file("a/b.txt", b"content repo 1")
    return repo


@pytest.fixture
def repo_2(username, repo_2_name) -> MockRepoAPI:
    repo = MockRepoAPI(f"{username}/{repo_2_name}")
    repo.add_repo_file("a/b.txt", b"content repo 2")
    return repo


def mock_repo_api_patch(repo_api: MockRepoAPI):
    def mocked(_self: DagsHubFilesystem, _path):
        return repo_api

    return mocked


@pytest.fixture
def mock_fs_1(repo_1, tmp_path) -> DagsHubFilesystem:
    mock_fs = generate_mock_fs(repo_1, tmp_path / repo_1.repo_name)
    yield mock_fs
    # Uninstall hooks in the end to be sure that it didn't get left over
    mock_fs.uninstall_hooks()


@pytest.fixture
def mock_fs_2(repo_2, tmp_path) -> DagsHubFilesystem:
    mock_fs = generate_mock_fs(repo_2, tmp_path / repo_2.repo_name)
    yield mock_fs
    mock_fs.uninstall_hooks()


def generate_mock_fs(repo_api: MockRepoAPI, file_dir: Path) -> DagsHubFilesystem:
    with patch("dagshub.streaming.DagsHubFilesystem._generate_repo_api", mock_repo_api_patch(repo_api)):
        fs = DagsHubFilesystem(project_root=file_dir, repo_url="https://localhost.invalid")
        return fs


def test_mock_fs_works(repo_1, tmp_path):
    fs = generate_mock_fs(repo_1, tmp_path)
    assert fs.open(tmp_path / "a/b.txt", "rb").read() == b"content repo 1"
    pass


@pytest.mark.parametrize(
    "repo_1_dir, repo_2_dir", [("repo1", "repo2"), ("mount", "mount/repo2"), ("mount/repo1", "mount")]
)
def test_two_mock_fs(repo_1, repo_2, tmp_path, repo_1_dir, repo_2_dir):
    path1 = tmp_path / repo_1_dir
    path2 = tmp_path / repo_2_dir
    fs1 = generate_mock_fs(repo_1, path1)
    fs2 = generate_mock_fs(repo_2, path2)
    try:
        fs1.install_hooks()
        fs2.install_hooks()

        assert open(path1 / "a/b.txt", "rb").read() == b"content repo 1"
        assert open(path2 / "a/b.txt", "rb").read() == b"content repo 2"
    finally:
        uninstall_hooks()


def test_nesting_priority(repo_1, repo_2, tmp_path):
    path1 = tmp_path / "mount"
    path2 = tmp_path / "mount/repo2"

    repo_1.add_repo_file("repo2/a/b.txt", b"FAILED")

    fs1 = generate_mock_fs(repo_1, path1)
    fs2 = generate_mock_fs(repo_2, path2)
    try:
        fs1.install_hooks()
        fs2.install_hooks()

        assert open(path2 / "a/b.txt", "rb").read() == b"content repo 2"
    finally:
        uninstall_hooks()


def test_nesting_priority_reverse_order(repo_1, repo_2, tmp_path):
    path1 = tmp_path / "mount"
    path2 = tmp_path / "mount/repo2"

    repo_1.add_repo_file("repo2/a/b.txt", b"FAILED")

    fs1 = generate_mock_fs(repo_1, path1)
    fs2 = generate_mock_fs(repo_2, path2)
    try:
        fs2.install_hooks()
        fs1.install_hooks()

        assert open(path2 / "a/b.txt", "rb").read() == b"content repo 2"
    finally:
        uninstall_hooks()


def test_cant_hook_in_the_same_folder(repo_1, repo_2, tmp_path):
    path1 = tmp_path / "mount"
    path2 = tmp_path / "mount"

    fs1 = generate_mock_fs(repo_1, path1)
    fs2 = generate_mock_fs(repo_2, path2)

    try:
        fs1.install_hooks()
        with pytest.raises(FilesystemAlreadyMountedError):
            fs2.install_hooks()

    finally:
        uninstall_hooks()


def test_initial_state_has_no_hooks():
    assert len(get_mounted_filesystems()) == 0


def test_install_hooks_adds_to_list_of_active(mock_fs_1):
    mock_fs_1.install_hooks()
    mounted_fses = get_mounted_filesystems()
    assert len(mounted_fses) == 1
    assert mounted_fses[0][1] == mock_fs_1


def test_uninstall_hooks_removes_from_list_of_active(mock_fs_1):
    mock_fs_1.install_hooks()
    mock_fs_1.uninstall_hooks()
    assert len(get_mounted_filesystems()) == 0


def test_global_uninstall_hooks_removes_all_by_default(mock_fs_1, mock_fs_2):
    mock_fs_1.install_hooks()
    mock_fs_2.install_hooks()
    uninstall_hooks()
    assert len(get_mounted_filesystems()) == 0


def test_global_uninstall_hooks_remove_by_fs(mock_fs_1, mock_fs_2):
    mock_fs_1.install_hooks()
    mock_fs_2.install_hooks()
    uninstall_hooks(fs=mock_fs_1)
    mounted_fses = get_mounted_filesystems()
    assert len(mounted_fses) == 1
    assert mounted_fses[0][1] == mock_fs_2


def test_global_uninstall_hooks_remove_by_path(mock_fs_1, mock_fs_2):
    mock_fs_1.install_hooks()
    mock_fs_2.install_hooks()
    uninstall_hooks(path=mock_fs_1.project_root)
    mounted_fses = get_mounted_filesystems()
    assert len(mounted_fses) == 1
    assert mounted_fses[0][1] == mock_fs_2


def test_cant_access_after_uninstall_hooks(mock_fs_1):
    mock_fs_1.install_hooks()
    mock_fs_1.uninstall_hooks()
    with pytest.raises(FileNotFoundError):
        open(mock_fs_1.project_root / "a/b.txt")
