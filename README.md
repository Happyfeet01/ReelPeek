# ReelPeek

ReelPeek ist eine kleine private Web-App, die öffentlich erreichbare Instagram-Reels und -Beiträge ohne Instagram-Konto anzeigt. Sie verwaltet eine Inbox, einen Verlauf und Favoriten. Es werden weder Instagram-Cookies noch ein Instagram-Konto verwendet.

## Funktionen

- Öffentliche Instagram-Reel-/Post-URL einfügen
- Videos, Bilder und Carousels im eigenen Browser ansehen
- Inbox, Verlauf und Favoriten
- Persönliche Konten per Einladung und E-Mail-Bestätigung, getrennte Sammlungen
- Kontoverwaltung sowie öffentliche Hilfe, Datenschutz und Impressum
- `yt-dlp` und `gallery-dl` (jeweils mit URL-Variante `/reel/` ↔ `/p/`), als letzter anonymer Fallback direkter Abruf der servergerenderten Embed-Seite (`/embed/captioned/`)
- SQLite ohne externen Datenbankserver
- Instagram-Beschreibung direkt unter den Videos (sofern die öffentlich gelieferten Metadaten sie enthalten)
- Kommentare (max. 50) über `yt-dlp --write-comments` bzw. die Embed-Seite, soweit Instagram sie anonym ausliefert
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

## Konten einrichten und bestehende Instanzen aktualisieren

Vor dem Update den Container stoppen und das gesamte Datenverzeichnis sichern. In der bestehenden `.env` die neuen Einstellungen aus `.env.example` ergänzen. `ADMIN_EMAIL` muss ein vom Betreiber kontrolliertes Postfach sein. `PUBLIC_BASE_URL` ist die öffentliche HTTPS-Adresse ohne Pfad. SMTP-Host, Absender und gegebenenfalls Zugangsdaten für STARTTLS (Standardport 587) konfigurieren.

Nach dem Neustart auf der Anmeldeseite **Bestätigungslink anfordern** wählen, die konfigurierte Admin-Adresse eingeben, den Link im Postfach öffnen und ein neues Passwort festlegen. Das frühere gemeinsame Passwort und alte Anmeldesitzungen gewähren keinen Zugang mehr. Vorhandene Einträge, Favoriten und Medien gehören automatisch zum vorab reservierten Betreiberkonto. Eine spätere Änderung von `ADMIN_EMAIL` überträgt dieses Konto nicht.

Unter **Konten verwalten** können Admins Adressen einladen, erneut anschreiben, sperren, freigeben und dauerhaft samt Sammlung löschen. Jeder Empfänger wählt sein Passwort selbst. Ein neuer Link ersetzt den alten. Die erneute Zustellung ist je Adresse einmal pro Minute möglich.

### Öffentliche Informationsseiten

`/hilfe` zeigt die eingestellte Aufbewahrungsfrist. `/impressum` und `/datenschutz` sind bereits vor der Anmeldung über den Seitenfuß erreichbar. Vor dem Freischalten Name, ladungsfähige Anschrift und Kontaktadresse des tatsächlichen Betreibers eintragen. In `IMPRINT_EXTRA` je nach Betreiber weitere Kontakt-, Vertretungs-, Register- oder sonstige Pflichtangaben ergänzen. In `PRIVACY_OPERATIONS` die konkret verwendeten Hosting- und Mailanbieter, Verarbeitungsorte, ggf. Drittlandübermittlungen samt Garantien, Zwecke und Fristen der Server-/Mailprotokolle und Backup-Löschfristen beschreiben. Zuständige Aufsicht und ggf. Datenschutzbeauftragten ergänzen. Für Zeilenumbrüche in der Compose-Env-Datei mehrzeilige einfach zitierte Werte verwenden. Die Texte und Rechtsgrundlagen auf die tatsächlichen Betriebsverhältnisse abstimmen.

Rechtsquellen: [§ 5 DDG](https://www.gesetze-im-internet.de/ddg/__5.html), [Art. 13 und 14 DSGVO](https://eur-lex.europa.eu/eli/reg/2016/679/oj/deu), [§ 25 TDDDG](https://www.gesetze-im-internet.de/ttdsg/__25.html).

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
