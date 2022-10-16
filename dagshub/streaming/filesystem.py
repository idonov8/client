import builtins
import io
import os
import re
import subprocess
from configparser import ConfigParser
from contextlib import contextmanager
from functools import partial, wraps, lru_cache
from multiprocessing import AuthenticationError
from os import PathLike
from os.path import ismount
from pathlib import Path
from pathlib import _NormalAccessor as _pathlib
from typing import Optional, TypeVar, Union
from urllib.parse import urlparse
from dagshub.common import config
import logging
import requests

T = TypeVar('T')
logger = logging.getLogger(__name__)


def wrapreturn(wrappertype):
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            return wrappertype(func(*args, **kwargs))

        return wrapper

    return decorator


class dagshub_ScandirIterator:
    def __init__(self, iterator):
        self._iterator = iterator

    def __iter__(self):
        return self._iterator

    def __next__(self):
        return self._iterator.__next__()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self


def cache_by_path(func):
    cache = {}

    @wraps(func)
    def wrapper(self, path: str, include_size: bool = False):
        if not include_size and (path, True) in cache:
            cache[path, False] = cache[path, True]
        if (path, include_size) not in cache:
            cache[path, include_size] = func(self, path, include_size)
        return cache[path, include_size]

    wrapper.cache = cache
    return wrapper


SPECIAL_FILE = Path('.dagshub-streaming')


