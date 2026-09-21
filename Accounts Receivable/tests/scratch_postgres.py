"""A disposable local PostgreSQL cluster for the PostgreSQL test variants.

Usage (from the project root)::

    python -m tests.scratch_postgres -- python -m pytest -q
    python -m tests.scratch_postgres --keep -- python -m pytest -q tests/receivables
    python -m tests.scratch_postgres --capture -- python -m pytest -q   # output to an owned temp file

It creates a brand-new cluster, runs the command with ``FMS_TEST_POSTGRES_URL``
pointing at it, then stops the server and deletes the cluster - but only the
exact cluster this process created. Nothing about the cluster is ever read back
from a shared file: its identity lives in this process's memory and is checked
immediately before every destructive step.

Identity of the cluster (captured at creation):

* the directory path returned by ``tempfile.mkdtemp`` (unique, mode 0700) and
  the device/inode of that directory;
* a random token written with ``O_CREAT | O_EXCL`` into a marker file inside it;
* the PID of the postmaster we started, read from ``postmaster.pid`` right
  after start.

Before ``stop``: the directory (opened with ``O_NOFOLLOW``) must still be the
same inode, owned by us, carry our token, and its ``postmaster.pid`` must name
our server. Before ``remove``: the same checks, the server must be stopped, and
the deletion runs through the directory file descriptors that were verified
(``shutil.rmtree`` with ``dir_fd``, which also refuses symlinks). If any check
fails the resource is left exactly as it is and the reason is reported.

``--capture`` writes the command's output to a temporary file created with
``mkstemp`` (unique name, mode 0600) whose first line is a random token; the
tail is printed at the end and the file is deleted only after the same
identity checks (same inode, our uid, regular file opened with ``O_NOFOLLOW``,
token intact). ``OwnedTempFile`` is reusable for any scratch file.

SIGTERM, SIGHUP and SIGINT are trapped: the command is terminated and the same
verified cleanup runs while the identity is still in memory. SIGKILL cannot be
trapped; a cluster orphaned that way can no longer be verified by any run and
is deliberately left alone - remove it by hand once you have confirmed it is
disposable (the path and port are printed when the cluster is created).
"""

from __future__ import annotations

import argparse
import hmac
import os
import secrets
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

PREFIX = "fms_pg_scratch_"
MARKER_NAME = ".fms-scratch-owner"
DATA_DIR = "data"
USER = "fms"
DATABASE = "fms"


class NotVerified(Exception):
    """The target could not be proven to be this run's resource; nothing was touched."""


@dataclass
class ScratchCluster:
    root: Path
    base: Path
    token: str = field(repr=False)
    device: int
    inode: int
    port: int
    server_pid: int | None = None

    @property
    def data(self) -> Path:
        return self.root / DATA_DIR

    @property
    def url(self) -> str:
        return f"postgresql+psycopg://{USER}@127.0.0.1:{self.port}/{DATABASE}"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def prepare(base: str | os.PathLike[str] | None = None) -> ScratchCluster:
    """Create a new, stamped, empty directory for a cluster (no server yet)."""
    base_dir = Path(base or tempfile.gettempdir()).resolve()
    root = Path(tempfile.mkdtemp(prefix=PREFIX, dir=base_dir))
    info = os.lstat(root)
    token = secrets.token_hex(32)
    fd = os.open(root / MARKER_NAME, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, token.encode())
    finally:
        os.close(fd)
    return ScratchCluster(root=root, base=base_dir, token=token, device=info.st_dev,
                          inode=info.st_ino, port=_free_port())


# --- Verification -----------------------------------------------------------------


