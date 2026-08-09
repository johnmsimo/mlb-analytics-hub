import os
import threading
from pathlib import Path

from reference_snapshots import DATASET_NAMES, MAGIC, ReferenceSnapshotStore


def _datasets(marker="first"):
    return {
        name: {
            f"{name}-player": {
                "marker": marker,
                "value": index + 0.25,
            }
        }
        for index, name in enumerate(DATASET_NAMES)
    }


def test_snapshot_round_trip_is_versioned_verified_and_deduplicated(tmp_path):
    path = tmp_path / "reference.snapshot"
    store = ReferenceSnapshotStore(path)

    first = store.publish(
        _datasets(),
        effective_date="2026-08-09",
        generated_at="2026-08-09T04:00:00Z",
    )
    first_signature = path.stat().st_ino, path.stat().st_mtime_ns
    loaded = ReferenceSnapshotStore(path).load()

    assert first["changed"] is True
    assert first["version"].startswith("r1-")
    assert loaded["metadata"]["version"] == first["version"]
    assert loaded["metadata"]["effectiveDate"] == "2026-08-09"
    assert loaded["datasets"]["fg_bat"]["fg_bat-player"]["marker"] == "first"
    assert path.read_bytes().startswith(MAGIC)

    second = store.publish(_datasets(), effective_date="2026-08-09")
    assert second["changed"] is False
    assert (path.stat().st_ino, path.stat().st_mtime_ns) == first_signature


def test_invalid_replacement_keeps_last_good_then_accepts_repair(tmp_path):
    path = tmp_path / "reference.snapshot"
    store = ReferenceSnapshotStore(path)
    original = store.publish(_datasets("good"), effective_date="2026-08-09")
    assert store.load()["metadata"]["version"] == original["version"]

    replacement = tmp_path / "broken.tmp"
    replacement.write_bytes(MAGIC + b"{}\nnot-gzip")
    os.replace(replacement, path)

    fallback = store.load()
    assert fallback["metadata"]["version"] == original["version"]
    assert fallback["datasets"]["fg_pit"]["fg_pit-player"]["marker"] == "good"
    assert store.last_error

    repaired = store.publish(_datasets("repaired"), effective_date="2026-08-10")
    assert repaired["changed"] is True
    assert store.last_error is None
    assert store.load()["datasets"]["fg_pit"]["fg_pit-player"]["marker"] == "repaired"


def test_concurrent_cold_reads_decode_one_shared_version(tmp_path):
    path = tmp_path / "reference.snapshot"
    ReferenceSnapshotStore(path).publish(_datasets(), effective_date="2026-08-09")

    class CountingStore(ReferenceSnapshotStore):
        def __init__(self, snapshot_path):
            super().__init__(snapshot_path)
            self.decode_count = 0

        def _decode(self):
            self.decode_count += 1
            return super()._decode()

    store = CountingStore(path)
    barrier = threading.Barrier(9)
    results = []

    def read():
        barrier.wait()
        results.append(store.load()["metadata"]["version"])

    threads = [threading.Thread(target=read) for _ in range(8)]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join(timeout=5)

    assert len(set(results)) == 1
    assert store.decode_count == 1


def test_missing_file_reports_unavailable_without_creating_artifacts(tmp_path):
    path = tmp_path / "missing.snapshot"
    store = ReferenceSnapshotStore(path)

    assert store.load() is None
    assert store.status()["available"] is False
    assert store.last_error == "snapshot_not_found"
    assert not path.exists()