# TODO: Singleton metaclass that lets us keep a "main" DvcFilesystem instance
class DagsHubFilesystem:
    __slots__ = ('project_root',
                 'project_root_fd',
                 'dvc_remote_url',
                 'user_specified_branch',
                 'parsed_repo_url',
                 'dirtree',
                 'username',
                 'password',
                 'dagshub_remotes',
                 'token',
                 '__weakref__')

    def __init__(self,
                 project_root: Optional[PathLike] = None,
                 repo_url: Optional[str] = None,
                 branch: Optional[str] = None,
                 username: Optional[str] = None,
                 password: Optional[str] = None,
                 token: Optional[str] = None,
                 _project_root_fd: Optional[int] = None):

        # Find root directory of Git project
        if not project_root:
            self.project_root = Path(os.path.abspath('.'))
            while not (self.project_root / '.git').is_dir():
                if ismount(self.project_root):
                    raise ValueError('No git project found! (stopped at mountpoint {self.project_root})')
                self.project_root = self.project_root / '..'
        else:
            self.project_root = Path(os.path.abspath(project_root))
        del project_root
        # TODO: if no Git project found, search for .dvc project?

        if _project_root_fd:
            self.project_root_fd = _project_root_fd
        else:
            self.project_root_fd = os.open(self.project_root, os.O_DIRECTORY)

        self.dagshub_remotes = []
        self.parse_git_config()

        if not repo_url:
            if len(self.dagshub_remotes) > 0:
                repo_url = self.dagshub_remotes[0]
            else:
                raise ValueError('No DagsHub git remote detected, please specify repo_url= argument or --repo_url flag')

        self.user_specified_branch = branch
        self.parsed_repo_url = urlparse(repo_url)
        self.dvc_remote_url = f'{repo_url}.dvc/cache'
        self.dirtree = {}

        # Determine if any authentication is needed
        self.username = username or config.username
        self.password = password or config.password
        self.token = token or config.token

        response = self._api_listdir('')
        if response.ok:
            pass
        else:
            # TODO: Check .dvc/config{,.local} for credentials
            raise AuthenticationError('DagsHub credentials required, however none provided or discovered')

    @property
    @lru_cache(maxsize=None)
    def _current_revision(self) -> str:
        """
        Gets current revision on repo:
        - If User specified a branch, returns HEAD of that brunch on the remote
        - If HEAD is a branch, tries to find a dagshub remote associated with it and get its HEAD
        - If HEAD is a commit revision, checks that the commit exists on DagsHub
        """

        if self.user_specified_branch:
            branch = self.user_specified_branch
        else:
            with open(self.project_root / ".git/HEAD") as head_file:
                head = head_file.readline().strip()
            if head.startswith("ref"):
                branch = head.split("/")[-1]
            else:
                # contents of HEAD is the revision - check that this commit exists on remote
                if self.is_commit_on_remote(head):
                    return head
                else:
                    raise RuntimeError(f"Current HEAD ({head}) doesn't exist on the remote. "
                                       f"Please push your changes to the remote or checkout a tracked branch.")
        return self.get_remote_branch_head(branch)

    @property
    def content_api_url(self):
        return self.get_api_url(f"/api/v1/repos{self.parsed_repo_url.path}/content/{self._current_revision}")

    @property
    def raw_api_url(self):
        return self.get_api_url(f"/api/v1/repos{self.parsed_repo_url.path}/raw/{self._current_revision}")

    def is_commit_on_remote(self, sha1):
        url = self.get_api_url(f"/api/v1/repos{self.parsed_repo_url.path}/commits/{sha1}")
        resp = requests.get(url, auth=self.auth)
        return resp.status_code == 200

    def get_remote_branch_head(self, branch):
        url = self.get_api_url(f"/api/v1/repos{self.parsed_repo_url.path}/branches/{branch}")
        resp = requests.get(url, auth=self.auth)
        if resp.status_code != 200:
            raise RuntimeError(f"Got status {resp.status_code} while trying to get head of branch {branch}. \r\n"
                               f"Response body: {resp.content}")
        return resp.json()["commit"]["id"]

    def get_api_url(self, path):
        return self.parsed_repo_url._replace(path=path).geturl()

    @property
    def auth(self):
        import dagshub.auth
        from dagshub.auth.token_auth import HTTPBearerAuth

        if self.username is not None and self.password is not None:
            return self.username, self.password

        try:
            token = self.token or dagshub.auth.get_token(code_input_timeout=0)
        except dagshub.auth.OauthNonInteractiveShellException:
            logger.debug("Failed to perform OAuth in a non interactive shell")
        if token is not None:
            return HTTPBearerAuth(token)

        # Try to fetch credentials from the git credential file
        proc = subprocess.run(['git', 'credential', 'fill'],
                              input=f'url={self.repo_url}'.encode(),
                              capture_output=True)
        answer = {line[:line.index('=')]: line[line.index('=') + 1:]
                  for line in proc.stdout.decode().splitlines()}
        if 'username' in answer and 'password' in answer:
            return answer['username'], answer['password']

    def parse_git_config(self):
        # Get URLs of dagshub remotes
        git_config = ConfigParser()
        git_config.read(Path(self.project_root) / '.git/config')
        git_remotes = [urlparse(git_config[remote]['url'])
                       for remote in git_config
                       if remote.startswith('remote ')]
        for remote in git_remotes:
            if remote.hostname != config.hostname:
                continue
            remote = remote._replace(netloc=remote.hostname)
            remote = remote._replace(path=re.compile(r'(\.git)?/?$').sub('', remote.path))
            self.dagshub_remotes.append(remote.geturl())

    def __del__(self):
        os.close(self.project_root_fd)

    def _relative_path(self, file: Union[PathLike, int]):
        if isinstance(file, int):
            return None
        path = Path(file).resolve()
        try:
            rel = path.resolve().relative_to(self.project_root.resolve())
            if str(rel).startswith("<"):
                return None
            return rel
        except ValueError:
            return None

    def _passthrough_path(self, relative_path: PathLike):
        return str(relative_path).startswith(('.git/', '.dvc/'))

    def _special_file(self):
        # TODO Include more information in this file
        return b'v0\n'

    def open(self, file: Union[PathLike, int], mode: str = 'r', opener=None, *args, **kwargs):
        if opener is not None:
            raise NotImplementedError('DagsHub\'s patched open() does not support custom openers')
        relative_path = self._relative_path(file)
        if relative_path:
            project_root_opener = partial(os.open, dir_fd=self.project_root_fd)
            if self._passthrough_path(relative_path):
                return self.__open(relative_path, mode, *args, **kwargs, opener=project_root_opener)
            elif relative_path == SPECIAL_FILE:
                return io.BytesIO(self._special_file())
            else:
                try:
                    return self.__open(relative_path, mode, *args, **kwargs, opener=project_root_opener)
                except FileNotFoundError:
                    resp = self._api_download_file_git(relative_path)
                    if resp.ok:
                        self._mkdirs(relative_path.parent, dir_fd=self.project_root_fd)
                        # TODO: Handle symlinks
                        with self.__open(relative_path, 'wb', opener=project_root_opener) as output:
                            output.write(resp.content)
                        return self.__open(relative_path, mode, opener=project_root_opener)
                    else:
                        # TODO: After API no longer 500s on FileNotFounds
                        #       check status code and only return FileNotFound on 404s
                        raise FileNotFoundError(f'Error finding {relative_path} in repo or on DagsHub')
        else:
            return self.__open(file, mode, *args, **kwargs)

    def stat(self, path: PathLike, *, dir_fd=None, follow_symlinks=True):
        if dir_fd is not None or not follow_symlinks:
            raise NotImplementedError('DagsHub\'s patched stat() does not support dir_fd or follow_symlinks')
        relative_path = self._relative_path(path)
        if relative_path:
            if self._passthrough_path(relative_path):
                return self.__stat(relative_path, dir_fd=self.project_root_fd)
            elif relative_path == SPECIAL_FILE:
                return dagshub_stat_result(self, path, len(self._special_file()), is_directory=False)
            else:
                try:
                    return self.__stat(relative_path, dir_fd=self.project_root_fd)
                except FileNotFoundError:
                    parent_tree = self.dirtree.get(str(relative_path.parent))
                    if parent_tree is not None and str(relative_path.name) not in parent_tree:
                        return dagshub_stat_result(self, path, is_directory=False)
                    else:
                        # self._mkdirs(path, dir_fd=self.project_root_fd)
                        return self.__stat(relative_path, dir_fd=self.project_root_fd)
                        # TODO: perhaps don't create directories on stat
        else:
            return self.__stat(path, follow_symlinks=follow_symlinks)

    def chdir(self, path):
        relative_path = self._relative_path(path)
        if relative_path:
            abspath = os.path.join(self.project_root, relative_path)
            try:
                self.__chdir(abspath)
            except FileNotFoundError:
                resp = self._api_listdir(relative_path)
                # FIXME: if path is file, return FileNotFound instead of the listdir error
                if resp.ok:
                    self._mkdirs(relative_path, dir_fd=self.project_root_fd)
                    self.__chdir(abspath)
                else:
                    raise
        else:
            self.__chdir(path)

    def listdir(self, path='.'):
        relative_path = self._relative_path(path)
        if relative_path:
            if self._passthrough_path(relative_path):
                with self._open_fd(relative_path) as fd:
                    return self.__listdir(fd)
            else:
                dircontents: set[str] = set()
                error = None
                try:
                    with self._open_fd(relative_path) as fd:
                        dircontents.update(self.__listdir(fd))
                except FileNotFoundError as e:
                    error = e
                if relative_path == Path():
                    dircontents.add(SPECIAL_FILE.name)
                resp = self._api_listdir(relative_path)
                if resp.ok:
                    dircontents.update(Path(f['path']).name for f in resp.json())
                    # TODO: optimize + make subroutine async
                    self.dirtree[str(path)] = [Path(f['path']).name for f in resp.json() if f['type'] == 'dir']
                    return list(dircontents)
                else:
                    if error is not None:
                        raise error
                    else:
                        return list(dircontents)
        else:
            return self.__listdir(path)

    @wrapreturn(dagshub_ScandirIterator)
    def scandir(self, path='.'):
        path = Path(path)
        relative_path = self._relative_path(path)
        if relative_path:
            if self._passthrough_path(relative_path):
                with self._open_fd(relative_path) as fd:
                    return self.__scandir(fd)
            else:
                local_filenames = set()
                try:
                    with self._open_fd(relative_path) as fd:
                        for direntry in self.__scandir(fd):
                            local_filenames.add(direntry.name)
                            yield direntry
                    if relative_path == Path():
                        if SPECIAL_FILE.name not in local_filenames:
                            yield dagshub_DirEntry(self, path / SPECIAL_FILE, is_directory=False)
                except FileNotFoundError:
                    pass
                resp = self._api_listdir(relative_path)
                if resp.ok:
                    for f in resp.json():
                        name = Path(f['path']).name
                        if name not in local_filenames:
                            yield dagshub_DirEntry(self, path / name, f['type'] == 'dir')
        else:
            return self.__scandir(path)

    @cache_by_path
    def _api_listdir(self, path: str, include_size: bool = False):
        return requests.get(f'{self.content_api_url}/{path}', auth=self.auth,
                            params={'include_size': 'true'} if include_size else {})

    def _api_download_file_git(self, path: str):
        return requests.get(f'{self.raw_api_url}/{path}', auth=self.auth)

    def install_hooks(self):
        if not hasattr(self.__class__, f'_{self.__class__.__name__}__unpatched'):
            # TODO: DRY this dictionary. i.e. __open() links cls.__open
            #  and io.open even though this dictionary links them
            #  Cannot use a dict as the source of truth because type hints rely on
            #  __get_unpatched inferring the right type
            self.__class__.__unpatched = {
                'open': io.open,
                'stat': os.stat,
                'listdir': os.listdir,
                'scandir': os.scandir,
                'chdir': os.chdir,
            }
        io.open = builtins.open = _pathlib.open = self.open
        os.stat = _pathlib.stat = self.stat
        os.listdir = _pathlib.listdir = self.listdir
        os.scandir = _pathlib.scandir = self.scandir
        os.chdir = self.chdir
        self.__class__.hooked_instance = self

    @classmethod
    def uninstall_hooks(cls):
        if hasattr(cls, f'_{cls.__name__}__unpatched'):
            io.open = builtins.open = cls.__unpatched['open']
            os.stat = _pathlib.stat = cls.__unpatched['stat']
            os.listdir = _pathlib.listdir = cls.__unpatched['listdir']
            os.scandir = _pathlib.scandir = cls.__unpatched['scandir']
            os.chdir = cls.__unpatched['chdir']

    def _mkdirs(self, relative_path: PathLike, dir_fd: Optional[int] = None):
        for parent in list(relative_path.parents)[::-1]:
            try:
                self.__stat(parent, dir_fd=dir_fd)
            except (OSError, ValueError):
                os.mkdir(parent, dir_fd=dir_fd)
        try:
            self.__stat(relative_path, dir_fd=dir_fd)
        except (OSError, ValueError):
            os.mkdir(relative_path, dir_fd=dir_fd)

    @contextmanager
    def _open_fd(self, relative_path):
        fd = None
        try:
            fd = os.open(relative_path, os.O_DIRECTORY, dir_fd=self.project_root_fd)
            yield fd
        finally:
            if fd is not None:
                os.close(fd)

    @classmethod
    def __get_unpatched(cls, key, alt: T) -> T:
        if hasattr(cls, f'_{cls.__name__}__unpatched'):
            return cls.__unpatched[key]
        else:
            return alt

    @property
    def __open(self):
        return self.__get_unpatched('open', io.open)

    @property
    def __stat(self):
        return self.__get_unpatched('stat', os.stat)

    @property
    def __listdir(self):
        return self.__get_unpatched('listdir', os.listdir)

    @property
    def __scandir(self):
        return self.__get_unpatched('scandir', os.scandir)

    @property
    def __chdir(self):
        return self.__get_unpatched("chdir", os.chdir)


