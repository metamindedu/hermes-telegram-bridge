"""Home guards for the native session-complete package."""
from __future__ import annotations

from pathlib import Path

PLUGIN_ID = "session-complete-telegram"


def resolve_package_home(module_file: str | Path, *, expected_id: str = PLUGIN_ID) -> Path:
    """Resolve this package's active default home without machine-root fallback.

    A home nested under ``profiles/<name>`` is always a named profile and is
    refused. The active Hermes scope must already select the exact home holding
    this package; this prevents a foreign-profile environment from redirecting
    writes or helper dispatch.
    """
    package = Path(module_file).resolve(strict=True).parent
    if package.name != expected_id or package.parent.name != "plugins":
        raise RuntimeError("native_plugin_location_invalid")
    home = package.parent.parent.resolve(strict=True)
    if home.parent.name.casefold() == "profiles":
        raise RuntimeError("named_profile_home_refused")
    try:
        from hermes_constants import get_hermes_home

        active_home = Path(get_hermes_home()).expanduser().resolve(strict=True)
    except Exception as exc:
        raise RuntimeError("hermes_home_unavailable") from exc
    if active_home != home:
        raise RuntimeError("foreign_hermes_home_refused")
    return home


def state_directory(home: str | Path | None = None) -> Path:
    """Existing shared state root; callers choose whether to create it."""
    if home is None:
        from hermes_constants import get_hermes_home

        home = get_hermes_home()
    return Path(home) / "plugin-data" / PLUGIN_ID
