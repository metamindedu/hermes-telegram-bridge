"""Fail-closed, test-process-only SQLite isolation for canonical bridge tests.

This module is standard-library-only. ``guarded_unittest.py`` loads it in a fresh
``python -I -S`` process, validates and marks an exclusive fixture root, then installs
this fence before adding Hermes or dependency import paths.
"""
from __future__ import annotations

import importlib.abc
import importlib.machinery
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
from urllib.parse import parse_qsl, unquote, urlsplit

ISOLATION_KEY = "_hermes_telegram_bridge_db_isolation"
ROOT_MARKER = ".hermes-telegram-bridge-owned-v1"
_CORE_PREFIXES = (
    "agent", "gateway", "hermes_state", "hermes_constants", "hermes_cli",
    "hermes_state_registry", "hermes_state_runtime", "tools", "acp_adapter",
)
_BAD_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_WINDOWS_ABSOLUTE = re.compile(r"^[A-Za-z]:[\\/]")


class IsolationError(RuntimeError):
    """A requested SQLite target or test root is not proven to be fixture-owned."""


class DatabaseFence:
    def __init__(self, fixture_root: Path, artifact_root: Path, token: str):
        self.fixture_root = self._verify_marked_root(fixture_root, artifact_root, token)
        self.artifact_root = Path(artifact_root).resolve(strict=True)
        self.token = token
        self._lock = threading.Lock()
        self.counters = {
            "rejected_db_targets": 0,
            "guarded_connect_calls": 0,
            "delegated_connect_calls": 0,
            "sqlite_audit_authorized": 0,
            "sqlite_audit_rejected": 0,
            "connections_returned": 0,
            "attach_authorized": 0,
            "attach_denied": 0,
            "native_constructor_delegations": 0,
            "native_acquire_delegations": 0,
            "session_store_open_delegations": 0,
            "epoch_calls": 0,
        }
        self.core_root: Path | None = None
        self.core_ref: str | None = None
        self._guarded_connection_type = None
        self._raw_connect = None
        self._native_fences_installed = False
        self._authority_fence_installed = False
        self._native_restore = []
        self.core_finder = None
        self.ready = False

    @staticmethod
    def _verify_marked_root(fixture_root: Path, artifact_root: Path, token: str) -> Path:
        fixture = Path(fixture_root).resolve(strict=True)
        artifact = Path(artifact_root).resolve(strict=True)
        if fixture == artifact or not fixture.is_relative_to(artifact):
            raise IsolationError("fixture root is not a child of its owned artifact root")
        if not token or len(token) < 24:
            raise IsolationError("test-owned root marker is missing or ambiguous")
        for root in (artifact, fixture):
            marker = root / ROOT_MARKER
            try:
                marker_value = marker.read_text(encoding="ascii")
            except OSError as exc:
                raise IsolationError("test-owned root marker cannot be verified") from exc
            if marker_value != token:
                raise IsolationError("test-owned root marker does not match this run")
        return fixture

    def _bump(self, name: str, amount: int = 1) -> None:
        with self._lock:
            self.counters[name] += amount

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self.counters)

    def require_ready(self) -> "DatabaseFence":
        if not self.ready or sys.modules.get(ISOLATION_KEY) is not self:
            raise IsolationError(
                "DB isolation bootstrap is absent; use python -I -S guarded_unittest.py"
            )
        self._verify_marked_root(self.fixture_root, self.artifact_root, self.token)
        return self

    @staticmethod
    def _inside(target: Path, root: Path) -> bool:
        target_text = os.path.normcase(os.path.normpath(str(target)))
        root_text = os.path.normcase(os.path.normpath(str(root)))
        try:
            return os.path.commonpath((target_text, root_text)) == root_text
        except (ValueError, OSError):
            return False

    def validate_path(self, value) -> Path:
        """Resolve one filesystem path physically, case-aware, and without opening it."""
        self.require_ready()
        try:
            raw = os.fspath(value)
            if not isinstance(raw, str) or not raw or "\x00" in raw:
                raise ValueError("not a non-empty text path")
            # A Windows absolute path passed to a non-Windows runner must not quietly
            # become an innocuous relative filename.
            if os.name != "nt" and _WINDOWS_ABSOLUTE.match(raw):
                raise ValueError("foreign absolute path")
            candidate = Path(raw)
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            resolved = candidate.resolve(strict=False)
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._bump("rejected_db_targets")
            raise IsolationError("SQLite target path could not be resolved safely") from exc
        if not self._inside(resolved, self.fixture_root):
            self._bump("rejected_db_targets")
            raise IsolationError("SQLite target is outside the marked fixture root")
        return resolved

    def database_path(self, database, *, uri: bool | None = None) -> Path | None:
        """Return the physical file target, or ``None`` for an in-memory database."""
        self.require_ready()
        if isinstance(database, os.PathLike):
            try:
                database = os.fspath(database)
            except TypeError as exc:
                self._bump("rejected_db_targets")
                raise IsolationError("SQLite target is not a supported path") from exc
        if not isinstance(database, str) or not database or "\x00" in database:
            self._bump("rejected_db_targets")
            raise IsolationError("SQLite target is not a supported path")
        if database == ":memory:":
            return None
        is_uri = bool(uri) or database.startswith("file:")
        if not is_uri:
            return self.validate_path(database)
        try:
            if not database.startswith("file:") or _BAD_PERCENT.search(database):
                raise ValueError("unsupported SQLite URI")
            parsed = urlsplit(database)
            if parsed.scheme != "file" or parsed.netloc not in ("", "localhost"):
                raise ValueError("unsupported SQLite URI authority")
            query_pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
            query = {}
            for key, value in query_pairs:
                if key in query:
                    raise ValueError("duplicate SQLite URI option")
                query[key] = value
            if set(query) - {"mode", "cache", "immutable"}:
                raise ValueError("unsupported SQLite URI option")
            if query.get("mode") not in (None, "ro", "rw", "rwc", "memory"):
                raise ValueError("unsupported SQLite URI mode")
            if query.get("cache") not in (None, "shared", "private"):
                raise ValueError("unsupported SQLite URI cache")
            if query.get("immutable") not in (None, "0", "1"):
                raise ValueError("unsupported SQLite URI immutable option")
            decoded = unquote(parsed.path, encoding="utf-8", errors="strict")
            if decoded in (":memory:",) and query.get("mode") in (None, "memory"):
                return None
            if query.get("mode") == "memory":
                # Named shared-memory databases are process-local, not files. Disallow
                # slash, drive, and traversal spellings even though SQLite would not
                # persist them, keeping the accepted grammar intentionally small.
                if not decoded or "/" in decoded or "\\" in decoded or decoded in (".", ".."):
                    raise ValueError("unsupported named in-memory SQLite URI")
                return None
            if not decoded:
                raise ValueError("SQLite URI has no database path")
            if os.name == "nt" and re.match(r"^/[A-Za-z]:[\\/]", decoded):
                decoded = decoded[1:]
        except (UnicodeError, ValueError) as exc:
            self._bump("rejected_db_targets")
            raise IsolationError("SQLite URI could not be validated safely") from exc
        return self.validate_path(decoded)

    def assert_path_pair(self, requested, actual) -> tuple[Path, Path]:
        requested_path = self.validate_path(requested)
        actual_path = self.validate_path(actual)
        if os.path.normcase(os.path.normpath(str(requested_path))) != os.path.normcase(
            os.path.normpath(str(actual_path))
        ):
            self._bump("rejected_db_targets")
            raise IsolationError("cached SessionDB handle does not match the requested fixture path")
        return requested_path, actual_path

    def assert_connection_paths(self, connection, expected=None) -> dict:
        """Read ``PRAGMA database_list`` only after the owning handle path was accepted."""
        self.require_ready()
        if connection is None:
            raise IsolationError("SessionDB has no verified SQLite connection")
        expected_path = self.validate_path(expected) if expected is not None else None
        rows = connection.execute("PRAGMA database_list").fetchall()
        database_files = []
        main_path = None
        for _sequence, name, filename in rows:
            if not filename:
                continue
            path = self.validate_path(filename)
            database_files.append(path)
            if name == "main":
                main_path = path
        if expected_path is None or main_path is None:
            self._bump("rejected_db_targets")
            raise IsolationError("SessionDB database_list did not identify an expected main file")
        if os.path.normcase(os.path.normpath(str(main_path))) != os.path.normcase(
            os.path.normpath(str(expected_path))
        ):
            self._bump("rejected_db_targets")
            raise IsolationError("SessionDB main file does not match the expected fixture path")
        return {
            "main_path": str(main_path),
            "main_under_fixture_root": True,
            "all_file_databases_under_fixture_root": True,
            "file_database_count": len(database_files),
        }

    def assert_session_db_handle(self, db, expected=None, *, verify_connection=False) -> dict:
        actual = getattr(db, "db_path", None)
        if actual is None:
            self._bump("rejected_db_targets")
            raise IsolationError("SessionDB handle does not expose its actual path")
        actual_path = self.validate_path(actual)
        if expected is not None:
            self.assert_path_pair(expected, actual_path)
        receipt = {"db_path": str(actual_path), "db_path_under_fixture_root": True}
        if verify_connection:
            receipt.update(self.assert_connection_paths(getattr(db, "_conn", None), actual_path))
        return receipt

    def verify_session_db(self, db, expected) -> dict:
        """Verify an already-guarded SessionDB before any session-authority lifecycle call."""
        self.require_ready()
        expected_path, _ = self.assert_path_pair(expected, getattr(db, "db_path", None))
        receipt = self.assert_session_db_handle(db, expected_path, verify_connection=True)
        receipt["fixture_root"] = str(self.fixture_root)
        return receipt

    def validate_cached_store(self, store) -> None:
        for key, handle in dict(getattr(store, "_db_handles", {}) or {}).items():
            if handle is None:
                continue
            key_path = self.validate_path(key)
            self.assert_session_db_handle(handle, key_path)

    def validate_registry_cache(self, registry) -> None:
        generations = getattr(registry, "_generations", {}) or {}
        for key, generation in tuple(generations.items()):
            key_path = self.validate_path(key)
            db = getattr(generation, "db", generation)
            self.assert_session_db_handle(db, key_path)

    def _sqlite_authorizer(self, action, argument1, argument2, database_name, trigger_name):
        del argument2, database_name, trigger_name
        if action == sqlite3.SQLITE_ATTACH:
            try:
                self.database_path(argument1)
            except IsolationError:
                self._bump("attach_denied")
                return sqlite3.SQLITE_DENY
            self._bump("attach_authorized")
        return sqlite3.SQLITE_OK

    def raw_connect_probe(self, database, *args, **kwargs):
        """Test-only proof that the SQLite audit hook protects the unwrapped C entrypoint."""
        if self._raw_connect is None:
            raise IsolationError("raw SQLite probe is unavailable before guard installation")
        return self._raw_connect(database, *args, **kwargs)

    def install_native_fences(self, hermes_state, registry, persistence, authority=None, *, mark_installed=False):
        """Install test-process preflight wrappers; production Hermes source is untouched."""
        self.require_ready()
        original_class = hermes_state.SessionDB
        original_default_path = hermes_state._default_db_path
        original_acquire = registry.acquire
        original_open = persistence.SessionPersistenceMixin._open_session_db_for_active_scope
        original_initialize = getattr(authority, "initialize_session_authority", None) if authority else None
        fence = self

        class CheckedSessionDB(original_class):
            def __init__(self, db_path=None, read_only=False):
                requested = db_path if db_path is not None else original_default_path()
                resolved = fence.validate_path(requested)
                fence._bump("native_constructor_delegations")
                super().__init__(db_path=resolved, read_only=read_only)
                fence.assert_session_db_handle(self, resolved)

        def checked_acquire(db_path=None):
            requested = db_path if db_path is not None else original_default_path()
            resolved = fence.validate_path(requested)
            fence.validate_registry_cache(registry)
            fence._bump("native_acquire_delegations")
            db = original_acquire(resolved)
            fence.assert_session_db_handle(db, resolved)
            return db

        def checked_store_open(store, db_path=None):
            requested = db_path if db_path is not None else original_default_path()
            resolved = fence.validate_path(requested)
            fence.validate_cached_store(store)
            fence._bump("session_store_open_delegations")
            db = original_open(store, db_path=resolved)
            if db is not None:
                fence.assert_session_db_handle(db, resolved)
            return db

        checked_initialize = None
        if original_initialize is not None:
            async def checked_initialize(runner, *, profile_id, instance_id, db=None, register=True):
                candidate = db if db is not None else getattr(runner._session_db, "_db", runner._session_db)
                fence.assert_session_db_handle(candidate, verify_connection=True)
                fence._bump("epoch_calls")
                return await original_initialize(
                    runner, profile_id=profile_id, instance_id=instance_id, db=candidate, register=register
                )

        hermes_state.SessionDB = CheckedSessionDB
        registry.acquire = checked_acquire
        persistence.SessionPersistenceMixin._open_session_db_for_active_scope = checked_store_open
        self._native_restore.append((hermes_state, "SessionDB", original_class))
        self._native_restore.append((registry, "acquire", original_acquire))
        self._native_restore.append((persistence.SessionPersistenceMixin,
                                     "_open_session_db_for_active_scope", original_open))
        if checked_initialize is not None:
            authority.initialize_session_authority = checked_initialize
            self._native_restore.append((authority, "initialize_session_authority", original_initialize))
        if mark_installed:
            self._native_fences_installed = True
            self._authority_fence_installed = checked_initialize is not None
        return {
            "sessiondb_constructor": True,
            "registry_acquire": True,
            "session_store_cache": True,
            "authority_pre_epoch": checked_initialize is not None,
        }

    def install_actual_sessiondb_fences(self) -> None:
        self.require_ready()
        if self._native_fences_installed:
            return
        import hermes_state
        import hermes_state_registry
        import gateway.session_persistence as persistence

        self.install_native_fences(
            hermes_state, hermes_state_registry, persistence, authority=None, mark_installed=True
        )

    def install_actual_native_fences(self) -> None:
        self.require_ready()
        if self._native_fences_installed:
            if self._authority_fence_installed:
                return
            raise IsolationError("DB-only fences cannot be upgraded to epoch fences in the same process")
        import hermes_state
        import hermes_state_registry
        import gateway.session_persistence as persistence

        authority = None
        if self.core_root is not None and (self.core_root / "gateway" / "session_authority.py").is_file():
            import gateway.session_authority as authority

        self.install_native_fences(
            hermes_state, hermes_state_registry, persistence, authority, mark_installed=True
        )

    def require_db_fences(self) -> None:
        self.require_ready()
        if not self._native_fences_installed:
            raise IsolationError("native DB constructor/acquire/cache fences are not installed")

    def require_native_fences(self) -> None:
        self.require_db_fences()
        if not self._authority_fence_installed:
            raise IsolationError("pre-epoch authority fence is not installed")

    def pin_core_sources(self, core_root: Path, core_ref: str | None = None) -> Path:
        """Restrict Hermes imports to one already-qualified source checkout."""
        self.require_ready()
        root = Path(core_root).resolve(strict=True)
        required = (
            "hermes_constants.py", "hermes_state.py", "hermes_state_registry.py",
            "gateway/session.py", "gateway/session_persistence.py", "agent/redact.py",
        )
        for relative in required:
            path = (root / relative).resolve(strict=True)
            if not path.is_relative_to(root):
                raise IsolationError("qualified Hermes source file escaped its declared checkout")
        authority_path = root / "gateway" / "session_authority.py"
        if authority_path.exists():
            if not authority_path.resolve(strict=True).is_relative_to(root):
                raise IsolationError("qualified Hermes authority source escaped its declared checkout")
        self.core_root = root
        self.core_ref = core_ref
        finder = _CoreOriginFinder(root)
        self.core_finder = finder
        path_finder_index = next(
            (index for index, candidate in enumerate(sys.meta_path)
             if candidate is importlib.machinery.PathFinder), len(sys.meta_path)
        )
        sys.meta_path.insert(path_finder_index, finder)
        return root

    def assert_module_origin(self, module, root: Path | None = None) -> Path:
        expected = Path(root or self.core_root or "").resolve(strict=True)
        origin = Path(module.__file__).resolve(strict=True)
        if not origin.is_relative_to(expected):
            raise IsolationError("Hermes module origin escaped the pinned source checkout")
        return origin

    def loaded_core_origin_count(self) -> int:
        if self.core_root is None:
            return 0
        count = 0
        for name, module in tuple(sys.modules.items()):
            top = name.partition(".")[0]
            candidate = self.core_root / top
            if not (candidate.exists() or candidate.with_suffix(".py").exists()):
                continue
            origin = getattr(module, "__file__", None)
            if origin is not None:
                self.assert_module_origin(module, self.core_root)
                count += 1
        return count

    def install_sqlite_guard(self) -> None:
        self.require_ready()
        if self._raw_connect is not None:
            raise IsolationError("SQLite guard is already installed")
        original_connect = sqlite3.connect
        original_connection = sqlite3.Connection
        self._raw_connect = original_connect
        fence = self
        opening = threading.local()

        class GuardedConnection(original_connection):
            def __init__(self, database, *args, **kwargs):
                fence.database_path(database)
                previous = getattr(opening, "guarded_constructor", False)
                opening.guarded_constructor = True
                try:
                    super().__init__(database, *args, **kwargs)
                finally:
                    opening.guarded_constructor = previous
                self.set_authorizer(fence._sqlite_authorizer)
                fence._bump("connections_returned")

        def guarded_connect(database, *args, **kwargs):
            fence._bump("guarded_connect_calls")
            uri = kwargs.get("uri", args[6] if len(args) > 6 else False)
            fence.database_path(database, uri=uri)
            positional = list(args)
            if len(positional) > 4:
                factory = positional[4]
                if factory is None:
                    positional[4] = GuardedConnection
                elif not isinstance(factory, type) or not issubclass(factory, GuardedConnection):
                    raise IsolationError("SQLite connection factory is not guarded")
            else:
                factory = kwargs.get("factory")
                if factory is None:
                    kwargs["factory"] = GuardedConnection
                elif not isinstance(factory, type) or not issubclass(factory, GuardedConnection):
                    raise IsolationError("SQLite connection factory is not guarded")
            fence._bump("delegated_connect_calls")
            return original_connect(database, *positional, **kwargs)

        def audit(event, args):
            if event != "sqlite3.connect":
                return
            database = args[0] if args else None
            try:
                fence.database_path(database)
                if not getattr(opening, "guarded_constructor", False):
                    raise IsolationError("unwrapped SQLite connection cannot install the ATTACH fence")
            except IsolationError:
                fence._bump("sqlite_audit_rejected")
                raise
            fence._bump("sqlite_audit_authorized")

        self._guarded_connection_type = GuardedConnection
        # Connections made through every public CPython SQLite module path use a guarded
        # subclass. The audit hook remains the pre-open backstop for saved/raw C entrypoints.
        sqlite3.connect = guarded_connect
        sqlite3.Connection = GuardedConnection
        try:
            import sqlite3.dbapi2 as dbapi2
            dbapi2.connect = guarded_connect
            dbapi2.Connection = GuardedConnection
        except ImportError:
            pass
        try:
            import _sqlite3
            _sqlite3.connect = guarded_connect
            _sqlite3.Connection = GuardedConnection
        except (ImportError, AttributeError):
            pass
        sys.addaudithook(audit)


