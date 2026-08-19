from pathlib import Path

import pytest

from app import config  # noqa: F401
from app.core import library, metadata, pipeline
from app.models import Job


def _candidate(title="Attack on Titan", year=2013, source="anilist"):
    return metadata.Candidate(
        source=source,
        external_id=16498,
        media_type="anime" if source == "anilist" else "series",
        title=title,
        original_title="進撃の巨人",
        english_title=title,
        year=year,
        score=0.98,
        episodes=25,
        alt_titles=[title, "Shingeki no Kyojin"],
    )


@pytest.fixture
def stub_metadata(monkeypatch):
    monkeypatch.setattr(metadata, "lookup", lambda title, hint, year=None: ([_candidate()], []))
    monkeypatch.setattr(pipeline.metadata, "lookup", lambda title, hint, year=None: ([_candidate()], []))
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))


def _make_file(path: Path, size: int = 4096) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def _run(session, times=3):
    for _ in range(times):
        pipeline.tick(session)
        session.flush()


def test_existing_folder_wins_over_default_path(session, library_tree, stub_metadata):
    existing = library_tree["anime_two"] / "Attack on Titan (2013)"
    (existing / "Season 01").mkdir(parents=True)
    library.reindex(session)

    source = _make_file(
        library_tree["downloads"] / "Attack.On.Titan.S02.GERMAN" / "Attack.On.Titan.WEBX.1080pS02E01.mkv"
    )
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.media_type == "anime"
    assert job.status == "planned"  # dry run
    assert job.target_path == str(existing / "Season 02" / "Attack on Titan (2013) - S02E01.mkv")
    assert source.exists(), "dry run must not move anything"


def test_new_anime_uses_default_path(session, library_tree, stub_metadata):
    library.reindex(session)
    _make_file(library_tree["downloads"] / "Attack.On.Titan.S02E02.1080p.mkv")
    _run(session)

    job = session.scalars(
        pipeline.select(Job).where(Job.filename == "Attack.On.Titan.S02E02.1080p.mkv")
    ).one()
    assert job.target_path.startswith(str(library_tree["anime_one"]))


def test_move_and_subtitles(session, library_tree, stub_metadata, set_config):
    library.reindex(session)
    folder = library_tree["downloads"] / "AoT.S02E03"
    source = _make_file(folder / "Attack.On.Titan.S02E03.1080p.mkv")
    _make_file(folder / "Attack.On.Titan.S02E03.1080p.de.srt", 64)
    _make_file(folder / "sample.mkv", 32)
    set_config({"dry_run": False})
    try:
        _run(session)
    finally:
        set_config({"dry_run": True})

    job = session.scalars(pipeline.select(Job).where(Job.filename == source.name)).one()
    assert job.status == "done", job.error
    target = Path(job.target_path)
    assert target.exists()
    assert not source.exists()
    assert (target.parent / "Attack on Titan (2013) - S02E03.de.srt").exists()


def test_duplicate_is_never_overwritten(session, library_tree, stub_metadata, set_config):
    library.reindex(session)
    existing_dir = library_tree["anime_one"] / "Attack on Titan (2013)" / "Season 02"
    existing_dir.mkdir(parents=True)
    (existing_dir / "Attack on Titan (2013) - S02E04.mkv").write_bytes(b"old")
    library.reindex(session)

    source = _make_file(library_tree["downloads"] / "Attack.On.Titan.S02E04.1080p.mkv")
    set_config({"dry_run": False})
    try:
        _run(session)
    finally:
        set_config({"dry_run": True})

    job = session.scalars(pipeline.select(Job).where(Job.filename == source.name)).one()
    assert job.status == "duplicate"
    assert job.duplicate_of
    assert source.exists()
    assert (existing_dir / "Attack on Titan (2013) - S02E04.mkv").read_bytes() == b"old"


def test_absolute_episode_goes_to_review(session, library_tree, stub_metadata):
    library.reindex(session)
    _make_file(library_tree["downloads"] / "[SubsPlease] Attack On Titan - 87 (1080p).mkv")
    _run(session)
    job = session.scalars(
        pipeline.select(Job).where(Job.filename.like("%Attack On Titan - 87%"))
    ).one()
    assert job.status == "review"
    assert "Absolute Folgennummer" in (job.reason or "")


def test_decision_approve_moves_file(session, library_tree, stub_metadata, set_config):
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "[SubsPlease] Attack On Titan - 88 (1080p).mkv")
    _run(session)
    job = session.scalars(pipeline.select(Job).where(Job.filename == source.name)).one()
    assert job.status == "review"

    set_config({"dry_run": False})
    try:
        pipeline.apply_decision(
            session, job, "approve", {"season": 4, "episode": 28, "save_rule": True}
        )
    finally:
        set_config({"dry_run": True})
    assert job.status == "done", job.error
    assert Path(job.target_path).name == "Attack on Titan (2013) - S04E28.mkv"
    assert not source.exists()