def install_hooks(project_root: Optional[PathLike] = None,
                  repo_url: Optional[str] = None,
                  branch: Optional[str] = None,
                  username: Optional[str] = None,
                  password: Optional[str] = None):
    fs = DagsHubFilesystem(project_root=project_root, repo_url=repo_url, branch=branch, username=username,
                           password=password)
    fs.install_hooks()


def uninstall_hooks():
    DagsHubFilesystem.uninstall_hooks()


class dagshub_stat_result:
    def __init__(self, fs: 'DagsHubFilesystem', path: PathLike, is_directory: bool):
        self._fs = fs
        self._path = path
        self._is_directory = is_directory
        assert not self._is_directory  # TODO make folder stats lazy?

    def __getattr__(self, name: str):
        if not name.startswith('st_'):
            raise AttributeError
        if hasattr(self, '_true_stat'):
            return os.stat_result.__getattribute__(self._true_stat, name)
        if name == 'st_uid':
            return os.getuid()
        elif name == 'st_gid':
            return os.getgid()
        elif name == 'st_atime' or name == 'st_mtime' or name == 'st_ctime':
            return 0
        elif name == 'st_mode':
            return 0o100644
        elif name == 'st_size':
            return 1100  # hardcoded size because size requests take a disproportionate amount of time
        self._fs.open(self._path)
        self._true_stat = self._fs._DagsHubFilesystem__stat(self._fs._relative_path(self._path),
                                                            dir_fd=self._fs.project_root_fd)
        return os.stat_result.__getattribute__(self._true_stat, name)

    def __repr__(self):
        inner = repr(self._true_stat) if hasattr(self, '_true_stat') else 'pending...'
        return f'dagshub_stat_result({inner}, path={self._path})'


