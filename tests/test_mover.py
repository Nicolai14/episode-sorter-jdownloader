from pathlib import Path

import pytest

from app import config
from app.core import files, mover, naming


def test_preflight_blocks_existing_target(tmp_path):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")
    target = tmp_path / "out.mkv"
    target.write_bytes(b"old")
    problems = mover.preflight(source, target)
    assert any("existiert bereits" in problem for problem in problems)


def test_dry_run_leaves_everything_in_place(tmp_path):
    source = tmp_path / "in.mkv"
    source.write_bytes(b"data")
    target = tmp_path / "library" / "out.mkv"
    result = mover.move(source, target, dry_run=True)
    assert result.method == "Dry Run"
    assert source.exists() and not target.exists()


def test_cross_dataset_copy_verifies_then_deletes(tmp_path, monkeypatch):
    monkeypatch.setattr(files, "same_filesystem", lambda a, b: False)
    monkeypatch.setattr(mover.files, "same_filesystem", lambda a, b: False)
    source = tmp_path / "in.mkv"
    payload = b"a" * 5000
    source.write_bytes(payload)
    target = tmp_path / "library" / "Show" / "out.mkv"
    result = mover.move(source, target)
    assert result.method == "Kopieren und Prüfen"
    assert target.read_bytes() == payload
    assert not source.exists()
    assert not list(target.parent.glob("*.es-part"))


def test_failed_copy_keeps_the_source(tmp_path, monkeypatch):
    monkeypatch.setattr(mover.files, "same_filesystem", lambda a, b: False)

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    source = tmp_path / "in.mkv"
    source.write_bytes(b"a" * 100)
    target = tmp_path / "library" / "out.mkv"
    # Bricht mitten im Kopieren ab, kurz bevor die Daten auf der Platte landen.
    monkeypatch.setattr(mover.os, "fsync", _boom)
    with pytest.raises(OSError):
        mover.move(source, target)
    assert source.exists()
    assert not target.exists()
    assert not list(target.parent.glob("*.es-part"))
    assert mover.active_transfers() == []


def test_a_copy_reports_its_progress(tmp_path, monkeypatch):
    monkeypatch.setattr(mover.files, "same_filesystem", lambda a, b: False)
    monkeypatch.setattr(mover, "CHUNK", 16)

    gesehen = []
    echtes_fsync = mover.os.fsync

    def _mitlesen(*args, **kwargs):
        gesehen.append(mover.active_transfers())
        return echtes_fsync(*args, **kwargs)

    monkeypatch.setattr(mover.os, "fsync", _mitlesen)
    source = tmp_path / "in.mkv"
    source.write_bytes(b"a" * 100)
    target = tmp_path / "library" / "out.mkv"
    mover.move(source, target)

    assert gesehen and gesehen[0], "während des Kopierens muss ein Eintrag sichtbar sein"
    eintrag = gesehen[0][0]
    assert eintrag["name"] == "out.mkv"
    assert eintrag["total"] == 100
    assert eintrag["copied"] == 100
    assert eintrag["phase"] == "kopieren"
    # Nach dem Ende bleibt nichts stehen.
    assert mover.active_transfers() == []


def test_episode_and_movie_naming():
    config.update({
        "episode_template": "{title} ({year}) - S{season:02d}E{episode:02d}",
        "movie_template": "{title} ({year})",
        "season_folder_template": "S{season}",
    })
    episode = naming.build_plan(
        media_type="anime", title="Attack on Titan", year=2013, season=2, episode=1,
        episode_end=None, extension=".mkv", base_dir="/mnt/poolToshiba/Animes",
    )
    assert episode.path == "/mnt/poolToshiba/Animes/Attack on Titan (2013)/S2/Attack on Titan (2013) - S02E01.mkv"

    movie = naming.build_plan(
        media_type="movie", title="Inception", year=2010, season=None, episode=None,
        episode_end=None, extension=".mkv", base_dir="/mnt/poolToshiba/Filme",
    )
    assert movie.path == "/mnt/poolToshiba/Filme/Inception (2010)/Inception (2010).mkv"


def test_specials_go_into_their_own_folder():
    plan = naming.build_plan(
        media_type="anime", title="Bleach", year=2004, season=0, episode=2, episode_end=None,
        extension=".mkv", base_dir="/mnt/poolToshiba/Animes", special_kind="ova",
    )
    assert "/Specials/" in plan.path
    assert plan.filename == "Bleach (2004) - S00E02.mkv"


def test_companion_naming():
    assert naming.companion_name("Show (2020) - S01E01.mkv", "de", ".srt") == "Show (2020) - S01E01.de.srt"
    assert naming.companion_name("Show (2020) - S01E01.mkv", "", ".srt") == "Show (2020) - S01E01.srt"


def test_ignored_files(tmp_path):
    assert files.is_ignored(Path("Some.Sample.mkv"))
    assert files.is_ignored(Path("setup.exe"))
    assert not files.is_ignored(Path("Attack.On.Titan.S02E01.mkv"))


def test_jdownloader_paths_are_mapped_to_the_host(tmp_path):
    from app.core import jdownloader

    config.update({"download_dir": str(tmp_path / "downloads"), "jd_path_prefix": "/output"})
    assert jdownloader.host_path("/output/Some.Release") == str(tmp_path / "downloads" / "Some.Release")
    assert jdownloader.host_path("/output") == str(tmp_path / "downloads")
    # Paths outside the prefix stay untouched
    assert jdownloader.host_path("/mnt/other/place") == "/mnt/other/place"
    assert jdownloader.host_path(None) is None


def test_rename_across_mount_points_falls_back_to_copy(tmp_path, monkeypatch):
    """Bind mounts inside a container report the same device but refuse a rename."""
    import errno as errno_module

    monkeypatch.setattr(mover.files, "same_filesystem", lambda a, b: True)
    calls = {"replace": 0}
    real_replace = mover.os.replace

    def fake_replace(src, dst):
        calls["replace"] += 1
        raise OSError(errno_module.EXDEV, "Invalid cross-device link")

    monkeypatch.setattr(mover.os, "replace", fake_replace)
    source = tmp_path / "in.mkv"
    payload = b"z" * 4096
    source.write_bytes(payload)
    target = tmp_path / "lib" / "out.mkv"

    # os.replace is also used to put the verified copy in place, so only the first
    # call has to fail.
    def replace_once(src, dst):
        calls["replace"] += 1
        if calls["replace"] == 1:
            raise OSError(errno_module.EXDEV, "Invalid cross-device link")
        return real_replace(src, dst)

    monkeypatch.setattr(mover.os, "replace", replace_once)
    result = mover.move(source, target)

    assert result.method == "Kopieren und Prüfen"
    assert target.read_bytes() == payload
    assert not source.exists()
