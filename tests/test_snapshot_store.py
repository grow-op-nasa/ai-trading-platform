"""Sprint 22: the snapshot store's guarantees (ADR-0059): exact round trip, content addressing, integrity, atomicity."""

from __future__ import annotations

import gzip
import json
import random
import threading

import numpy as np
import pandas as pd
import pytest

from src.snapshots import (FORMAT_VERSION, SnapshotCorruptError, SnapshotFormatError, SnapshotMissingError,
                           SnapshotStore)
from src.snapshots.store import decode_payload, encode_payload
from src.utils.hashing import dataframe_fingerprint


def _frame(n: int = 300, seed: int = 1) -> pd.DataFrame:
    rng = random.Random(seed)
    idx = pd.date_range("2024-01-02", periods=n, freq="B", tz="UTC", name="timestamp")
    idx.freq = None
    c = [100.0]
    for _ in range(n - 1):
        c.append(c[-1] * (1 + rng.uniform(-0.02, 0.021)))
    return pd.DataFrame({"open": c, "high": [x * 1.0123456789 for x in c], "low": [x / 1.0071 for x in c], "close": c,
                         "volume": [rng.uniform(1.0, 1e9) for _ in range(n)]}, index=idx)


def test_round_trip_is_bit_exact_including_awkward_floats(tmp_path):
    df = _frame()
    df.iloc[3, 0] = 1e-300
    df.iloc[4, 1] = 0.1 + 0.2                 # classic non-terminating binary fraction
    df.iloc[5, 2] = np.nextafter(100.0, 101.0)
    df.iloc[6, 3] = float("nan")
    df.iloc[7, 4] = float("inf")
    snap = SnapshotStore(tmp_path).put(df, {"symbol": "X"})
    out = snap.candles
    assert dataframe_fingerprint(out) == dataframe_fingerprint(df)
    for c in df.columns:
        a, b = df[c].to_numpy(), out[c].to_numpy()
        assert np.array_equal(a.view("int64"), b.view("int64"))          # identical bit patterns, NaN included
    assert out.index.equals(df.index) and out.index.tz is not None and out.index.name == "timestamp"
    assert list(out.columns) == list(df.columns) and (out.dtypes == df.dtypes).all()


def test_reload_from_a_new_store_object_matches(tmp_path):
    df = _frame()
    sid = SnapshotStore(tmp_path).put(df, {"symbol": "X", "content_hash": dataframe_fingerprint(df)}).snapshot_id
    again = SnapshotStore(tmp_path).load(sid)
    assert dataframe_fingerprint(again.candles) == dataframe_fingerprint(df) and again.meta["symbol"] == "X"


def test_identity_is_the_content_and_creation_time_does_not_enter_it(tmp_path):
    df = _frame()
    st = SnapshotStore(tmp_path)
    a = st.put(df, {"symbol": "X"})
    b = st.put(df.copy(), {"symbol": "X"})
    assert a.created and not b.created and a.snapshot_id == b.snapshot_id and len(st.list_ids()) == 1
    changed = df.copy()
    changed.iloc[10, 3] += 1e-9                                           # one tiny change => a different snapshot
    assert st.put(changed, {"symbol": "X"}).snapshot_id != a.snapshot_id
    assert st.put(df, {"symbol": "Y"}).snapshot_id != a.snapshot_id       # metadata is part of the identity


def test_storage_is_deterministic_bytes(tmp_path):
    df = _frame()
    p1 = SnapshotStore(tmp_path / "a").put(df, {"s": 1})
    p2 = SnapshotStore(tmp_path / "b").put(df, {"s": 1})
    assert SnapshotStore(tmp_path / "a").path_for(p1.snapshot_id).read_bytes() == SnapshotStore(tmp_path / "b").path_for(p2.snapshot_id).read_bytes()


def test_no_partial_files_are_left_behind(tmp_path):
    st = SnapshotStore(tmp_path)
    st.put(_frame(), {"s": 1})
    assert not list(tmp_path.rglob("*.part")) and not list(tmp_path.rglob(".tmp-*"))


