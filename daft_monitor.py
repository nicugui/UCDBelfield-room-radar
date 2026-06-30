"""
daft_monitor.py  ·  v3 — multi-source + zone-grouped interactive email
Monitors multiple Dublin housing platforms for room listings,
groups results by zone (distance from UCD), and emails an interactive
tabbed digest (no endless scrolling — click a zone tab to jump).

Sources:
  • Daft.ie       — primary, internal API (reliable)
  • MyHome.ie     — best-effort HTML/API scrape
  • SpareRoom.ie  — best-effort (works better from an Irish IP)
  • Rent.ie       — best-effort (often Cloudflare-gated)
Each source degrades gracefully: if one fails, the others still run.

Setup:   pip install curl_cffi schedule beautifulsoup4 lxml
Run:     python3 daft_monitor.py --once
Daemon:  python3 daft_monitor.py --daemon
Clear:   python3 daft_monitor.py --clear --once
"""

import os, sqlite3, smtplib, re, time, math, json, argparse, logging
from collections import defaultdict
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from curl_cffi import requests

# ── CONFIG ────────────────────────────────────────────────────────────────────

GMAIL_FROM    = "YOUR_EMAIL@gmail.com"       # ← your Gmail address
GMAIL_TO      = "YOUR_EMAIL@gmail.com"       # ← destination (can be same)
GMAIL_APP_PW  = "XXXX XXXX XXXX XXXX"        # ← 16-char Google App Password

MAX_PRICE      = YOUR_MAX_PRICE                   # ← target monthly max rent
GENDER_FILTER  = "YOUR_GENDER"          # "male", "female", or "" for all listings
POLL_INTERVAL  = 20
DASHBOARD_PORT = 8765          # local dashboard at http://localhost:8765
DB_PATH        = os.path.expanduser("~/daft/daft_seen.db")
TRANSIT_DB     = os.path.expanduser("~/daft/gtfs_transit.db")

# Which sources to run (toggle off any that misbehave from your location)
ENABLE_DAFT      = True
ENABLE_MYHOME    = True
ENABLE_SPAREROOM = True
ENABLE_RENT_IE   = True

UCD_LAT, UCD_LON = 53.3079, -6.2236

# ── ZONES ─────────────────────────────────────────────────────────────────────

ZONES = {
    "Zone A — Walk / Cycle  (< 3 km)": {
        "color": "#059669", "est": "5–25 min walk/cycle",
        # Closest ring to UCD. Windy Arbour added (1.5 km, closer than most of A).
        "areas": ["Belfield","Clonskeagh","Merrion","Donnybrook","Windy Arbour",
                  "Mount Merrion","Booterstown","Goatstown","Milltown","Stillorgan"],
    },
    "Zone B — South Dublin  (2–5 km)": {
        "color": "#2563eb", "est": "15–30 min by bus / Luas",
        # Inner southside suburbs. Kilmacud moved here from D (it is inland,
        # adjacent to Stillorgan — nowhere near the coast).
        "areas": ["Ranelagh","Rathgar","Rathmines","Dartry","Harold's Cross",
                  "Churchtown","Dundrum","Sandyford","Blackrock","Ballinteer","Kilmacud"],
    },
    "Zone C — South-West  (4–8.5 km)": {
        "color": "#d97706", "est": "25–45 min by bus",
        # Inland south-west corridor (Dublin 6W / 12 / 16).
        "areas": ["Terenure","Kimmage","Templeogue","Rathfarnham","Crumlin",
                  "Drimnagh","Perrystown","Knocklyon","Firhouse","Ballyboden"],
    },
    "Zone D — Coastal / South County  (4.5–12 km)": {
        "color": "#7c3aed", "est": "20–45 min by bus / DART",
        # Coastal DART corridor + outer south county. Kilmacud removed (→ B).
        "areas": ["Monkstown","Dun Laoghaire","Glasthule","Sandycove","Foxrock",
                  "Leopardstown","Cabinteely","Cornelscourt","Carrickmines",
                  "Glenageary","Dalkey","Killiney","Shankill"],
    },
    "Zone E — City / North-of-Canal  (3.5–8 km)": {
        "color": "#0891b2", "est": "20–40 min by bus",
        # City-centre-side and just north of the canal/Liffey.
        "areas": ["Portobello","South Circular Road","Rialto","Phibsborough",
                  "Stoneybatter","Drumcondra","Glasnevin"],
    },
}

AREA_TO_ZONE = {}
for zlabel, cfg in ZONES.items():
    for a in cfg["areas"]:
        AREA_TO_ZONE[a] = zlabel

# Daft.ie geoFilter IDs
DAFT_AREAS = {
    "Clonskeagh":824,"Stillorgan":2323,"Donnybrook":1872,"Booterstown":2071,
    "Mount Merrion":2171,"Goatstown":1887,"Milltown":2169,"Merrion":2168,"Belfield":2066,
    "Ranelagh":2259,"Rathgar":2262,"Rathmines":2264,"Dartry":2118,"Harold's Cross":1030,
    "Windy Arbour":2325,"Churchtown":2099,"Dundrum":1881,"Sandyford":2315,"Blackrock":2067,"Ballinteer":2050,
    "Terenure":1893,"Kimmage":2157,"Templeogue":1892,"Rathfarnham":2261,"Crumlin":1848,
    "Drimnagh":1874,"Perrystown":2223,"Knocklyon":2160,"Firhouse":1096,"Ballyboden":2052,
    "Monkstown":2170,"Dun Laoghaire":1882,"Glasthule":2131,"Sandycove":2314,"Foxrock":2129,
    "Leopardstown":2161,"Kilmacud":498,"Cabinteely":2073,"Cornelscourt":2114,"Carrickmines":2076,
    "Glenageary":1884,"Dalkey":1849,"Killiney":440,"Shankill":2318,
    "Portobello":2246,"South Circular Road":2321,"Rialto":2267,"Glasnevin":1363,
    "Drumcondra":1875,"Phibsborough":2224,"Stoneybatter":2130,
}

# Centroid coords for each area — used to classify scraped listings (which
# may lack GPS) into the correct zone, and to estimate distance to UCD.
AREA_COORDS = {
    "Clonskeagh":(53.3083,-6.2356),"Stillorgan":(53.2889,-6.2003),"Donnybrook":(53.3193,-6.2305),
    "Booterstown":(53.3060,-6.1989),"Mount Merrion":(53.2967,-6.2089),"Goatstown":(53.2900,-6.2270),
    "Milltown":(53.3155,-6.2520),"Merrion":(53.3138,-6.2089),"Belfield":(53.3079,-6.2236),
    "Ranelagh":(53.3245,-6.2540),"Rathgar":(53.3162,-6.2700),"Rathmines":(53.3236,-6.2654),
    "Dartry":(53.3120,-6.2620),"Harold's Cross":(53.3217,-6.2790),"Windy Arbour":(53.3010,-6.2440),
    "Churchtown":(53.2967,-6.2580),"Dundrum":(53.2940,-6.2440),"Sandyford":(53.2780,-6.2230),
    "Blackrock":(53.3020,-6.1780),"Ballinteer":(53.2840,-6.2570),
    "Terenure":(53.3090,-6.2870),"Kimmage":(53.3170,-6.2980),"Templeogue":(53.2960,-6.3080),
    "Rathfarnham":(53.2980,-6.2870),"Crumlin":(53.3270,-6.3050),"Drimnagh":(53.3270,-6.3170),
    "Perrystown":(53.3100,-6.3170),"Knocklyon":(53.2820,-6.3120),"Firhouse":(53.2790,-6.3370),
    "Ballyboden":(53.2830,-6.2960),
    "Monkstown":(53.2940,-6.1560),"Dun Laoghaire":(53.2940,-6.1340),"Glasthule":(53.2920,-6.1240),
    "Sandycove":(53.2890,-6.1140),"Foxrock":(53.2640,-6.1730),"Leopardstown":(53.2660,-6.2010),
    "Kilmacud":(53.2900,-6.2030),"Cabinteely":(53.2570,-6.1530),"Cornelscourt":(53.2680,-6.1610),
    "Carrickmines":(53.2480,-6.1740),"Glenageary":(53.2810,-6.1270),"Dalkey":(53.2770,-6.1010),
    "Killiney":(53.2560,-6.1130),"Shankill":(53.2310,-6.1230),
    "Portobello":(53.3300,-6.2660),"South Circular Road":(53.3340,-6.2900),"Rialto":(53.3340,-6.2990),
    "Glasnevin":(53.3720,-6.2710),"Drumcondra":(53.3690,-6.2570),"Phibsborough":(53.3580,-6.2720),
    "Stoneybatter":(53.3530,-6.2870),
}

# ── LOGGING ───────────────────────────────────────────────────────────────────

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()])
log = logging.getLogger("daft_monitor")

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# ── HELPERS ───────────────────────────────────────────────────────────────────

