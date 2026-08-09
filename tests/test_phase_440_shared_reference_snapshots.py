from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_production_web_hydrates_shared_snapshot_without_upstream_loaders():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    preload = app.split("def _preload_caches():", 1)[1].split(
        "# _preload_caches() is now triggered", 1
    )[0]
    shared_branch = preload.split(
        "if _production_web_uses_reference_snapshot():", 1
    )[1].split("# Legacy research endpoints", 1)[0]

    assert "_start_reference_snapshot_watcher()" in shared_branch
    assert "else:" in shared_branch
    assert "threading.Thread(target=load_fg" in shared_branch
    assert "threading.Thread(target=load_sv" in shared_branch
    assert "threading.Thread(target=load_rosters" in shared_branch
    assert "threading.Thread(target=load_arsenal" in shared_branch
    assert shared_branch.index("else:") < shared_branch.index(
        "threading.Thread(target=load_fg"
    )


def test_web_refresh_paths_reload_volume_snapshot_not_upstream_data():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    fg_refresh = app.split("def _maybe_refresh_fg():", 1)[1].split(
        "# ── Pitch-type key mapping", 1
    )[0]
    sv_refresh = app.split("def _maybe_refresh_savant():", 1)[1].split(
        "def sv_pitcher", 1
    )[0]

    for source in (fg_refresh, sv_refresh):
        guard = source.split("if _production_web_uses_reference_snapshot():", 1)[1]
        assert "_refresh_shared_reference_snapshot_async()" in guard
        assert guard.index("return") < guard.index("with _")


def test_worker_publishes_one_complete_fg_savant_version():
    app = (ROOT / "app.py").read_text(encoding="utf-8")
    publish = app.split("def _publish_reference_snapshot_if_ready():", 1)[1].split(
        "def _apply_shared_reference_snapshot", 1
    )[0]

    assert "settings.process_role != 'worker'" in publish
    assert "_fg_load_date == today" in publish
    assert "_sv_load_date == today" in publish
    assert "_reference_snapshot_store.publish" in publish
    assert "with _fg_lock:" in publish
    assert "with _sv_lock:" in publish


def test_snapshot_format_is_atomic_checksummed_and_bounded():
    source = (ROOT / "reference_snapshots.py").read_text(encoding="utf-8")

    assert "hashlib.sha256" in source
    assert "gzip.compress" in source
    assert "os.fsync" in source
    assert "os.replace" in source
    assert "MAX_HEADER_BYTES" in source
    assert "snapshot digest mismatch" in source
