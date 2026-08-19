# Episode Sorter

Sortiert fertige JDownloader-Downloads automatisch in die passenden Anime-, Serien- und Filmordner
auf dem TrueNAS. Erkennung über Dateinamen-Parser, Abgleich über TMDb und AniList, Kontrolle über ein
Dashboard. Unsichere Fälle werden nie geraten, sondern zur Entscheidung vorgelegt.

```
Browser -> JDownloader -> Download und automatisches Entpacken -> Episode Sorter -> Zielordner
```

## Was die Anwendung macht

1. **JDownloader überwachen.** Über My JDownloader werden laufende, fertige und fehlgeschlagene Pakete
   gelesen. Parallel läuft eine Ordnerwache über den Downloadordner, damit die Sortierung auch ohne
   JD-Verbindung funktioniert.
2. **Warten, bis wirklich fertig.** Eine Datei wird erst angefasst, wenn sie mehrfach hintereinander
   unverändert groß ist und im Ordner kein Archiv mehr entpackt wird.
3. **Namen auswerten.** Der Parser versteht `S02E01`, `S2E1`, `S02.E01`, `2x01`, `S02E01E02`,
   `S02E01-E03`, `Episode 37`, `EP037`, `Season 2 Episode 5`, `Special`, `OVA`, `S00E01` und absolute
   Anime-Nummern wie `[SubsPlease] Frieren - 12`. Technische Bestandteile (1080p, 2160p, WEB-DL, WEBRip,
   BluRay, x264, x265, HEVC, GERMAN, ENGLISH, DUBBED, MULTI, Release-Gruppen) fallen aus dem Titel raus.
4. **Metadaten prüfen.** TMDb für Filme und Serien (v3 API Key oder v4 Read Access Token, beides
   wird erkannt), AniList zusätzlich für Anime. Geprüft werden
   Existenz des Titels, englischer Titel, Erscheinungsjahr, Plausibilität von Staffel und Folge sowie
   alternative Titel. Deutsche Releasenamen werden dabei auf den englischen Titel gemappt
   (`Die.Verurteilten.1994` landet in `The Shawshank Redemption (1994)`). Bei zwei gleich guten
   Treffern wird nichts verschoben. Welche Quelle bei einem mehrdeutigen Titel wie `Dark` gewinnt,
   entscheidet der Dateiname: ohne Anime-Merkmale zählt TMDb mehr, mit Anime-Merkmalen AniList.
5. **Bestehende Ordner gewinnen.** Alle vier Zielpfade werden indexiert. Existiert der erkannte Titel
   schon irgendwo, landet die Folge genau dort, unabhängig vom eingestellten Standardpfad. Das gilt
   besonders für die beiden Anime-Speicherorte. Bestehende Ordner werden nicht umbenannt.
6. **Sicher verschieben.** Vor dem Verschieben werden Quelle, Zielpfad, Schreibrechte, freier Speicher
   und Zieldatei geprüft. Über Dataset-Grenzen hinweg wird unter temporärem Namen kopiert, die Größe
   oder Prüfsumme kontrolliert, am Ziel umbenannt und erst dann die Quelle gelöscht. Bei einem Fehler
   bleibt die Quelldatei liegen.
7. **Begleitdateien.** Passende Untertitel werden mitgenommen und mit umbenannt
   (`... - S02E01.de.srt`). Samples, Werbedateien und ausführbare Dateien werden ignoriert.
8. **Dubletten.** Vorhandene Episoden und Filme werden nie automatisch überschrieben. Das Dashboard
   zeigt beide Dateien mit Größe, Auflösung, Codec, Audio- und Untertitelsprachen zum Vergleich.

## Schnellstart

```bash
git clone https://github.com/Nicolai14/episode-sorter-jdownloader.git
cd episode-sorter-jdownloader
cp .env.example .env      # TMDB_API_KEY und optional die JD-Zugangsdaten eintragen
docker compose up -d --build
```

Dashboard: `http://<truenas-ip>:8080`, über cloudflared zusätzlich unter `<hostname>`.

Der Container startet im **Dry Run**. Es wird nichts verschoben, nur geplant. Erst wenn genügend echte
Downloads richtig geplant wurden, den Schalter oben rechts im Dashboard umlegen.

## Konfiguration

Alles lässt sich im Dashboard unter *Einstellungen* ändern, die Werte liegen in der SQLite-Datenbank.
Beim ersten Start werden sie aus den Umgebungsvariablen vorbelegt.