def hav_km(lat1, lon1, lat2, lon2):
    R = 6371
    p, q = math.radians(lat1), math.radians(lat2)
    a = (math.sin(math.radians(lat2-lat1)/2)**2
         + math.cos(p)*math.cos(q)*math.sin(math.radians(lon2-lon1)/2)**2)
    return R*2*math.atan2(math.sqrt(a), math.sqrt(1-a))

def parse_monthly_price(s):
    s = str(s).replace(",","").replace("€","").replace("£","").strip()
    m = re.search(r"[\d.]+", s)
    if not m: return 0.0
    v = float(m.group())
    if "week" in str(s).lower() or "pw" in str(s).lower():
        v = v*52/12
    return round(v,2)

def classify_area(text):
    """Best-effort: match a known area name inside an address string."""
    if not text: return None
    t = text.lower()
    for area in AREA_COORDS:
        if area.lower() in t:
            return area
    return None

def make_listing(source, lid, title, price, area, lat, lon, url, posted="", seller="", facilities=""):
    """Normalised listing dict shared across all sources."""
    if (lat is None or lon is None) and area in AREA_COORDS:
        lat, lon = AREA_COORDS[area]
    dist = round(hav_km(lat, lon, UCD_LAT, UCD_LON),1) if lat and lon else 99.0
    return {
        "_source": source, "id": f"{source}:{lid}", "raw_id": str(lid),
        "title": title or "—", "price": price or "?",
        "_area_name": area or "Dublin", "_zone": AREA_TO_ZONE.get(area, "Other"),
        "_dist_km": dist, "lat": lat, "lon": lon, "url": url,
        "_posted": posted, "_seller": seller, "_facilities": facilities,
        "_transit_info": "",
    }

