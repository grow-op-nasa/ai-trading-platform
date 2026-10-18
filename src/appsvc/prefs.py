"""Persisted local application preferences (Sprint 20, ADR-0056): the last-used paper account.

A tiny JSON file under `data/app/` (git-ignored with the rest of `data/`), written atomically (temp file + rename).
It stores ONLY a stable account id and a timestamp -- never a secret, never ledger state. Reading never creates or
modifies anything; a missing, malformed or obsolete file is reported, not repaired and not guessed around. The
selected account is a *context*, never an instruction: restoring it executes nothing.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.appsvc.paths import AppPaths

SCHEMA_VERSION = 1

# resolution states
SELECTED = "SELECTED"            # a saved id exists and is an eligible account
NONE_SAVED = "NONE_SAVED"        # nothing saved yet (or the file does not exist): the user must choose
NO_ACCOUNTS = "NO_ACCOUNTS"      # there are no eligible accounts at all
SAVED_MISSING = "SAVED_MISSING"  # the saved id is not (or no longer) an eligible account: the user must choose
MALFORMED = "MALFORMED"          # the preference file cannot be read as valid preferences


@dataclass(frozen=True)
class AccountResolution:
    selected: int | None
    state: str
    message: str
    saved_id: int | None = None


def _read(path: Path) -> tuple[dict | None, str | None]:
    """(data, problem). A missing file is (None, None)."""
    if not path.is_file():
        return None, None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"preferences could not be read ({type(exc).__name__})"
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None, "preferences have an unrecognised format or version"
    acct = data.get("last_account_id")
    if acct is not None and (isinstance(acct, bool) or not isinstance(acct, int)):
        return None, "the saved account id is not an integer"
    return data, None


def saved_account_id(paths: AppPaths) -> int | None:
    data, _ = _read(paths.prefs_file)
    return None if data is None else data.get("last_account_id")


def resolve_account(paths: AppPaths, eligible_ids: list[int]) -> AccountResolution:
    """Decide the startup account WITHOUT guessing: only a saved id that is still an eligible account is selected."""
    data, problem = _read(paths.prefs_file)
    if problem:
        return AccountResolution(None, MALFORMED, problem + "; choose an account to continue (the file is not modified)")
    if not eligible_ids:
        return AccountResolution(None, NO_ACCOUNTS, "No paper account exists yet. Set one up explicitly; nothing is created automatically.",
                                 (data or {}).get("last_account_id"))
    saved = (data or {}).get("last_account_id")
    if saved is None:
        return AccountResolution(None, NONE_SAVED, "No account has been selected yet. Choose one in the sidebar.")
    if saved in eligible_ids:
        return AccountResolution(saved, SELECTED, f"Restored your last-used account #{saved}.", saved)
    return AccountResolution(None, SAVED_MISSING,
                             f"Your last-used account #{saved} is no longer available (it was removed or has no saved portfolio). "
                             "It was not recreated or replaced; choose an account.", saved)


def save_account(paths: AppPaths, account_id: int) -> None:
    """Persist an EXPLICIT selection atomically. Never touches the financial stores."""
    if isinstance(account_id, bool) or not isinstance(account_id, int):
        raise ValueError("account id must be an integer")
    target = paths.prefs_file
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {"schema_version": SCHEMA_VERSION, "last_account_id": account_id,
               "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    fd, tmp = tempfile.mkstemp(prefix=".prefs-", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
