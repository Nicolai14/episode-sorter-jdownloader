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
    assert job.target_path == str(existing / "S2" / "Attack on Titan (2013) - S02E01.mkv")
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


def _stub_lookup(monkeypatch, candidates):
    monkeypatch.setattr(pipeline.metadata, "lookup", lambda title, hint, year=None: (candidates, []))
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))


def _cand(**kwargs):
    base = dict(
        source="tmdb", external_id=1, media_type="series", title="Beispiel",
        original_title=None, english_title="Beispiel", year=2024, score=1.0,
    )
    base.update(kwargs)
    return metadata.Candidate(**base)


def test_tmdb_japanese_production_counts_as_anime(session, library_tree, monkeypatch):
    """TMDb lists anime as a series. Language and country say what it really is."""
    _stub_lookup(monkeypatch, [_cand(title="Akame ga Kill!", year=2014, anime_signal=True)])
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "pkg" / "Akame.Ga.Kill.S01E05.German.DL.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.media_type == "anime"
    assert job.target_path.startswith(str(library_tree["anime_one"]))


def test_subtitle_marker_in_the_name_counts_as_anime(session, library_tree, monkeypatch):
    """Ger.Eng.Sub appears on the anime releases in this library and on no series."""
    _stub_lookup(monkeypatch, [_cand(title="Wind Breaker", year=2024)])
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "pkg2" / "Wind.Breaker.S01E03.Ger.Eng.Sub.AAC.1080p.WebDL.x264-Grp.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.media_type == "anime"


def test_german_live_action_stays_a_series(session, library_tree, monkeypatch):
    """The anime leaning must not swallow German live action."""
    _stub_lookup(monkeypatch, [_cand(title="Dark", year=2017)])
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "pkg3" / "Dark.S01E03.German.DL.1080p.WEB.h264-XYZ.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.media_type == "series"
    assert job.target_path.startswith(str(library_tree["series"]))


def test_alternative_titles_find_the_existing_folder(session, library_tree, monkeypatch):
    """The folder carries the romaji title, the release the English one."""
    existing = library_tree["anime_one"] / "Mushoku-tensei ~Isekai ittara honki dasu~"
    (existing / "S3").mkdir(parents=True)
    _stub_lookup(monkeypatch, [_cand(
        title="Mushoku Tensei: Jobless Reincarnation", year=2021, anime_signal=True,
        alt_titles=["Mushoku Tensei: Jobless Reincarnation", "Mushoku Tensei: Isekai Ittara Honki Dasu"],
    )])
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "pkg4" / "Mushoku.Tensei.Jobless.Reincarnation.2021.S03E09.Ger.Eng.Sub.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.existing_folder == str(existing)
    assert job.target_dir == str(existing / "S3")


def test_flat_folders_stay_flat(session, library_tree, monkeypatch):
    """63 folders in this library keep their episodes directly in the series folder."""
    existing = library_tree["anime_one"] / "The Worlds Strongest Rearguard"
    existing.mkdir(parents=True)
    (existing / "The.Worlds.Strongest.Rearguard.S01E07.Ger.Eng.Sub.mkv").write_bytes(b"x")
    _stub_lookup(monkeypatch, [_cand(title="The World's Strongest Rearguard", year=2026, anime_signal=True)])
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "pkg5" / "The.Worlds.Strongest.Rearguard.2026.S01E08.Ger.Eng.Sub.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.target_dir == str(existing), job.target_dir
    assert "Season" not in job.target_path


def test_prune_keeps_the_database_small(session, library_tree, set_config):
    """The log must not grow forever on a machine that runs for months."""
    from app.models import Event

    set_config({"event_retention": 20, "job_retention_days": 1})
    for index in range(60):
        pipeline.log(session, f"Ereignis {index}")
    session.flush()

    removed = pipeline.prune(session)
    session.flush()
    remaining = session.scalar(pipeline.select(pipeline.func.count(Event.id)))
    assert removed["events"] > 0
    assert remaining <= 20

    old = Job(
        source_path="/tmp/old.mkv", filename="old.mkv", status="done",
        updated_at=pipeline.utcnow() - __import__("datetime").timedelta(days=5),
    )
    session.add(old)
    session.flush()
    assert pipeline.prune(session)["jobs"] >= 1
    set_config({"event_retention": 5000, "job_retention_days": 60})