def _open_verified(cluster: ScratchCluster) -> tuple[int, int]:
    """Open (parent, root) directory fds and prove root is our cluster; raise NotVerified otherwise.

    The caller must close both fds. Every check is made on the opened
    descriptors, so what was verified is what the caller then operates on.
    """
    if cluster.root.parent != cluster.base or not cluster.root.name.startswith(PREFIX):
        raise NotVerified(f"{cluster.root} is not a {PREFIX}* directory directly under {cluster.base}")
    try:
        parent_fd = os.open(cluster.base, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise NotVerified(f"cannot open {cluster.base}: {exc}") from exc
    try:
        try:
            root_fd = os.open(cluster.root.name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:  # gone, a symlink, or not a directory
            raise NotVerified(f"{cluster.root} cannot be opened as a real directory: {exc}") from exc
        try:
            info = os.fstat(root_fd)
            if (info.st_dev, info.st_ino) != (cluster.device, cluster.inode):
                raise NotVerified(f"{cluster.root} is not the directory this run created (inode changed)")
            if info.st_uid != os.getuid():
                raise NotVerified(f"{cluster.root} is not owned by the current user")
            try:
                marker_fd = os.open(MARKER_NAME, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=root_fd)
            except OSError as exc:
                raise NotVerified(f"ownership marker in {cluster.root} is missing or not a regular file") from exc
            try:
                if not stat.S_ISREG(os.fstat(marker_fd).st_mode):
                    raise NotVerified("ownership marker is not a regular file")
                content = os.read(marker_fd, 256).decode(errors="replace")
            finally:
                os.close(marker_fd)
            if not hmac.compare_digest(content, cluster.token):
                raise NotVerified(f"ownership marker in {cluster.root} belongs to another run")
            return parent_fd, root_fd
        except BaseException:
            os.close(root_fd)
            raise
    except BaseException:
        os.close(parent_fd)
        raise


def verify(cluster: ScratchCluster) -> None:
    """Raise ``NotVerified`` unless ``cluster.root`` is provably this run's cluster."""
    parent_fd, root_fd = _open_verified(cluster)
    os.close(root_fd)
    os.close(parent_fd)


def _running_pid(root_fd: int) -> int | None:
    """PID from data/postmaster.pid (read without following symlinks), or None."""
    try:
        data_fd = os.open(DATA_DIR, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=root_fd)
    except FileNotFoundError:
        return None
    try:
        try:
            pid_fd = os.open("postmaster.pid", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=data_fd)
        except FileNotFoundError:
            return None
        try:
            first_line = os.read(pid_fd, 64).decode(errors="replace").splitlines()[0]
        finally:
            os.close(pid_fd)
    finally:
        os.close(data_fd)
    return int(first_line.strip())


# --- Owned temporary files -------------------------------------------------------------


@dataclass
class OwnedTempFile:
    """A scratch file this process created, deletable only after re-verification."""

    path: Path
    device: int
    inode: int
    token: str = field(repr=False)
    fd: int | None = None

    @property
    def header(self) -> bytes:
        return f"# fms-scratch {self.token}\n".encode()


def create_owned_tempfile(prefix: str = "fms_scratch_", suffix: str = ".log", directory: str | os.PathLike[str] | None = None) -> OwnedTempFile:
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=directory)
    info = os.fstat(fd)
    owned = OwnedTempFile(path=Path(name), device=info.st_dev, inode=info.st_ino, token=secrets.token_hex(16), fd=fd)
    os.write(fd, owned.header)
    return owned


def _verified_tempfile(owned: OwnedTempFile) -> int:
    """Open the parent directory and prove ``owned.path`` is still our file; returns the dir fd."""
    try:
        parent_fd = os.open(owned.path.parent, os.O_RDONLY | os.O_DIRECTORY)
    except OSError as exc:
        raise NotVerified(f"cannot open {owned.path.parent}: {exc}") from exc
    try:
        try:
            file_fd = os.open(owned.path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        except OSError as exc:
            raise NotVerified(f"{owned.path} cannot be opened as a regular file: {exc}") from exc
        try:
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode):
                raise NotVerified(f"{owned.path} is not a regular file")
            if (info.st_dev, info.st_ino) != (owned.device, owned.inode):
                raise NotVerified(f"{owned.path} is not the file this run created (inode changed)")
            if info.st_uid != os.getuid():
                raise NotVerified(f"{owned.path} is not owned by the current user")
            if not hmac.compare_digest(os.read(file_fd, len(owned.header)), owned.header):
                raise NotVerified(f"{owned.path} does not carry this run's token")
        finally:
            os.close(file_fd)
        return parent_fd
    except BaseException:
        os.close(parent_fd)
        raise


def remove_owned_tempfile(owned: OwnedTempFile) -> None:
    """Delete the file; raise ``NotVerified`` (touching nothing) unless it is provably ours."""
    if owned.fd is not None:
        os.close(owned.fd)
        owned.fd = None
    parent_fd = _verified_tempfile(owned)
    try:
        os.unlink(owned.path.name, dir_fd=parent_fd)
    finally:
        os.close(parent_fd)


# --- Lifecycle -----------------------------------------------------------------------


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise RuntimeError(f"{name} not found on PATH")
    return path


def initdb(cluster: ScratchCluster) -> None:
    verify(cluster)
    subprocess.run(
        [_binary("initdb"), "-D", str(cluster.data), "-U", USER, "--auth=trust", "-E", "UTF8"],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


def start(cluster: ScratchCluster) -> None:
    verify(cluster)
    options = (f"-p {cluster.port} -k {cluster.root} -c listen_addresses=127.0.0.1 "
               "-c fsync=off -c synchronous_commit=off -c full_page_writes=off")
    subprocess.run(
        [_binary("pg_ctl"), "-D", str(cluster.data), "-o", options, "-l", str(cluster.root / "server.log"), "-w", "start"],
        check=True, stdout=subprocess.DEVNULL,
    )
    parent_fd, root_fd = _open_verified(cluster)
    try:
        cluster.server_pid = _running_pid(root_fd)
    finally:
        os.close(root_fd)
        os.close(parent_fd)
    import psycopg  # local import: only needed when a server is actually started

    with psycopg.connect(host="127.0.0.1", port=cluster.port, user=USER, dbname="postgres", autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{DATABASE}"')


def stop(cluster: ScratchCluster) -> None:
    """Stop our server; raise ``NotVerified`` (leaving it running) if it is not provably ours.

    The server is identified through the verified data directory. If this run
    recorded a PID, the running server must be that PID. If it did not get
    that far (``start`` failed after launching the server), a server running
    on the verified - private, stamped, same-inode - directory can only be the
    one this run launched.
    """
    parent_fd, root_fd = _open_verified(cluster)
    try:
        pid = _running_pid(root_fd)
        if pid is None:
            cluster.server_pid = None
            return
        if cluster.server_pid is not None and pid != cluster.server_pid:
            raise NotVerified(f"postmaster.pid names PID {pid}, not the server this run started ({cluster.server_pid})")
    finally:
        os.close(root_fd)
        os.close(parent_fd)
    subprocess.run([_binary("pg_ctl"), "-D", str(cluster.data), "-m", "fast", "-w", "stop"],
                   check=True, stdout=subprocess.DEVNULL)
    cluster.server_pid = None


def remove(cluster: ScratchCluster) -> None:
    """Delete the cluster directory; raise ``NotVerified`` (touching nothing) unless provably ours."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise NotVerified("this platform's shutil.rmtree is not symlink-safe; refusing to delete")
    parent_fd, root_fd = _open_verified(cluster)
    try:
        if _running_pid(root_fd) is not None:
            raise NotVerified(f"a server is still running in {cluster.root}; stop it first")
        # Delete through the verified parent descriptor; rmtree re-checks that the
        # entry it opens is the same directory and never follows symlinks.
        shutil.rmtree(cluster.root.name, dir_fd=parent_fd)
    finally:
        os.close(root_fd)
        os.close(parent_fd)


class _Interrupted(Exception):
    def __init__(self, signum: int) -> None:
        super().__init__(signal.Signals(signum).name)
        self.signum = signum


def _raise_interrupted(signum, frame):  # noqa: ARG001 - signal handler signature
    raise _Interrupted(signum)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--keep", action="store_true", help="leave the cluster in place afterwards")
    parser.add_argument("--capture", action="store_true",
                        help="write the command's output to an owned temporary file; print its tail at the end")
    parser.add_argument("command", nargs=argparse.REMAINDER, help="command to run, after --")
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("give the command to run after --")

    for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(signum, _raise_interrupted)

    trapped = {signal.SIGTERM, signal.SIGHUP, signal.SIGINT}
    cluster = prepare()
    print(f"[scratch-postgres] cluster {cluster.root} on port {cluster.port}", file=sys.stderr, flush=True)
    status = 1
    child: subprocess.Popen | None = None
    capture: OwnedTempFile | None = create_owned_tempfile() if args.capture else None
    if capture is not None:
        print(f"[scratch-postgres] output captured in {capture.path}", file=sys.stderr, flush=True)
    try:
        # Signals are held back while the server is being created, so an interrupt
        # is only handled once its identity (PID) has been recorded.
        signal.pthread_sigmask(signal.SIG_BLOCK, trapped)
        try:
            initdb(cluster)
            start(cluster)
        finally:
            signal.pthread_sigmask(signal.SIG_UNBLOCK, trapped)  # a pending signal is raised here
        output = {"stdout": capture.fd, "stderr": subprocess.STDOUT} if capture is not None else {}
        child = subprocess.Popen(command, env={**os.environ, "FMS_TEST_POSTGRES_URL": cluster.url}, **output)
        status = child.wait()
    except _Interrupted as exc:
        print(f"[scratch-postgres] interrupted by {exc}; cleaning up", file=sys.stderr, flush=True)
        status = 128 + exc.signum
    finally:
        # From here on, further signals must not abort the verified cleanup half-way.
        for signum in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            signal.signal(signum, signal.SIG_IGN)
        if child is not None and child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=30)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        try:
            stop(cluster)
            if args.keep:
                print(f"[scratch-postgres] kept {cluster.root}", file=sys.stderr)
            else:
                remove(cluster)
                print(f"[scratch-postgres] removed {cluster.root}", file=sys.stderr)
        except NotVerified as exc:
            print(f"[scratch-postgres] NOT cleaned up, left as is: {exc}", file=sys.stderr)
            status = status or 1
        if capture is not None:
            _print_tail_and_remove(capture)
    return status


def _print_tail_and_remove(capture: OwnedTempFile, lines: int = 40) -> None:
    try:
        with open(capture.path, "rb") as handle:
            tail = handle.read().decode(errors="replace").splitlines()[-lines:]
    except OSError as exc:
        tail = [f"(could not read captured output: {exc})"]
    print("[scratch-postgres] captured output (tail):", file=sys.stderr)
    for line in tail:
        print("  " + line, file=sys.stderr)
    try:
        remove_owned_tempfile(capture)
        print(f"[scratch-postgres] removed {capture.path}", file=sys.stderr)
    except NotVerified as exc:
        print(f"[scratch-postgres] NOT removed, left as is: {exc}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
