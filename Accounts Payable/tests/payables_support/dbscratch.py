"""Disposable PostgreSQL databases with ownership tracking.

Rules (see ``docs/accounts-payable.md``, *Test infrastructure*):

* every run gets a **uniquely named** database (``fmsscratch_<label>_<time>_<pid>_<random>``);
* a database is created only if that name does **not** exist -- an existing database is
  never reused, overwritten or dropped; a collision means "pick another name";
* ownership is recorded in the database comment
  (``fms-scratch-owner:<token>;host=<hostname>;pid=<pid>;boot=<boot-id>;created=<utc>``);
  the secret token lives only in the creating process;
* a process drops a database only after re-reading the comment and matching **its own
  token** (:func:`drop_scratch_database`); anything else (foreign database, missing
  comment, wrong token) is refused;
* cleanup never uses ``DROP DATABASE IF EXISTS``, never drops by name pattern, and only
  forces disconnects for a database whose token it owns;
* a database whose creation failed is never dropped by the cleanup paths (there is
  nothing we own; the creating statement removes its own half-made database while the
  name is still unknown to anyone else).

Cleaning up after *other* runs (leaks of crashed processes) is a separate, explicit,
administrative action (:func:`administrative_cleanup`) that drops a database only when
ownership **can be established as abandoned**: the comment parses, the creator ran on
this host (same boot id), that pid no longer exists, the database is older than the
grace period and nobody is connected to it. Databases of live processes, of other
hosts, with unparsable comments, with connections, or younger than the grace period
are refused with a reason. Each decision is returned so it can be reviewed.
"""

import os
import secrets
import socket
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import make_url, text

from app.db import make_engine

NAME_PREFIX = "fmsscratch_"
OWNER_PREFIX = "fms-scratch-owner:"
_REGISTRY: dict[str, "ScratchDatabase"] = {}
_REGISTRY_LOCK = threading.Lock()


class ScratchDatabaseError(RuntimeError):
    pass


class ScratchNameCollision(ScratchDatabaseError):
    """A database with the generated name already exists; it is left untouched."""


class ScratchOwnershipError(ScratchDatabaseError):
    """The database is not (or no longer) owned by this process; it is left untouched."""


