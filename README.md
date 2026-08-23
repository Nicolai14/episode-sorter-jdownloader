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
5. **Bestehende Ordner gewinnen.** Alle konfigurierten Zielpfade werden indexiert (zwei Anime-,
   zwei Serien- und ein Filmspeicherort). Existiert der erkannte Titel
   schon irgendwo, landet die Folge genau dort, unabhängig vom eingestellten Standardpfad. Das gilt
   besonders für die beiden Anime-Speicherorte. Bestehende Ordner werden nicht umbenannt, und ein
   vorhandener Staffelordner behält seine Schreibweise: liegt dort `S2`, kommt die Folge nach `S2`
   und nicht in ein neues `Season 02`.
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

## Betrieb auf dem TrueNAS

Der Stack liegt unter `/mnt/AppPool/DockerStacks/episode-sorter` und läuft als Docker Compose
Projekt neben den bestehenden Apps.

| Was | Wert |
| --- | --- |
| Dashboard intern | `http://<nas-ip>:18080` |
| Dashboard extern | `https://<hostname>` (Cloudflare Access) |
| JDownloader intern | `http://<nas-ip>:5800` |
| JDownloader extern | `https://<hostname>` (Cloudflare Access) und `my.jdownloader.org` |
| Downloadordner | `/mnt/SmallPool/dataGrepDataset/Downloads/JDownloader` |
| Container-Nutzer | `3000:0` (smb), damit verschobene Dateien über SMB nutzbar bleiben |

Der Cloudflare-Tunnel läuft als eigene TrueNAS-App (`truenas-claude`), deshalb bleibt der
cloudflared-Service in der Compose-Datei hinter dem Profil `tunnel` und startet hier nicht mit.

Beide Hostnamen liegen hinter Cloudflare Access. Ohne Anmeldung kommt niemand an ein Dashboard,
das Dateien verschieben und löschen kann.

```bash
ssh nas
cd /mnt/AppPool/DockerStacks/episode-sorter
git pull && sudo docker compose build episode-sorter && sudo docker compose up -d
sudo docker compose logs -f --tail 50 episode-sorter
```

## Konfiguration

Alles lässt sich im Dashboard unter *Einstellungen* ändern, die Werte liegen in der SQLite-Datenbank.
Beim ersten Start werden sie aus den Umgebungsvariablen vorbelegt.

| Einstellung | Standard | Zweck |
| --- | --- | --- |
| `download_dir` | `/mnt/SmallPool/dataGrepDataset/Downloads/JDownloader` | überwachter Downloadordner |
| `anime_path_1` | `/mnt/poolToshiba/Anime` | Anime-Speicherort 1 |
| `anime_path_2` | `/mnt/SmallPool/dataGrepDataset/Anime` | Anime-Speicherort 2 |
| `series_path` | `/mnt/poolToshiba/Serien` | Serien 1 |
| `series_path_2` | `/mnt/SmallPool/dataGrepDataset/Serien` | Serien 2 |
| `movies_path` | `/mnt/poolToshiba/Filme` | Filme |
| `default_anime_path` | `/mnt/SmallPool/dataGrepDataset/Anime` | Ziel nur für völlig neue Anime |
| `default_series_path` | `/mnt/SmallPool/dataGrepDataset/Serien` | Ziel nur für völlig neue Serien |
| `default_movie_path` | `/mnt/poolToshiba/Filme` | Ziel nur für völlig neue Filme |
| `auto_threshold` | `85` | ab welcher Sicherheit automatisch einsortiert wird |
| `dry_run` | `true` | nur planen, nicht verschieben |
| `verify_mode` | `size` | `size` oder `sha256` beim Kopieren über Datasets |
| `episode_template` | `{title} ({year}) - S{season:02d}E{episode:02d}` | Dateiname Episode |
| `season_folder_template` | `S{season}` | Staffelordner für neue Titel, vorhandene behalten ihre Schreibweise |
| `prefer_anime` | `true` | unklare Serienfälle gelten als Anime |
| `movie_template` | `{title} ({year})` | Dateiname Film |
| `ignored_terms` | `sample, trailer, proof, ...` | Dateien, die nie angefasst werden |

## Zielstruktur

```
Attack on Titan (2013)/
└── S2/
    ├── Attack on Titan (2013) - S02E01.mkv
    ├── Attack on Titan (2013) - S02E01.de.srt
    └── Attack on Titan (2013) - S02E01.en.srt

Inception (2010)/
└── Inception (2010).mkv
```

## JDownloader

Die Anbindung läuft über My JDownloader. Im Dashboard unter *Einstellungen* die Kontodaten
eintragen, `jd_enabled` einschalten, danach zeigt die Ansicht *JDownloader* laufende, fertige,
entpackende und fehlgeschlagene Pakete.

JDownloader meldet seine eigenen Containerpfade (`/output/...`). `jd_path_prefix` übersetzt sie
auf den überwachten Downloadordner, damit im Dashboard der Pfad steht, den auch TrueNAS zeigt.

Von unterwegs geht die Steuerung über my.jdownloader.org, die Handy-App oder
`https://<hostname>`. Die Sortierung selbst braucht die Verbindung nicht, der
Ordnerwächter arbeitet unabhängig weiter.

## Click'n'Load auf einen entfernten JDownloader

Click'n'Load funktioniert so, dass die Webseite per JavaScript an
`http://127.0.0.1:9666` schickt, also immer an den Rechner, auf dem der Browser läuft. Ein
JDownloader auf dem NAS bekommt davon nichts mit. Die offizielle Browser-Erweiterung war
Manifest V2 und funktioniert in Chrome seit Anfang 2025 nicht mehr.

Der Weg, der ohne Erweiterung funktioniert: auf dem eigenen Rechner Port 9666 auf den NAS
weiterleiten. Für die Webseite sieht das aus wie ein lokaler JDownloader.

**Windows, einmalig als Administrator, überlebt Neustarts:**

```powershell
netsh interface portproxy add v4tov4 listenaddress=127.0.0.1 listenport=9666 connectaddress=<nas-ip> connectport=9666
netsh interface portproxy show all      # zum Prüfen
netsh interface portproxy delete v4tov4 listenaddress=127.0.0.1 listenport=9666   # zum Entfernen
```

**macOS oder Linux, solange das Fenster offen bleibt:**

```bash
ssh -N -L 9666:127.0.0.1:9666 root@<nas-ip>
```

Zwei Dinge sind dabei wichtig:

* Ein lokal laufender JDownloader belegt Port 9666 selbst und gewinnt. Also lokal beenden.
* JDownloader fragt bei jeder Webseite einmal nach, ob sie Links hinzufügen darf. Diese
  Rückfrage erscheint jetzt auf dem NAS, also im JD-Fenster unter `<hostname>`.
  Einmal *immer erlauben* klicken, danach steht die Seite in der Liste
  `ExternInterfaceAuth` und die Frage kommt nicht wieder.

Der Container veröffentlicht dafür Port 9666 im LAN. Dieser Port nimmt Links ohne
Anmeldung an, er darf nicht ins Internet.

Ohne Click'n'Load geht außerdem immer: einfache Links über `my.jdownloader.org`, die
Handy-App oder das JD-Fenster einwerfen.

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
