import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1] / "backend"
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("ES_DATA_DIR", tempfile.mkdtemp(prefix="episode-sorter-test-"))

import pytest  # noqa: E402

from app import config  # noqa: E402
from app.db import engine, session_scope  # noqa: E402
from app.models import Base  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _database():
    Base.metadata.create_all(engine)
    config.bootstrap()
    yield


@pytest.fixture
def session():
    with session_scope() as sess:
        yield sess


@pytest.fixture
def set_config(session):
    """Change settings from inside a test without deadlocking the open session."""

    def _apply(values):
        config.update(values, session=session)
        session.commit()
        config.refresh()

    return _apply


@pytest.fixture
def library_tree(tmp_path):
    """A download folder plus the four library roots."""
    downloads = tmp_path / "downloads"
    anime_one = tmp_path / "poolToshiba" / "Animes"
    anime_two = tmp_path / "dataGrepDataset" / "Anime"
    series = tmp_path / "poolToshiba" / "Serien"
    movies = tmp_path / "poolToshiba" / "Filme"
    for path in (downloads, anime_one, anime_two, series, movies):
        path.mkdir(parents=True, exist_ok=True)
    config.update({
        "download_dir": str(downloads),
        "anime_path_1": str(anime_one),
        "anime_path_2": str(anime_two),
        "series_path": str(series),
        "series_path_2": "",
        "movies_path": str(movies),
        "movies_path_2": "",
        "default_anime_path": str(anime_one),
        "default_series_path": str(series),
        "default_movie_path": str(movies),
        "min_video_size_mb": 0,
        "stability_checks": 1,
        "dry_run": True,
        "auto_threshold": 85,
        "tmdb_api_key": "",
        "use_anilist": False,
    })
    return {
        "downloads": downloads,
        "anime_one": anime_one,
        "anime_two": anime_two,
        "series": series,
        "movies": movies,
    }