def boot_id() -> str:
    """Identifies this host's current boot; a pid is only meaningful within one boot."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as fh:
            return fh.read().strip()
    except OSError:
        return "unknown"


@dataclass(frozen=True)
class Ownership:
    token: str
    host: str
    pid: int | None
    boot: str
    created: datetime | None

    def comment(self) -> str:
        created = self.created.isoformat() if self.created else ""
        return (
            f"{OWNER_PREFIX}{self.token};host={self.host};pid={self.pid if self.pid is not None else ''};"
            f"boot={self.boot};created={created}"
        )


def parse_ownership(comment: str | None) -> Ownership | None:
    """Parse an owner comment; ``None`` when it is absent or not ours / malformed."""
    if not comment or not comment.startswith(OWNER_PREFIX):
        return None
    body = comment[len(OWNER_PREFIX) :]
    parts = body.split(";")
    token = parts[0]
    if not token or any(ch not in "0123456789abcdef" for ch in token):
        return None
    fields = {}
    for part in parts[1:]:
        if "=" not in part:
            return None
        key, value = part.split("=", 1)
        fields[key] = value
    try:
        pid = int(fields["pid"]) if fields.get("pid") else None
        created = datetime.fromisoformat(fields["created"]) if fields.get("created") else None
    except (KeyError, ValueError):
        return None
    if created is not None and created.tzinfo is None:
        created = created.replace(tzinfo=UTC)
    return Ownership(
        token=token,
        host=fields.get("host", ""),
        pid=pid,
        boot=fields.get("boot", ""),
        created=created,
    )


@dataclass(frozen=True)
class ScratchDatabase:
    admin_url: str
    name: str
    token: str
    ownership: Ownership = field(compare=False, default=None)

    @property
    def url(self) -> str:
        return str(make_url(self.admin_url).set(database=self.name))

    @property
    def owner_comment(self) -> str:
        return self.ownership.comment()


def generate_name(label: str = "test") -> str:
    stamp = _base36(int(time.time() * 1000))
    safe = "".join(ch for ch in label.lower() if ch.isalnum())[:12] or "test"
    return f"{NAME_PREFIX}{safe}_{stamp}_{os.getpid()}_{secrets.token_hex(4)}"[:63]


def _base36(value: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out or "0"


def _admin_engine(admin_url: str):
    return make_engine(admin_url).execution_options(isolation_level="AUTOCOMMIT")


def _exists(conn, name: str) -> bool:
    return (
        conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": name}).scalar()
        is not None
    )


def _comment(conn, name: str) -> str | None:
    return conn.execute(
        text("SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :n"),
        {"n": name},
    ).scalar()


def _connections(conn, name: str) -> int:
    return conn.execute(
        text(
            "SELECT COUNT(*) FROM pg_stat_activity WHERE datname = :n AND pid <> pg_backend_pid()"
        ),
        {"n": name},
    ).scalar()


def _quote(name: str) -> str:
    if not name.startswith(NAME_PREFIX) or any(
        ch not in "0123456789abcdefghijklmnopqrstuvwxyz_" for ch in name
    ):
        raise ScratchDatabaseError(
            f"refusing to touch a database that is not a scratch name: {name!r}"
        )
    return f'"{name}"'


def create_scratch_database(
    admin_url: str, *, label: str = "test", attempts: int = 3, name_factory=generate_name
) -> ScratchDatabase:
    """Create a uniquely named database owned by this process. Never touches existing ones."""
    admin = _admin_engine(admin_url)
    try:
        last_error: Exception | None = None
        for _ in range(attempts):
            name = name_factory(label)
            token = secrets.token_hex(16)
            ownership = Ownership(
                token=token,
                host=socket.gethostname(),
                pid=os.getpid(),
                boot=boot_id(),
                created=datetime.now(UTC).replace(microsecond=0),
            )
            db = ScratchDatabase(admin_url=admin_url, name=name, token=token, ownership=ownership)
            with admin.connect() as conn:
                if _exists(conn, name):
                    last_error = ScratchNameCollision(
                        f"database {name!r} already exists; not touching it"
                    )
                    continue
                conn.execute(text(f"CREATE DATABASE {_quote(name)}"))
                if not _exists(conn, name):
                    raise ScratchDatabaseError(f"database {name!r} was not created")
                try:
                    # COMMENT ON is DDL: no bind parameters. Every part of the comment is
                    # generated here (hex token, hostname, pid, boot id, timestamp).
                    comment = db.owner_comment.replace("'", "")
                    conn.execute(text(f"COMMENT ON DATABASE {_quote(name)} IS '{comment}'"))
                    if _comment(conn, name) != db.owner_comment:
                        raise ScratchDatabaseError(f"could not record ownership of {name!r}")
                except Exception:
                    # Created a moment ago by this statement and still untagged (or tagged by
                    # us): remove it while nobody else can know the name, then report.
                    if _comment(conn, name) in (None, db.owner_comment):
                        conn.execute(text(f"DROP DATABASE {_quote(name)}"))
                    raise
            with _REGISTRY_LOCK:
                _REGISTRY[name] = db
            return db
        raise last_error or ScratchDatabaseError("could not create a scratch database")
    finally:
        admin.dispose()


def verify_ownership(db: ScratchDatabase) -> None:
    admin = _admin_engine(db.admin_url)
    try:
        with admin.connect() as conn:
            if not _exists(conn, db.name):
                raise ScratchDatabaseError(f"database {db.name!r} does not exist")
            if _comment(conn, db.name) != db.owner_comment:
                raise ScratchOwnershipError(f"database {db.name!r} is not owned by this run")
    finally:
        admin.dispose()


def drop_scratch_database(db: ScratchDatabase) -> bool:
    """Drop ``db`` after proving ownership with the token. Returns False if it no longer exists.

    The only path that forces disconnects: it is our own database, so the only sessions
    that can be connected are ours.
    """
    admin = _admin_engine(db.admin_url)
    try:
        with admin.connect() as conn:
            if not _exists(conn, db.name):
                return False
            if _comment(conn, db.name) != db.owner_comment:
                raise ScratchOwnershipError(f"refusing to drop {db.name!r}: not owned by this run")
            conn.execute(text(f"DROP DATABASE {_quote(db.name)} WITH (FORCE)"))
        with _REGISTRY_LOCK:
            _REGISTRY.pop(db.name, None)
        return True
    finally:
        admin.dispose()


def reset_schema(db: ScratchDatabase) -> None:
    """Empty the owned database between tests (ownership is re-verified first)."""
    verify_ownership(db)
    engine = make_engine(db.url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@contextmanager
def scratch_database(admin_url: str, *, label: str = "test"):
    """Create an owned database for the block and drop exactly that database afterwards."""
    db = create_scratch_database(admin_url, label=label)
    try:
        yield db
    finally:
        try:
            drop_scratch_database(db)
        except ScratchOwnershipError:
            # Someone replaced our database under the same name: leave it alone.
            pass


def owned_databases() -> list[ScratchDatabase]:
    with _REGISTRY_LOCK:
        return list(_REGISTRY.values())


def drop_owned_databases() -> list[str]:
    """Cleanup by the owning process: drop every database this process created (token-verified)."""
    dropped = []
    for db in owned_databases():
        try:
            if drop_scratch_database(db):
                dropped.append(db.name)
        except ScratchOwnershipError:
            continue
    return dropped


@dataclass(frozen=True)
class ScratchListing:
    name: str
    comment: str | None
    ownership: Ownership | None
    connections: int


def list_scratch_databases(admin_url: str) -> list[ScratchListing]:
    """Databases carrying the scratch name prefix, with their parsed ownership (read-only)."""
    admin = _admin_engine(admin_url)
    try:
        with admin.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT datname, shobj_description(oid, 'pg_database') FROM pg_database WHERE datname LIKE :p"
                ),
                {"p": NAME_PREFIX + "%"},
            ).all()
            return [
                ScratchListing(name, comment, parse_ownership(comment), _connections(conn, name))
                for name, comment in rows
            ]
    finally:
        admin.dispose()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


@dataclass(frozen=True)
class CleanupDecision:
    name: str
    action: str  # dropped | refused
    reason: str


def classify_abandoned(
    listing: ScratchListing,
    *,
    now: datetime | None = None,
    grace: timedelta = timedelta(minutes=10),
    host: str | None = None,
    boot: str | None = None,
    pid_alive=_pid_alive,
) -> str | None:
    """Why a database must *not* be dropped administratively, or ``None`` if it is abandoned."""
    now = now or datetime.now(UTC)
    host = host or socket.gethostname()
    boot = boot or boot_id()
    own = listing.ownership
    if own is None:
        return "ownership cannot be established (missing or unparsable owner comment)"
    if own.host != host or own.boot != boot:
        return (
            f"created on another host/boot ({own.host}/{own.boot[:8]}); cannot verify its process"
        )
    if own.pid is None:
        return "owner comment has no pid; cannot verify its process"
    if pid_alive(own.pid):
        return f"owning process {own.pid} is still alive"
    if own.created is None or now - own.created < grace:
        return "younger than the grace period"
    if listing.connections:
        return f"{listing.connections} session(s) still connected"
    return None


def administrative_cleanup(
    admin_url: str,
    names: list[str],
    *,
    confirm: bool = False,
    operator: str = "",
    grace: timedelta = timedelta(minutes=10),
    tokens: dict[str, str] | None = None,
) -> list[CleanupDecision]:
    """Explicit maintenance for leaked scratch databases of *other* runs.

    Drops one of the *listed* names only when (a) ``tokens`` supplies the database's own
    token (cross-run ownership handed over explicitly), or (b) the database is proven
    abandoned by :func:`classify_abandoned`. Requires ``confirm=True`` and an operator
    name; never forces disconnects; every name gets a decision with a reason.
    """
    if not confirm or not operator:
        return [
            CleanupDecision(
                n, "refused", "administrative cleanup requires confirm=True and an operator"
            )
            for n in names
        ]
    listings = {entry.name: entry for entry in list_scratch_databases(admin_url)}
    decisions: list[CleanupDecision] = []
    admin = _admin_engine(admin_url)
    try:
        with admin.connect() as conn:
            for name in names:
                entry = listings.get(name)
                if entry is None:
                    decisions.append(
                        CleanupDecision(name, "refused", "not a scratch database on this server")
                    )
                    continue
                own = entry.ownership
                handed_over = (
                    tokens is not None and own is not None and tokens.get(name) == own.token
                )
                if handed_over:
                    reason = "ownership token supplied by the owning run"
                    if entry.connections:
                        decisions.append(
                            CleanupDecision(
                                name, "refused", f"{entry.connections} session(s) still connected"
                            )
                        )
                        continue
                else:
                    refusal = classify_abandoned(entry, grace=grace)
                    if refusal is not None:
                        decisions.append(CleanupDecision(name, "refused", refusal))
                        continue
                    reason = f"abandoned by dead process {own.pid} on this host"
                # Re-read right before dropping: the comment must still be the one we judged.
                if _comment(conn, name) != entry.comment:
                    decisions.append(
                        CleanupDecision(name, "refused", "ownership changed while deciding")
                    )
                    continue
                conn.execute(
                    text(f"DROP DATABASE {_quote(name)}")
                )  # no FORCE: connections refuse it
                decisions.append(CleanupDecision(name, "dropped", f"{reason} (by {operator})"))
    finally:
        admin.dispose()
    return decisions


def drop_listed_scratch_databases(
    admin_url: str, names: list[str], *, confirm: bool = False, operator: str = "maintenance"
) -> list[str]:
    """Backwards-compatible wrapper: the names are handed to :func:`administrative_cleanup`;
    a name prefix and an owner-comment prefix are *not* grounds for deletion any more."""
    return [
        d.name
        for d in administrative_cleanup(admin_url, names, confirm=confirm, operator=operator)
        if d.action == "dropped"
    ]