# ── DATABASE ──────────────────────────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS listings (
        id TEXT PRIMARY KEY, source TEXT, area TEXT, zone TEXT,
        title TEXT, price TEXT, url TEXT, lat REAL, lon REAL, dist_km REAL,
        posted TEXT, seller TEXT, facilities TEXT, transit TEXT,
        rating TEXT DEFAULT '', checked INTEGER DEFAULT 0, notes TEXT DEFAULT '',
        first_seen TEXT, last_seen TEXT)""")
    # Add notes column to pre-existing DBs that don't have it yet
    existing_cols = {c[1] for c in conn.execute("PRAGMA table_info(listings)").fetchall()}
    if "notes" not in existing_cols:
        conn.execute("ALTER TABLE listings ADD COLUMN notes TEXT DEFAULT ''")
    # Migrate from any older schema if present (column set varies by version)
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='seen_listings'")
    if cur.fetchone():
        have = {c[1] for c in conn.execute("PRAGMA table_info(seen_listings)").fetchall()}
        def g(row, cols, name):
            return row[cols.index(name)] if name in cols else None
        cols = [c[1] for c in conn.execute("PRAGMA table_info(seen_listings)").fetchall()]
        for r in conn.execute(f"SELECT {','.join(cols)} FROM seen_listings").fetchall():
            conn.execute("""INSERT OR IGNORE INTO listings
                (id,source,area,title,price,url,first_seen,last_seen)
                VALUES (?,?,?,?,?,?,?,?)""",
                (g(r,cols,"id"), g(r,cols,"source"), g(r,cols,"area"),
                 g(r,cols,"title"), g(r,cols,"price"), g(r,cols,"url"),
                 g(r,cols,"first_seen"), g(r,cols,"first_seen")))
        conn.execute("DROP TABLE seen_listings")
        conn.commit()
    conn.commit()
    return conn

def upsert_listing(conn, l):
    """Insert new, or refresh mutable fields while preserving rating/checked.
       Returns True if this listing is brand new."""
    now = datetime.now().isoformat()
    row = conn.execute("SELECT first_seen FROM listings WHERE id=?", (l["id"],)).fetchone()
    if row:
        conn.execute("""UPDATE listings SET source=?,area=?,zone=?,title=?,price=?,url=?,
            lat=?,lon=?,dist_km=?,posted=?,seller=?,facilities=?,transit=?,last_seen=? WHERE id=?""",
            (l["_source"],l["_area_name"],l["_zone"],l["title"],l["price"],l["url"],
             l.get("lat"),l.get("lon"),l["_dist_km"],l.get("_posted",""),l.get("_seller",""),
             l.get("_facilities",""),l.get("_transit_info",""),now,l["id"]))
        return False
    conn.execute("""INSERT INTO listings
        (id,source,area,zone,title,price,url,lat,lon,dist_km,posted,seller,facilities,transit,
         rating,checked,first_seen,last_seen)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,'',0,?,?)""",
        (l["id"],l["_source"],l["_area_name"],l["_zone"],l["title"],l["price"],l["url"],
         l.get("lat"),l.get("lon"),l["_dist_km"],l.get("_posted",""),l.get("_seller",""),
         l.get("_facilities",""),l.get("_transit_info",""),now,now))
    return True

# Column order used everywhere we read a listing row back out of the DB
LISTING_COLS = ("id,source,area,zone,title,price,url,lat,lon,dist_km,"
                "posted,seller,facilities,transit,rating,checked,first_seen,last_seen,notes")

def row_to_listing(r):
    return {
        "id": r[0], "_source": r[1], "_area_name": r[2], "_zone": r[3],
        "title": r[4], "price": r[5], "url": r[6], "lat": r[7], "lon": r[8],
        "_dist_km": r[9] if r[9] is not None else 99.0, "_posted": r[10] or "",
        "_seller": r[11] or "", "_facilities": r[12] or "", "_transit_info": r[13] or "",
        "_rating": r[14] or "", "_checked": r[15] or 0,
        "_first_seen": r[16] or "", "_last_seen": r[17] or "",
        "_notes": (r[18] if len(r) > 18 else "") or "",
    }

def fetch_emailable(conn):
    """Unchecked and not-disliked listings, nearest-first. Includes ones from
       earlier runs that you have not acted on yet."""
    rows = conn.execute(
        f"SELECT {LISTING_COLS} FROM listings "
        "WHERE checked=0 AND rating!='dislike' ORDER BY dist_km").fetchall()
    return [row_to_listing(r) for r in rows]

# ════════════════════════════════════════════════════════════════════════════
#  SOURCE ADAPTERS — each returns a list of normalised listing dicts
# ════════════════════════════════════════════════════════════════════════════

def fetch_daft():
    """Daft.ie internal API — primary, reliable source."""
    out = []
    endpoint = "https://gateway.daft.ie/api/v2/ads/listings"
    headers = {"Content-Type":"application/json","brand":"daft","platform":"web",
               "User-Agent":UA,"Accept":"application/json, text/plain, */*",
               "Origin":"https://www.daft.ie","Referer":"https://www.daft.ie/"}
    for area_name, area_id in DAFT_AREAS.items():
        page_from = 0
        while True:
            payload = {
                "section":"sharing",
                "filters":[{"name":"suitableFor","values":[GENDER_FILTER]}] if GENDER_FILTER else [],
                "ranges":[],
                "geoFilter":{"storedShapeIds":[area_id],"geoSearchType":"STORED_SHAPES"},
                "sort":"publishDateDesc",
                "paging":{"from":page_from,"pageSize":20},
            }
            try:
                r = requests.post(endpoint, json=payload, headers=headers,
                                  impersonate="chrome110", timeout=20)
                if r.status_code != 200:
                    break
                data = r.json()
                batch = data.get("listings", [])
                for item in batch:
                    l = item["listing"]
                    coords = l.get("point",{}).get("coordinates",[None,None])
                    lon_c, lat_c = (coords[0], coords[1]) if coords and coords[0] else (None,None)
                    posted = ""
                    if l.get("publishDate"):
                        posted = datetime.fromtimestamp(l["publishDate"]/1000).strftime("%d %b %Y %H:%M")
                    out.append(make_listing(
                        "Daft", l["id"], l.get("title"), l.get("price"),
                        area_name, lat_c, lon_c,
                        f"https://www.daft.ie{l.get('seoFriendlyPath','')}",
                        posted, l.get("seller",{}).get("name","Private"),
                        ", ".join(f["name"] for f in l.get("facilities",[])[:5]),
                    ))
                total = data.get("paging",{}).get("totalResults",0)
                page_from += 20
                if page_from >= total:
                    break
                time.sleep(1.0)
            except Exception as e:
                log.warning(f"  [Daft] {area_name}: {e}")
                break
        time.sleep(1.2)
    return out


def fetch_myhome():
    """MyHome.ie — best-effort HTML scrape of the Dublin sharing section."""
    out = []
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        log.warning("  [MyHome] beautifulsoup4 not installed, skipping")
        return out
    headers = {"User-Agent":UA,"Accept":"text/html"}
    for page in range(1, 4):
        url = f"https://www.myhome.ie/rentals/dublin/property-to-rent?page={page}"
        try:
            r = requests.get(url, headers=headers, impersonate="chrome110", timeout=20)
            if r.status_code != 200:
                break
            soup = BeautifulSoup(r.text, "lxml")
            cards = soup.select("[class*=PropertyListingCard], [class*=property-card], article")
            if not cards:
                break
            for c in cards:
                link = c.find("a", href=True)
                if not link:
                    continue
                href = link["href"]
                lid_m = re.search(r"/(\d{6,8})", href)
                if not lid_m:
                    continue
                lid = lid_m.group(1)
                title = (c.get_text(" ", strip=True) or "")[:80]
                price_m = re.search(r"€[\d,]+", c.get_text())
                price = price_m.group() + " per month" if price_m else "?"
                area = classify_area(title) or classify_area(href)
                if not area:
                    continue
                full_url = href if href.startswith("http") else f"https://www.myhome.ie{href}"
                out.append(make_listing("MyHome", lid, title, price, area, None, None, full_url))
            time.sleep(1.5)
        except Exception as e:
            log.warning(f"  [MyHome] page {page}: {e}")
            break
    return out


def fetch_spareroom():
    """SpareRoom.ie — best-effort. Often needs an Irish IP to return results."""
    out = []
    headers = {"User-Agent":UA,"Accept":"text/html"}
    try:
        url = ("https://www.spareroom.co.uk/flatshare/?search_id=1&mode=list"
               "&offered=true&where=Dublin&max_per_month=" + str(MAX_PRICE))
        r = requests.get(url, headers=headers, impersonate="chrome110", timeout=30)
        if r.status_code != 200:
            log.warning(f"  [SpareRoom] HTTP {r.status_code}")
            return out
        # SpareRoom list items: links to flatshare_detail.pl?flatshare_id=NNN
        ids = re.findall(r"flatshare_detail\.pl\?flatshare_id=(\d+)", r.text)
        for lid in dict.fromkeys(ids):
            url_d = f"https://www.spareroom.co.uk/flatshare/flatshare_detail.pl?flatshare_id={lid}"
            # Area unknown without detail fetch; default to Dublin/Other
            out.append(make_listing("SpareRoom", lid, "SpareRoom listing (open to view area)",
                                    "?", None, None, None, url_d))
    except Exception as e:
        log.warning(f"  [SpareRoom] {e}")
    return out


def fetch_rent_ie():
    """Rent.ie — best-effort. Frequently Cloudflare-gated; may need browser session."""
    out = []
    headers = {"User-Agent":UA,"Accept":"text/html"}
    try:
        r = requests.get("https://www.rent.ie/houses-to-let/renting_dublin/sharing/",
                         headers=headers, impersonate="chrome110", timeout=20)
        if r.status_code != 200:
            log.warning(f"  [Rent.ie] HTTP {r.status_code} (likely Cloudflare)")
            return out
        ids = re.findall(r"/houses-to-let/[\w-]+/(\d{5,8})", r.text)
        for lid in dict.fromkeys(ids):
            out.append(make_listing("Rent.ie", lid, "Rent.ie listing",
                                    "?", None, None,
                                    f"https://www.rent.ie/searchProperty.php?id_property={lid}"))
    except Exception as e:
        log.warning(f"  [Rent.ie] {e}")
    return out


SOURCES = []
if ENABLE_DAFT:      SOURCES.append(("Daft.ie", fetch_daft))
if ENABLE_MYHOME:    SOURCES.append(("MyHome.ie", fetch_myhome))
if ENABLE_SPAREROOM: SOURCES.append(("SpareRoom.ie", fetch_spareroom))
if ENABLE_RENT_IE:   SOURCES.append(("Rent.ie", fetch_rent_ie))


def fetch_all_sources():
    all_listings = []
    for name, fn in SOURCES:
        log.info(f"Fetching {name}...")
        try:
            res = fn()
            log.info(f"  {name}: {len(res)} listings")
            all_listings.extend(res)
        except Exception as e:
            log.error(f"  {name} failed entirely: {e}")
    # Deduplicate (same source+id)
    seen, unique = set(), []
    for l in all_listings:
        if l["id"] not in seen:
            seen.add(l["id"]); unique.append(l)
    return unique


def filter_by_price(listings):
    keep = []
    for l in listings:
        p = parse_monthly_price(l["price"])
        if p == 0 or p <= MAX_PRICE:   # keep unknown-price (scraped) listings
            keep.append(l)
    return keep

# ── TRANSIT LOOKUP (unchanged from v2) ───────────────────────────────────────

def get_transit_info(lat, lon, max_walk_m=700):
    if not lat or not lon:
        return ""

    # A bus leg this short (in-vehicle minutes only, not counting the walk to
    # the stop) isn't worth waiting for in real life — the schedule data has
    # no notion of headway/wait time, so a "1 min bus ride" often means
    # standing at a stop for 5-10 min for a journey you could've just walked.
    TRIVIAL_BUS_MAX_MIN = 2
    WALK_SPEED_MPM  = 83    # ≈5 km/h
    CYCLE_SPEED_MPM = 250   # ≈15 km/h, typical city cycling pace
    ROUTE_INFLATION = 1.2   # straight-line undercounts real street distance

    def hav_m(a,b,c,d):
        R=6_371_000; p,q=math.radians(a),math.radians(c)
        x=(math.sin(math.radians(c-a)/2)**2+math.cos(p)*math.cos(q)*math.sin(math.radians(d-b)/2)**2)
        return R*2*math.atan2(math.sqrt(x),math.sqrt(1-x))
    def wmin(dist_m):
        # Walking time in minutes, always shown explicitly — even a very
        # short distance still rounds up to "1 min" rather than vanishing
        # to "0 min", which read as if no walk was needed at all.
        return max(1, round(dist_m / 83))
    def opt_row(walk_m, route, dest, total_min, badge="", best=False):
        dest = (dest or "UCD").replace(", Dublin 4", "").replace(", Co. Dublin", "")
        return (
            f'<div class="t-opt{" t-best" if best else ""}">'
              f'<span class="t-leg"><span class="t-ic">🚶</span>{walk_m} min</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-route">{route}</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-leg t-dest"><span class="t-ic">🏁</span>{dest}</span>'
              f'<span class="t-total{" t-best" if best else ""}">{badge}≈{total_min} min</span>'
            f'</div>'
        )

    # Cycling time only needs straight-line distance to UCD and a fixed
    # speed assumption — it doesn't depend on the GTFS database at all, so
    # it's always computable. Shown on every listing regardless of whether
    # transit data is available, since plenty of students would happily
    # cycle anywhere reasonably close to campus rather than wait for a bus.
    d_ucd_m   = hav_m(lat, lon, UCD_LAT, UCD_LON) * ROUTE_INFLATION
    cycle_min = max(1, round(d_ucd_m / CYCLE_SPEED_MPM))
    walk_all_min = max(1, round(d_ucd_m / WALK_SPEED_MPM))
    cycle_row = (
        '<div class="t-cyclerow">'
          f'<span class="t-ic">🚲</span>Cycle to UCD <b>{cycle_min} min</b>'
          f'<span class="t-cyclerow-sub"> · walk all the way {walk_all_min} min</span>'
        '</div>'
    )
    cycle_row_standalone = cycle_row.replace('class="t-cyclerow"', 'class="t-cyclerow t-cyclerow-solo"')
    def wrap(main_html):
        if not main_html:
            return f'<div class="t-block">{cycle_row_standalone}</div>'
        return f'<div class="t-block">{main_html}{cycle_row}</div>'

    if not os.path.exists(TRANSIT_DB):
        # No GTFS data built yet — still show what we can compute (cycling
        # and walking) rather than leaving the listing blank.
        return wrap("")

    conn = sqlite3.connect(TRANSIT_DB)
    try:
        dlat = max_walk_m/111_000
        dlon = max_walk_m/(111_000*math.cos(math.radians(lat)))
        raw = conn.execute("SELECT stop_id,stop_name,lat,lon FROM stops "
            "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
            (lat-dlat,lat+dlat,lon-dlon,lon+dlon)).fetchall()
        within = sorted([(s,n,round(hav_m(lat,lon,sl,so))) for s,n,sl,so in raw
                         if hav_m(lat,lon,sl,so)<=max_walk_m], key=lambda x:x[2])
        if not within:
            return wrap(f'<div class="t-muted">🚶 No bus stops within {max_walk_m}m</div>')
        seen_r, direct = set(), []
        for sid,sname,dist_m in within[:10]:
            w = wmin(dist_m)
            for rn,avg,un in conn.execute("SELECT route_name,MIN(avg_min) m,ucd_stop_name "
                "FROM transit_times WHERE stop_id=? GROUP BY route_name,ucd_stop_name ORDER BY m",(sid,)).fetchall():
                if rn not in seen_r:
                    seen_r.add(rn)
                    direct.append({"r":rn,"f":sname,"w":w,"b":round(avg),"t":w+round(avg),"u":un})
        if direct:
            # Drop any option whose actual bus-riding time is trivially
            # short — those aren't real transit options, just statistical
            # noise from a stop happening to be one hop from a UCD stop.
            substantial = [d for d in direct if d["b"] > TRIVIAL_BUS_MAX_MIN]
            if not substantial:
                return wrap('<div class="t-muted">🚌 Nearest bus only saves a minute or two '
                            '— probably not worth the wait</div>')
            substantial.sort(key=lambda x:x["t"])
            shown = substantial[:2]
            best_t = shown[0]["t"]
            rows = [
                opt_row(d["w"], d["r"], d["u"], d["t"],
                        badge="⚡ " if (d["t"]==best_t and len(shown)>1) else "",
                        best=(d["t"]==best_t and len(shown)>1))
                for d in shown
            ]
            return wrap("".join(rows))
        nearby_ids=tuple(s[0] for s in within[:12]); ph=",".join("?"*len(nearby_ids))
        nrts=tuple(r[0] for r in conn.execute(
            f"SELECT DISTINCT route_name FROM stop_routes WHERE stop_id IN ({ph})",nearby_ids).fetchall())
        nr=within[0]
        if not nrts:
            return wrap(f'<div class="t-muted">⚠️ No bus near {nr[1]} ({nr[2]}m)</div>')
        ph2=",".join("?"*len(nrts))
        tr=conn.execute(f"""SELECT sr1.route_name,s.stop_name,tt.route_name,MIN(tt.avg_min),tt.ucd_stop_name
            FROM stop_routes sr1 JOIN stop_routes sr2 ON sr1.stop_id=sr2.stop_id
            JOIN transit_times tt ON tt.route_name=sr2.route_name AND tt.stop_id=sr2.stop_id
            JOIN stops s ON s.stop_id=sr1.stop_id
            WHERE sr1.route_name IN ({ph2})
              AND sr2.route_name IN (SELECT DISTINCT route_name FROM transit_times)
            GROUP BY sr1.route_name,tt.route_name ORDER BY MIN(tt.avg_min) LIMIT 1""",nrts).fetchall()
        w = wmin(nr[2])
        rl = ", ".join(sorted(set(nrts))[:4])
        if not tr:
            return wrap(f'<div class="t-muted">⚠️ No direct route — walk {w} min to {nr[1]} '
                        f'for {rl}, but no onward connection to UCD found</div>')
        t = tr[0]
        dest = (t[4] or "UCD").replace(", Dublin 4", "").replace(", Co. Dublin", "")
        row = (
            f'<div class="t-opt t-transfer">'
              f'<span class="t-leg"><span class="t-ic">🚶</span>{w} min</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-route">{t[0]}</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-leg t-swap"><span class="t-ic">⇄</span>{t[1]}</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-route t-route-2">{t[2]}</span>'
              f'<span class="t-arrow">›</span>'
              f'<span class="t-leg t-dest"><span class="t-ic">🏁</span>{dest}</span>'
              f'<span class="t-total">≈{round(t[3])} min<span class="t-sub"> after transfer</span></span>'
            f'</div>'
        )
        return wrap(row)
    except sqlite3.OperationalError as e:
        # gtfs_transit.db exists but its schema doesn't match what we expect
        # (e.g. a build script that skipped the stop_routes table). Never let
        # a transit lookup take down the whole run — fall back to the
        # cycling/walking estimate, which doesn't depend on this table.
        log.warning(f"  Transit DB query failed ({e}) — gtfs_transit.db looks incomplete/outdated. "
                    f"Re-run gtfs_build_db.py to rebuild it. Showing cycling estimate only.")
        return wrap("")
    finally:
        conn.close()

# ── INTERACTIVE EMAIL ────────────────────────────────────────────────────────

SOURCE_COLORS = {"Daft":"#e4002b","MyHome":"#0b6efd","SpareRoom":"#00a991","Rent.ie":"#f59e0b"}

# Max listings shown PER ZONE in the email summary. Keeps the message well
# under Gmail's ~102 KB clip threshold. The dashboard always shows everything.
EMAIL_PER_ZONE_CAP = 8

def format_html_email(new_listings):
    """Gmail-safe compact summary.

    Gmail clips emails over ~102 KB and strips <style> blocks / :checked
    selectors / <script>, so interactive tabs cannot work in Gmail. This builds
    a lightweight, table-based digest using only inline styles: a zone overview,
    then a slim per-zone preview (price + area + link), capped per zone. The full
    interactive experience (rate / check / filter) lives in the dashboard.
    """
    zone_groups = defaultdict(list)
    for l in new_listings:
        zone_groups[l["_zone"]].append(l)
    for z in zone_groups:
        zone_groups[z].sort(key=lambda x: x["_dist_km"])

    zone_order = [z for z in ZONES if z in zone_groups] + (["Other"] if "Other" in zone_groups else [])
    total = len(new_listings)
    src_counts = defaultdict(int)
    for l in new_listings:
        src_counts[l["_source"]] += 1
    src_summary = " · ".join(f"{s}: {c}" for s, c in src_counts.items())
    now = datetime.now().strftime("%d %b %Y %H:%M")
    dash_url = f"http://localhost:{DASHBOARD_PORT}/"

    # ── Zone overview table (counts per zone) ──────────────────────────────────
    overview = ""
    for z in zone_order:
        cfg = ZONES.get(z, {"color": "#6b7280", "est": ""})
        color = cfg["color"]
        short = z.split("—")[0].strip() if "—" in z else z
        band = z.split("(")[-1].rstrip(")") if "(" in z else ""
        overview += (
            f'<tr>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef2f7">'
            f'<span style="display:inline-block;width:10px;height:10px;border-radius:50%;'
            f'background:{color};margin-right:8px"></span>'
            f'<b style="color:#0f172a">{short}</b> '
            f'<span style="color:#94a3b8;font-size:12px">{band}</span></td>'
            f'<td style="padding:7px 10px;border-bottom:1px solid #eef2f7;text-align:right;'
            f'font-weight:700;color:{color}">{len(zone_groups[z])}</td>'
            f'</tr>'
        )

    # ── Slim per-zone preview lists ────────────────────────────────────────────
    sections = ""
    for z in zone_order:
        cfg = ZONES.get(z, {"color": "#6b7280", "est": ""})
        color = cfg["color"]
        listings = zone_groups[z]
        shown = listings[:EMAIL_PER_ZONE_CAP]
        more = len(listings) - len(shown)

        rows = ""
        for l in shown:
            dist = f"{l['_dist_km']} km" if l["_dist_km"] < 90 else ""
            new_tag = ('<span style="color:#15803d;font-weight:700;font-size:11px">● NEW</span> '
                       if l.get("_is_new") else "")
            rmap = {"like": "👍", "neutral": "😐", "dislike": "👎"}
            rtag = rmap.get(l.get("_rating", ""), "")
            rtag = (f' <span style="font-size:12px">{rtag}</span>' if rtag else "")
            meta = " · ".join(x for x in [l["_area_name"], dist] if x)
            rows += (
                f'<tr>'
                f'<td style="padding:8px 10px;border-bottom:1px solid #f1f5f9;font-size:13px">'
                f'{new_tag}<a href="{l["url"]}" style="color:#1d4ed8;text-decoration:none;'
                f'font-weight:600">{l["title"][:70]}</a>{rtag}<br>'
                f'<span style="color:#16a34a;font-weight:700">{l["price"]}</span>'
                f'<span style="color:#64748b;font-size:12px"> · {meta}</span></td>'
                f'</tr>'
            )

        more_row = ""
        if more > 0:
            more_row = (
                f'<tr><td style="padding:8px 10px;font-size:12px">'
                f'<a href="{dash_url}" style="color:{color};text-decoration:none;font-weight:600">'
                f'+ {more} more in this zone — open dashboard →</a></td></tr>'
            )

        short = z.split("—")[0].strip() if "—" in z else z
        sections += (
            f'<tr><td style="padding:16px 0 0">'
            f'<div style="background:{color};color:#fff;font-weight:700;font-size:13px;'
            f'padding:8px 12px;border-radius:6px">{z} · {cfg.get("est","")}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="margin-top:6px;border-collapse:collapse">{rows}{more_row}</table>'
            f'</td></tr>'
        )

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:14px;background:#f1f5f9;
             font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Arial,sans-serif;color:#0f172a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:680px;margin:0 auto">
  <tr><td style="background:#0f172a;padding:18px 20px;border-radius:12px 12px 0 0">
    <div style="color:#fff;font-size:19px;font-weight:700">🏠 {total} new room{"s" if total!=1 else ""} — Dublin</div>
    <div style="color:#94a3b8;font-size:12px;margin-top:5px">
      {("Suitable for: "+GENDER_FILTER) if GENDER_FILTER else "All listings"} · ≤€{MAX_PRICE}/mo · {src_summary} · {now}
    </div>
  </td></tr>

  <tr><td style="background:#fff;padding:18px 20px">
    <!-- Primary call to action: open the interactive dashboard -->
    <a href="{dash_url}" style="display:block;background:#1d4ed8;color:#fff;text-align:center;
       padding:13px;border-radius:8px;text-decoration:none;font-weight:700;font-size:15px">
      Open the dashboard to rate, filter &amp; see all {total} →
    </a>
    <div style="text-align:center;color:#94a3b8;font-size:11px;margin-top:6px">
      Gmail blocks direct clicks on localhost links — right-click the button above → <b>Open in New Tab</b>, or type <b>localhost:{DASHBOARD_PORT}</b> in your browser.
    </div>

    <!-- Zone overview -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0"
           style="margin-top:18px;border-collapse:collapse;border:1px solid #eef2f7;border-radius:8px">
      <tr><td colspan="2" style="padding:8px 10px;background:#f8fafc;font-size:11px;
          font-weight:700;color:#64748b;text-transform:uppercase;letter-spacing:.05em">
          New listings by zone</td></tr>
      {overview}
    </table>

    <!-- Per-zone previews (capped) -->
    <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse">
      {sections}
    </table>
  </td></tr>

  <tr><td style="background:#f8fafc;padding:12px 20px;border-radius:0 0 12px 12px;
      font-size:11px;color:#94a3b8;border-top:1px solid #e2e8f0">
    Showing up to {EMAIL_PER_ZONE_CAP} nearest per zone · Sources: {", ".join(s for s,_ in SOURCES)} ·
    Transit &amp; full list in the dashboard · North Dublin excluded
  </td></tr>
</table>
</body></html>"""


