"""Private file access, canonical JSON and atomic no-replace publication.

The store is for one OS user and one Garmin account. Never chmod an arbitrary
existing directory to make it acceptable; reject it instead. Parent system
directories may be shared, but every component inside the store is owner-only.
"""

import ctypes
import errno
import fcntl
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
from contextlib import contextmanager

from .errors import BridgeError

MAX_FILE_BYTES = 128 * 1024 * 1024


def canonical(value) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def default_root() -> Path:
    override = os.environ.get("GARMIN_BRIDGE_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/garmin-connect-bridge"
    return (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
        / "garmin-connect-bridge"
    )


def empty_state():
    return {
        "version": 1,
        "activities": {},
        "covered_ranges": [],
        "pending_ranges": [],
        "last_success_at": None,
        "last_attempt_at": None,
        "last_failure": None,
        "history_initialized": False,
        "full_completed_at": None,
    }


def check_private(path: Path, directory=False):
    info = path.lstat()
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        not expected(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or (not directory and info.st_nlink != 1)
    ):
        raise BridgeError("unsafe_store")


class Store:
    def __init__(self, root: Path | None = None):
        # Resolve platform aliases such as macOS /tmp, but reject a symlink at
        # the store itself and all descendants. No upstream string becomes a path.
        candidate = Path(os.path.abspath((root or default_root()).expanduser()))
        if candidate.is_symlink():
            raise BridgeError("unsafe_store")
        self.root = candidate.resolve()
        self._absent_snapshot = False
        # Some sandboxes mount empty .git placeholders at /tmp or /workspace;
        # a checkout has a gitfile, symlink or HEAD, not merely an empty folder.
        markers = (p / ".git" for p in (self.root, *self.root.parents))
        if any(p.is_file() or p.is_symlink() or (p / "HEAD").exists() for p in markers):
            raise BridgeError("unsafe_store")

    def path(self, relative: str) -> Path:
        parts = relative.split("/")
        if not parts or any(p in ("", ".", "..") or "\\" in p for p in parts):
            raise BridgeError("unsafe_store")
        path = self.root
        if path.exists() or path.is_symlink():
            check_private(path, directory=True)
        for part in parts:
            path = path / part
            if path.is_symlink():
                raise BridgeError("unsafe_store")
            if path.exists():
                check_private(path, directory=path.is_dir())
        return path

    def ensure(self):
        if not self.root.exists():
            # Create every missing parent without opening a permissive window.
            missing = []
            parent = self.root
            while not parent.exists():
                missing.append(parent)
                parent = parent.parent
            for directory in reversed(missing):
                directory.mkdir(mode=0o700, exist_ok=True)
        check_private(self.root, directory=True)

    @contextmanager
    def lock(self, write=False):
        if not self.root.exists() and not write:
            # Queries must not create a missing root just to lock it. Freeze
            # that empty snapshot so a concurrent first writer cannot make us
            # read its unlocked, partially published journal/index pair.
            self._absent_snapshot = True
            try:
                yield
            finally:
                self._absent_snapshot = False
            return
        self.ensure()
        path = self.path(".lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            check_private(path)
            try:
                fcntl.flock(fd, (fcntl.LOCK_EX if write else fcntl.LOCK_SH) | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BridgeError("busy") from None
            yield
        finally:
            os.close(fd)

    def read_bytes(self, relative: str, limit=None) -> bytes | None:
        if self._absent_snapshot:
            return None
        limit = MAX_FILE_BYTES if limit is None else limit
        path = self.path(relative)
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return None
        with os.fdopen(fd, "rb") as handle:
            check_private(path)
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise BridgeError("storage_failed")
        return data

    def read_json(self, relative: str):
        data = self.read_bytes(relative)
        if data is None:
            return None
        try:
            return json.loads(data)
        except (ValueError, UnicodeError):
            raise BridgeError("storage_failed") from None

    def write_bytes(self, relative: str, content: bytes):
        # Never publish a file that this store cannot subsequently read. This
        # also preserves the last usable index when the MVP size cap is reached.
        if len(content) > MAX_FILE_BYTES:
            raise BridgeError("storage_failed")
        target = self.path(relative)
        parents = []
        parent = target.parent
        while parent != self.root and not parent.exists():
            parents.append(parent)
            parent = parent.parent
        for directory in reversed(parents):
            directory.mkdir(mode=0o700, exist_ok=True)
            self._fsync_dir(directory.parent)
        self.path(relative)  # recheck newly created parents
        fd, name = tempfile.mkstemp(prefix=".pending-", dir=target.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, target)
            self._fsync_dir(target.parent)
        finally:
            try:
                os.unlink(name)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_dir(directory: Path):
        fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def write_json(self, relative: str, value):
        self.write_bytes(relative, canonical(value))


def decode(content):
    try:
        value = json.loads(content)
        # Reject duplicate keys, nonfinite numbers, trailing bytes and hidden
        # noncanonical content. Every package produced here uses this encoding.
        if canonical(value) != content:
            raise BridgeError("storage_failed")
        return value
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise BridgeError("storage_failed") from None


def publish_directory(source: Path, destination: Path):
    """Atomic no-replace rename on both supported operating systems.

    A check followed by os.rename can overwrite a concurrently created empty
    directory. These kernel primitives fail even for an empty destination.
    There is deliberately no unsafe fallback on unsupported filesystems.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "linux" and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        result = function(-100, os.fsencode(source), -100, os.fsencode(destination), 1)
    elif sys.platform == "darwin" and hasattr(libc, "renamex_np"):
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        function.restype = ctypes.c_int
        result = function(os.fsencode(source), os.fsencode(destination), 4)
    else:
        raise BridgeError("storage_failed")
    if result != 0:
        code = ctypes.get_errno()
        raise BridgeError(
            "destination_exists" if code in (errno.EEXIST, errno.ENOTEMPTY) else "storage_failed"
        )
