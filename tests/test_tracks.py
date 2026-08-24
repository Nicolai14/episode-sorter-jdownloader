"""Die Entscheidung, welche Spur bleibt, ist der riskante Teil. Deutsch und
Japanisch bleiben, unklare Spuren bleiben, im Zweifel wird nichts angefasst."""
from app.core.tracks import Stream, decide

DUR = 1440.0  # 24 Minuten
SIZE = 1_400_000_000


def stream(index, kind="audio", codec="aac", language="", title="", channels=2,
           bitrate=192.0, measured=True, forced=False, default=False):
    return Stream(index=index, kind=kind, codec=codec, language=language, title=title,
                  channels=channels, default=default, forced=forced,
                  bitrate_kbps=bitrate, bitrate_measured=measured)


def video(index=0):
    return stream(index, kind="video", codec="h264", bitrate=8000.0)


def test_keeps_german_and_japanese_drops_the_rest():
    streams = [
        video(),
        stream(1, language="ger", title="German"),
        stream(2, language="jpn", title="Japanese"),
        stream(3, language="rus", title="Russian"),
        stream(4, language="eng", title="English"),
        stream(5, kind="subtitle", codec="ass", language="ger", bitrate=15.0),
        stream(6, kind="subtitle", codec="ass", language="eng", bitrate=15.0),
    ]
    plan = decide(streams, DUR, SIZE)
    assert plan.keep == [0, 1, 2, 5]
    assert plan.drop == [3, 4, 6]
    assert plan.saving_bytes > 0


def test_keeps_german_dub_and_german_sub_together():
    """Wenn Dub und Sub da sind, bleibt beides."""
    streams = [
        video(),
        stream(1, language="deu", title="German Dub"),
        stream(2, language="jpn", title="Japanese"),
        stream(3, kind="subtitle", codec="ass", language="deu", title="German Full", bitrate=20.0),
        stream(4, kind="subtitle", codec="ass", language="deu", title="German Forced", forced=True, bitrate=5.0),
        stream(5, kind="subtitle", codec="ass", language="eng", bitrate=20.0),
    ]
    plan = decide(streams, DUR, SIZE)
    assert plan.keep == [0, 1, 2, 3, 4]
    assert plan.drop == [5]


def test_untagged_tracks_are_never_dropped():
    streams = [video(), stream(1, language=""), stream(2, language="und"), stream(3, language="rus")]
    plan = decide(streams, DUR, SIZE)
    assert plan.keep == [0, 1, 2]
    assert plan.drop == [3]


def test_language_from_the_title_when_the_tag_is_missing():
    streams = [
        video(),
        stream(1, language="", title="Deutsch AAC 2.0"),
        stream(2, language="", title="Russian 5.1"),
    ]
    plan = decide(streams, DUR, SIZE)
    assert 1 in plan.keep
    assert plan.drop == [2]


def test_nothing_recognisable_means_hands_off():
    streams = [video(), stream(1, language="rus"), stream(2, language="eng")]
    plan = decide(streams, DUR, SIZE)
    assert plan.skip_reason
    assert not plan.drop


def test_japanese_audio_without_german_subtitle_keeps_its_subtitles():
    """Sonst bliebe eine japanische Tonspur ohne lesbaren Untertitel zurück."""
    streams = [
        video(),
        stream(1, language="jpn"),
        stream(2, kind="subtitle", codec="ass", language="eng", bitrate=20.0),
    ]
    plan = decide(streams, DUR, SIZE)
    assert plan.skip_reason == "nichts zu entfernen" or 2 in plan.keep
    assert 2 not in plan.drop


def test_single_german_track_is_left_alone():
    streams = [video(), stream(1, language="ger")]
    plan = decide(streams, DUR, SIZE)
    assert plan.skip_reason == "nichts zu entfernen"


def test_saving_is_marked_as_estimated_when_bitrates_are_guessed():
    streams = [
        video(),
        stream(1, language="ger"),
        stream(2, language="rus", measured=False),
    ]
    plan = decide(streams, DUR, SIZE)
    assert plan.drop == [2]
    assert plan.saving_estimated is True


def test_forced_german_signs_track_survives():
    streams = [
        video(),
        stream(1, language="jpn"),
        stream(2, kind="subtitle", codec="ass", language="ger", forced=True, title="Signs", bitrate=8.0),
        stream(3, kind="subtitle", codec="pgs", language="fre", bitrate=200.0),
    ]
    plan = decide(streams, DUR, SIZE)
    assert 2 in plan.keep
    assert plan.drop == [3]
