"""The scratch PostgreSQL cluster tool only ever stops/deletes the exact cluster it created.

Every test here builds its own directories under pytest's ``tmp_path``; the
tool is pointed at them explicitly, so nothing outside the test's own tmp dir
can be touched even if a check were wrong.
"""

import os
import shutil
import subprocess
import sys

import pytest

from tests import scratch_postgres as sp
from tests.conftest import PROJECT_ROOT

HAVE_POSTGRES_BINARIES = shutil.which("initdb") is not None and shutil.which("pg_ctl") is not None
requires_binaries = pytest.mark.skipif(not HAVE_POSTGRES_BINARIES, reason="initdb/pg_ctl not available")


@pytest.fixture
def cluster(tmp_path):
    return sp.prepare(base=tmp_path)


def _forbid_subprocess(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError(f"a process was started although verification should have failed: {args}")

    monkeypatch.setattr(sp.subprocess, "run", refuse)


# --- Creation ------------------------------------------------------------------------


def test_prepare_creates_a_unique_private_stamped_directory(tmp_path):
    a = sp.prepare(base=tmp_path)
    b = sp.prepare(base=tmp_path)
    assert a.root != b.root and a.token != b.token
    assert a.root.parent == tmp_path.resolve() and a.root.name.startswith(sp.PREFIX)
    assert oct(a.root.stat().st_mode & 0o777) == "0o700"
    assert (a.root / sp.MARKER_NAME).read_text() == a.token
    sp.verify(a)
    sp.verify(b)


def test_remove_deletes_the_verified_cluster(cluster):
    (cluster.root / "data").mkdir()
    (cluster.root / "data" / "file").write_text("x")
    sp.remove(cluster)
    assert not cluster.root.exists()


# --- Refusals: the target is not provably ours -> nothing is touched -------------------


def test_refuses_when_the_marker_belongs_to_another_run(cluster):
    (cluster.root / sp.MARKER_NAME).write_text("someone-else")
    with pytest.raises(sp.NotVerified, match="another run"):
        sp.remove(cluster)
    assert cluster.root.exists()


def test_refuses_when_the_marker_is_missing(cluster):
    (cluster.root / sp.MARKER_NAME).unlink()
    with pytest.raises(sp.NotVerified, match="missing"):
        sp.remove(cluster)
    assert cluster.root.exists()


def test_refuses_when_the_marker_is_a_symlink(cluster, tmp_path):
    real = tmp_path / "real_marker"
    real.write_text(cluster.token)  # even with the right content
    (cluster.root / sp.MARKER_NAME).unlink()
    (cluster.root / sp.MARKER_NAME).symlink_to(real)
    with pytest.raises(sp.NotVerified):
        sp.remove(cluster)
    assert cluster.root.exists() and real.exists()


def test_refuses_when_the_directory_was_deleted_and_recreated(cluster):
    """Same path (the filesystem may even reuse the inode number) but not our stamp."""
    shutil.rmtree(cluster.root)
    cluster.root.mkdir()
    (cluster.root / "someone_elses_file").write_text("keep me")
    with pytest.raises(sp.NotVerified):
        sp.remove(cluster)
    assert (cluster.root / "someone_elses_file").read_text() == "keep me"


def test_refuses_a_different_directory_even_with_a_copied_token(cluster, tmp_path):
    """The token alone is not enough: it must be the very directory this run created."""
    impostor = tmp_path / "impostor"
    impostor.mkdir()
    (impostor / sp.MARKER_NAME).write_text(cluster.token)
    (impostor / "someone_elses_file").write_text("keep me")
    original = cluster.root.rename(tmp_path / "moved_away")  # both exist: inodes must differ
    impostor.rename(cluster.root)
    with pytest.raises(sp.NotVerified, match="inode changed"):
        sp.remove(cluster)
    assert (cluster.root / "someone_elses_file").read_text() == "keep me"
    assert original.exists()


def test_refuses_when_the_directory_was_swapped_for_a_symlink(cluster, tmp_path):
    victim = tmp_path / "victim"
    victim.mkdir()
    (victim / sp.MARKER_NAME).write_text(cluster.token)
    (victim / "precious").write_text("keep me")
    shutil.rmtree(cluster.root)
    cluster.root.symlink_to(victim, target_is_directory=True)
    with pytest.raises(sp.NotVerified, match="real directory"):
        sp.remove(cluster)
    assert (victim / "precious").read_text() == "keep me"


def test_refuses_a_path_that_is_not_the_created_one(cluster, tmp_path):
    other = tmp_path / "not_a_scratch_dir"
    other.mkdir()
    (other / sp.MARKER_NAME).write_text(cluster.token)
    cluster.root = other
    with pytest.raises(sp.NotVerified):
        sp.remove(cluster)
    assert other.exists()


def test_refuses_to_delete_while_a_server_is_running(cluster):
    (cluster.root / "data").mkdir()
    (cluster.root / "data" / "postmaster.pid").write_text("12345\n")
    with pytest.raises(sp.NotVerified, match="still running"):
        sp.remove(cluster)
    assert cluster.root.exists()


def test_stop_refuses_a_server_it_did_not_start(cluster, monkeypatch):
    (cluster.root / "data").mkdir()
    (cluster.root / "data" / "postmaster.pid").write_text("99999\n")
    cluster.server_pid = 12345
    _forbid_subprocess(monkeypatch)
    with pytest.raises(sp.NotVerified, match="not the server this run started"):
        sp.stop(cluster)
    assert cluster.server_pid == 12345  # still considered running; nothing was done


def test_stop_refuses_when_the_cluster_is_not_verified(cluster, monkeypatch):
    cluster.server_pid = 12345
    (cluster.root / "data").mkdir()
    (cluster.root / "data" / "postmaster.pid").write_text("12345\n")
    (cluster.root / sp.MARKER_NAME).write_text("tampered")
    _forbid_subprocess(monkeypatch)
    with pytest.raises(sp.NotVerified):
        sp.stop(cluster)


def test_deletion_never_follows_symlinks_inside_the_cluster(cluster, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious").write_text("keep me")
    (cluster.root / "link_to_outside").symlink_to(outside, target_is_directory=True)
    sp.remove(cluster)
    assert not cluster.root.exists()
    assert (outside / "precious").read_text() == "keep me"


def test_the_token_is_not_exposed_in_repr(cluster):
    assert cluster.token not in repr(cluster)


# --- Real server (only where PostgreSQL binaries exist) -----------------------------------------


@requires_binaries
def test_full_lifecycle_with_a_real_server(tmp_path):
    import psycopg

    cluster = sp.prepare(base=tmp_path)
    sp.initdb(cluster)
    sp.start(cluster)
    try:
        assert cluster.server_pid is not None
        with psycopg.connect(host="127.0.0.1", port=cluster.port, user=sp.USER, dbname=sp.DATABASE) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)

        # tampering is detected right before stopping: the server is left running
        marker = cluster.root / sp.MARKER_NAME
        marker.write_text("tampered")
        with pytest.raises(sp.NotVerified):
            sp.stop(cluster)
        with psycopg.connect(host="127.0.0.1", port=cluster.port, user=sp.USER, dbname=sp.DATABASE) as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        marker.write_text(cluster.token)  # restore the stamp this test created
    finally:
        sp.stop(cluster)
        sp.remove(cluster)
    assert not cluster.root.exists()


@requires_binaries
def test_command_line_runs_a_command_against_a_fresh_cluster_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setenv("TMPDIR", str(tmp_path))  # keep the cluster inside this test's tmp dir
    code = "import os; print(os.environ['FMS_TEST_POSTGRES_URL'])"
    result = subprocess.run([sys.executable, "-m", "tests.scratch_postgres", "--", sys.executable, "-c", code],
                            cwd=PROJECT_ROOT, capture_output=True, text=True, env={**os.environ, "TMPDIR": str(tmp_path)})
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("postgresql+psycopg://fms@127.0.0.1:")
    assert "removed" in result.stderr
    assert not any(p.name.startswith(sp.PREFIX) for p in tmp_path.iterdir())


@requires_binaries
@pytest.mark.parametrize("signame", ["SIGTERM", "SIGHUP", "SIGINT"])
def test_command_line_cleans_up_when_interrupted(tmp_path, signame):
    """A trappable signal still ends in the verified cleanup (the identity is still in memory).

    The signal is sent as early as possible - as soon as the server writes its
    pid file, i.e. while ``start`` may still be running - which is the window
    in which an unrecorded server used to be left behind.
    """
    import signal
    import time

    proc = subprocess.Popen(
        [sys.executable, "-m", "tests.scratch_postgres", "--", sys.executable, "-c", "import time; time.sleep(120)"],
        cwd=PROJECT_ROOT, stderr=subprocess.PIPE, text=True, env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    try:
        deadline = time.monotonic() + 60
        while not any((p / "data" / "postmaster.pid").exists() for p in tmp_path.glob(f"{sp.PREFIX}*")):
            assert time.monotonic() < deadline, "cluster did not start"
            time.sleep(0.2)
        proc.send_signal(getattr(signal, signame))
        _, stderr = proc.communicate(timeout=60)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "interrupted" in stderr and "removed" in stderr, stderr
    assert not any(p.name.startswith(sp.PREFIX) for p in tmp_path.iterdir())


# --- Owned temporary files ----------------------------------------------------------------------


def test_owned_tempfile_is_unique_private_and_tokenised(tmp_path):
    a = sp.create_owned_tempfile(directory=tmp_path)
    b = sp.create_owned_tempfile(directory=tmp_path)
    assert a.path != b.path and a.token != b.token
    assert oct(a.path.stat().st_mode & 0o777) == "0o600"
    assert a.path.read_bytes() == a.header
    sp.remove_owned_tempfile(a)
    sp.remove_owned_tempfile(b)
    assert not a.path.exists() and not b.path.exists()


def test_owned_tempfile_refusals_leave_the_target_alone(tmp_path):
    owned = sp.create_owned_tempfile(directory=tmp_path)
    os.close(owned.fd)
    owned.fd = None
    os.link(owned.path, tmp_path / "keepalive")  # keeps the original inode allocated throughout

    owned.path.write_bytes(b"# fms-scratch someone-elses-token\nprecious")  # token gone (same inode)
    with pytest.raises(sp.NotVerified, match="token"):
        sp.remove_owned_tempfile(owned)
    assert owned.path.read_bytes().endswith(b"precious")

    victim = tmp_path / "victim"
    victim.write_bytes(owned.header + b"keep me")
    owned.path.unlink()
    owned.path.symlink_to(victim)  # swapped for a symlink to a file with the right header
    with pytest.raises(sp.NotVerified):
        sp.remove_owned_tempfile(owned)
    assert victim.read_bytes().endswith(b"keep me")

    other = tmp_path / "other"
    other.write_bytes(owned.header)  # created while the symlink still occupies the path: distinct inode
    owned.path.unlink()
    other.rename(owned.path)  # a different file carrying a copied header
    with pytest.raises(sp.NotVerified, match="inode"):
        sp.remove_owned_tempfile(owned)
    assert owned.path.exists()


@requires_binaries
def test_command_line_capture_uses_an_owned_file_and_removes_it(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tests.scratch_postgres", "--capture", "--", sys.executable, "-c", "print('hello from the command')"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, env={**os.environ, "TMPDIR": str(tmp_path)},
    )
    assert result.returncode == 0, result.stderr
    assert "hello from the command" in result.stderr  # shown in the tail
    assert "output captured in" in result.stderr and result.stderr.count("removed") == 2
    assert not any(p.name.startswith("fms_scratch_") for p in tmp_path.iterdir())