def test_prune_and_retry_survive_stored_timestamps(session, library_tree, set_config):
    """Values read back from SQLite are naive. Comparing them with an aware now() crashed
    every pass and rolled the whole pass back."""
    import datetime as dt

    assert pipeline.utcnow().tzinfo is None, "sonst vergleicht sich das nicht mit der Datenbank"

    set_config({"job_retention_days": 1})
    old = Job(
        source_path="/tmp/prune-me.mkv", filename="prune-me.mkv", status="done",
        updated_at=pipeline.utcnow() - dt.timedelta(days=3),
    )
    failed = Job(
        source_path="/tmp/retry-me.mkv", filename="retry-me.mkv", status="failed",
        next_attempt_at=pipeline.utcnow() + dt.timedelta(minutes=10),
    )
    session.add_all([old, failed])
    session.flush()

    assert pipeline.prune(session)["jobs"] >= 1
    pipeline.retry_failed(session)  # darf nicht werfen
    set_config({"job_retention_days": 60})


def test_a_full_pass_commits(session, library_tree, stub_metadata):
    """A pass has to survive to the end, otherwise everything it did is rolled back."""
    library.reindex(session)
    _make_file(library_tree["downloads"] / "commit-check" / "Attack.On.Titan.S02E11.1080p.WEB.mkv")
    result = pipeline.tick(session)
    assert "error" not in result
    assert result["discovered"] == 1


