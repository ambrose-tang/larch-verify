"""Running a service under test: a throwaway database, the service process, and resets.

A `service` subject in LARCH.md says how to start it:

    ## service orders
    start: uvicorn shop.api:app --port {port}   # {port} is a free port Larch picks
    database: postgres                          # postgres | sqlite | none | an external URL
    setup: python -m shop.migrate               # optional, once after the database is up
    reset: POST /test/reset                     # optional; default: empty every table
    openapi: /openapi.json                      # optional (FastAPI and most frameworks serve one)
    cwd: .                                      # optional, relative to LARCH.md

Larch starts a database that exists only for the run, passes it to the service as
DATABASE_URL (and PORT), and empties it between call sequences, so every sequence starts
from the same state. Postgres runs in Docker when a Docker daemon is reachable,
otherwise from local Postgres binaries (as an unprivileged user when Larch runs as root).
Nothing outside the run's scratch directory is touched.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path


class ServiceError(RuntimeError):
    pass


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_port(host: str, port: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.2)
    return False


# ---------------------------------------------------------------------------
# Databases
# ---------------------------------------------------------------------------

@dataclass
class Database:
    kind: str  # postgres | sqlite | none | external
    url: str = ""
    how: str = ""  # human description
    _stop: list = field(default_factory=list)
    _psql: list[str] = field(default_factory=list)  # command prefix running psql against the database
    _sqlite_path: str = ""

    def reset(self) -> None:
        """Empty every table (keeping the schema), restarting identity sequences."""
        if self.kind == "postgres" or (self.kind == "external" and self.url.startswith("postgres")):
            sql = ("DO $$ DECLARE r record; BEGIN FOR r IN SELECT tablename FROM pg_tables WHERE schemaname = 'public' "
                   "AND tablename NOT IN ('alembic_version', 'schema_migrations', 'flyway_schema_history', "
                   "'knex_migrations', '_prisma_migrations') LOOP EXECUTE 'TRUNCATE TABLE ' || quote_ident(r.tablename) "
                   "|| ' RESTART IDENTITY CASCADE'; END LOOP; END $$;")
            r = subprocess.run(self._psql + ["-v", "ON_ERROR_STOP=1", "-q", "-c", sql], capture_output=True, text=True, timeout=60)
            if r.returncode != 0:
                raise ServiceError(f"could not reset the database: {r.stderr.strip()[-500:]}")
        elif self.kind == "sqlite" and self._sqlite_path and os.path.exists(self._sqlite_path):
            import sqlite3

            con = sqlite3.connect(self._sqlite_path)
            try:
                names = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' "
                                                    "AND name NOT IN ('alembic_version', 'schema_migrations')")]
                for n in names:
                    con.execute(f'DELETE FROM "{n}"')
                if any(r[0] == "sqlite_sequence" for r in con.execute("SELECT name FROM sqlite_master")):
                    con.execute("DELETE FROM sqlite_sequence")
                con.commit()
            finally:
                con.close()

    def stop(self) -> None:
        for fn in reversed(self._stop):
            try:
                fn()
            except Exception:  # noqa: BLE001
                pass
        self._stop.clear()


def _docker_ok() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=20).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pg_bin() -> Path | None:
    w = shutil.which("pg_ctl")
    if w:
        return Path(w).parent
    for d in sorted(Path("/usr/lib/postgresql").glob("*/bin"), reverse=True) + sorted(Path("/opt/homebrew/opt").glob("postgresql*/bin")):
        if (d / "pg_ctl").exists():
            return d
    return None


def start_database(kind: str, workdir: Path, image: str = "postgres:16-alpine") -> Database:
    kind = (kind or "none").strip()
    if kind in ("", "none"):
        return Database("none", how="no database")
    if "://" in kind:
        db = Database("external", url=kind, how=f"external database {kind.split('@')[-1]}")
        if kind.startswith("postgres") and shutil.which("psql"):
            db._psql = ["psql", kind]
        return db
    if kind == "sqlite":
        path = workdir / "larch.sqlite3"
        return Database("sqlite", url=f"sqlite:///{path}", how=f"SQLite file {path}", _sqlite_path=str(path))
    if kind != "postgres":
        raise ServiceError(f"unknown database {kind!r} (use postgres, sqlite, none, or a URL)")
    if _docker_ok():
        port = free_port()
        name = f"larch-pg-{os.getpid()}-{port}"
        r = subprocess.run(["docker", "run", "-d", "--rm", "--name", name, "-e", "POSTGRES_PASSWORD=larch", "-e", "POSTGRES_DB=larch",
                            "-p", f"127.0.0.1:{port}:5432", image], capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise ServiceError(f"docker could not start {image}: {r.stderr.strip()[-500:]}")
        db = Database("postgres", url=f"postgresql://postgres:larch@127.0.0.1:{port}/larch", how=f"Postgres in Docker ({image})")
        db._stop.append(lambda: subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=60))
        db._psql = ["docker", "exec", "-i", name, "psql", "-U", "postgres", "-d", "larch"]
        _wait_ready(db, ["docker", "exec", name, "pg_isready", "-U", "postgres", "-d", "larch"])
        return db
    bindir = _pg_bin()
    if bindir is None:
        raise ServiceError("`database: postgres` needs Docker or local Postgres binaries (initdb, pg_ctl); neither was found")
    run_as: list[str] = []
    base = _shared_tmp() if os.geteuid() == 0 else workdir
    data = Path(tempfile.mkdtemp(prefix="larch-pg-", dir=base))
    sock = Path(tempfile.mkdtemp(prefix="pgs-", dir=_shared_tmp()))  # short path: unix sockets have a length limit
    if os.geteuid() == 0:
        # Postgres refuses to run as root: run it as an unprivileged account.
        user = next((u for u in ("postgres", "nobody") if _user_exists(u)), None)
        if user is None or not shutil.which("runuser"):
            raise ServiceError("running as root: local Postgres needs a `postgres` user and runuser (or use Docker)")
        for d in (data, sock):
            shutil.chown(d, user=user)
        os.chmod(sock, 0o777)
        run_as = ["runuser", "-u", user, "--"]
    r = subprocess.run(run_as + [str(bindir / "initdb"), "-D", str(data), "-A", "trust", "-U", "postgres", "--no-sync"],
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise ServiceError(f"initdb failed: {r.stderr.strip()[-500:]}")
    port = free_port()
    log = data / "server.log"
    r = subprocess.run(run_as + [str(bindir / "pg_ctl"), "-D", str(data), "-l", str(log), "-w", "-t", "60", "-o",
                                 f"-p {port} -k {sock} -c listen_addresses=127.0.0.1 -c fsync=off -c synchronous_commit=off "
                                 f"-c full_page_writes=off", "start"], capture_output=True, text=True, timeout=120)
    if r.returncode != 0:
        tail = log.read_text(errors="replace")[-800:] if log.exists() else r.stderr
        raise ServiceError(f"pg_ctl could not start Postgres: {tail.strip()}")
    db = Database("postgres", url=f"postgresql://postgres@127.0.0.1:{port}/postgres", how=f"local Postgres ({bindir.parent.name})")
    db._stop.append(lambda: shutil.rmtree(sock, ignore_errors=True))
    db._stop.append(lambda: shutil.rmtree(data, ignore_errors=True))
    db._stop.append(lambda: subprocess.run(run_as + [str(bindir / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                                           capture_output=True, timeout=60))
    db._psql = [str(bindir / "psql"), "-h", "127.0.0.1", "-p", str(port), "-U", "postgres", "-d", "postgres"]
    return db


def _shared_tmp() -> Path:
    """A temp directory an unprivileged account can reach (the run's scratch may not be)."""
    for d in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        p = Path(d)
        if p.is_dir() and os.stat(p).st_mode & 0o001:
            return p
    return Path(tempfile.gettempdir())


def _user_exists(name: str) -> bool:
    try:
        import pwd

        pwd.getpwnam(name)
        return True
    except (KeyError, ImportError):
        return False


def _wait_ready(db: Database, probe: list[str], timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if subprocess.run(probe, capture_output=True).returncode == 0 and \
                subprocess.run(db._psql + ["-c", "SELECT 1"], capture_output=True).returncode == 0:
            return
        time.sleep(0.5)
    db.stop()
    raise ServiceError("the database did not become ready in time")


# ---------------------------------------------------------------------------
# The service process
# ---------------------------------------------------------------------------

@dataclass
class ServiceHandle:
    """What the test driver needs to talk to a running service: its URL and how to reset
    it. Serializable, so the driver process (and mutant runs) can rebuild it."""

    name: str
    base_url: str
    db: Database
    settings: dict
    cwd: Path

    def to_json(self) -> dict:
        return {"name": self.name, "base_url": self.base_url, "settings": self.settings, "cwd": str(self.cwd),
                "db": {"kind": self.db.kind, "url": self.db.url, "how": self.db.how, "psql": self.db._psql,
                       "sqlite": self.db._sqlite_path}}

    @staticmethod
    def from_json(d: dict) -> "ServiceHandle":
        db = d["db"]
        return ServiceHandle(d["name"], d["base_url"], Database(db["kind"], db["url"], db["how"], _psql=db["psql"], _sqlite_path=db["sqlite"]),
                             d["settings"], Path(d["cwd"]))

    def request(self, method: str, path: str, *, body=None, headers: dict | None = None, timeout: float = 10.0) -> tuple[int, object]:
        data = None if body is None else json.dumps(body).encode()
        hdrs = {"Accept": "application/json", **({"Content-Type": "application/json"} if body is not None else {}), **(headers or {})}
        req = urllib.request.Request(self.base_url + path, data=data, method=method.upper(), headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw, status = resp.read(), resp.status
        except urllib.error.HTTPError as e:
            raw, status = e.read(), e.code
        try:
            return status, json.loads(raw) if raw else None
        except (ValueError, UnicodeDecodeError):
            return status, raw.decode(errors="replace")

    def reset(self) -> None:
        """Bring the service back to its initial state between call sequences."""
        how = self.settings.get("reset", "").strip()
        self.db.reset()
        if how and how.split()[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH"):
            method, path = how.split(None, 1)
            status, body = self.request(method, path.strip())
            if status >= 400:
                raise ServiceError(f"`reset: {how}` answered {status}: {body}")
        elif how and how != "database":
            r = subprocess.run(how, shell=True, cwd=self.cwd, capture_output=True, text=True, timeout=120,
                               env={**os.environ, "DATABASE_URL": self.db.url})
            if r.returncode != 0:
                raise ServiceError(f"`reset: {how}` failed: {r.stderr.strip()[-500:]}")

    def openapi(self) -> dict | None:
        path = self.settings.get("openapi", "/openapi.json")
        try:
            status, doc = self.request("GET", path)
        except OSError:
            return None
        return doc if status == 200 and isinstance(doc, dict) else None



@dataclass
class Service(ServiceHandle):
    proc: subprocess.Popen | None = None
    log_path: Path | None = None

    def handle(self) -> ServiceHandle:
        return ServiceHandle(self.name, self.base_url, self.db, self.settings, self.cwd)

    def alive(self) -> bool:
        return self.proc.poll() is None

    def stop(self, keep_db: bool = False) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
                self.proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                try:
                    os.killpg(self.proc.pid, signal.SIGKILL)
                except Exception:  # noqa: BLE001
                    pass
        if not keep_db:
            self.db.stop()


def project_path_env(cwd: Path) -> str:
    """PATH with the project's virtualenv first, so `uvicorn`, `gunicorn`, ... resolve to it."""
    from .py.env import candidates

    path = os.environ.get("PATH", "")
    for exe, why in candidates(cwd / "_.py"):
        if why.startswith(("project virtualenv", "active virtualenv", "$UV_PROJECT_ENVIRONMENT", "poetry", "pipenv", "pdm", "hatch")):
            return str(Path(exe).parent) + os.pathsep + path
    node_bin = next((d / "node_modules" / ".bin" for d in [cwd, *cwd.parents] if (d / "node_modules" / ".bin").is_dir()), None)
    return (str(node_bin) + os.pathsep + path) if node_bin else path


def start_service(name: str, settings: dict, root: Path, workdir: Path, *, cwd_override: Path | None = None,
                  database: Database | None = None, startup_timeout: float = 90.0,
                  path_from: Path | None = None) -> Service:
    if not settings.get("start"):
        raise ServiceError(f"service {name}: LARCH.md must give `start:` (the command that runs it, with {{port}})")
    cwd = cwd_override or (root / settings.get("cwd", ".")).resolve()
    workdir.mkdir(parents=True, exist_ok=True)
    owns_db = database is None  # a database passed in is shared; never stop it here
    db = database or start_database(settings.get("database", "none"), workdir)
    port = free_port()
    env = {**os.environ, "PORT": str(port), "LARCH_TEST": "1", "PATH": project_path_env(path_from or cwd), "PYTHONDONTWRITEBYTECODE": "1"}
    if db.url:
        env["DATABASE_URL"] = db.url
    if settings.get("setup"):
        r = subprocess.run(settings["setup"], shell=True, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            if owns_db:
                db.stop()
            raise ServiceError(f"`setup: {settings['setup']}` failed: {(r.stderr or r.stdout).strip()[-800:]}")
    cmd = settings["start"].replace("{port}", str(port))
    log_path = workdir / f"service-{name}.log"
    log = open(log_path, "ab")
    proc = subprocess.Popen(cmd, shell=True, cwd=cwd, env=env, stdout=log, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL, start_new_session=True)
    log.close()
    svc = Service(name, f"http://127.0.0.1:{port}", db, settings, cwd, proc=proc, log_path=log_path)
    if not _wait_port("127.0.0.1", port, startup_timeout) or proc.poll() is not None:
        tail = log_path.read_text(errors="replace")[-1500:]
        svc.stop(keep_db=not owns_db)
        raise ServiceError(f"service {name} did not start (`{cmd}` in {cwd}):\n{tail.strip()}")
    return svc


def describe_start(settings: dict) -> str:
    return " ".join(shlex.split(settings.get("start", "")))[:120]
