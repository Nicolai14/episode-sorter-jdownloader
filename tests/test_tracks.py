"""Die Entscheidung, welche Spur bleibt, ist der riskante Teil. Deutsch und
Japanisch bleiben, unklare Spuren bleiben, im Zweifel wird nichts angefasst."""
from app.core.tracks import Stream, decide

DUR = 1440.0  # 24 Minuten
SIZE = 1_400_000_000


def stream(index, kind="audio", codec="aac", language="", title="", channels=2,
           bitrate=192.0, measured=True, forced=False, default=False, duration=0.0):
    return Stream(index=index, kind=kind, codec=codec, language=language, title=title,
                  channels=channels, default=default, forced=forced,
                  bitrate_kbps=bitrate, bitrate_measured=measured, duration=duration)


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


def test_mode_is_only_copied_when_the_original_has_read_bits(tmp_path, monkeypatch):
    """Auf ZFS stehen viele Dateien auf Modus 000 und haengen an einer ACL. Ein
    chmod darauf wuerde die ACL neu schreiben und die Datei unlesbar machen."""
    import os
    from app.core import tracks as t

    quelle = tmp_path / "film.mkv"
    quelle.write_bytes(b"x" * 2048)
    plan = t.Plan(path=str(quelle), size=2048, keep=[0, 1], drop=[2],
                  streams=[stream(0, kind="video", codec="h264", bitrate=8000.0)])

    os.chmod(quelle, 0o000)  # vor dem Abfangen von chmod, sonst greift es nicht
    aufrufe = []
    monkeypatch.setattr(t.os, "chmod", lambda p, m: aufrufe.append(m))
    monkeypatch.setattr(t, "probe", lambda p: ([stream(0, kind="video", codec="h264", bitrate=8000.0)], 60.0, {}))
    monkeypatch.setattr(t.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0, "stderr": b""})())
    monkeypatch.setattr(t, "_verify", lambda *a, **k: None)
    monkeypatch.setattr(t.shutil, "disk_usage", lambda p: type("D", (), {"free": 10**12})())

    def fake_replace(src, dst):
        os.rename(src, dst)

    temp = quelle.with_name("film.pruning.mkv")
    temp.write_bytes(b"y" * 1024)
    monkeypatch.setattr(t.os, "replace", fake_replace)

    t.remux(plan, dry_run=False)
    assert aufrufe == [], "bei Modus 000 darf kein chmod passieren"


def test_dropping_the_longest_track_is_not_a_shortened_file(tmp_path, monkeypatch):
    """Der englische Untertitel lief eine Minute laenger als das Video und bestimmte
    damit die Containerlaufzeit. Faellt er weg, ist die Datei kuerzer ohne Verlust."""
    from app.core import tracks as t

    quelle = [
        stream(0, kind="video", codec="h264", bitrate=3000.0, duration=1449.0),
        stream(1, language="ger", duration=1450.0),
        stream(2, kind="subtitle", codec="ass", language="ger", bitrate=20.0, duration=1449.3),
        stream(3, kind="subtitle", codec="ass", language="eng", bitrate=20.0, duration=1508.6),
    ]
    neu = [s for s in quelle if s.index != 3]
    monkeypatch.setattr(t, "probe", lambda p: (neu, 1455.8, {}))
    assert t._verify(quelle, [0, 1, 2], tmp_path / "x.mkv", 1509.9) is None

    # Fehlt dagegen wirklich Inhalt, faellt es weiterhin auf
    monkeypatch.setattr(t, "probe", lambda p: (neu, 900.0, {}))
    assert t._verify(quelle, [0, 1, 2], tmp_path / "x.mkv", 1509.9)


def test_duration_tolerance_scales_with_the_runtime(tmp_path, monkeypatch):
    """Faellt eine Tonspur weg, die etwas laenger lief als das Video, wird die Datei
    rechnerisch ein paar Sekunden kuerzer. Ein echter Abbruch reisst Minuten."""
    from app.core import tracks as t

    video = stream(0, kind="video", codec="h264", bitrate=8000.0)
    monkeypatch.setattr(t, "probe", lambda p: ([video], 5730.0, {}))
    knapp = t._verify([video], [0], tmp_path / "x.mkv", 5734.7)
    assert knapp is None, "4,7 Sekunden bei 96 Minuten sind normal"

    monkeypatch.setattr(t, "probe", lambda p: ([video], 6479.6, {}))
    grob = t._verify([video], [0], tmp_path / "x.mkv", 6971.0)
    assert grob and "Laufzeit" in grob, "acht Minuten fehlen ist ein Abbruch"


def test_data_streams_do_not_break_the_check(tmp_path, monkeypatch):
    """MP4 traegt oft eine Datenspur, und ffmpeg legt beim Schreiben selbst eine an.
    Gezaehlt werden nur Bild, Ton und Untertitel."""
    from app.core import tracks as t

    quelle = [
        stream(0, kind="video", codec="h264", bitrate=3000.0),
        stream(1, language="ger"),
        stream(2, language="jpn"),
        stream(4, kind="data", codec="bin_data", bitrate=1.0, measured=False),
    ]
    neu = [
        stream(0, kind="video", codec="h264", bitrate=3000.0),
        stream(1, language="ger"),
        stream(2, language="jpn"),
        stream(3, kind="data", codec="bin_data", bitrate=1.0, measured=False),
        stream(4, kind="data", codec="timecode", bitrate=1.0, measured=False),
    ]
    monkeypatch.setattr(t, "probe", lambda p: (neu, 1440.0, {}))
    assert t._verify(quelle, [0, 1, 2, 4], tmp_path / "x.mp4", 1440.0) is None

    fehlt_ton = [s for s in neu if not (s.kind == "audio" and s.language == "jpn")]
    monkeypatch.setattr(t, "probe", lambda p: (fehlt_ton, 1440.0, {}))
    problem = t._verify(quelle, [0, 1, 2, 4], tmp_path / "x.mp4", 1440.0)
    assert problem and "audio" in problem
