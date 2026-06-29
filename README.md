# 🏠 Dublin Room Radar

Automated room rental monitor for Dublin, Ireland. Polls multiple property listing platforms on a schedule, sends a compact email alert when new listings appear, and serves a local interactive dashboard where you can search, sort, filter by zone, rate every listing, and keep private notes — all saved to a local database.

Built around **UCD Belfield** as the reference point for distance and transit calculations, but the zone system and area list are straightforward to customise for any other location.

---

## Table of contents

1. [Features](#features)
2. [How it works](#how-it-works)
3. [Zones](#zones)
4. [Requirements](#requirements)
5. [Installation — macOS / Linux](#installation--macos--linux)
6. [Installation — Windows](#installation--windows)
7. [Configuration](#configuration)
8. [First-time setup — Transit data](#first-time-setup--transit-data-optional-but-recommended)
9. [Running the script](#running-the-script)
10. [The email digest](#the-email-digest)
11. [The dashboard](#the-dashboard)
12. [Platform notes](#platform-notes)
13. [File structure](#file-structure)
14. [Customising search areas](#customising-search-areas)
15. [Troubleshooting](#troubleshooting)
16. [Disclaimer](#disclaimer)
17. [License](#license)
18. [Acknowledgements](#acknowledgements)

---

## Features

- **Multi-source monitoring** — polls Daft.ie (primary internal API), MyHome.ie, SpareRoom.ie, and Rent.ie simultaneously. Each source degrades gracefully: if one fails, the rest keep running
- **Zone-grouped results** — all listings are automatically classified into five distance zones from UCD Belfield, verified against real centroid coordinates, and sorted nearest-first within each zone
- **Gender filter** — search for male-suitable, female-suitable, or all listings at the Daft.ie API level
- **Compact email digest** — a lightweight summary email that stays well under Gmail's 102 KB clip limit; shows a zone overview table and up to 8 nearest listings per zone, with a button to open the full dashboard
- **NEW TODAY badge** — highlights listings that appeared for the first time today
- **Smart email cadence** — only sends a new email when at least one genuinely new listing is found; never spams you with identical digests
- **Persistent state** — ratings, checked/done status, private notes, and seen history are all saved to SQLite and survive restarts and script updates
- **Interactive local dashboard** — a professional web UI served at `http://localhost:8765` with real-time search, zone and status filters, sort controls, per-listing notes, and live stat counters
- **GTFS transit info** — per-listing bus directions computed from TFI's official static GTFS feed, accurate to the nearest bus stop rather than just the neighbourhood

---

## How it works

```
Every 20 minutes (daemon mode):
  ├── Fetch listings from Daft.ie, MyHome.ie, SpareRoom.ie, Rent.ie
  ├── Filter by price (≤€1,250/mo) and gender preference
  ├── Classify each listing into a zone using GPS coordinates
  ├── Look up nearest bus stop and route to UCD (GTFS database)
  ├── Upsert into SQLite — new listings inserted, existing ones updated
  │     └── Ratings, checked status, and notes are never overwritten
  ├── If any brand-new listings found → send email digest
  └── Dashboard at localhost:8765 always reflects the current database
```

---

## Zones

All 51 search areas are assigned to one of five zones by straight-line distance from UCD Belfield (`53.3079, -6.2236`). Every area has been individually distance-verified against its actual centroid. The bands overlap slightly because commuting character (a coastal DART suburb vs an inland one at the same distance) does not follow hard rings.

### Zone A — Walk / Cycle (< 3 km) 🟢
*5–25 min on foot or by bike. Closest ring to campus.*

Belfield · Clonskeagh · Merrion · Donnybrook · Windy Arbour · Mount Merrion · Booterstown · Goatstown · Milltown · Stillorgan

> Windy Arbour is only 1.5 km from UCD — closer than most Zone A areas — and was moved here from Zone B after a distance audit.

### Zone B — South Dublin (2–5 km) 🔵
*15–30 min by bus or Luas Green line. Inner southside suburbs.*

Ranelagh · Rathgar · Rathmines · Dartry · Harold's Cross · Churchtown · Dundrum · Sandyford · Blackrock · Ballinteer · Kilmacud

> Kilmacud is inland at 2.4 km, adjacent to Stillorgan. It was corrected from Zone D (Coastal), where it never belonged.

### Zone C — South-West (4–8.5 km) 🟡
*25–45 min by bus. Inland south-west corridor (Dublin 6W / D12 / D16).*

Terenure · Kimmage · Templeogue · Rathfarnham · Crumlin · Drimnagh · Perrystown · Knocklyon · Firhouse · Ballyboden

### Zone D — Coastal / South County (4.5–12 km) 🟣
*20–45 min by bus or DART. Coastal DART corridor and outer south county.*

Monkstown · Dun Laoghaire · Glasthule · Sandycove · Foxrock · Leopardstown · Cabinteely · Cornelscourt · Carrickmines · Glenageary · Dalkey · Killiney · Shankill

### Zone E — City / North-of-Canal (3.5–8 km) 🔵
*20–40 min by bus. City-centre side and just north of the Grand Canal.*

Portobello · South Circular Road · Rialto · Phibsborough · Stoneybatter · Drumcondra · Glasnevin

> **North Dublin excluded by default** — Swords, Malahide, Artane, and Clare Hall are not searched because journey times to UCD are too long for daily commuting.

---

## Requirements

- Python 3.9 or later (Anaconda works fine)
- A Gmail account with an **App Password** — requires 2-Step Verification to be enabled on your Google account

---

## Installation — macOS / Linux

```bash
# 1. Clone the repository
git clone https://github.com/your-username/dublin-room-radar.git
cd dublin-room-radar

# 2. Install Python dependencies
pip install curl_cffi schedule beautifulsoup4 lxml flask

# If you use Anaconda
pip install curl_cffi schedule beautifulsoup4 lxml flask --break-system-packages
```

Verify everything installed correctly:
```bash
python3 -c "from curl_cffi import requests; import flask, schedule; print('All OK')"
```

---

## Installation — Windows

Windows requires a couple of extra steps. `curl_cffi` ships native compiled builds for Windows and works without any additional setup.

### Step 1 — Install Python

Download Python **3.11 or later** from [python.org/downloads](https://www.python.org/downloads/windows/).

During installation:
- ✅ Check **"Add Python to PATH"** before clicking Install
- ✅ Check **"Install pip"** (should be on by default)

Verify in Command Prompt (`Win + R` → type `cmd` → Enter):
```cmd
python --version
pip --version
```

### Step 2 — Clone and install

Open Command Prompt and run:
```cmd
git clone https://github.com/your-username/dublin-room-radar.git
cd dublin-room-radar
pip install curl_cffi schedule beautifulsoup4 lxml flask
```

If you use **Anaconda on Windows**, open **Anaconda Prompt** instead of Command Prompt and run the same `pip install` line.

### Step 3 — Verify

```cmd
python -c "from curl_cffi import requests; import flask, schedule; print('All OK')"
```

### Windows-specific notes

- Use **`python`** instead of `python3` in every command shown in this guide
- File paths use `%USERPROFILE%` instead of `~` — for example, `~/daft/` becomes `C:\Users\YourName\daft\`
- The dashboard at `http://localhost:8765` works in Edge, Chrome, or Firefox
- If Windows Firewall shows a prompt when the daemon starts, click **Allow access**

---

## Configuration

Open `daft_monitor.py` in any text editor and update the config block near the top of the file. These are the only values you need to change before your first run.

### Gmail credentials

```python
GMAIL_FROM   = "your.email@gmail.com"    # Gmail address to send FROM
GMAIL_TO     = "your.email@gmail.com"    # Gmail address to send TO (can be the same)
GMAIL_APP_PW = "xxxx xxxx xxxx xxxx"     # 16-character App Password from Google
```

**How to generate an App Password:**
1. Go to [myaccount.google.com](https://myaccount.google.com) → Security
2. Enable **2-Step Verification** if it is not already on
3. Go to **App Passwords** → create one → select Mail
4. Copy the 16-character code (shown once) into `GMAIL_APP_PW`

> ⚠️ **Security warning.** Your App Password grants full send access to your Gmail account. Never share it, never commit it to a public repository. If one is accidentally exposed, revoke it immediately at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) and generate a new one.

### Gender filter

```python
# "male"   → listings explicitly marked male-suitable only
# "female" → listings explicitly marked female-suitable only
# ""       → all listings regardless of gender preference
GENDER_FILTER = "male"
```

| Value | What is returned |
|-------|-----------------|
| `"male"` | Only listings the landlord has marked as male-suitable |
| `"female"` | Only listings the landlord has marked as female-suitable |
| `""` | Everything — listings with no gender preference are included |

> The gender filter is applied at the Daft.ie API level. Scraper sources (MyHome, SpareRoom, Rent.ie) return all results regardless.

### Price ceiling

```python
MAX_PRICE = 1250    # Maximum monthly rent in euros — no lower bound
```

Prices listed weekly on Daft are automatically converted to monthly equivalents.

### Source toggles

```python
ENABLE_DAFT      = True    # Daft.ie — primary; leave this True
ENABLE_MYHOME    = True    # MyHome.ie — best-effort HTML scrape
ENABLE_SPAREROOM = True    # SpareRoom.ie — works much better from an Irish IP
ENABLE_RENT_IE   = True    # Rent.ie — often blocked by Cloudflare from abroad
```

Set any of the last three to `False` if they are timing out or returning errors from your location.

### Poll interval and dashboard port

```python
POLL_INTERVAL  = 20     # Minutes between checks in daemon mode
DASHBOARD_PORT = 8765   # Access the dashboard at http://localhost:8765
```

---

## First-time setup — Transit data (optional but recommended)

Build the GTFS transit database once before running the monitor. This enables accurate, stop-level bus information per listing — for example: *"walk 3 min to Terenure Road West → S4 → UCD Sports Centre, ≈26 min total"*.

```bash
# macOS / Linux
python3 gtfs_build_db.py

# Windows
python gtfs_build_db.py
```

**What this does:**
1. Downloads the TFI GTFS static feed (~144 MB) from Transport for Ireland
2. Processes 9 million rows of stop-times data in two passes (~2–3 minutes on an SSD)
3. Writes `~/daft/gtfs_transit.db` — a compact SQLite lookup table used by the monitor on every run

Re-run this script once a month to stay current with route changes.

### If the download fails with a 403 error

The TFI server blocks automated downloads from non-browser clients, particularly from outside Ireland. The fix is a manual browser download.

**1. Open this URL in Safari or Chrome — it will auto-download:**
```
https://www.transportforireland.ie/transitData/Data/GTFS_Realtime.zip
```

**2. Move the file into place:**
```bash
# macOS / Linux
mv ~/Downloads/GTFS_Realtime.zip ~/daft/gtfs.zip

# Windows (Command Prompt)
move "%USERPROFILE%\Downloads\GTFS_Realtime.zip" "%USERPROFILE%\daft\gtfs.zip"
```

**3. Re-run the script** — it detects the file, skips the download, and processes immediately:
```bash
python3 gtfs_build_db.py   # macOS / Linux
python  gtfs_build_db.py   # Windows
```

The monitor works without the transit database — listings simply won't include bus directions.

---

## Running the script

### macOS / Linux

| Command | What it does |
|---------|-------------|
| `python3 daft_monitor.py --once` | Fetch listings, send one email, then exit. Good for testing config. |
| `python3 daft_monitor.py --daemon` | Fetch on a schedule **and** serve the dashboard. Main mode. |
| `python3 daft_monitor.py --dashboard` | Serve the dashboard only, no fetching. Browse what you already have. |
| `python3 daft_monitor.py --clear --once` | Wipe database and do a fresh run. Deletes all history, ratings, and notes. |

### Windows

Same commands, with `python` instead of `python3`:

```cmd
python daft_monitor.py --once
python daft_monitor.py --daemon
python daft_monitor.py --dashboard
python daft_monitor.py --clear --once
```

### Running in the background (macOS / Linux)

```bash
nohup python3 ~/daft/daft_monitor.py --daemon > ~/daft/monitor.log 2>&1 &
echo $! > ~/daft/monitor.pid
echo "Started. PID: $(cat ~/daft/monitor.pid)"
```

Check that it started:
```bash
tail -f ~/daft/monitor.log
# Expect to see: "Dashboard serving at http://localhost:8765"
```

Stop it:
```bash
kill $(cat ~/daft/monitor.pid)
```

### Running in the background (Windows)

**Option A — Task Scheduler (recommended):**
1. Open Task Scheduler (`Win + S` → "Task Scheduler")
2. Create Basic Task → name it "Dublin Room Radar"
3. Trigger: **When I log on**
4. Action: **Start a program**
   - Program: `python`
   - Arguments: `C:\Users\YourName\daft\daft_monitor.py --daemon`
5. Finish

**Option B — Quick background start:**
```cmd
start /B python %USERPROFILE%\daft\daft_monitor.py --daemon > %USERPROFILE%\daft\monitor.log 2>&1
```

---

## The email digest

An email is only sent when at least one brand-new listing is found. It is designed to be lightweight and fully Gmail-compatible — well under Gmail's 102 KB clip limit.

### Email layout

```
┌──────────────────────────────────────────────────────┐
│ 🏠  218 new rooms — Dublin                           │
│ Suitable for: male · ≤€1250/mo · Daft: 218 · 21:06  │
├──────────────────────────────────────────────────────┤
│  [ Open the dashboard to rate, filter & see all → ]  │ ← big button
│  Gmail blocks direct clicks — right-click → New Tab  │ ← caption
├──────────────────────────────────────────────────────┤
│ NEW LISTINGS BY ZONE                                  │
│  🟢 Zone A  < 3 km                           32      │
│  🔵 Zone B  2–5 km                           45      │
│  🟡 Zone C  4–8.5 km                         57      │
│  🟣 Zone D  4.5–12 km                        20      │
│  🔵 Zone E  3.5–8 km                         64      │
├──────────────────────────────────────────────────────┤
│ 🟢 Zone A — Walk / Cycle · 5–25 min                  │
│  ● NEW  Roebuck Castle, Clonskeagh — €900  0.7 km →  │
│  ● NEW  Glenard Hall, Goatstown — €550     1.0 km →  │
│  ...  (up to 8 nearest per zone)                     │
│  + 24 more in this zone — open dashboard →           │
└──────────────────────────────────────────────────────┘
```

### ⚠️ Important: opening the dashboard link from Gmail

> **Gmail blocks direct clicks on `localhost` links.**
>
> When you click any link in Gmail, Google intercepts it and routes it through their safety proxy on Google's own servers. `localhost:8765` on Google's servers is not your computer — so the click silently fails or shows a connection error.
>
> **The fix is simple: right-click the button → "Open Link in New Tab"** (Chrome / Edge) or **"Open in New Tab"** (Safari / Firefox). This bypasses Gmail's proxy and opens the URL directly in your browser, where `localhost:8765` correctly reaches your running dashboard.
>
> Alternatively, type **`localhost:8765`** directly into your browser's address bar at any time.

This is a Gmail behaviour, not a bug in the script. It affects every tool that links to localhost from email.

Each listing row in the email shows:
- **● NEW** if first seen today
- Your existing rating (👍 / 😐 / 👎) if you've already rated it in the dashboard
- Price, area name, and distance to UCD
- A direct link to the listing on Daft / MyHome / SpareRoom

Only the 8 nearest listings per zone appear in the email preview. The complete list is always available in the dashboard.

---

## The dashboard

Open **`http://localhost:8765`** in your browser while the daemon is running.

> **If you see "Can't connect to the server"** — the daemon is not running. Start it with `python3 daft_monitor.py --daemon` and keep that Terminal window open. Then open `http://localhost:8765`.

### Stats header

The dark header shows four live counters that update in real time as you rate and act on listings:

| Stat | What it means |
|------|---------------|
| **New today** | Listings first inserted today |
| **To review** | Unchecked listings you haven't disliked |
| **👍 Liked** | Listings you've rated as interested |
| **Avg / month** | Average monthly price across all listings |

### Search

Type any text into the search box to filter in real time across title, area name, price string, and platform name simultaneously.

```
Examples:
  "ranelagh"    → all Ranelagh listings
  "clonskeagh"  → Zone A Clonskeagh listings only
  "900"         → listings mentioning €900
  "spareroom"   → listings from SpareRoom.ie
  "parking"     → listings mentioning parking in their title
```

### Sort

Choose the sort order from the dropdown next to the search box. Sorting always happens within each zone, so zone grouping is preserved.

| Option | Behaviour |
|--------|-----------|
| Nearest to UCD | Shortest straight-line distance first (default) |
| Price: low → high | Cheapest first within each zone |
| Price: high → low | Most expensive first |
| Newest first | Most recently discovered listings first |

### Status filter pills

| Pill | Shows |
|------|-------|
| **To review** | Unchecked, not disliked — your default working view |
| **All** | Every listing in the database |
| **👍 Liked** | Listings you've rated as interested |
| **📝 Noted** | Listings where you've written a private note |
| **✓ Done** | Listings you've marked as checked |

### Zone chips

Coloured chips below the pills let you narrow to a single zone. Zone and status filters combine — for example "To review" + "Zone A" shows only unchecked Zone A listings.

### Listing cards

```
┌──────────────────────────────────────────────────────────┐
│ [□]  Clonskeagh Road — bright double room        [Daft]  │
│      [● NEW TODAY]                                       │
│      €900/mo  📍 Clonskeagh · 0.7 km  📅 28 Jun 2026   │
│      🚌 ✅ 39A walk 3min → Stop → 18min → UCD (≈21min)  │
│      [👍][😐][👎]  [View listing →] [📍 Map] [📝 Note]  │
└──────────────────────────────────────────────────────────┘
```

**Checkbox (top-left)** — click to toggle between "to review" and "done". Checked listings move to the ✓ Done view and are excluded from future emails.

**Rating buttons** — three emoji buttons in a segmented control:

| Button | Meaning | Email effect |
|--------|---------|-------------|
| 👍 Like | Interested — want to follow up | Kept in "To review"; appears in future emails |
| 😐 Neutral | Undecided | Kept in "To review"; appears in future emails |
| 👎 Dislike | Not interested | **Removed from all future email digests permanently** |

Click a rating button again to clear it. Every click saves immediately.

**View listing →** — opens the original listing on Daft.ie / MyHome.ie / SpareRoom.ie in a new tab.

**📍 Map** — opens Google Maps centred on the listing's GPS location.

**📝 Note** — opens a text field below the card. Write anything: the landlord's name and number, a viewing date, deposit amount, pros and cons. Notes are:
- Saved automatically when you stop typing (no submit button needed)
- Persisted in the database and visible every time you open the dashboard
- Flagged with a **Note •** amber highlight when a note exists
- Filterable via the **📝 Noted** status pill
- HTML-escaped for safety (special characters cannot break the page)

### Auto-refresh

The dashboard reloads silently every 5 minutes so newly discovered listings from the background daemon appear without a manual refresh.

---

## Platform notes

| Platform | Reliability | Method | Notes |
|----------|-------------|--------|-------|
| **Daft.ie** | ✅ Reliable | Internal API | 51 areas covered; gender filter at API level |
| **MyHome.ie** | ⚠️ Best-effort | HTML scrape | Smaller room-share inventory; best from an Irish IP |
| **SpareRoom.ie** | ⚠️ Best-effort | HTML scrape | Works significantly better from an Irish IP |
| **Rent.ie** | ⚠️ Best-effort | HTML scrape | Frequently blocked by Cloudflare from outside Ireland |

Each source is wrapped in a try/except. If one fails entirely, it logs a warning and the others continue. The log output after each run shows per-source counts:

```
Fetching Daft.ie...
  Daft.ie: 218 listings
Fetching MyHome.ie...
  MyHome.ie: 0 listings       ← empty but not an error
Fetching SpareRoom.ie...
  SpareRoom.ie: 0 listings
```

---

## File structure

```
dublin-room-radar/
├── daft_monitor.py        # Main script — fetcher, email, dashboard
├── gtfs_build_db.py       # One-time GTFS preprocessing (run once before first use)
├── README.md
└── LICENSE
```

Files generated at runtime (in `~/daft/` on macOS/Linux, `%USERPROFILE%\daft\` on Windows):

```
~/daft/
├── daft_seen.db           # SQLite — listings, ratings, notes, checked state
├── gtfs_transit.db        # SQLite — bus route lookup (built by gtfs_build_db.py)
├── gtfs.zip               # TFI GTFS download cache (safe to delete to force re-download)
└── monitor.log            # Log output when running in the background
```

### Database columns

The `listings` table in `daft_seen.db` stores:

| Column | Description |
|--------|-------------|
| `id` | Unique ID, e.g. `Daft:6588909` |
| `source` | Platform: `Daft`, `MyHome`, `SpareRoom`, `Rent.ie` |
| `area` · `zone` | Area name and full zone label |
| `title` · `price` · `url` | Listing details |
| `lat` · `lon` · `dist_km` | GPS coordinates and distance to UCD |
| `posted` · `seller` · `facilities` | Metadata |
| `transit` | GTFS bus directions string |
| `rating` | `like`, `neutral`, `dislike`, or `""` |
| `checked` | `1` = done, `0` = to review |
| `notes` | Your private notes (max 2,000 characters) |
| `first_seen` · `last_seen` | Timestamps |

The `notes` column is added automatically to any existing database via `ALTER TABLE` — no data loss, no need to run `--clear` after an update.

---

## Customising search areas

Three dicts near the top of `daft_monitor.py` define the search geography:

- **`ZONES`** — zone definitions: area lists, colours, commute estimate labels
- **`DAFT_AREAS`** — maps each area name to its Daft.ie geoFilter integer ID
- **`AREA_COORDS`** — maps each area name to a `(lat, lon)` centroid used when GPS coordinates are unavailable from the listing

To **add a new area**, add entries to all three dicts. To find the Daft.ie geoFilter ID:

```python
from daftlistings import Location
print([(l.name, l.value["id"]) for l in Location if "DUBLIN" in l.name])
```

Place the area in whichever zone's distance band matches its actual straight-line distance from UCD. The bands overlap deliberately — a coastal DART suburb at 5 km belongs in Zone D even if an inland suburb at the same distance would be Zone B or C.

To **remove an area**, delete its entry from all three dicts.

---

## Troubleshooting

**`ModuleNotFoundError: No module named 'curl_cffi'`**
```bash
pip install curl_cffi
pip install curl_cffi --break-system-packages   # Anaconda
```

**`sqlite3.OperationalError: no such column`**
Your database is from an older version. Delete it to start fresh (this erases all history, ratings, and notes):
```bash
rm ~/daft/daft_seen.db                            # macOS / Linux
del "%USERPROFILE%\daft\daft_seen.db"             # Windows
python3 daft_monitor.py --once
```

**`SMTPAuthenticationError`**
Your App Password is wrong or has been revoked. Generate a new one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).

**`Safari / Chrome can't connect to the server "localhost"`**
The daemon is not running. Start it:
```bash
python3 daft_monitor.py --daemon
```
Keep that Terminal open, then navigate to `http://localhost:8765`.

**The dashboard button in Gmail does nothing**
Gmail routes all link clicks through Google's proxy. `localhost:8765` on Google's servers is not your Mac.
**Fix: right-click the button → "Open Link in New Tab"**, or type `localhost:8765` directly into your browser's address bar.

**GTFS download returns `403 Forbidden`**
See [manual download steps](#if-the-download-fails-with-a-403-error) above.

**All scraper sources returning 0 listings**
Normal if you are connecting from outside Ireland. Disable them and rely on Daft:
```python
ENABLE_SPAREROOM = False
ENABLE_RENT_IE   = False
```

**Dashboard stops updating**
The daemon may have crashed. Check the log and restart:
```bash
tail ~/daft/monitor.log
python3 daft_monitor.py --daemon
```

---

## Disclaimer

This tool makes automated HTTP requests to Daft.ie and other platforms. Automated scraping may be against their Terms of Service. It is intended for personal, non-commercial use only. Use responsibly — the built-in delays between requests exist for a reason. The author takes no responsibility for any consequences arising from the use of this tool.

---

## License

MIT — see [LICENSE](LICENSE) for details.

---

## Acknowledgements

- [Transport for Ireland](https://www.transportforireland.ie/) — GTFS static feed used for transit data
- [daftlistings](https://github.com/AnthonyBloomer/daftlistings) — area ID reference used during zone setup
