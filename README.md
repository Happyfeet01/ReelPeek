# ReelPeek

ReelPeek ist eine kleine private Web-App, die öffentlich erreichbare Instagram-Reels und -Beiträge ohne Instagram-Konto anzeigt. Sie verwaltet eine Inbox, einen Verlauf und Favoriten. Es werden weder Instagram-Cookies noch ein Instagram-Konto verwendet.

## Funktionen

- Öffentliche Instagram-Reel-/Post-URL einfügen
- Videos, Bilder und Carousels im eigenen Browser ansehen
- Inbox, Verlauf und Favoriten
- Passwortgeschützter Zugriff
- `gallery-dl` mit `yt-dlp` als Fallback
- SQLite ohne externen Datenbankserver
- Instagram-Beschreibung direkt unter den Videos (sofern die öffentlich gelieferten Metadaten sie enthalten)
- Automatische Bereinigung aller nicht favorisierten Videos, Bilder und Vorschaubilder nach standardmäßig 14 Tagen, auch ohne Seitenaufrufe
- Favorisierte Medien bleiben erhalten; URL, Beschreibung und Verlauf bleiben nach einer Bereinigung bestehen
- Docker Compose und fertige Nginx-Reverse-Proxy-Konfiguration

Wichtig: Instagram ändert seine öffentlichen Schnittstellen häufig. Manche öffentlichen Beiträge werden anonym trotzdem nicht ausgeliefert. ReelPeek umgeht keine Anmeldung, keine privaten Profile und keine Zugriffsbeschränkungen.

## Installation

```bash
git clone https://github.com/Happyfeet01/ReelPeek.git reelpeek
cd reelpeek
cp .env.example .env
nano .env
mkdir -p data
docker compose up -d --build
```

Für `SESSION_SECRET` kann ein Wert so erzeugt werden:

```bash
openssl rand -hex 32
```

Die App lauscht anschließend ausschließlich lokal auf `127.0.0.1:8777`.

## Nginx

1. In `nginx/reelpeek.conf` die Domain und Zertifikatspfade ersetzen.
2. Konfiguration kopieren und aktivieren:

```bash
sudo cp nginx/reelpeek.conf /etc/nginx/sites-available/reelpeek.conf
sudo ln -s /etc/nginx/sites-available/reelpeek.conf /etc/nginx/sites-enabled/reelpeek.conf
sudo nginx -t
sudo systemctl reload nginx
```

Falls noch kein Zertifikat existiert, zuerst nur einen HTTP-VHost bzw. Certbot verwenden und anschließend den enthaltenen HTTPS-VHost aktivieren.

## Aktualisieren

Weil Instagram seine Auslieferung regelmäßig ändert, sollten `gallery-dl` und `yt-dlp` aktuell gehalten werden. Beide werden beim Image-Build frisch installiert:

```bash
docker compose build --pull --no-cache
docker compose up -d
```

## Betrieb und Datenschutz

- Alle Daten liegen in `./data`.
- Favorisierte Medien bleiben gespeichert.
- Nicht favorisierte Videos/Bilder **und ihre Vorschaubilder** werden 14 Tage nach dem letzten erfolgreichen Download entfernt (`RETENTION_DAYS=14`); URL, Beschreibung und Verlauf bleiben erhalten.
- Favoriten werden nie automatisch entfernt. Beim Entfernen der Favoriten-Markierung greift wieder das ursprüngliche Download-Datum (nicht das Datum der Markierungsänderung).
- Die Bereinigung läuft beim Start, beim Aufruf der Übersicht und unabhängig davon einmal täglich. `CACHE_TTL_HOURS` aus der Vorversion wird **ignoriert**.
- Beim Update von 0.1.0 wird das Download-Datum für bestehende Medien aus `created_at` übernommen. Alte, nicht favorisierte Medien können deshalb bereits beim ersten Start entfernt werden.
- Vor dem Update immer das ganze Verzeichnis `data/` (inklusive SQLite-WAL-Dateien und Medien) konsistent sichern. Dazu ReelPeek stoppen.
- Zum Anzeigen der Löschkandidaten **vor** dem Update kann die mitgelieferte CLI verwendet werden (zunächst auf einer Backup-Kopie der Daten): `python -m app.cleanup --dry-run --data-dir /pfad/zur/kopie` (benötigt eine lauffähige Python-Umgebung).
- Löschen in der Oberfläche entfernt Datenbankeintrag, Medien und Vorschaubild.
- Für Backups reicht das Verzeichnis `data/`. Vor einem konsistenten Dateibackup den Container kurz stoppen.
- Stelle ReelPeek nur über HTTPS ins Internet und verwende ein langes Passwort.

## Test

Im Projektverzeichnis:

```bash
python -m unittest discover -s tests
```

Der eigentliche Instagram-Abruf ist bewusst kein automatisierter Test, weil er von Instagrams jeweils aktuellem anonymem Zugriff abhängt.
