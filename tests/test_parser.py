import pytest

from app.core.parser import parse, title_key


@pytest.mark.parametrize(
    "name,title,season,episode",
    [
        ("Attack.On.Titan.WEBX.1080pS02E01.mkv", "Attack On Titan", 2, 1),
        ("Attack on Titan S2E1.mkv", "Attack on Titan", 2, 1),
        ("Attack.on.Titan.S02.E01.German.DL.1080p.WEB.h264-GRP.mkv", "Attack on Titan", 2, 1),
        ("The.Expanse.2x01.1080p.mkv", "The Expanse", 2, 1),
        ("Dark.Season 3 Episode 8.German.mkv", "Dark", 3, 8),
        ("One Piece - Episode 37.mkv", "One Piece", None, 37),
        ("One.Piece.EP037.1080p.mkv", "One Piece", None, 37),
    ],
)
def test_episode_patterns(name, title, season, episode):
    result = parse(name)
    assert result.title == title
    assert result.season == season
    assert result.episode == episode


def test_multi_episode_forms():
    assert (parse("Show.S02E01E02.mkv").episode, parse("Show.S02E01E02.mkv").episode_end) == (1, 2)
    assert (parse("Show.S02E01-E03.mkv").episode, parse("Show.S02E01-E03.mkv").episode_end) == (1, 3)
    assert (parse("Show.S02E01-02.mkv").episode, parse("Show.S02E01-02.mkv").episode_end) == (1, 2)


def test_specials_and_ova():
    special = parse("Naruto.S00E01.mkv")
    assert special.season == 0 and special.special_kind == "special"
    ova = parse("Bleach OVA 2 GerSub.mkv")
    assert ova.season == 0 and ova.special_kind == "ova" and ova.media_hint == "anime"


def test_absolute_anime_numbering():
    result = parse("[SubsPlease] Frieren - 12 (1080p) [A1B2C3D4].mkv")
    assert result.title == "Frieren"
    assert result.absolute_episode == 12
    assert result.episode is None
    assert result.media_hint == "anime"
    assert result.group == "SubsPlease"


def test_movie_detection():
    result = parse("Inception.2010.1080p.BluRay.x264-AMIABLE.mkv")
    assert result.title == "Inception"
    assert result.year == 2010
    assert result.media_hint == "movie"
    assert result.episode is None


def test_technical_tokens_are_removed():
    result = parse("Some.Show.S01E02.German.DL.2160p.UHD.BluRay.HEVC.DTS-HD.MA-GROUP.mkv")
    assert result.title == "Some Show"
    assert result.resolution == "2160p"
    assert "german" in result.languages


def test_title_key_normalisation():
    assert title_key("The Melancholy of Haruhi Suzumiya") == "melancholy of haruhi suzumiya"
    assert title_key("Fullmetal Alchemist: Brotherhood") == title_key("Fullmetal Alchemist Brotherhood")


def test_title_falls_back_to_folder():
    result = parse("01.mkv", folder_name="Attack.on.Titan.S02.German.1080p.WEB")
    assert result.title == "Attack on Titan"


def test_release_style_folder_names_are_understood():
    from app.core.library import folder_title

    assert folder_title("3.Idiots.2009.German.AC3D.DL.720p.BluRay.x264-Pate") == ("3 Idiots", 2009)
    assert folder_title("28.Years.Later.The.Bone.Temple.2026.German.EAC3.DL.2160p.WEB.h265-VECTOR") == (
        "28 Years Later The Bone Temple",
        2026,
    )
    # Plain library folders stay untouched
    assert folder_title("Attack on Titan (2013)") == ("Attack on Titan", 2013)
    assert folder_title("Akame Ga Kill") == ("Akame Ga Kill", None)
    assert folder_title("Dr. Stone") == ("Dr. Stone", None)
