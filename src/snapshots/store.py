"""Content-addressed snapshot store -- src/snapshots/store.py.

A snapshot is the FULL normalised candle dataset (every row the provider layer returned after canonicalisation and
validation, including a still-forming bar) plus caller-supplied JSON metadata. It exists so a stored decision can be
re-evaluated later without contacting a provider.

Guarantees
----------
* Identity: `snapshot_id` is the SHA-256 of the canonical payload bytes (metadata + data, no timestamps of creation), so
  identical content is stored once and the id proves the content.
* Exact round trip: timestamps are stored as integer nanoseconds (plus the index unit), floats via Python's shortest
  round-trip `repr`, so `dataframe_fingerprint(loaded) == dataframe_fingerprint(original)` bit for bit. Non-finite values are
  stored as explicit tokens, never dropped.
* Integrity on every read: the stored bytes must hash to the id, the format version must be supported, and, when the
  metadata records the dataset's `content_hash`, the rebuilt frame must fingerprint to it. Any failure raises; nothing
  partially valid is ever returned.
* Atomic, race-safe creation: written to a temp file in the destination directory, fsync'd, then published with
  `os.link` (fails if the id already exists), so a reader never sees a partial object and two writers cannot interleave.
  An existing object is verified before reuse; a corrupt one is moved aside (never silently trusted or overwritten).
* The store never deletes or rewrites a valid snapshot, and imports nothing from the broker, execution, AI, UI or
  provider layers.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import math
import os
import re
import tempfile
import zlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.utils.hashing import dataframe_fingerprint

FORMAT_NAME = "atp-market-snapshot"
FORMAT_VERSION = 1
MAX_SNAPSHOT_BYTES = 256 * 1024 * 1024          # sanity bound on the decompressed payload
_ID = re.compile(r"^[0-9a-f]{64}$")
_NONFINITE = {"nan": float("nan"), "inf": float("inf"), "-inf": float("-inf")}


class SnapshotError(RuntimeError):
    """Base class for snapshot failures."""


class SnapshotMissingError(SnapshotError):
    """No snapshot object exists for the id."""


class SnapshotCorruptError(SnapshotError):
    """The object exists but its bytes do not match its id or its recorded content hash."""


class SnapshotFormatError(SnapshotError):
    """The object is intact but written in a format this code does not understand."""


@dataclass(frozen=True)
class Snapshot:
    snapshot_id: str
    meta: dict[str, Any]
    candles: pd.DataFrame
    created: bool = False           # True only on the call that wrote the object


# ------------------------------------------------------------------------------------------------ encoding
def _encode_float(x: float) -> Any:
    if math.isnan(x):
        return "nan"
    if math.isinf(x):
        return "inf" if x > 0 else "-inf"
    return x


def _decode_float(v: Any) -> float:
    return _NONFINITE[v] if isinstance(v, str) else float(v)


def _index_unit(idx: pd.DatetimeIndex) -> str:
    return str(getattr(idx, "unit", "ns"))


def encode_payload(candles: pd.DataFrame, meta: dict[str, Any]) -> bytes:
    """Canonical, deterministic payload bytes for (candles, meta)."""
    if not isinstance(candles.index, pd.DatetimeIndex) or candles.index.tz is None:
        raise SnapshotError("a snapshot needs a timezone-aware DatetimeIndex")
    idx = candles.index
    unit = _index_unit(idx)
    ns = idx.as_unit("ns").asi8 if hasattr(idx, "as_unit") else idx.asi8
    body = {
        "format": FORMAT_NAME,
        "version": FORMAT_VERSION,
        "meta": meta,
        "index": {"ns": [int(v) for v in ns], "unit": unit, "tz": str(idx.tz), "name": idx.name},
        "columns": [str(c) for c in candles.columns],
        "dtypes": {str(c): str(candles[c].dtype) for c in candles.columns},
        "data": {str(c): [_encode_float(float(v)) for v in candles[c].to_numpy(dtype="float64")] for c in candles.columns},
    }
    try:
        return json.dumps(body, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SnapshotError(f"snapshot metadata is not JSON-serialisable: {exc}") from exc


def decode_payload(raw: bytes) -> tuple[dict[str, Any], pd.DataFrame]:
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotCorruptError(f"snapshot payload is not valid JSON: {exc}") from exc
    if not isinstance(body, dict) or body.get("format") != FORMAT_NAME:
        raise SnapshotFormatError("not a market snapshot")
    if body.get("version") != FORMAT_VERSION:
        raise SnapshotFormatError(f"unsupported snapshot format version {body.get('version')!r} (this code reads {FORMAT_VERSION})")
    try:
        ix = body["index"]
        idx = pd.DatetimeIndex(pd.to_datetime(np.array(ix["ns"], dtype="int64"), unit="ns", utc=True))
        if ix.get("unit") and hasattr(idx, "as_unit"):
            idx = idx.as_unit(ix["unit"])
        idx = idx.tz_convert(ix["tz"])
        idx.name = ix.get("name")
        cols = body["columns"]
        frame = pd.DataFrame({c: np.array([_decode_float(v) for v in body["data"][c]], dtype=body["dtypes"][c]) for c in cols},
                             index=idx, columns=cols)
        if frame.index.freq is not None:
            frame.index.freq = None
    except (KeyError, TypeError, ValueError) as exc:
        raise SnapshotCorruptError(f"snapshot payload is malformed: {type(exc).__name__}: {exc}") from exc
    return body["meta"], frame


def _gzip(data: bytes) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0, compresslevel=6) as fh:
        fh.write(data)
    return buf.getvalue()


# ------------------------------------------------------------------------------------------------ store
class SnapshotStore:
    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    @staticmethod
    def _check_id(snapshot_id: str) -> str:
        if not isinstance(snapshot_id, str) or not _ID.match(snapshot_id):
            raise SnapshotMissingError("not a valid snapshot id")
        return snapshot_id

    def path_for(self, snapshot_id: str) -> Path:
        sid = self._check_id(snapshot_id)
        return self._root / sid[:2] / f"{sid}.json.gz"

    def exists(self, snapshot_id: str) -> bool:
        try:
            return self.path_for(snapshot_id).is_file()
        except SnapshotMissingError:
            return False

    # -------------------------------------------------------------------------------- write
    def put(self, candles: pd.DataFrame, meta: dict[str, Any]) -> Snapshot:
        """Store (or find) the snapshot for this content and return it re-read from disk and verified."""
        payload = encode_payload(candles, meta)
        sid = hashlib.sha256(payload).hexdigest()
        final = self.path_for(sid)
        final.parent.mkdir(parents=True, exist_ok=True)
        created = False
        if final.exists():
            try:
                return self._loaded(sid, False)          # identical content already stored and intact: reuse it
            except SnapshotError:
                self._quarantine(final)                  # never trust or overwrite a damaged object
        fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", suffix=".part", dir=str(final.parent))
        tmp = Path(tmp_name)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(_gzip(payload))
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp, final)                      # atomic publish; fails if another writer got there first
                created = True
            except FileExistsError:
                created = False
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
        try:
            self._fsync_dir(final.parent)
            return self._loaded(sid, created)             # read-back verification: nothing is returned unverified
        except SnapshotError:
            if created:
                self._quarantine(final)
            raise

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        try:
            fd = os.open(str(path), os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(fd)
        except OSError:
            pass
        finally:
            os.close(fd)

    @staticmethod
    def _quarantine(path: Path) -> None:
        try:
            os.replace(path, path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
        except OSError:
            pass

    # -------------------------------------------------------------------------------- read
    def _read_verified(self, snapshot_id: str) -> tuple[dict[str, Any], pd.DataFrame]:
        path = self.path_for(snapshot_id)
        try:
            with gzip.open(path, "rb") as fh:
                raw = fh.read(MAX_SNAPSHOT_BYTES + 1)
        except FileNotFoundError as exc:
            raise SnapshotMissingError(f"snapshot {snapshot_id[:12]} is not in the store") from exc
        except (OSError, EOFError, zlib.error, gzip.BadGzipFile) as exc:
            raise SnapshotCorruptError(f"snapshot {snapshot_id[:12]} cannot be read: {type(exc).__name__}") from exc
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise SnapshotCorruptError(f"snapshot {snapshot_id[:12]} is larger than the allowed size")
        if hashlib.sha256(raw).hexdigest() != snapshot_id:
            raise SnapshotCorruptError(f"snapshot {snapshot_id[:12]} does not match its content hash (altered or damaged)")
        meta, frame = decode_payload(raw)
        recorded = meta.get("content_hash")
        if recorded and dataframe_fingerprint(frame) != recorded:
            raise SnapshotCorruptError(f"snapshot {snapshot_id[:12]} rebuilt to a different dataset fingerprint than recorded")
        return meta, frame

    def _loaded(self, snapshot_id: str, created: bool) -> Snapshot:
        meta, frame = self._read_verified(snapshot_id)
        return Snapshot(snapshot_id, meta, frame, created)

    def load(self, snapshot_id: str) -> Snapshot:
        """Verified read. Raises SnapshotMissingError / SnapshotCorruptError / SnapshotFormatError; never a partial result."""
        return self._loaded(self._check_id(snapshot_id), False)

    def verify(self, snapshot_id: str) -> None:
        self._read_verified(self._check_id(snapshot_id))

    def list_ids(self) -> list[str]:
        if not self._root.is_dir():
            return []
        return sorted(p.name[:-len(".json.gz")] for p in self._root.glob("*/*.json.gz") if _ID.match(p.name[:-len(".json.gz")]))