def test_waiting_while_extraction_runs(session, library_tree, stub_metadata):
    library.reindex(session)
    folder = library_tree["downloads"] / "Some.Show.S01E01"
    source = _make_file(folder / "Some.Show.S01E01.1080p.mkv")
    (folder / "Some.Show.S01E01.part01.rar").write_bytes(b"archive")
    pipeline.tick(session)
    session.flush()
    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.status == "waiting"
    assert "entpackt" in job.reason or "wächst" in job.reason


def test_generic_title_prefers_tmdb_for_series(session, library_tree, monkeypatch):
    """A name like "Dark" exists twice. Without anime hints TMDb has to win."""
    from app.core.parser import parse as parse_name

    def fake_lookup(title, hint, year=None):
        candidates = [
            metadata.Candidate(source="anilist", external_id=1, media_type="anime", title="Dark",
                               original_title=None, english_title="Dark", year=2000, score=1.0),
            metadata.Candidate(source="tmdb", external_id=70523, media_type="series", title="Dark",
                               original_title="Dark", english_title="Dark", year=2017, score=1.0),
        ]
        preferred = {"anime": "anilist", "series": "tmdb", "movie": "tmdb"}.get(hint)
        for candidate in candidates:
            if preferred and candidate.source != preferred:
                candidate.score = round(candidate.score - metadata.SOURCE_PENALTY, 3)
        candidates.sort(key=lambda c: c.score, reverse=True)
        return candidates, []

    monkeypatch.setattr(pipeline.metadata, "lookup", fake_lookup)
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))
    library.reindex(session)

    assert parse_name("Dark.S01E03.German.DL.1080p.WEB.h264-XYZ.mkv").media_hint == "series"
    _make_file(library_tree["downloads"] / "Dark.S01E03.German.DL.1080p.WEB.h264-XYZ.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.filename.like("Dark.S01E03%"))).one()
    assert job.media_type == "series"
    assert job.year == 2017
    assert job.target_path.startswith(str(library_tree["series"]))


def test_equal_scores_across_media_types_go_to_review(session, library_tree, monkeypatch):
    def fake_lookup(title, hint, year=None):
        return [
            metadata.Candidate(source="tmdb", external_id=1, media_type="series", title="Nowhere",
                               original_title=None, english_title="Nowhere", year=2019, score=0.92),
            metadata.Candidate(source="tmdb", external_id=2, media_type="movie", title="Nowhere",
                               original_title=None, english_title="Nowhere", year=2023, score=0.9),
        ], []

    monkeypatch.setattr(pipeline.metadata, "lookup", fake_lookup)
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))
    library.reindex(session)
    _make_file(library_tree["downloads"] / "Nowhere.S01E01.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.filename.like("Nowhere%"))).one()
    assert job.status == "review"
    assert "passen gleich gut" in job.reason


def test_second_series_root_is_indexed_and_wins(session, library_tree, stub_metadata, set_config):
    """A folder in the second series location beats the default series path."""
    second = library_tree["series"].parent / "SerienZwei"
    (second / "Attack on Titan (2013)").mkdir(parents=True)
    set_config({"series_path_2": str(second)})
    library.reindex(session)

    _make_file(library_tree["downloads"] / "Attack.On.Titan.S03E01.German.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.filename.like("%S03E01%"))).one()
    assert job.existing_folder == str(second / "Attack on Titan (2013)")
    assert job.target_path.startswith(str(second))
    set_config({"series_path_2": ""})


def test_existing_season_folder_style_is_kept(session, library_tree, stub_metadata):
    """The library uses S1 and S2. New episodes must not create a second Season 01 folder."""
    folder = library_tree["anime_two"] / "Attack on Titan (2013)"
    (folder / "S1").mkdir(parents=True)
    (folder / "S2").mkdir()
    library.reindex(session)

    _make_file(library_tree["downloads"] / "Attack.On.Titan.S02E09.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.filename.like("%S02E09%"))).one()
    assert job.target_dir == str(folder / "S2")
    assert not (folder / "Season 02").exists()


def test_german_anime_release_is_recognised_as_anime(session, library_tree, monkeypatch):
    """Akame.Ga.Kill.S01E05.German.DL.1080p.WEB looks like a series. AniList says otherwise."""
    def fake_lookup(title, hint, year=None):
        return [
            metadata.Candidate(source="tmdb", external_id=1, media_type="series", title="Akame ga Kill!",
                               original_title=None, english_title="Akame ga Kill!", year=2014, score=1.0),
            metadata.Candidate(source="anilist", external_id=2, media_type="anime", title="Akame ga Kill!",
                               original_title=None, english_title="Akame ga Kill!", year=2014, score=0.92,
                               alt_titles=["Akame ga Kill!", "Akame ga KILL!"]),
        ], []

    monkeypatch.setattr(pipeline.metadata, "lookup", fake_lookup)
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))
    library.reindex(session)

    _make_file(library_tree["downloads"] / "Akame.Ga.Kill.S01E05.German.DL.1080p.WEB.h264-GRP.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.filename.like("Akame%"))).one()
    assert job.media_type == "anime"
    assert job.target_path.startswith(str(library_tree["anime_one"]))