def test_a_file_already_at_the_target_counts_as_done(session, library_tree, stub_metadata, set_config):
    """A pass can move a file and still lose its bookkeeping. The job must not end up
    as an error when the file sits exactly where it was planned."""
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "already" / "Attack.On.Titan.S02E12.1080p.WEB.mkv", 4096)
    _run(session)
    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.target_path

    # Simulate the move without telling the job about it.
    target = Path(job.target_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    source.unlink()

    pipeline.analyze(session, job)
    assert job.status == "done"
    assert "nachgetragen" in job.reason


def test_the_same_show_from_two_sources_is_not_ambiguous(session, library_tree, monkeypatch):
    """AniList liefert Spy x Family als Anime, TMDb dieselbe Serie als Serie. Gleicher
    Titel, gleiches Jahr, also derselbe Stoff und keine offene Frage."""
    def fake_lookup(title, hint, year=None):
        return [
            metadata.Candidate(source="anilist", external_id=1, media_type="anime", title="SPY x FAMILY",
                               original_title=None, english_title="SPY x FAMILY", year=2022, score=1.0,
                               anime_signal=True),
            metadata.Candidate(source="tmdb", external_id=2, media_type="series", title="SPY x FAMILY",
                               original_title=None, english_title="SPY x FAMILY", year=2022, score=1.0,
                               anime_signal=True),
        ], []

    monkeypatch.setattr(pipeline.metadata, "lookup", fake_lookup)
    monkeypatch.setattr(pipeline.metadata, "tmdb_season_plausible", lambda *args: (True, None))
    library.reindex(session)
    source = _make_file(library_tree["downloads"] / "sxf" / "Spy.x.Family.S02E05.Ger.Eng.Sub.1080p.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert "passen gleich gut" not in (job.reason or ""), job.reason
    assert job.media_type == "anime"
    assert job.status in {"planned", "ready", "done"}, job.reason


def test_system_folders_are_not_indexed_as_titles(session, library_tree):
    """Neben den Medien liegen Systemordner von TrueNAS. Die sind keine Serien."""
    (library_tree["series"] / "ix_volumes").mkdir(parents=True, exist_ok=True)
    (library_tree["series"] / ".system").mkdir(parents=True, exist_ok=True)
    (library_tree["series"] / "Echte Serie (2020)").mkdir(parents=True, exist_ok=True)
    library.reindex(session)

    from app.models import LibraryItem
    namen = {item.folder_name for item in session.scalars(pipeline.select(LibraryItem))}
    assert "Echte Serie (2020)" in namen
    assert "ix_volumes" not in namen
    assert ".system" not in namen


def test_duplicate_found_in_a_differently_named_season_folder(session, library_tree, stub_metadata):
    """Die vorhandene Folge liegt in S2, geplant wird nach Season 02, und sie heisst
    voellig anders. Trotzdem ist es dieselbe Folge."""
    ordner = library_tree["anime_one"] / "Attack on Titan (2013)" / "S2"
    ordner.mkdir(parents=True)
    (ordner / "[Zero] Attack on Titan - 2x07 [1080p].mkv").write_bytes(b"alt")
    library.reindex(session)

    source = _make_file(library_tree["downloads"] / "dup1" / "Attack.On.Titan.S02E07.German.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.status == "duplicate", job.reason
    assert "2x07" in (job.duplicate_of or "")


def test_duplicate_found_in_the_other_library(session, library_tree, stub_metadata, set_config):
    """Derselbe Titel liegt in beiden Anime-Pfaden. Die Folge im anderen Pfad zaehlt auch."""
    zweiter = library_tree["anime_two"] / "Attack on Titan (2013)" / "S3"
    zweiter.mkdir(parents=True)
    (zweiter / "Attack on Titan (2013) - S03E05.mkv").write_bytes(b"alt")
    erster = library_tree["anime_one"] / "Attack on Titan (2013)" / "S3"
    erster.mkdir(parents=True)
    (erster / "Attack on Titan (2013) - S03E01.mkv").write_bytes(b"alt")
    library.reindex(session)

    source = _make_file(library_tree["downloads"] / "dup2" / "Attack.On.Titan.S03E05.German.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.status == "duplicate", job.reason
    assert str(zweiter) in (job.duplicate_of or "")


def test_a_new_episode_is_not_a_duplicate(session, library_tree, stub_metadata):
    """Gegenprobe: eine Folge, die es noch nicht gibt, darf nicht als Dublette gelten."""
    from app.models import Rule
    session.execute(pipeline.delete(Rule))  # Regeln aus anderen Tests stoeren hier
    ordner = library_tree["anime_one"] / "Attack on Titan (2013)" / "S2"
    ordner.mkdir(parents=True)
    (ordner / "Attack on Titan (2013) - S02E06.mkv").write_bytes(b"alt")
    library.reindex(session)

    source = _make_file(library_tree["downloads"] / "dup3" / "Attack.On.Titan.S02E08.German.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.status in {"planned", "ready", "done"}, f"{job.status}: {job.reason} | {job.duplicate_of}"
    assert not job.duplicate_of


def test_a_saved_rule_actually_automates(session, library_tree, monkeypatch):
    """Eine Regel ist eine Entscheidung des Benutzers. Mit ihr darf die Datei nicht
    erneut in der Entscheidungsliste landen."""
    from app.models import Rule

    monkeypatch.setattr(pipeline.metadata, "lookup",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("bei einer Regel wird nicht abgefragt")))
    session.add(Rule(match_kind="title", pattern="beispielserie", media_type="anime",
                     title="Beispielserie", year=2024))
    session.flush()
    library.reindex(session)

    source = _make_file(library_tree["downloads"] / "regel" / "Beispielserie.S01E04.German.1080p.WEB.mkv")
    _run(session)

    job = session.scalars(pipeline.select(Job).where(Job.source_path == str(source))).one()
    assert job.status in {"planned", "ready", "done"}, f"{job.status}: {job.reason}"
    assert job.confidence >= 85, job.confidence
    assert job.media_type == "anime"
