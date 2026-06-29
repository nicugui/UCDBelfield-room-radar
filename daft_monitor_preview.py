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

GMAIL_FROM    = "your@gmail.com"       # ← your Gmail address
GMAIL_TO      = "your@gmail.com"       # ← destination (can be same)
GMAIL_APP_PW  = "xxxx xxxx xxxx xxxx"        # ← 16-char Google App Password

MAX_PRICE      = 1250                   # ← target monthly max rent
GENDER_FILTER  = "YOUR GENDER"          # "male", "female", or "" for all listings
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
        rating TEXT DEFAULT '', checked INTEGER DEFAULT 0,
        first_seen TEXT, last_seen TEXT)""")
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
                "posted,seller,facilities,transit,rating,checked,first_seen,last_seen")

def row_to_listing(r):
    return {
        "id": r[0], "_source": r[1], "_area_name": r[2], "_zone": r[3],
        "title": r[4], "price": r[5], "url": r[6], "lat": r[7], "lon": r[8],
        "_dist_km": r[9] if r[9] is not None else 99.0, "_posted": r[10] or "",
        "_seller": r[11] or "", "_facilities": r[12] or "", "_transit_info": r[13] or "",
        "_rating": r[14] or "", "_checked": r[15] or 0,
        "_first_seen": r[16] or "", "_last_seen": r[17] or "",
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
    if not os.path.exists(TRANSIT_DB):
        return ""
    def hav_m(a,b,c,d):
        R=6_371_000; p,q=math.radians(a),math.radians(c)
        x=(math.sin(math.radians(c-a)/2)**2+math.cos(p)*math.cos(q)*math.sin(math.radians(d-b)/2)**2)
        return R*2*math.atan2(math.sqrt(x),math.sqrt(1-x))
    conn = sqlite3.connect(TRANSIT_DB)
    dlat = max_walk_m/111_000
    dlon = max_walk_m/(111_000*math.cos(math.radians(lat)))
    raw = conn.execute("SELECT stop_id,stop_name,lat,lon FROM stops "
        "WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?",
        (lat-dlat,lat+dlat,lon-dlon,lon+dlon)).fetchall()
    within = sorted([(s,n,round(hav_m(lat,lon,sl,so))) for s,n,sl,so in raw
                     if hav_m(lat,lon,sl,so)<=max_walk_m], key=lambda x:x[2])
    if not within:
        conn.close(); return f"No stops within {max_walk_m}m"
    seen_r, direct = set(), []
    for sid,sname,dist_m in within[:10]:
        walk = dist_m/83
        for rn,avg,un in conn.execute("SELECT route_name,MIN(avg_min) m,ucd_stop_name "
            "FROM transit_times WHERE stop_id=? GROUP BY route_name,ucd_stop_name ORDER BY m",(sid,)).fetchall():
            if rn not in seen_r:
                seen_r.add(rn)
                us=(un or "UCD").replace(", Dublin 4","").replace(", Co. Dublin","")
                direct.append({"r":rn,"f":sname,"w":round(walk),"b":round(avg),"t":round(walk+avg),"u":us})
    if direct:
        direct.sort(key=lambda x:x["t"])
        parts=[f"✅ <b>{d['r']}</b> walk {d['w']}min → {d['f']} → {d['b']}min → {d['u']} (≈{d['t']}min)"
               for d in direct[:2]]
        conn.close(); return "  |  ".join(parts)
    nearby_ids=tuple(s[0] for s in within[:12]); ph=",".join("?"*len(nearby_ids))
    nrts=tuple(r[0] for r in conn.execute(
        f"SELECT DISTINCT route_name FROM stop_routes WHERE stop_id IN ({ph})",nearby_ids).fetchall())
    if not nrts:
        conn.close(); return f"⚠️ No bus nearby ({within[0][1]}, {within[0][2]}m)"
    ph2=",".join("?"*len(nrts))
    tr=conn.execute(f"""SELECT sr1.route_name,s.stop_name,tt.route_name,MIN(tt.avg_min),tt.ucd_stop_name
        FROM stop_routes sr1 JOIN stop_routes sr2 ON sr1.stop_id=sr2.stop_id
        JOIN transit_times tt ON tt.route_name=sr2.route_name AND tt.stop_id=sr2.stop_id
        JOIN stops s ON s.stop_id=sr1.stop_id
        WHERE sr1.route_name IN ({ph2})
          AND sr2.route_name IN (SELECT DISTINCT route_name FROM transit_times)
        GROUP BY sr1.route_name,tt.route_name ORDER BY MIN(tt.avg_min) LIMIT 2""",nrts).fetchall()
    conn.close()
    nr=within[0]; rl=", ".join(sorted(set(nrts))[:4])
    head=f"⚠️ Transfer — {nr[1]} ({nr[2]}m): <b>{rl}</b>"
    if tr:
        t=tr[0]; us=(t[4] or "UCD").replace(", Dublin 4","").replace(", Co. Dublin","")
        return head+f"  |  → {t[0]} → {t[1]} → transfer <b>{t[2]}</b> → {us} (≈{round(t[3])}min)"
    return head

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
      Dashboard runs locally while the monitor is on ({dash_url})
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
    # Group by zone, nearest-first within zone
    zg = defaultdict(list)
    for l in listings:
        zg[l["_zone"]].append(l)
    for z in zg:
        zg[z].sort(key=lambda x: (x["_checked"], x["_dist_km"]))

    zone_order = [z for z in ZONES if z in zg] + (["Other"] if "Other" in zg else [])
    total     = len(listings)
    unchecked = sum(1 for l in listings if not l["_checked"])
    liked     = sum(1 for l in listings if l["_rating"] == "like")

    cards = ""
    for z in zone_order:
        cfg = ZONES.get(z, {"color":"#6b7280","est":""})
        color = cfg["color"]
        cards += f'<div class="zhdr" style="background:{color}">{z} · {cfg.get("est","")} · {len(zg[z])}</div>'
        for l in zg[z]:
            scolor = SOURCE_COLORS.get(l["_source"], "#6b7280")
            dist = f"{l['_dist_km']} km" if l["_dist_km"] < 90 else ""
            maps = f"https://maps.google.com/?q={l['lat']},{l['lon']}" if l.get("lat") else ""
            rr = l["_rating"]; ck = l["_checked"]
            focus_cls = " focus" if str(l["id"]) == str(focus) else ""
            checked_cls = " checked" if ck else ""
            cards += f"""
    <div class="card{focus_cls}{checked_cls}" id="{l['id']}" data-rating="{rr}" data-checked="{ck}"
         data-zone="{re.sub(r'[^a-z0-9]','',l['_zone'].lower())}">
      <div class="ctop">
        <span class="check" onclick="toggleCheck('{l['id']}')" title="Mark checked">{"✓" if ck else "○"}</span>
        <a href="{l['url']}" target="_blank" class="ttl">{l['title'][:95]}</a>
        <span class="src" style="background:{scolor}">{l['_source']}</span>
      </div>
      <div class="meta">
        <span class="price">{l['price']}</span>
        <span>📍 {l['_area_name']}{(" · "+dist) if dist else ""}</span>
        {"<span>📅 "+l['_posted']+"</span>" if l.get('_posted') else ""}
        {"<span>🆔 "+str(l['id']).split(':')[-1]+"</span>"}
      </div>
      {"<div class='transit'>🚌 "+l['_transit_info']+"</div>" if l.get('_transit_info') else ""}
      <div class="rate">
        <button class="rb like{' on' if rr=='like' else ''}"    onclick="rate('{l['id']}','like')">👍 Like</button>
        <button class="rb neu{' on' if rr=='neutral' else ''}"  onclick="rate('{l['id']}','neutral')">😐 Neutral</button>
        <button class="rb dis{' on' if rr=='dislike' else ''}"  onclick="rate('{l['id']}','dislike')">👎 Dislike</button>
        <a href="{l['url']}" target="_blank" class="rb view">View →</a>
        {"<a href='"+maps+"' target='_blank' class='rb view'>📍 Map</a>" if maps else ""}
      </div>
    </div>"""

    tabs = (f'<button class="ft on" onclick="filt(this,\'unchecked\')">Unchecked <b>{unchecked}</b></button>'
            f'<button class="ft" onclick="filt(this,\'all\')">All <b>{total}</b></button>'
            f'<button class="ft" onclick="filt(this,\'like\')">👍 Liked <b>{liked}</b></button>'
            f'<button class="ft" onclick="filt(this,\'checked\')">✓ Checked</button>')

    return f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Dublin Housing Dashboard</title>
<style>
  *{{box-sizing:border-box}}
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;margin:0;color:#0f172a}}
  .wrap{{max-width:780px;margin:0 auto;padding:16px}}
  h1{{font-size:20px;margin:0 0 2px}}
  .sub{{color:#64748b;font-size:13px;margin:0 0 14px}}
  .filters{{position:sticky;top:0;background:#f1f5f9;padding:10px 0;display:flex;gap:6px;flex-wrap:wrap;z-index:5}}
  .ft{{border:1px solid #cbd5e1;background:#fff;padding:7px 13px;border-radius:8px;font-size:13px;cursor:pointer;color:#334155}}
  .ft.on{{background:#0f172a;color:#fff;border-color:#0f172a}}
  .ft b{{margin-left:4px}}
  .zhdr{{color:#fff;font-size:13px;font-weight:600;padding:8px 12px;border-radius:6px;margin:16px 0 8px}}
  .card{{background:#fff;border:1px solid #e2e8f0;border-radius:9px;padding:13px 15px;margin-bottom:10px;transition:.15s}}
  .card.checked{{opacity:.55}}
  .card.focus{{box-shadow:0 0 0 3px #fbbf24}}
  .ctop{{display:flex;align-items:flex-start;gap:9px}}
  .check{{cursor:pointer;font-size:18px;line-height:1;color:#16a34a;user-select:none;width:20px;flex:none}}
  .ttl{{font-size:14px;font-weight:600;color:#111;text-decoration:none;flex:1}}
  .src{{color:#fff;font-size:10px;font-weight:700;padding:2px 7px;border-radius:4px;white-space:nowrap;height:fit-content}}
  .meta{{display:flex;flex-wrap:wrap;gap:10px;margin:8px 0 0 29px;font-size:12px;color:#475569;align-items:center}}
  .price{{font-weight:700;color:#16a34a;font-size:14px}}
  .transit{{margin:7px 0 0 29px;font-size:11.5px;color:#475569;line-height:1.4}}
  .rate{{margin:10px 0 0 29px;display:flex;gap:6px;flex-wrap:wrap}}
  .rb{{border:1px solid #cbd5e1;background:#fff;padding:5px 11px;border-radius:6px;font-size:12px;cursor:pointer;text-decoration:none;color:#334155;font-weight:600}}
  .rb.like.on{{background:#dcfce7;border-color:#16a34a;color:#15803d}}
  .rb.neu.on{{background:#fef9c3;border-color:#ca8a04;color:#a16207}}
  .rb.dis.on{{background:#fee2e2;border-color:#dc2626;color:#b91c1c}}
  .rb.view{{background:#f8fafc}}
  .toast{{position:fixed;bottom:20px;left:50%;transform:translateX(-50%);background:#0f172a;color:#fff;
          padding:9px 18px;border-radius:8px;font-size:13px;opacity:0;transition:.2s;pointer-events:none}}
  .toast.show{{opacity:1}}
</style></head><body>
<div class="wrap">
  <h1>🏠 Dublin Housing Dashboard</h1>
  <p class="sub">{total} listings · {unchecked} unchecked · {liked} liked · auto-refreshes every 5 min · ratings save to your database</p>
  <div class="filters">{tabs}</div>
  {cards if cards else '<p style="color:#64748b;padding:40px 0;text-align:center">No listings yet. Run a check first.</p>'}
</div>
<div class="toast" id="toast"></div>
<script>
  function toast(m){{const t=document.getElementById('toast');t.textContent=m;t.classList.add('show');
    clearTimeout(window._tt);window._tt=setTimeout(()=>t.classList.remove('show'),1400);}}
  async function rate(id, r){{
    const card=document.getElementById(id);
    const cur=card.dataset.rating;
    const val=(cur===r)?'':r;                       // click same rating again to clear
    await fetch('/api/rate',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{id:id,rating:val}})}});
    card.dataset.rating=val;
    card.querySelectorAll('.rb').forEach(b=>b.classList.remove('on'));
    if(val) card.querySelector('.rb.'+({{like:'like',neutral:'neu',dislike:'dis'}}[val])).classList.add('on');
    toast(val?('Saved: '+val):'Rating cleared');
    if(val==='dislike') applyFilter();              // disliked drops out of unchecked view
  }}
  async function toggleCheck(id){{
    const card=document.getElementById(id);
    const now=card.dataset.checked==='1'?0:1;
    await fetch('/api/check',{{method:'POST',headers:{{'Content-Type':'application/json'}},
      body:JSON.stringify({{id:id,checked:now}})}});
    card.dataset.checked=String(now);
    card.classList.toggle('checked',now===1);
    card.querySelector('.check').textContent=now?'✓':'○';
    toast(now?'Marked checked':'Unchecked');
    applyFilter();
  }}
  let curFilter='unchecked';
  function filt(btn,f){{document.querySelectorAll('.ft').forEach(b=>b.classList.remove('on'));
    btn.classList.add('on');curFilter=f;applyFilter();}}
  function applyFilter(){{
    document.querySelectorAll('.card').forEach(c=>{{
      const ck=c.dataset.checked==='1', rt=c.dataset.rating;
      let show=true;
      if(curFilter==='unchecked') show=!ck && rt!=='dislike';
      else if(curFilter==='checked') show=ck;
      else if(curFilter==='like') show=rt==='like';
      c.style.display=show?'block':'none';
    }});
    document.querySelectorAll('.zhdr').forEach(h=>{{
      let n=h.nextElementSibling, any=false;
      while(n && n.classList.contains('card')){{if(n.style.display!=='none')any=true;n=n.nextElementSibling;}}
      h.style.display=any?'block':'none';
    }});
  }}
  applyFilter();
  // gentle auto-refresh so new daemon finds appear without manual reload
  setTimeout(()=>location.reload(), 5*60*1000);
  // jump to a focused listing (from an email "Rate" link)
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
