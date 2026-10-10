"""Deprecated compatibility stub; the unified package owns Gateway handlers."""


def register(ctx) -> None:
    """Intentionally register no handlers.

    Existing installations remain user-controlled: this stub never edits
    plugins.enabled, moves state, or deletes anything.
    """
    return None


__all__ = ["register"]