def send_email(subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"], msg["From"], msg["To"] = subject, GMAIL_FROM, GMAIL_TO
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(GMAIL_FROM, GMAIL_APP_PW)
        s.sendmail(GMAIL_FROM, GMAIL_TO, msg.as_string())
    log.info(f"Email sent: {subject}")

# ── MAIN ──────────────────────────────────────────────────────────────────────

def run_check():
    log.info("="*60)
    log.info(f"Check at {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    conn = init_db()

    listings = fetch_all_sources()
    log.info(f"Total fetched (all sources): {len(listings)}")
    listings = filter_by_price(listings)
    log.info(f"After price filter (≤€{MAX_PRICE}): {len(listings)}")

    new_ids = set()
    for l in listings:
        # Only the brand-new ones need a (relatively costly) transit lookup
        is_new = conn.execute("SELECT 1 FROM listings WHERE id=?", (l["id"],)).fetchone() is None
        if is_new and l.get("lat") and l.get("lon"):
            l["_transit_info"] = get_transit_info(l["lat"], l["lon"])
        if upsert_listing(conn, l):
            new_ids.add(l["id"])
            log.info(f"  NEW [{l['_source']}/{l['_zone'][:6]}] {l['_area_name']} — {l['price']} — {l['title'][:45]}")
    conn.commit()
    log.info(f"New this run: {len(new_ids)}")

    # Email everything you haven't dealt with yet (unchecked & not disliked),
    # including recurring listings from earlier runs. Send only when something
    # brand new appeared, so the daemon doesn't email an identical digest hourly.
    emailable = fetch_emailable(conn)
    for l in emailable:
        l["_is_new"] = l["id"] in new_ids
    log.info(f"Unchecked & not-disliked total: {len(emailable)} "
             f"({len(new_ids)} new, {len(emailable)-len(new_ids)} recurring)")

    # Modified to ALWAYS send an email if there is ANYTHING unchecked, 
    # even if there are 0 brand-new listings this run.
    if emailable:
        n  = len(emailable)
        zc = len({l["_zone"] for l in emailable})
        sc = len({l["_source"] for l in emailable})
        subject = (f"[Housing Test] {len(new_ids)} new + {n-len(new_ids)} pending · "
                   f"{sc} source{'s' if sc!=1 else ''} · {zc} zone{'s' if zc!=1 else ''}")
        html = format_html_email(emailable)
        try:
            send_email(subject, html)
            log.info("Success! The email was sent to your inbox.")
        except Exception as e:
            log.error(f"Email failed: {e}")
            fb = os.path.expanduser("~/daft/alert_preview.html")
            with open(fb,"w") as f: f.write(html)
            log.info(f"Saved preview: {fb}")
    else:
        log.info("Nothing unchecked to email. You are all caught up!")
    conn.close()
    log.info(f"Dashboard: http://localhost:{DASHBOARD_PORT}")
    log.info("Done.\n")


# ════════════════════════════════════════════════════════════════════════════
#  LOCAL DASHBOARD — interactive, writes ratings/checked state back to the DB
# ════════════════════════════════════════════════════════════════════════════

def render_dashboard(rows, focus=""):
    listings = [row_to_listing(r) for r in rows]

    # Parse a clean numeric monthly price for sorting/stats
    def num_price(l):
        return parse_monthly_price(l.get("price", "")) or 0

    # Mark "new today" using first_seen date
    today = datetime.now().strftime("%Y-%m-%d")
    for l in listings:
        l["_num_price"] = num_price(l)
        l["_new_today"] = str(l.get("_first_seen", "")).startswith(today)

    # Group by zone, nearest-first within zone
    zg = defaultdict(list)
    for l in listings:
        zg[l["_zone"]].append(l)
    for z in zg:
        zg[z].sort(key=lambda x: (x["_checked"], x["_dist_km"]))

    zone_order = [z for z in ZONES if z in zg] + (["Other"] if "Other" in zg else [])

    # ── Stats ──────────────────────────────────────────────────────────────────
    total     = len(listings)
    unchecked = sum(1 for l in listings if not l["_checked"])
    liked     = sum(1 for l in listings if l["_rating"] == "like")
    new_today = sum(1 for l in listings if l["_new_today"])
    priced    = [l["_num_price"] for l in listings if l["_num_price"] > 0]
    avg_price = round(sum(priced) / len(priced)) if priced else 0

    # ── Zone filter chips ──────────────────────────────────────────────────────
    zone_chips = '<button class="zchip on" data-zone="all" onclick="setZone(this)">All zones</button>'
    for z in zone_order:
        cfg = ZONES.get(z, {"color": "#6b7280"})
        short = z.split("—")[0].strip() if "—" in z else z
        zid = re.sub(r'[^a-z0-9]', '', z.lower())
        zone_chips += (f'<button class="zchip" data-zone="{zid}" onclick="setZone(this)" '
                       f'style="--zc:{cfg["color"]}">{short} '
                       f'<span class="zc-count">{len(zg[z])}</span></button>')

    # ── Cards grouped by zone ──────────────────────────────────────────────────
    cards = ""
    for z in zone_order:
        cfg = ZONES.get(z, {"color": "#6b7280", "est": ""})
        color = cfg["color"]
        zid = re.sub(r'[^a-z0-9]', '', z.lower())
        cards += (f'<div class="zone-section" data-zonegroup="{zid}">'
                  f'<div class="zhdr" style="--zhc:{color}">'
                  f'<span class="zhdr-dot"></span>{z}'
                  f'<span class="zhdr-est">{cfg.get("est","")}</span>'
                  f'<span class="zhdr-count">{len(zg[z])}</span></div>')
        for l in zg[z]:
            scolor = SOURCE_COLORS.get(l["_source"], "#6b7280")
            dist = f"{l['_dist_km']} km" if l["_dist_km"] < 90 else ""
            maps = f"https://maps.google.com/?q={l['lat']},{l['lon']}" if l.get("lat") else ""
            rr = l["_rating"]; ck = l["_checked"]
            note = l.get("_notes", "")
            has_note = "1" if note.strip() else "0"
            focus_cls = " focus" if str(l["id"]) == str(focus) else ""
            checked_cls = " checked" if ck else ""
            # searchable text blob (lowercased) for the search box
            search_blob = f"{l['title']} {l['_area_name']} {l['price']} {l['_source']}".lower().replace('"', '')
            new_badge = '<span class="tag tag-new">NEW TODAY</span>' if l.get("_new_today") else ""
            import html as _html
            note_esc = _html.escape(note)
            cards += f"""
    <div class="card{focus_cls}{checked_cls}" id="{l['id']}" data-rating="{rr}" data-checked="{ck}"
         data-zone="{zid}" data-price="{l['_num_price']}" data-dist="{l['_dist_km']}"
         data-note="{has_note}" data-search="{search_blob}" data-first="{l.get('_first_seen','')}">
      <div class="card-head">
        <button class="check-btn" onclick="toggleCheck('{l['id']}')" title="Mark as checked / done">
          <span class="check-icon">{"✓" if ck else ""}</span>
        </button>
        <div class="card-headmain">
          <a href="{l['url']}" target="_blank" class="card-title">{l['title'][:100]}</a>
          <div class="card-tagrow">
            <span class="src-tag" style="--src:{scolor}">{l['_source']}</span>
            {new_badge}
          </div>
        </div>
      </div>
      <div class="card-meta">
        <span class="meta-price">{l['price']}</span>
        <span class="meta-item">📍 {l['_area_name']}{(" · "+dist) if dist else ""}</span>
        {"<span class='meta-item'>📅 "+l['_posted']+"</span>" if l.get('_posted') else ""}
        <span class="meta-item meta-id">#{str(l['id']).split(':')[-1]}</span>
      </div>
      {"<div class='card-transit'>"+l['_transit_info']+"</div>" if l.get('_transit_info') else ""}
      <div class="card-actions">
        <div class="rate-group">
          <button class="rate-btn like{' on' if rr=='like' else ''}" onclick="rate('{l['id']}','like')" title="Interested">👍</button>
          <button class="rate-btn neu{' on' if rr=='neutral' else ''}" onclick="rate('{l['id']}','neutral')" title="Maybe">😐</button>
          <button class="rate-btn dis{' on' if rr=='dislike' else ''}" onclick="rate('{l['id']}','dislike')" title="Not interested">👎</button>
        </div>
        <div class="link-group">
          <a href="{l['url']}" target="_blank" class="link-btn primary">View listing →</a>
          {"<a href='"+maps+"' target='_blank' class='link-btn'>📍 Map</a>" if maps else ""}
          <button class="link-btn note-toggle{' has-note' if note.strip() else ''}" onclick="toggleNote('{l['id']}')">📝 {'Note' if not note.strip() else 'Note •'}</button>
        </div>
      </div>
      <div class="note-area" id="note-{l['id']}" style="display:none">
        <textarea class="note-input" placeholder="Add a private note — landlord contact, viewing date, pros/cons…"
                  onchange="saveNote('{l['id']}', this.value)">{note_esc}</textarea>
      </div>
    </div>"""
        cards += '</div>'

    empty_state = ('<div class="empty"><div class="empty-icon">🏠</div>'
                   '<div class="empty-title">No listings yet</div>'
                   '<div class="empty-sub">Run a search to populate the dashboard.</div></div>')

    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dublin Room Radar</title>
<style>
  :root{{
    --bg:#f6f7f9; --surface:#ffffff; --border:#e6e8ec; --border2:#eef0f3;
    --ink:#0f172a; --ink2:#475569; --ink3:#94a3b8;
    --brand:#1d4ed8; --brand-dark:#1e40af;
    --green:#16a34a; --green-bg:#dcfce7; --green-ink:#15803d;
    --amber:#ca8a04; --amber-bg:#fef9c3; --amber-ink:#a16207;
    --red:#dc2626; --red-bg:#fee2e2; --red-ink:#b91c1c;
    --shadow:0 1px 2px rgba(16,24,40,.04),0 1px 3px rgba(16,24,40,.06);
    --shadow-lg:0 4px 12px rgba(16,24,40,.08);
    --radius:12px;
  }}
  *{{box-sizing:border-box;margin:0;padding:0}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Inter,Roboto,Arial,sans-serif;
        background:var(--bg);color:var(--ink);line-height:1.45;-webkit-font-smoothing:antialiased}}
  a{{color:inherit}}

  /* ── Header ── */
  .topbar{{background:linear-gradient(135deg,#0f172a 0%,#1e293b 100%);color:#fff;
           padding:20px 0 18px;position:sticky;top:0;z-index:30;box-shadow:var(--shadow-lg)}}
  .container{{max-width:880px;margin:0 auto;padding:0 18px}}
  .brand-row{{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}}
  .brand{{display:flex;align-items:center;gap:10px;font-size:19px;font-weight:800;letter-spacing:-.01em}}
  .brand .logo{{font-size:22px}}
  .live-dot{{display:inline-flex;align-items:center;gap:6px;font-size:12px;font-weight:600;
             color:#86efac;background:rgba(34,197,94,.12);padding:4px 10px;border-radius:20px}}
  .live-dot::before{{content:"";width:7px;height:7px;border-radius:50%;background:#22c55e;
                     box-shadow:0 0 0 0 rgba(34,197,94,.7);animation:pulse 2s infinite}}
  @keyframes pulse{{0%{{box-shadow:0 0 0 0 rgba(34,197,94,.6)}}70%{{box-shadow:0 0 0 6px rgba(34,197,94,0)}}100%{{box-shadow:0 0 0 0 rgba(34,197,94,0)}}}}

  /* ── Stat cards ── */
  .stats{{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin-top:16px}}
  .stat{{background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.1);
         border-radius:10px;padding:11px 13px}}
  .stat-num{{font-size:22px;font-weight:800;letter-spacing:-.02em;line-height:1}}
  .stat-lbl{{font-size:11px;color:#cbd5e1;margin-top:4px;font-weight:500}}
  .stat.accent .stat-num{{color:#86efac}}

  /* ── Controls bar ── */
  .controls{{background:var(--surface);border-bottom:1px solid var(--border);
             position:sticky;top:0;z-index:20;padding:12px 0}}
  .controls-inner{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}
  .search-wrap{{position:relative;flex:1;min-width:200px}}
  .search-wrap svg{{position:absolute;left:11px;top:50%;transform:translateY(-50%);width:16px;height:16px;fill:var(--ink3)}}
  .search{{width:100%;border:1px solid var(--border);border-radius:9px;padding:9px 12px 9px 34px;
           font-size:14px;color:var(--ink);background:#fbfcfd;outline:none;transition:.15s}}
  .search:focus{{border-color:var(--brand);background:#fff;box-shadow:0 0 0 3px rgba(29,78,216,.1)}}
  .sort-sel{{border:1px solid var(--border);border-radius:9px;padding:9px 30px 9px 12px;font-size:13px;
             color:var(--ink2);background:#fbfcfd url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 24 24' fill='%2394a3b8'%3E%3Cpath d='M7 10l5 5 5-5z'/%3E%3C/svg%3E") no-repeat right 9px center;
             cursor:pointer;outline:none;-webkit-appearance:none;appearance:none;font-weight:500}}

  /* ── Status filter pills ── */
  .pills{{display:flex;gap:7px;flex-wrap:wrap;padding:12px 0 0}}
  .pill{{border:1px solid var(--border);background:var(--surface);padding:7px 13px;border-radius:20px;
         font-size:13px;cursor:pointer;color:var(--ink2);font-weight:600;transition:.15s;display:inline-flex;align-items:center;gap:6px}}
  .pill:hover{{border-color:#cbd5e1;background:#fafbfc}}
  .pill.on{{background:var(--ink);color:#fff;border-color:var(--ink)}}
  .pill .pc{{background:rgba(0,0,0,.08);border-radius:10px;padding:1px 7px;font-size:11px;font-weight:700}}
  .pill.on .pc{{background:rgba(255,255,255,.2)}}

  /* ── Zone chips ── */
  .zchips{{display:flex;gap:7px;flex-wrap:wrap;padding:10px 0 2px}}
  .zchip{{border:1px solid var(--border);background:var(--surface);padding:5px 11px;border-radius:8px;
          font-size:12px;cursor:pointer;color:var(--ink2);font-weight:600;transition:.15s;
          border-left:3px solid var(--zc,#cbd5e1)}}
  .zchip:hover{{background:#fafbfc}}
  .zchip.on{{background:var(--zc,var(--ink));color:#fff;border-color:var(--zc,var(--ink))}}
  .zchip[data-zone=all]{{border-left-color:#94a3b8}}
  .zchip[data-zone=all].on{{background:var(--ink);border-color:var(--ink)}}
  .zc-count{{opacity:.7;font-weight:700;margin-left:3px}}

  /* ── Body ── */
  .body{{padding:18px 0 60px}}
  .zone-section{{margin-bottom:8px}}
  .zhdr{{display:flex;align-items:center;gap:9px;font-size:13px;font-weight:700;color:#fff;
         padding:9px 14px;border-radius:9px;margin:18px 0 10px;background:var(--zhc,#6b7280)}}
  .zhdr-dot{{width:8px;height:8px;border-radius:50%;background:rgba(255,255,255,.6)}}
  .zhdr-est{{font-weight:500;font-size:12px;opacity:.85}}
  .zhdr-count{{margin-left:auto;background:rgba(255,255,255,.22);border-radius:11px;padding:2px 9px;font-size:12px}}

  /* ── Card ── */
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:var(--radius);
         padding:14px 16px;margin-bottom:11px;box-shadow:var(--shadow);transition:.18s}}
  .card:hover{{box-shadow:var(--shadow-lg);border-color:#dbe0e6}}
  .card.checked{{opacity:.6;background:#fafbfc}}
  .card.focus{{box-shadow:0 0 0 3px #fbbf24,var(--shadow-lg)}}
  .card-head{{display:flex;align-items:flex-start;gap:11px}}
  .check-btn{{flex:none;width:24px;height:24px;border:2px solid #cbd5e1;border-radius:7px;background:#fff;
              cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s;margin-top:1px}}
  .check-btn:hover{{border-color:var(--green)}}
  .card.checked .check-btn{{background:var(--green);border-color:var(--green)}}
  .check-icon{{color:#fff;font-size:14px;font-weight:800;line-height:1}}
  .card-headmain{{flex:1;min-width:0}}
  .card-title{{font-size:15px;font-weight:700;color:var(--ink);text-decoration:none;display:block;
               letter-spacing:-.01em}}
  .card-title:hover{{color:var(--brand)}}
  .card-tagrow{{display:flex;gap:6px;align-items:center;margin-top:5px;flex-wrap:wrap}}
  .src-tag{{font-size:10px;font-weight:800;color:#fff;background:var(--src,#6b7280);padding:2px 8px;
            border-radius:5px;letter-spacing:.02em}}
  .tag{{font-size:10px;font-weight:800;padding:2px 8px;border-radius:5px;letter-spacing:.02em}}
  .tag-new{{background:var(--green-bg);color:var(--green-ink)}}
  .card-meta{{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:10px 0 0 35px;
              font-size:12.5px;color:var(--ink2)}}
  .meta-price{{font-weight:800;color:var(--green);font-size:15px;letter-spacing:-.01em}}
  .meta-item{{color:var(--ink2)}}
  .meta-id{{color:var(--ink3);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}}
  /* ── Transit journey strip ── */
  .card-transit{{margin:9px 0 0 35px}}
  .t-block{{display:flex;flex-direction:column;gap:5px}}
  .t-opt{{display:flex;align-items:center;flex-wrap:wrap;gap:0;background:#f8fafc;
          border:1px solid var(--border2);border-radius:8px;padding:7px 10px;font-size:12px}}
  .t-opt.t-best{{background:#f0fdf4;border-color:#bbf7d0}}
  .t-opt.t-transfer{{background:#fffbeb;border-color:#fde68a}}
  .t-leg{{display:inline-flex;align-items:center;gap:4px;color:var(--ink2);white-space:nowrap;font-weight:500}}
  .t-ic{{font-size:12px;opacity:.85}}
  .t-arrow{{color:var(--ink3);margin:0 7px;font-size:13px;font-weight:700}}
  .t-route{{display:inline-flex;align-items:center;font-size:11.5px;font-weight:800;color:#fff;
            background:var(--brand);padding:2px 9px;border-radius:5px;letter-spacing:.01em;white-space:nowrap}}
  .t-route-2{{background:#7c3aed}}
  .t-dest{{color:var(--ink2)}}
  .t-swap{{color:var(--amber-ink);font-weight:700}}
  .t-total{{margin-left:auto;font-size:11.5px;font-weight:800;color:var(--ink);background:#fff;
            border:1px solid var(--border);padding:3px 10px;border-radius:20px;white-space:nowrap}}
  .t-total.t-best{{color:var(--green-ink);border-color:#bbf7d0}}
  .t-sub{{font-weight:500;color:var(--ink3);margin-left:2px}}
  .t-muted{{font-size:12px;color:var(--ink3);background:#f8fafc;border-radius:8px;padding:7px 10px;
            border:1px solid var(--border2)}}
  .t-cyclerow{{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--ink2);
              padding:6px 10px 2px;margin-top:3px;border-top:1px dashed var(--border2)}}
  .t-cyclerow.t-cyclerow-solo{{border-top:none;padding:7px 10px;margin-top:0;
              background:#f8fafc;border-radius:8px;border:1px solid var(--border2)}}
  .t-cyclerow b{{color:var(--ink);font-weight:800}}
  .t-cyclerow-sub{{color:var(--ink3);font-weight:500}}
  .card-meta{{display:flex;flex-wrap:wrap;gap:9px;align-items:center;margin:10px 0 0 35px;
              font-size:12.5px;color:var(--ink2)}}
  .meta-price{{font-weight:800;color:var(--green);font-size:15px;letter-spacing:-.01em}}
  .meta-item{{color:var(--ink2)}}
  .meta-id{{color:var(--ink3);font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:11px}}
  .card-actions{{display:flex;align-items:center;gap:10px;margin:12px 0 0 35px;flex-wrap:wrap;
                 justify-content:space-between}}
  .rate-group{{display:flex;gap:5px;background:#f1f5f9;padding:3px;border-radius:9px}}
  .rate-btn{{border:none;background:transparent;width:34px;height:30px;border-radius:7px;cursor:pointer;
             font-size:15px;transition:.12s;opacity:.5;filter:grayscale(.3)}}
  .rate-btn:hover{{opacity:.9;background:rgba(0,0,0,.04)}}
  .rate-btn.on{{opacity:1;filter:none;background:#fff;box-shadow:var(--shadow)}}
  .rate-btn.like.on{{background:var(--green-bg)}}
  .rate-btn.neu.on{{background:var(--amber-bg)}}
  .rate-btn.dis.on{{background:var(--red-bg)}}
  .link-group{{display:flex;gap:6px;flex-wrap:wrap}}
  .link-btn{{border:1px solid var(--border);background:#fff;color:var(--ink2);padding:6px 12px;
             border-radius:8px;font-size:12.5px;cursor:pointer;text-decoration:none;font-weight:600;
             transition:.15s;display:inline-flex;align-items:center;gap:4px}}
  .link-btn:hover{{background:#fafbfc;border-color:#cbd5e1}}
  .link-btn.primary{{background:var(--brand);color:#fff;border-color:var(--brand)}}
  .link-btn.primary:hover{{background:var(--brand-dark)}}
  .link-btn.has-note{{border-color:var(--amber);color:var(--amber-ink);background:var(--amber-bg)}}
  .note-area{{margin:11px 0 0 35px}}
  .note-input{{width:100%;border:1px solid var(--border);border-radius:8px;padding:9px 11px;
               font-size:13px;font-family:inherit;color:var(--ink);resize:vertical;min-height:64px;outline:none;background:#fffdf7}}
  .note-input:focus{{border-color:var(--amber);box-shadow:0 0 0 3px rgba(202,138,4,.1)}}

  /* ── Empty / no-results ── */
  .empty,.noresults{{text-align:center;padding:60px 20px;color:var(--ink3)}}
  .empty-icon{{font-size:40px;margin-bottom:10px}}
  .empty-title{{font-size:17px;font-weight:700;color:var(--ink2)}}
  .empty-sub{{font-size:13px;margin-top:4px}}
  .noresults{{display:none}}

  /* ── Toast ── */
  .toast{{position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(8px);
          background:var(--ink);color:#fff;padding:11px 20px;border-radius:10px;font-size:13.5px;
          font-weight:600;opacity:0;transition:.22s;pointer-events:none;box-shadow:var(--shadow-lg);z-index:50}}
  .toast.show{{opacity:1;transform:translateX(-50%) translateY(0)}}

  @media (max-width:560px){{
    .stats{{grid-template-columns:repeat(2,1fr)}}
    .card-meta,.card-actions,.card-transit,.note-area{{margin-left:0}}
    .card-actions{{flex-direction:column;align-items:stretch}}
    .link-group{{justify-content:space-between}}
  }}
</style></head><body>

<div class="topbar">
  <div class="container">
    <div class="brand-row">
      <div class="brand"><span class="logo">🏠</span> Dublin Room Radar</div>
      <span class="live-dot">Live</span>
    </div>
    <div class="stats">
      <div class="stat accent"><div class="stat-num">{new_today}</div><div class="stat-lbl">New today</div></div>
      <div class="stat"><div class="stat-num">{unchecked}</div><div class="stat-lbl">To review</div></div>
      <div class="stat"><div class="stat-num">{liked}</div><div class="stat-lbl">👍 Liked</div></div>
      <div class="stat"><div class="stat-num">{("€"+str(avg_price)) if avg_price else "—"}</div><div class="stat-lbl">Avg / month</div></div>
    </div>
  </div>
</div>

<div class="controls">
  <div class="container">
    <div class="controls-inner">
      <div class="search-wrap">
        <svg viewBox="0 0 24 24"><path d="M15.5 14h-.79l-.28-.27a6.5 6.5 0 1 0-.7.7l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0A4.5 4.5 0 1 1 14 9.5 4.49 4.49 0 0 1 9.5 14z"/></svg>
        <input class="search" id="search" type="text" placeholder="Search area, price, title…" oninput="applyAll()">
      </div>
      <select class="sort-sel" id="sort" onchange="applyAll()">
        <option value="dist">Nearest to UCD</option>
        <option value="price-asc">Price: low → high</option>
        <option value="price-desc">Price: high → low</option>
        <option value="newest">Newest first</option>
      </select>
    </div>
    <div class="pills">
      <button class="pill on" data-filter="unchecked" onclick="setStatus(this)">To review <span class="pc">{unchecked}</span></button>
      <button class="pill" data-filter="all" onclick="setStatus(this)">All <span class="pc">{total}</span></button>
      <button class="pill" data-filter="like" onclick="setStatus(this)">👍 Liked <span class="pc">{liked}</span></button>
      <button class="pill" data-filter="note" onclick="setStatus(this)">📝 Noted</button>
      <button class="pill" data-filter="checked" onclick="setStatus(this)">✓ Done</button>
    </div>
    <div class="zchips">{zone_chips}</div>
  </div>
</div>

<div class="body">
  <div class="container">
    {cards if cards else empty_state}
    <div class="noresults" id="noresults"><div class="empty-icon">🔍</div>
      <div class="empty-title">No matches</div>
      <div class="empty-sub">Try a different search or filter.</div></div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
  const $=s=>document.querySelector(s), $$=s=>[...document.querySelectorAll(s)];
  let curStatus='unchecked', curZone='all';

  function toast(m){{const t=$('#toast');t.textContent=m;t.classList.add('show');
    clearTimeout(window._tt);window._tt=setTimeout(()=>t.classList.remove('show'),1500);}}

  function setStatus(btn){{$$('.pill').forEach(b=>b.classList.remove('on'));btn.classList.add('on');
    curStatus=btn.dataset.filter;applyAll();}}
  function setZone(btn){{$$('.zchip').forEach(b=>b.classList.remove('on'));btn.classList.add('on');
    curZone=btn.dataset.zone;applyAll();}}

  function updateCounts(){{
    let uc=0,lk=0;
    $$('.card').forEach(c=>{{
      if(c.dataset.checked!=='1'&&c.dataset.rating!=='dislike')uc++;
      if(c.dataset.rating==='like')lk++;
    }});
    const pills=$$('.pill .pc');
    if(pills[0])pills[0].textContent=uc;
    if(pills[1])pills[1].textContent=$$('.card').length;
    if(pills[2])pills[2].textContent=lk;
    // header stats
    const s=$$('.stat-num');
    if(s[1])s[1].textContent=uc;
    if(s[2])s[2].textContent=lk;
  }}

  async function rate(id,r){{
    const card=document.getElementById(id),cur=card.dataset.rating,val=(cur===r)?'':r;
    await fetch('/api/rate',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id,rating:val}})}});
    card.dataset.rating=val;
    card.querySelectorAll('.rate-btn').forEach(b=>b.classList.remove('on'));
    if(val)card.querySelector('.rate-btn.'+({{like:'like',neutral:'neu',dislike:'dis'}}[val])).classList.add('on');
    toast(val?({{like:'👍 Liked',neutral:'😐 Marked maybe',dislike:'👎 Not interested'}}[val]):'Rating cleared');
    updateCounts();applyAll();
  }}

  async function toggleCheck(id){{
    const card=document.getElementById(id),now=card.dataset.checked==='1'?0:1;
    await fetch('/api/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id,checked:now}})}});
    card.dataset.checked=String(now);card.classList.toggle('checked',now===1);
    card.querySelector('.check-icon').textContent=now?'✓':'';
    toast(now?'✓ Marked as done':'Moved back to review');
    updateCounts();applyAll();
  }}

  function toggleNote(id){{
    const a=document.getElementById('note-'+id);
    const open=a.style.display!=='none';
    a.style.display=open?'none':'block';
    if(!open)a.querySelector('textarea').focus();
  }}
  async function saveNote(id,val){{
    await fetch('/api/note',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{id,note:val}})}});
    const card=document.getElementById(id);
    const hasNote=val.trim()?'1':'0';
    card.dataset.note=hasNote;
    const btn=card.querySelector('.note-toggle');
    btn.classList.toggle('has-note',!!val.trim());
    btn.innerHTML='📝 '+(val.trim()?'Note •':'Note');
    toast(val.trim()?'📝 Note saved':'Note cleared');
  }}

  function applyAll(){{
    const q=($('#search').value||'').toLowerCase().trim();
    const sort=$('#sort').value;
    let visible=0;

    $$('.card').forEach(c=>{{
      const ck=c.dataset.checked==='1', rt=c.dataset.rating, nt=c.dataset.note==='1';
      let show=true;
      if(curStatus==='unchecked')show=!ck&&rt!=='dislike';
      else if(curStatus==='checked')show=ck;
      else if(curStatus==='like')show=rt==='like';
      else if(curStatus==='note')show=nt;
      if(show&&curZone!=='all')show=c.dataset.zone===curZone;
      if(show&&q)show=(c.dataset.search||'').includes(q);
      c.style.display=show?'':'none';
      if(show)visible++;
    }});

    // Sort within each zone section
    $$('.zone-section').forEach(sec=>{{
      const cards=[...sec.querySelectorAll('.card')];
      cards.sort((a,b)=>{{
        if(sort==='price-asc')return (+a.dataset.price||1e9)-(+b.dataset.price||1e9);
        if(sort==='price-desc')return (+b.dataset.price||0)-(+a.dataset.price||0);
        if(sort==='newest')return (b.dataset.first||'').localeCompare(a.dataset.first||'');
        return (+a.dataset.dist||99)-(+b.dataset.dist||99); // dist default
      }});
      cards.forEach(c=>sec.appendChild(c));
    }});

    // Hide empty zone sections + their headers
    $$('.zone-section').forEach(sec=>{{
      const anyVisible=[...sec.querySelectorAll('.card')].some(c=>c.style.display!=='none');
      sec.style.display=anyVisible?'':'none';
    }});

    $('#noresults').style.display=visible?'none':'block';
  }}

  applyAll();
  setTimeout(()=>location.reload(),5*60*1000);
  const f=new URLSearchParams(location.search).get('focus');
  if(f){{const el=document.getElementById(f);if(el)el.scrollIntoView({{behavior:'smooth',block:'center'}});}}
</script>
</body></html>"""


def run_dashboard():
    try:
        from flask import Flask, request, jsonify, redirect
    except ImportError:
        log.error("Flask not installed. Run: pip install flask")
        return
    app = Flask(__name__)
    log_w = logging.getLogger("werkzeug"); log_w.setLevel(logging.WARNING)

    @app.route("/")
    def index():
        focus = request.args.get("focus", "")
        conn = sqlite3.connect(DB_PATH)
        rows = conn.execute(
            f"SELECT {LISTING_COLS} FROM listings ORDER BY checked, dist_km").fetchall()
        conn.close()
        return render_dashboard(rows, focus)

    @app.route("/api/rate", methods=["POST"])
    def api_rate():
        d = request.get_json(force=True)
        if d.get("rating") not in ("", "like", "neutral", "dislike"):
            return jsonify(ok=False), 400
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE listings SET rating=? WHERE id=?", (d["rating"], d["id"]))
        conn.commit(); conn.close()
        return jsonify(ok=True)

    @app.route("/api/check", methods=["POST"])
    def api_check():
        d = request.get_json(force=True)
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE listings SET checked=? WHERE id=?",
                     (1 if d.get("checked") else 0, d["id"]))
        conn.commit(); conn.close()
        return jsonify(ok=True)

    @app.route("/api/note", methods=["POST"])
    def api_note():
        d = request.get_json(force=True)
        note = (d.get("note") or "")[:2000]      # cap length
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE listings SET notes=? WHERE id=?", (note, d["id"]))
        conn.commit(); conn.close()
        return jsonify(ok=True)

    log.info(f"Dashboard serving at http://localhost:{DASHBOARD_PORT}")
    app.run(host="127.0.0.1", port=DASHBOARD_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--once",      action="store_true", help="Run one fetch + email, then exit")
    ap.add_argument("--daemon",    action="store_true", help="Poll on a schedule AND serve the dashboard")
    ap.add_argument("--dashboard", action="store_true", help="Serve only the dashboard (no fetching)")
    ap.add_argument("--clear",     action="store_true", help="Wipe the database (fresh start)")
    args = ap.parse_args()

    if args.clear and os.path.exists(DB_PATH):
        os.remove(DB_PATH); print(f"Cleared {DB_PATH}")

    if args.dashboard:
        init_db()
        run_dashboard()                       # blocking
    elif args.daemon:
        import schedule, threading
        init_db()
        threading.Thread(target=run_dashboard, daemon=True).start()
        log.info(f"Dashboard live at http://localhost:{DASHBOARD_PORT}")
        log.info(f"Daemon: polling every {POLL_INTERVAL} min")
        run_check()
        schedule.every(POLL_INTERVAL).minutes.do(run_check)
        while True:
            schedule.run_pending(); time.sleep(30)
    else:
        run_check()
