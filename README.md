# Moon Bot

Discord-Bot, der die Athanor-Monde einer EVE-Corp trackt. Die Daten kommen ausschließlich über die SeAT-API, der Bot braucht keinen eigenen ESI-Login.

- **Status-Channel:** pro Athanor eine Nachricht, die jede Stunde (und sofort bei neuen Events) bearbeitet wird: Fortschrittsbalken, Ready-/Autofracture-Zeit, Fuel-Ampel, Ore-Zusammensetzung (bzw. „Last extraction“, wenn gerade nichts läuft).
- **Ping-Channel:** Wenn ein Mond poppt (Laser gefeuert oder Autofracture), kommt ein Ping mit zufälligem Spruch, Ore-Liste und ⛏️-Reaktion zum X-uppen.

## Woher die Daten kommen


| Info | SeAT-Endpoint |
|---|---|
| Athanors, Fuel, Drill-Service | `/api/v2/corporation/structures/{corp}` |
| Extraction (Ready, Autofracture, Ore) und Pops | `/api/v2/character/notifications/{char}` (`MoonminingExtractionStarted`, `…Finished`, `…Cancelled`, `MoonminingLaserFired`, `MoonminingAutomaticFracture`) |
| Namen von Monden und Erzen | öffentliches ESI `/universe/names/` (ohne Login) |

**Eigenheiten der SeAT-API:**
- Sie ignoriert `$filter` und liefert immer 15 Notifications pro Seite. Der Bot liest deshalb beim ersten Start alle Seiten, danach nur noch die neuesten.
- Bei `oreVolumeByType` gehen die Typ-IDs verloren, es bleibt nur eine Liste von Mengen. CCP sortiert diese Liste nach aufsteigender Typ-ID. Sobald ihr pro Mond einmal mit `/moons ores` angebt, welche Erze er hat, ordnet der Bot die Mengen richtig zu. Bis dahin steht dort „Ore #1, #2 …“.

Die SeAT-API hat keinen Extraction-Endpoint. Der Status wird deshalb aus den Notifications rekonstruiert. Monde, deren `ExtractionStarted` SeAT nie gesehen hat, stehen bis zur nächsten Extraction auf „No extraction data yet“.

**Voraussetzung:** Der Charakter in `NOTIFY_CHAR_IDS` muss Director oder Station Manager sein und in SeAT mit dem Notifications-Scope eingetragen sein.

## Setup

> **Secrets gehören nur in die `.env`.** Sie steht in `.gitignore` und darf nie committet werden.

### 1. Discord-Bot anlegen
1. <https://discord.com/developers/applications> → **New Application**
2. **Bot** → **Reset Token** → Token kopieren (für `DISCORD_TOKEN`). Es werden keine privilegierten Intents gebraucht.
3. **OAuth2 → URL Generator**: Scopes `bot` und `applications.commands`. Bot-Permissions: *View Channels, Send Messages, Embed Links, Read Message History, Add Reactions, Mention @everyone, @here and All Roles*
4. Generierte URL öffnen und den Bot auf euren Server einladen.

### 2. SeAT-API-Key
In SeAT unter **API Tokens** einen Key erstellen (für `SEAT_TOKEN`). Wenn SeAT eine IP-Whitelist verlangt, die IP des Docker-Hosts eintragen.

### 3. Holen und konfigurieren
Alles (Code, `.env`, Datenbank in `data/`) liegt in einem Verzeichnis, z. B. `~/moonbot`:
```bash
mkdir -p ~/moonbot && cd ~/moonbot
git clone https://github.com/Schlaibi/OPTM-Moon-Bot.git .   # der Punkt = in dieses Verzeichnis
mkdir -p data
cp .env.example .env    # alle Werte eintragen
id                      # uid/gid als PUID/PGID in die .env
```

### 3b. Optional: SeAT-Datenbank für Erznamen (empfohlen)
Die SeAT-API verliert die Erz-Typen, die SeAT-Datenbank hat sie noch. Läuft SeAT mit Docker auf demselben Server:
```bash
cp docker-compose.seatdb.yml docker-compose.override.yml
docker ps --format '{{.Names}}'                                            # DB-Container finden (mariadb/mysql)
docker inspect <db-container> --format '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}'
```
In die `.env`: `SEAT_NETWORK` (Netzwerk von oben), `SEAT_DB_HOST` (Name des DB-Containers) und Name, User und Passwort der Datenbank aus der `.env` von SeAT. Der Bot liest damit nur die Tabelle `character_notifications` (nur `SELECT`). Ohne DB fällt er auf die API zurück, dann müssen die Erze per `/moons ores` gesetzt werden.

### 4. Testlauf ohne Discord (empfohlen)
```bash
docker compose run --rm moonbot python -m moonbot.check
```
Zeigt alle Athanors und den erkannten Status. So seht ihr vor dem Go-live, ob der Key und die Daten passen.

### 5. Starten
```bash
docker compose up -d --build
docker compose logs -f
```

**Updates einspielen:**
```bash
git pull && docker compose up -d --build
```
Die Datenbank (Message-IDs, Ore-Konfiguration) liegt in `data/moonbot.db` und bleibt bei Updates erhalten. Zum Sichern einfach das Verzeichnis kopieren.

### 6. In Discord einrichten
```
/moons setup status_channel:#moons ping_channel:#moon-pops [ping_role:@Miners]
```
Ohne `ping_role` wird mit `@here` gepingt. Die Befehle sehen nur Leute mit „Server verwalten“; das lässt sich unter *Servereinstellungen → Integrationen* anpassen.

| Befehl | Was er macht |
|---|---|
| `/moons setup` | Channels und Ping-Rolle festlegen, danach sofort alle Posts erzeugen |
| `/moons ores <athanor> <erze>` | Einmalig pro Mond: welche Erze er hat, z. B. `Sylvite, Scheelite, Sperrylite, Chromite` (Reihenfolge egal) |
| `/moons refresh` | Sofort neue Daten aus SeAT holen und alle Posts aktualisieren |
| `/moons fuelwarn <tage>` | Ab wann der Fuel-Status rot wird (gelb ab dem Doppelten). Standard: 7 |
| `/moons config` | Aktuelle Einstellungen anzeigen |
| `/moons testpop` | Vorschau einer Pop-Nachricht, nur für dich sichtbar, ohne Ping |

## Verhalten
- **Erster Start:** Alle vorhandenen Notifications werden importiert, aber **nicht** gepingt.
- **Pings** gibt es nur für Pops, die jünger als `PING_MAX_AGE_H` (Standard 6 h) sind. War der Bot länger offline, kommt kein verspäteter Ping-Spam.
- **Doppelte Events** von mehreren Charakteren werden über Struktur, Typ und Minute zusammengelegt.
- **Gelöschte Status-Posts** werden beim nächsten Update neu erstellt. Wird ein Athanor entfernt, verschwindet auch sein Post.
- **Ein gesprengter Belt** gilt 48 h nach dem Pop als weg, danach steht der Mond auf „Idle“ mit der letzten Ore-Zusammensetzung.
- **Verzögerung:** ESI-Cache (~10 min) + SeAT-Abholintervall + `NOTIF_INTERVAL_MIN`.

Sprüche für die Pop-Nachricht stehen in `moonbot/messages.py`, einfach erweitern.

## Entwicklung
```bash
pip install -r requirements.txt pytest
python -m pytest
```