class dagshub_DirEntry:
    def __init__(self, fs: 'DagsHubFilesystem', path: PathLike, is_directory: bool = False):
        self._fs = fs
        self._path = path
        self._is_directory = is_directory

    @property
    def name(self):
        # TODO: create decorator for delegation
        if hasattr(self, '_true_direntry'):
            return self._true_direntry.name
        else:
            return self._path.name

    @property
    def path(self):
        if hasattr(self, '_true_direntry'):
            return self._true_direntry.path
        else:
            return str(self._path)

    def is_dir(self):
        if hasattr(self, '_true_direntry'):
            return self._true_direntry.is_dir()
        else:
            return self._is_directory

    def is_file(self):
        if hasattr(self, '_true_direntry'):
            return self._true_direntry.is_file()
        else:
            # TODO: Symlinks should return false
            return not self._is_directory

    def stat(self):
        if hasattr(self, '_true_direntry'):
            return self._true_direntry.stat()
        else:
            return self._fs.stat(self._path)

    def __getattr__(self, name: str):
        if name == '_true_direntry':
            raise AttributeError
        if hasattr(self, '_true_direntry'):
            return os.DirEntry.__getattribute__(self._true_direntry, name)
        if self._is_directory:
            self._fs._mkdirs(self._fs._relative_path(self._path), dir_fd=self._fs.project_root_fd)
        else:
            self._fs.open(self._path)
        with self._open_fd(self._fs._relative_path(self._path).parent) as fd:
            for direntry in self._fs._DagsHubFilesystem__scandir(fd):
                if direntry.name == self._path.name:
                    self._true_direntry = direntry
                    return os.DirEntry.__getattribute__(self._true_direntry, name)
            else:
                raise FileNotFoundError

    def __repr__(self):
        cached = ' (cached)' if hasattr(self, '_true_direntry') else ''
        return f'<dagshub_DirEntry \'{self.name}\'{cached}>'


# Used for testing purposes only
if __name__ == "__main__":
    logging.basicConfig(level=logging.DEBUG)
    fs = DagsHubFilesystem()
    fs.install_hooks()

__all__ = [DagsHubFilesystem.__name__, install_hooks.__name__]