class _CoreOriginFinder(importlib.abc.MetaPathFinder):
    def __init__(self, root: Path):
        self.root = Path(root).resolve(strict=True)
        self.top_levels = set()
        for child in self.root.iterdir():
            if child.is_dir() and (child / "__init__.py").is_file():
                self.top_levels.add(child.name)
            elif child.is_file() and child.suffix == ".py":
                self.top_levels.add(child.stem)

    def find_spec(self, fullname, path=None, target=None):
        del target
        top = fullname.partition(".")[0]
        if top not in self.top_levels:
            return None
        if path is None:
            search_path = [str(self.root)]
        else:
            search_path = list(path)
            core_locations = []
            for item in search_path:
                try:
                    resolved = Path(item).resolve(strict=True)
                except (OSError, RuntimeError):
                    continue
                if resolved.is_relative_to(self.root):
                    core_locations.append(str(resolved))
            if not core_locations:
                return None
            search_path = core_locations
        spec = importlib.machinery.PathFinder.find_spec(fullname, search_path)
        if spec is None:
            return None
        if spec.origin and spec.origin not in ("built-in", "frozen"):
            if not Path(spec.origin).resolve(strict=True).is_relative_to(self.root):
                raise ImportError("Hermes module origin escaped the pinned source checkout")
        locations = spec.submodule_search_locations or ()
        for location in locations:
            if not Path(location).resolve(strict=True).is_relative_to(self.root):
                raise ImportError("Hermes package path escaped the pinned source checkout")
        return spec


def install_guard(fixture_root: Path, artifact_root: Path, token: str) -> DatabaseFence:
    """Install only in a fresh test process, before any Hermes import or DB access."""
    already = sys.modules.get(ISOLATION_KEY)
    if already is not None:
        raise IsolationError("a DB isolation guard is already installed in this process")
    loaded = [name for name in sys.modules if any(
        name == prefix or name.startswith(prefix + ".") for prefix in _CORE_PREFIXES
    )]
    if loaded:
        raise IsolationError("Hermes modules were loaded before the SQLite isolation guard")
    fence = DatabaseFence(Path(fixture_root), Path(artifact_root), token)
    sys.modules[ISOLATION_KEY] = fence
    fence.ready = True
    fence.install_sqlite_guard()
    return fence


def require_ready() -> DatabaseFence:
    fence = sys.modules.get(ISOLATION_KEY)
    if not isinstance(fence, DatabaseFence):
        raise IsolationError("DB isolation bootstrap is absent; use python -I -S guarded_unittest.py")
    return fence.require_ready()