| Einstellung | Standard | Zweck |
| --- | --- | --- |
| `download_dir` | `/downloads` | überwachter Downloadordner |
| `anime_path_1` | `/mnt/poolToshiba/Animes` | Anime-Speicherort 1 |
| `anime_path_2` | `/mnt/dataGrepDataset/Anime` | Anime-Speicherort 2 |
| `series_path` | `/mnt/poolToshiba/Serien` | Serien |
| `movies_path` | `/mnt/poolToshiba/Filme` | Filme |
| `default_anime_path` | `/mnt/poolToshiba/Animes` | Ziel nur für völlig neue Anime |
| `auto_threshold` | `85` | ab welcher Sicherheit automatisch einsortiert wird |
| `dry_run` | `true` | nur planen, nicht verschieben |
| `verify_mode` | `size` | `size` oder `sha256` beim Kopieren über Datasets |
| `episode_template` | `{title} ({year}) - S{season:02d}E{episode:02d}` | Dateiname Episode |
| `movie_template` | `{title} ({year})` | Dateiname Film |
| `ignored_terms` | `sample, trailer, proof, ...` | Dateien, die nie angefasst werden |

## Zielstruktur

```
Attack on Titan (2013)/
└── Season 02/
    ├── Attack on Titan (2013) - S02E01.mkv
    ├── Attack on Titan (2013) - S02E01.de.srt
    └── Attack on Titan (2013) - S02E01.en.srt

Inception (2010)/
└── Inception (2010).mkv
```

## Manuelle Entscheidungen

Zur Entscheidung vorgelegt werden: mehrere passende Titel, unklare Medienart, unklare Staffel oder
Folge, absolute Anime-Nummern, mehrere Videodateien im Download, Staffelpakete, Specials und OVAs,
Dubletten sowie fehlende Metadaten. Jede manuelle Zuordnung lässt sich als **Regel** speichern.
Spätere Dateien mit gleichem Titelmuster werden dadurch automatisch erkannt.

## Aufbau

```
episode-sorter-jdownloader
├── backend/app
│   ├── main.py            FastAPI, statisches Dashboard, Start des Schedulers
│   ├── config.py          Einstellungen, Umgebungsvariablen, Defaults
│   ├── models.py          SQLite-Schema (Jobs, Regeln, Bibliothek, Pakete, Ereignisse)
│   ├── api/routes.py      REST-Schnittstelle
│   └── core
│       ├── parser.py      Dateinamen zu Titel, Staffel, Folge
│       ├── metadata.py    TMDb und AniList
│       ├── library.py     Index der vorhandenen Ordner
│       ├── naming.py      Zielpfade aus Vorlagen
│       ├── mover.py       Prüfen, kopieren, verifizieren, löschen
│       ├── files.py       Stabilität, Begleitdateien, Filter
│       ├── mediainfo.py   ffprobe für den Dublettenvergleich
│       ├── jdownloader.py My JDownloader
│       ├── pipeline.py    Ablauf und Entscheidungen
│       └── scheduler.py   Hintergrundschleife
├── web                    Dashboard (HTML, CSS, ES-Module, kein Buildschritt)
├── tests                  pytest
└── docker-compose.yml     episode-sorter, jdownloader, cloudflared
```

## Schnittstelle

| Methode | Pfad | Zweck |
| --- | --- | --- |
| GET | `/api/status` | Zähler, Pfadprüfung, JD-Status, Scheduler |
| GET | `/api/jobs?status=review` | Dateien nach Status |
| POST | `/api/jobs/{id}/decision` | `approve`, `override`, `select_candidate`, `skip`, `retry`, `defer`, `duplicate_replace`, `duplicate_keep_both`, `duplicate_discard` |
| POST | `/api/scan` | Durchlauf sofort starten |
| GET/PUT | `/api/settings` | Einstellungen lesen und schreiben |
| GET | `/api/library`, POST `/api/library/reindex` | Ordnerindex |
| GET/POST/DELETE | `/api/rules` | gespeicherte Zuordnungen |
| GET | `/api/jd/packages`, POST `/api/jd/connect` | JDownloader |
| GET | `/api/search?q=` | Metadatensuche für manuelle Zuordnung |

## Entwicklung

```bash
pip install -r backend/requirements.txt pytest httpx
python -m pytest                     # Parser, Pipeline, Verschiebung, API
ES_DATA_DIR=./data ES_WEB_DIR=./web uvicorn app.main:app --app-dir backend --reload --port 8080
```

## Sicherheitsnetz

* Dry Run ist der Auslieferungszustand.
* Eine Zieldatei wird nie automatisch überschrieben.
* Die Quelldatei wird erst gelöscht, wenn die Kopie verifiziert ist.
* Fehlgeschlagene Jobs werden bis zu dreimal mit Abstand erneut versucht und bleiben sonst sichtbar.
* Der Ordnerindex wird beim Start und auf Knopfdruck neu aufgebaut.