@pytest.mark.parametrize("damage", ["flip", "truncate", "garbage"])
def test_any_damage_is_detected_and_nothing_partial_is_returned(tmp_path, damage):
    st = SnapshotStore(tmp_path)
    sid = st.put(_frame(), {"s": 1}).snapshot_id
    p = st.path_for(sid)
    raw = p.read_bytes()
    p.write_bytes({"flip": raw[:50] + bytes([raw[50] ^ 1]) + raw[51:], "truncate": raw[: len(raw) // 2], "garbage": b"nope"}[damage])
    with pytest.raises(SnapshotCorruptError):
        st.load(sid)
    with pytest.raises(SnapshotCorruptError):
        st.verify(sid)


def test_payload_that_does_not_hash_to_its_id_is_corrupt(tmp_path):
    st = SnapshotStore(tmp_path)
    sid = st.put(_frame(), {"s": 1}).snapshot_id
    other = encode_payload(_frame(seed=2), {"s": 1})                       # valid snapshot content, wrong id
    st.path_for(sid).write_bytes(gzip.compress(other))
    with pytest.raises(SnapshotCorruptError, match="content hash"):
        st.load(sid)


def test_recorded_dataset_fingerprint_must_match_the_rebuilt_frame(tmp_path):
    st = SnapshotStore(tmp_path)
    df = _frame()
    with pytest.raises(SnapshotCorruptError, match="fingerprint"):
        st.put(df, {"content_hash": "0" * 64})


def test_missing_and_malformed_ids(tmp_path):
    st = SnapshotStore(tmp_path)
    with pytest.raises(SnapshotMissingError):
        st.load("a" * 64)
    for bad in ("../../etc/passwd", "short", "A" * 64, "", "g" * 64):
        with pytest.raises(SnapshotMissingError):
            st.load(bad)
        assert st.exists(bad) is False


def test_unsupported_format_version_is_refused_not_guessed(tmp_path):
    body = json.loads(encode_payload(_frame(5), {}))
    body["version"] = FORMAT_VERSION + 1
    with pytest.raises(SnapshotFormatError):
        decode_payload(json.dumps(body).encode())
    body["version"], body["format"] = FORMAT_VERSION, "something-else"
    with pytest.raises(SnapshotFormatError):
        decode_payload(json.dumps(body).encode())


def test_a_damaged_existing_object_is_moved_aside_and_replaced_not_trusted(tmp_path):
    st = SnapshotStore(tmp_path)
    df = _frame()
    sid = st.put(df, {"s": 1}).snapshot_id
    st.path_for(sid).write_bytes(b"damaged")
    again = st.put(df, {"s": 1})
    assert again.snapshot_id == sid and again.created
    assert dataframe_fingerprint(st.load(sid).candles) == dataframe_fingerprint(df)
    assert list(tmp_path.rglob("*.corrupt-*"))


def test_concurrent_writers_of_the_same_content_produce_one_valid_object(tmp_path):
    df = _frame()
    errors, ids = [], []

    def work():
        try:
            ids.append(SnapshotStore(tmp_path).put(df, {"s": 1}).snapshot_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and len(set(ids)) == 1 and len(SnapshotStore(tmp_path).list_ids()) == 1
    assert dataframe_fingerprint(SnapshotStore(tmp_path).load(ids[0]).candles) == dataframe_fingerprint(df)


def test_naive_index_is_rejected(tmp_path):
    df = _frame()
    df.index = df.index.tz_localize(None)
    from src.snapshots import SnapshotError
    with pytest.raises(SnapshotError):
        SnapshotStore(tmp_path).put(df, {})


def test_non_serialisable_metadata_is_an_error_not_a_partial_write(tmp_path):
    from src.snapshots import SnapshotError
    with pytest.raises(SnapshotError):
        SnapshotStore(tmp_path).put(_frame(5), {"bad": object()})
    assert not list(tmp_path.rglob("*.json.gz"))
