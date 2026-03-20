#!/usr/bin/env python3
"""
Land Lead Finder v75 — 100+ Acre Tracts  TN / AL / NC
=======================================================
Counties:
  TN: Blount, Hamilton, Knox, Madison, Sevier
  AL: DeKalb, Jackson, Madison, Marshall
  NC: Cherokee, Macon, Swain

v9 bug fixes vs v8:
  NC:
    - BUG FIXED: added `orderByFields=gisacres DESC` — v8 got 500 small residential
      lots (random order), never hit 100-ac tracts
    - BUG FIXED: NC field is `ownname` not `owner` in NC1Map_Parcels service
    - Statewide WHERE = cntyname only, filter in Python
  AL Madison:
    - BUG FIXED: confirmed layer 141 "Parcel View" on web3/AL47_GAMAWeb
      (fields: Acres, PropertyOwner, MailingAddress, TotalAppraisedValue, DeedDate)
      v8 probe missed this because ACRE_KEYS didn't include "Acres" (capital A)
    - Direct URL, no probe needed
  AL Marshall:
    - BUG FIXED: confirmed layer 9 on web2/AL50_VAM_MS with outFields=* normalization
  AL DeKalb / Jackson:
    - BUG FIXED: DeKalb uses AL28_GAMAWeb pattern, Jackson uses AL49_GAMAWeb
      (same pattern as confirmed Madison AL47_GAMAWeb)
    - Probe GAMAWeb service for "Parcel View" layer dynamically
  TN owner:
    - NEW: Post-process top-20 TN parcels via TN Comptroller TPAD scrape
    - Only top-N so we don't hammer the server
  Soil/pasture:
    - NEW: Per-parcel variation using LU class + acreage tier (not county copy-paste)
    - NRCS county context still used as baseline
  Dashboard:
    - Links removed; owner/sale data shown inline

Usage:
  pip install requests --break-system-packages
  python3 land_lead_finder_v75.py
  python3 land_lead_finder_v75.py --min-acres 200 --top 50
"""

import requests, re, sys, argparse, time, json, html as _html
from datetime import datetime
from time import sleep

# ── HTTP ──────────────────────────────────────────────────────────────────────
import urllib3
urllib3.disable_warnings()

SESSION = requests.Session()
SESSION.verify = False
SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
})

# KCS blocks requests without their Referer
KCS_SESSION = requests.Session()
KCS_SESSION.verify = False
KCS_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "Chrome/124.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Encoding": "gzip, deflate",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://isv.kcsgis.com/",
    "Origin": "https://isv.kcsgis.com",
})

def GET(url, params=None, timeout=28, retries=2, session=None):
    """HTTP GET with retries. Never raises — always returns Response or None."""
    sess = session or SESSION
    for attempt in range(retries):
        try:
            r = sess.get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r
            if r.status_code in (429, 503):
                time.sleep(2 ** attempt)
                continue
            return None  # 4xx/5xx — not retrying
        except (requests.exceptions.Timeout,
                requests.exceptions.SSLError,
                requests.exceptions.ConnectionError):
            if attempt < retries - 1: time.sleep(1)
        except KeyboardInterrupt:
            raise  # let Ctrl+C work
        except Exception:
            pass
    return None

def jget(url, params=None, timeout=28, kcs=False):
    try:
        r = GET(url, params=params, timeout=timeout,
                session=KCS_SESSION if kcs else SESSION)
    except Exception as e:
        return None, f"request: {e}"
    if not r:
        return None, "no response"
    try:
        # Handle gzip responses that requests didn't auto-decompress
        raw = r.content
        if raw[:2] == b'\x1f\x8b':
            import gzip as _gz
            raw = _gz.decompress(raw)
        import json as _json
        d = _json.loads(raw)
        if isinstance(d, dict) and d.get("error"):
            return None, d["error"].get("message", "API error")
        return d, None
    except Exception as e:
        return None, f"JSON: {e}"


# ── County assessor owner scraping ────────────────────────────────────────────
def _parse_owner_html(text):
    """Try common HTML patterns for owner name in assessor pages."""
    patterns = [
        r'OWNRNAME["\s>]+([A-Z][A-Z &,.\-\']{2,55})',
        r'Owner Name[^<]{0,20}<[^>]+>([A-Z][^<]{2,55})<',
        r'OwnerName[^<]{0,10}<[^>]+>([A-Z][^<]{2,55})<',
        r'PropertyOwner[^<]{0,10}<[^>]+>([A-Z][^<]{2,55})<',
        r'Taxpayer[^<]{0,20}<[^>]+>([A-Z][^<]{2,55})<',
        r'"ownerName"\s*:\s*"([^"]{3,60})"',
        r'"OwnerName"\s*:\s*"([^"]{3,60})"',
        r'"owner"\s*:\s*"([^"]{3,60})"',
        r'"OWNRNAME"\s*:\s*"([^"]{3,60})"',
        r'"PropertyOwner"\s*:\s*"([^"]{3,60})"',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip().rstrip(',')
            if len(val) > 2 and val.lower() not in ("null","none","n/a"):
                return val
    return ""

def _parse_mail_html(text):
    patterns = [
        r'"MailingAddress"\s*:\s*"([^"]{5,80})"',
        r'"mailingAddress"\s*:\s*"([^"]{5,80})"',
        r'Mailing[^<]{0,30}<[^>]+>([^<]{5,80})<',
        r'Mail Address[^<]{0,10}<[^>]+>([^<]{5,80})<',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            val = m.group(1).strip()
            if len(val) > 4:
                return val
    return ""

def _parse_sale_html(text):
    patterns = [
        r'"(?:SaleDate|saleDate|DeedDate|deedDate)"\s*:\s*"([^"]{4,20})"',
        r'(?:Sale|Deed|Last Sale) Date[^<]{0,20}<[^>]+>([^<]{4,20})<',
    ]
    for pat in patterns:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return parse_sale_year(m.group(1).strip())
    return ""

def scrape_owner_kcsgis(parcel_id, county_lower):
    """Try KCS ISV API for AL counties (Madison, Marshall, Jackson)."""
    if not parcel_id:
        return "", "", ""
    endpoints = [
        f"https://isv.kcsgis.com/al.{county_lower}_revenue/api/parcel/search",
        f"https://isv.kcsgis.com/al.{county_lower}_revenue/api/getParcel",
        f"https://isv.kcsgis.com/al.{county_lower}_revenue/",
    ]
    for url in endpoints:
        try:
            r = GET(url, {"q": parcel_id, "f": "json", "query": parcel_id},
                    timeout=10, session=KCS_SESSION)
            if not r:
                continue
            text = r.text
            owner = _parse_owner_html(text)
            if owner:
                return clean(owner), clean(_parse_mail_html(text)), _parse_sale_html(text)
        except Exception:
            continue
    return "", "", ""

def scrape_owner_delta(parcel_id, county_code="AL28"):
    """Delta Computer Systems — DeKalb AL and others."""
    if not parcel_id:
        return "", "", ""
    state = county_code[:2]
    url = f"http://www.deltacomputersystems.com/{state}/{county_code}/plinkquerya.html"
    try:
        r = GET(url, {"parcel": parcel_id}, timeout=12)
        if not r:
            return "", "", ""
        text = r.text
        return (clean(_parse_owner_html(text)),
                clean(_parse_mail_html(text)),
                _parse_sale_html(text))
    except Exception:
        return "", "", ""

def scrape_owner_tn(gislink):
    """TN Comptroller TPAD — try all known endpoints for owner data."""
    if not gislink: return "", "", ""
    for api_url, params in [
        ("https://assessment.cot.tn.gov/TPAD/api/v2/getParcelByGISLink", {"gisLink": gislink}),
        ("https://assessment.cot.tn.gov/TPAD/api/v1/getParcelByGISLink", {"gisLink": gislink}),
        ("https://assessment.cot.tn.gov/TPAD/api/parcel",                {"gislink": gislink, "gisLink": gislink}),
        ("https://assessment.cot.tn.gov/RE/PublicAssessmentSummary/GetOwnerInfo", {"gisLink": gislink}),
    ]:
        try:
            r = GET(api_url, params, timeout=12)
            if not r: continue
            try:
                d = r.json()
                data = d if isinstance(d, dict) else (d[0] if isinstance(d, list) and d else {})
                owner = (data.get("OwnerName") or data.get("ownerName") or
                         data.get("owner_name") or data.get("taxpayerName") or
                         data.get("TaxpayerName") or data.get("owner") or "")
                mail = (data.get("MailingAddress") or data.get("mailingAddress") or
                        data.get("mail_address") or "")
                sale = parse_sale_year(str(data.get("SaleDate") or data.get("saleDate") or ""))
                if owner: return clean(owner), clean(mail), sale
            except Exception:
                owner = _parse_owner_html(r.text)
                if owner:
                    return clean(owner), clean(_parse_mail_html(r.text)), _parse_sale_html(r.text)
        except Exception: continue
    for url, params in [
        ("https://assessment.cot.tn.gov/TPAD/Parcel/GIS",      {"gislink": gislink}),
        ("https://assessment.cot.tn.gov/RE/PublicAssessmentSummary", {"gisLink": gislink}),
    ]:
        try:
            r = GET(url, params, timeout=12)
            if not r: continue
            owner = _parse_owner_html(r.text)
            if owner:
                return clean(owner), clean(_parse_mail_html(r.text)), _parse_sale_html(r.text)
        except Exception: continue
    return "", "", ""

OWNER_SCRAPERS = {
    "AL_Madison":   lambda pid: scrape_owner_kcsgis(pid, "madison"),
    "AL_Marshall":  lambda pid: scrape_owner_kcsgis(pid, "marshall"),
    "AL_Jackson":   lambda pid: scrape_owner_kcsgis(pid, "jackson"),
    "AL_DeKalb":    lambda pid: (
        scrape_owner_kcsgis(pid, "dekalb") if scrape_owner_kcsgis(pid, "dekalb")[0]
        else scrape_owner_delta(pid, "AL28")
    ),
    "TN_Blount":    lambda pid: scrape_owner_tn(pid),
    "TN_Hamilton":  lambda pid: scrape_owner_tn(pid),
    "TN_Knox":      lambda pid: scrape_owner_tn(pid),
    "TN_Madison":   lambda pid: scrape_owner_tn(pid),
    "TN_Sevier":    lambda pid: scrape_owner_tn(pid),
}

def enrich_owners_web(leads, max_scrape=9999, verbose=False):
    """Web-scrape owner data for leads missing it, up to max_scrape."""
    missing = [l for l in leads if not l.get("owner") and l.get("parcel_id")]
    if not missing:
        return
    to_scrape = missing[:max_scrape]
    if verbose:
        print(f"  → Scraping owner data for {len(to_scrape)} parcels from county assessors...")
    for lead in to_scrape:
        key = f"{lead['state']}_{lead['county']}"
        scraper = OWNER_SCRAPERS.get(key)
        if not scraper:
            continue
        try:
            owner, mail, sale = scraper(lead["parcel_id"])
            if owner:
                lead["owner"] = owner
                if mail and not lead.get("mail_addr"):
                    lead["mail_addr"] = mail
                if sale and not lead.get("sale_year"):
                    lead["sale_year"] = sale
                if verbose:
                    print(f"      {lead['county']}/{str(lead['parcel_id'])[:18]} → {owner}")
        except Exception:
            pass
        sleep(0.35)

# ── Parcel map links ──────────────────────────────────────────────────────────
MAP_VIEWERS = {
    "TN_Blount":      "https://maps.blounttn.gov/",
    "TN_Hamilton":    "https://www.hamiltontn.gov/gis/",
    "TN_Knox":        "https://maps.knoxcounty.org/parcelviewer/",
    "TN_Madison":     "https://assessment.cot.tn.gov/RE/Search/",
    "TN_Sevier":      "https://gis.seviercountytn.org/",
    "AL_DeKalb":      "https://isv.kcsgis.com/al.dekalb_revenue/",
    "AL_Jackson":     "https://isv.kcsgis.com/al.jackson_revenue/",
    "AL_Madison":     "https://isv.kcsgis.com/al.madison_revenue/",
    "AL_Marshall":    "https://isv.kcsgis.com/al.marshall_revenue/",
    "NC_Cherokee":    "https://maps.cherokeecounty-nc.gov/ccgis/",
    "NC_Macon":       "https://gis2.maconnc.org/html5viewer/",
    "NC_Swain":       "https://maps.swaincountync.gov/gis/",
    "SC_Oconee":      "https://gis.oconeesc.com/",
    "SC_Pickens":     "https://gis.pickens.sc.gov/",
    "SC_Anderson":    "https://gis.andersoncountysc.org/",
    "SC_Greenville":  "https://gis.greenvillesc.gov/",
    "SC_Spartanburg": "https://gis.spartanburgcounty.org/",
}

REGRID_COUNTY_SLUGS = {
    "NC_Cherokee":"cherokee","NC_Clay":"clay","NC_Graham":"graham",
    "NC_Haywood":"haywood","NC_Jackson":"jackson","NC_Macon":"macon",
    "NC_Madison":"madison","NC_Mitchell":"mitchell","NC_Swain":"swain",
    "NC_Yancey":"yancey","NC_Watauga":"watauga","NC_Avery":"avery","NC_Ashe":"ashe","NC_Alleghany":"alleghany",
    "SC_Spartanburg":"spartanburg","SC_Anderson":"anderson",
    "SC_Greenville":"greenville","SC_Oconee":"oconee","SC_Pickens":"pickens",
    "AL_Jackson":"jackson","AL_Marshall":"marshall",
    "TN_Blount":"blount","TN_Hamilton":"hamilton","TN_Knox":"knox",
    "TN_Madison":"madison","TN_Sevier":"sevier",
}
# qPublic app names for counties that use it (direct record link)
QPUBLIC_APPS = {
    "SC_Spartanburg": "SpartanburgCountySC",
}

def regrid_url(lead):
    """Parcel boundary viewer URL.
    NC: parcels.nconemap.gov (confirmed auto-highlights parcel)
    SC/others: Regrid centered on coords, user searches parcel# manually
    """
    import urllib.parse
    state = lead.get("state") or ""
    pid = (lead.get("parcel_id") or "").strip()
    lat, lng = lead.get("lat"), lead.get("lng")

    # All states: Regrid with ?search=parcelID triggers auto-highlight
    # User confirmed Regrid highlights parcel perfectly when searched
    county = lead.get("county") or ""
    slug = REGRID_COUNTY_SLUGS.get(f"{state}_{county}",
                                    county.lower().replace(" ", "-"))
    state_l = state.lower()
    base = f"https://app.regrid.com/us/{state_l}/{slug}"
    search = f"?search={urllib.parse.quote(pid)}" if pid else ""
    if lat and lng:
        return f"{base}/@{lat:.5f},{lng:.5f},17z{search}"
    return f"{base}{search}"

def qpublic_url(lead):
    """Direct qPublic record URL for counties that use qPublic."""
    key = f"{lead.get('state','')}_{lead.get('county','')}"
    app = QPUBLIC_APPS.get(key)
    if not app: return None
    pid = (lead.get("parcel_id") or "").strip()
    if not pid: return None
    return (f"https://qpublic.schneidercorp.com/Application.aspx"
            f"?App={app}&Layer=Parcels&PageType=Record&KEY={pid}")

def parcel_map_urls(lead):
    """Return (county_gis_url, google_satellite_url)."""
    import urllib.parse
    key = f"{lead['state']}_{lead['county']}"
    gis_base = MAP_VIEWERS.get(key, "")
    pid = str(lead.get("parcel_id") or "")
    addr_raw = (lead.get("address") or
                f"{lead.get('county','')} County {lead.get('state','')}")

    # Build county GIS URL with parcel PIN deep-linked where possible
    if gis_base and pid:
        if "kcsgis.com" in gis_base:
            gis_url = f"{gis_base}?q={urllib.parse.quote(pid)}"
        elif any(x in gis_base for x in ["blounttn","hamiltontn","knoxcounty"]):
            gis_url = f"{gis_base}?pin={urllib.parse.quote(pid)}"
        elif "cherokeecounty-nc.gov" in gis_base:
            gis_url = f"{gis_base}?search={urllib.parse.quote(pid)}"
        elif "maconnc.org" in gis_base:
            gis_url = f"{gis_base}?find=parcel&pin={urllib.parse.quote(pid)}"
        elif "swaincountync.gov" in gis_base:
            gis_url = f"{gis_base}?pin={urllib.parse.quote(pid)}"
        else:
            gis_url = gis_base
    else:
        gis_url = gis_base

    # Google Earth web — drops a pin at exact coordinates if available
    lat2, lng2 = lead.get("lat"), lead.get("lng")
    if lat2 and lng2:
        # earth.google.com/web/@lat,lng,alt,range,tilt,heading,roll/
        google_url = (f"https://earth.google.com/web/@{lat2:.5f},{lng2:.5f}"
                      f",300a,1200d,35y,0h,0t,0r")
    else:
        q = urllib.parse.quote(f"{addr_raw} {lead.get('county','')} County {lead.get('state','')}")
        google_url = f"https://earth.google.com/web/search/{q}"
    return gis_url, google_url


def clean(v):
    if v is None: return ""
    s = str(v).strip()
    return "" if s.lower() in ("null","none","n/a","","0"," ","<null>") else s

def flt(v):
    try: return float(str(v).replace(",","").strip())
    except: return 0.0

def pick(d, keys):
    dl = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = dl.get(k.lower())
        if v not in (None,"","null","none",0,"0"," "):
            s = str(v).strip()
            if s and s.lower() not in ("null","none","n/a","<null>"):
                return s
    return ""

def pick_num(d, keys):
    dl = {k.lower(): v for k, v in d.items()}
    for k in keys:
        v = dl.get(k.lower())
        f = flt(v)
        if f > 0: return f
    return 0.0

# ── Field key lists ──────────────────────────────────────────────────────────
# IMPORTANT: Keep 'acres' early — KCS AL GAMAWeb uses 'Acres' (lowercase match)
ACRE_KEYS  = ["acres","lu_acres","deed_acres","acreage","recareano","gisacres",
              "calc_acres","total_acres","land_area_ac","areainacres","totacres",
              "parcel_acres","calculated_acreage","surveyed_ac","totalacres",
              "deedacres","calcacres","deededacres"]  # Marshall iWorQ
VALUE_KEYS = ["totalappraisedvalue","appraisal","appr_value","appraised_value",
              "total_value","market_value","just_value","tot_appr_val","totalvalue",
              "parval","totvalue","assessed_value","currval","landvalue","tot_value",
              "ttv","tav","clandvalue","cimpvalue"]  # Marshall iWorQ: TTV=total taxable value
OWNER_KEYS = ["propertyowner","ownname","ownrname","owner","owner_name","owner1",
              "own_name","ownername","grantee","taxpayer","ownerone","owner_full",
              "own1","grantor","name1","taxpayername",
              "previousowner"]  # Marshall iWorQ
ADDR_KEYS  = ["propertyaddress","address","phys_add","situs_address","site_address",
              "siteadd","property_address","phys_addr","location","prop_addr",
              "site_addr","streetaddress","situsaddname","situsaddnumber"]  # Marshall iWorQ
MAIL_ADDR  = ["mailingaddress","mail_addr","mail_address","mail_addr1","mailing_address",
              "own_addr1","mail_street","owner_addr","ownaddr","mailaddr","mail_add",
              "mailadd1","mailadd2"]  # iWorQ AL Marshall
MAIL_CITY  = ["mail_city","mailing_city","owner_city","own_city","mailcity","owncity",
              "mcity","mailcity"]  # Marshall iWorQ MailCity
MAIL_STATE = ["mail_state","mailing_state","owner_state","own_state","mailstate",
              "ownstate","mstate","mailstate"]  # Marshall iWorQ MailState
MAIL_ZIP   = ["mail_zip","mail_zipc","mailing_zip","owner_zip","own_zipc","mailzip",
              "ownzip","mzip","mailzip1"]  # Marshall iWorQ MailZip1
PARCEL_K   = ["pin","parcelno","parcel_id","gislink","parid","apn","map_num","gpin",
              "gis_parcelid","parcelid",  # Marshall iWorQ
              "taxparcelid","pinno","parcelnumber","property_id","parcelpro","parcel_num",
              "parcel","pid","tax_id","alt_pin","account","assess_num"]
SALE_YR_K  = ["deeddate","deed_date","sale_yr","sale_year","last_sale_yr","deed_year",
              "saledatetx","lastsaleyr","sale_yr1","saledate","convdate"]

def parse_sale_year(raw):
    if not raw: return ""
    # Greenville SC SALEDATE is epoch milliseconds
    if isinstance(raw, (int, float)) and raw > 1e11:
        try:
            import datetime
            yr = datetime.datetime.fromtimestamp(raw / 1000, tz=datetime.timezone.utc).year
            if 1900 < yr < 2100: return str(yr)
        except Exception: pass
    m = re.search(r'(19|20)\d{2}', str(raw))
    return m.group(0) if m else ""

# ── Government filter ─────────────────────────────────────────────────────────
GOVT_KEYWORDS = [
    "UNITED STATES","U.S. GOVERNMENT","US GOVERNMENT",
    "FEDERAL GOVERNMENT","USDA","USFS","U.S. FOREST","US FOREST",
    "NATIONAL FOREST","NATIONAL PARK","DEPT OF ","DEPARTMENT OF",
    "COUNTY OF "," COUNTY GOVERNMENT","CITY OF ","TOWN OF ",
    "TVA ","TENNESSEE VALLEY AUTHORITY","CORPS OF ENGINEERS",
    "ARMY CORPS","BLM ","BUREAU OF LAND","WILDLIFE MANAGEMENT",
    "STATE PARK","CONSERVATION DISTRICT","PUBLIC UTILITY",
    "WATER AUTHORITY","SCHOOL BOARD","SCHOOL DISTRICT",
    "MUNICIPALITY","COMMONWEALTH OF","GOVERNMENT OF ",
    "HABITAT FOR HUMANITY",
]
# Note: "STATE OF " intentionally removed — it false-positives on "ESTATE OF"
_GOVT_STARTSWITH = ["STATE OF "]  # only match at word boundary via startswith

def is_government(s):
    if not s: return False
    up = s.upper().strip()
    if any(kw in up for kw in GOVT_KEYWORDS): return True
    if any(up.startswith(kw) for kw in _GOVT_STARTSWITH): return True
    return False

import math as _math

def format_nc_pin(raw):
    """Return raw NC parno as-is (no dash formatting)."""
    return str(raw or "").strip()



def polygon_centroid_wgs84(geometry):
    """Extract centroid from esriGeometryPolygon in WGS84 (outSR=4326)."""
    rings = (geometry or {}).get("rings", [])
    if not rings: return None, None
    ring = rings[0]
    if len(ring) < 3: return None, None
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    lng = sum(xs) / len(xs)
    lat = sum(ys) / len(ys)
    if not (-180 <= lng <= 180 and -90 <= lat <= 90): return None, None
    return round(lat, 5), round(lng, 5)

def fetch_elevation_ft(lat, lng):
    """USGS EPQS — single point elevation in feet."""
    if lat is None or lng is None: return None
    try:
        r = GET(f"https://epqs.nationalmap.gov/v1/json?x={lng}&y={lat}&units=Feet&includeDate=false",
                timeout=8)
        if not r: return None
        val = r.json().get("value")
        if val and float(val) > -1000: return round(float(val))
    except Exception: pass
    return None

def fetch_elevation_range_ft(rings):
    """
    Sample 9 points across the parcel bounding box to get true min/max elevation.
    rings: polygon rings in WGS84 from GIS geometry.
    Returns (elev_min, elev_max) in feet, or (None, None).
    """
    if not rings: return None, None
    coords = [pt for ring in rings for pt in ring]
    if not coords: return None, None
    lngs = [p[0] for p in coords]
    lats = [p[1] for p in coords]
    min_lng, max_lng = min(lngs), max(lngs)
    min_lat, max_lat = min(lats), max(lats)
    # 3x3 grid of sample points across bounding box
    sample_pts = []
    for lat_frac in [0.1, 0.5, 0.9]:
        for lng_frac in [0.1, 0.5, 0.9]:
            sample_pts.append((
                round(min_lat + (max_lat - min_lat) * lat_frac, 5),
                round(min_lng + (max_lng - min_lng) * lng_frac, 5)
            ))
    elevs = []
    for lat, lng in sample_pts:
        e = fetch_elevation_ft(lat, lng)
        if e is not None: elevs.append(e)
        sleep(0.08)
    if not elevs: return None, None
    return min(elevs), max(elevs)



def enrich_gps_elevation(leads, verbose=False):
    """Fetch exact min/max elevation for parcels that have geometry rings."""
    need = [l for l in leads if l.get("lat") and l.get("elev_min") is None]
    if not need:
        if verbose: print("  No GPS coordinates available for elevation lookup")
        return
    if verbose: print(f"  Fetching elevation range for {len(need)} parcels (9-point sample)...")
    for lead in need:
        rings = lead.get("geom_rings")
        if rings:
            mn, mx = fetch_elevation_range_ft(rings)
        else:
            # Single centroid point fallback
            pt = fetch_elevation_ft(lead["lat"], lead["lng"])
            mn = mx = pt
        lead["elev_min"] = mn
        lead["elev_max"] = mx
        # Keep elevation_ft as centroid for backwards compat
        lead["elevation_ft"] = mn


def confidence(lead):
    s = 0
    notes = []
    if lead.get("address"): s += 20
    else: notes.append("No situs address")
    if lead.get("owner"): s += 25
    else: notes.append("Owner not in GIS")
    if lead.get("appr_value",0) > 0: s += 20
    else: notes.append("No appraised value")
    if lead.get("parcel_id"): s += 10
    else: notes.append("No parcel ID")
    if lead.get("sale_year"): s += 10
    else: notes.append("Sale year unknown")
    if lead.get("mail_addr") or lead.get("mail_city"): s += 15
    else: notes.append("No mailing address")
    return ("HIGH" if s >= 75 else "MED" if s >= 45 else "LOW"), s, notes

# ── Per-property analysis ─────────────────────────────────────────────────────
def analyze_property(lead, county_vpa_median):
    lines = []
    ac = lead["acres"]
    vpa = lead["vpa"]
    val = lead["appr_value"]
    owner = lead.get("owner","")
    ms = lead.get("mail_state","")
    sale_yr = lead.get("sale_year","")
    lu = lead.get("lu_class","")
    has_struct = lead.get("has_structure", False)
    state = lead["state"]

    if ac >= 500: lines.append(f"{ac:,.0f} ac — major holding, institutional scale")
    elif ac >= 200: lines.append(f"{ac:,.0f} ac — large tract, multi-use potential")
    else: lines.append(f"{ac:,.0f} ac — solid 100+ ac block")

    if vpa > 0 and county_vpa_median > 0:
        pct = (vpa / county_vpa_median) * 100
        if pct <= 50: lines.append(f"${vpa:,.0f}/ac — {100-pct:.0f}% below county median (strong discount)")
        elif pct <= 80: lines.append(f"${vpa:,.0f}/ac — below county median ({pct:.0f}% of median)")
        elif pct <= 110: lines.append(f"${vpa:,.0f}/ac — near county median ({pct:.0f}%)")
        else: lines.append(f"${vpa:,.0f}/ac — above county median ({pct:.0f}%); confirm utility")
    elif vpa > 0:
        lines.append(f"${vpa:,.0f}/ac appraised")

    if owner:
        up = owner.upper()
        if "LLC" in up: lines.append("LLC owner — entity may be inactive / distressed")
        elif "TRUST" in up: lines.append("Trust owner — estate/trust holdings often motivated")
        elif "ESTATE" in up or "HEIRS" in up or "HEIR" in up:
            lines.append("Estate/heir ownership — classic motivated seller")
        elif "CORP" in up or "INC" in up or "CO." in up:
            lines.append("Corporate owner — verify if core or peripheral asset")
        else:
            lines.append("Individual/family owner")
        if ms and ms.upper() not in (state,""):
            lines.append(f"OOS mailing: {ms.upper()} — absentee, mail campaign viable")
    else:
        lines.append("Owner data not in GIS (see assessor note)")

    if sale_yr:
        try:
            age = datetime.now().year - int(sale_yr)
            if age >= 40: lines.append(f"Held {age} yrs (since {sale_yr}) — generational, no mortgage likely")
            elif age >= 25: lines.append(f"Held {age} yrs (since {sale_yr}) — long hold, equity rich")
            elif age >= 15: lines.append(f"Held {age} yrs (since {sale_yr}) — moderate hold")
            else: lines.append(f"Acquired {sale_yr} — relatively recent")
        except: pass

    if lu:
        ll = lu.lower()
        if "timber" in ll or "forest" in ll: lines.append(f"LU: {lu} — verify harvestable timber")
        elif "agri" in ll or "farm" in ll or "crop" in ll: lines.append(f"LU: {lu} — may qualify for greenbelt rate")
        elif "vacant" in ll or "undevel" in ll: lines.append(f"LU: {lu} — clean slate, no improvements")
        elif "pasture" in ll or "range" in ll: lines.append(f"LU: {lu} — improved pasture, cattle-ready")
        elif "resid" in ll: lines.append(f"LU: {lu} — residential class; verify actual use")
        else: lines.append(f"LU: {lu}")

    if not has_struct:
        lines.append("No recorded structures — raw land or structures not captured in GIS")

    if val > 0:
        mao = val * 0.65
        lines.append(f"MAO at 65%: ${mao:,.0f} (${mao/ac:,.0f}/ac)")

    return lines

# ── Scoring ───────────────────────────────────────────────────────────────────
# ── Investment scoring (motivated-seller focus) ───────────────────────────────
def score_investment(lead, county_vpa_median=0):
    s = 0
    ac, vpa = lead["acres"], lead["vpa"]
    if ac >= 500: s += 20
    elif ac >= 200: s += 15
    elif ac >= 100: s += 10
    if vpa > 0:
        if county_vpa_median > 0:
            r = vpa / county_vpa_median
            if r <= 0.5: s += 30
            elif r <= 0.75: s += 20
            elif r <= 1.0: s += 12
            elif r <= 1.25: s += 5
        else:
            if vpa <= 1000: s += 25
            elif vpa <= 2500: s += 15
            elif vpa <= 5000: s += 8
    if lead.get("oos"): s += 15
    yr = lead.get("sale_year","")
    try:
        age = datetime.now().year - int(str(yr)[:4])
        if age >= 35: s += 18
        elif age >= 25: s += 13
        elif age >= 15: s += 8
        elif age >= 10: s += 4
    except: s += 3
    if not lead.get("has_structure"): s += 5
    own = (lead.get("owner") or "").upper()
    if "LLC" in own or "TRUST" in own or "ESTATE" in own or "HEIRS" in own: s += 8
    elif "CORP" in own or "INC " in own: s += 4
    if lead.get("owner"): s += 5
    return min(s, 99)

# ── Homestead scoring (group of 15, on/off-grid self-sufficiency) ─────────────
# Criteria weights designed around a group of 15 people needing:
#   - enough land to grow food, raise livestock, harvest timber
#   - clean water access (stream/spring region)
#   - adequate pasture for animals
#   - forest for building material, fuel, hunting
#   - defensible privacy / NF buffer
#   - accessible but not suburban (grid tie option)
#   - affordable enough to acquire outright

HOMESTEAD_GROW_ACRES_PER_PERSON = 10   # ~10 ac/person for full food production
HOMESTEAD_GROUP = 15

def score_homestead(lead):
    """0–100 homestead score weighted for group of 15."""
    s = 0
    ac = lead["acres"]
    vpa = lead["vpa"]
    soil = SOIL_DATA.get(f"{lead['state']}_{lead['county']}", {})
    pasture_pct = lead.get("est_pasture_pct", soil.get("base_pasture_pct", 40))
    nf = soil.get("nf_nearby", False)
    water_score = soil.get("water_score", 5)  # 1-10 county-level water access estimate
    climate_score = soil.get("climate_score", 5)  # 1-10 growing season quality
    elev_note = soil.get("climate", "")

    # ── Acreage (max 25 pts) ─────────────────────
    # Sweet spot for 15 people: 150–500 ac
    # Need at minimum ~150 ac; 300-500 ac ideal
    ideal = HOMESTEAD_GROUP * HOMESTEAD_GROW_ACRES_PER_PERSON  # 150 ac
    if ac >= 500:   s += 25
    elif ac >= 300: s += 22
    elif ac >= 200: s += 18
    elif ac >= 150: s += 14
    elif ac >= 100: s += 9

    # ── Pasture % (max 20 pts) ───────────────────
    # Livestock + crops need open ground; timber balance too
    if pasture_pct >= 60:   s += 20
    elif pasture_pct >= 45: s += 16
    elif pasture_pct >= 30: s += 11
    elif pasture_pct >= 20: s += 6
    else:                   s += 2   # mostly timber — still useful but less balanced

    # ── Water access (max 15 pts) ────────────────
    s += min(15, water_score * 1.5)

    # ── Climate / growing season (max 10 pts) ────
    s += min(10, climate_score)

    # ── National Forest / public land nearby (max 10 pts) ────
    # Hunting, foraging, water shed protection, privacy buffer
    if nf: s += 10

    # ── Affordability for outright purchase (max 10 pts) ────
    # 15 people pooling resources; lower VPA = more accessible
    if vpa > 0:
        if vpa <= 1000:   s += 10
        elif vpa <= 2000: s += 8
        elif vpa <= 3500: s += 5
        elif vpa <= 6000: s += 2

    # ── No/minimal structures = build-your-own layout (max 5 pts) ───
    if not lead.get("has_structure"): s += 5

    # ── Soil class bonus (from county data) (max 5 pts) ────
    soil_str = (soil.get("base_soil") or "").lower()
    if "class i" in soil_str: s += 5
    elif "class ii" in soil_str: s += 3
    elif "class iii" in soil_str: s += 1

    return min(round(s), 99)

# ── Homestead analysis text ───────────────────────────────────────────────────
def homestead_analysis(lead):
    ac = lead["acres"]
    soil = SOIL_DATA.get(f"{lead['state']}_{lead['county']}", {})
    pasture_pct = lead.get("est_pasture_pct", soil.get("base_pasture_pct", 40))
    nf = soil.get("nf_nearby", False)
    nf_name = soil.get("nf_name", "")
    water_score = soil.get("water_score", 5)
    vpa = lead.get("vpa", 0)
    val = lead.get("appr_value", 0)

    lines = []
    # Acreage per person
    per_person = ac / HOMESTEAD_GROUP
    lines.append(f"{ac:,.0f} ac ÷ {HOMESTEAD_GROUP} people = {per_person:.1f} ac/person")
    if per_person >= 15: lines.append("✓ Excellent land-to-person ratio for full self-sufficiency")
    elif per_person >= 10: lines.append("✓ Solid ratio — full food production viable")
    elif per_person >= 7: lines.append("△ Workable — tight for livestock + crops + timber")
    else: lines.append("✗ Tight — supplemental food sourcing likely needed")

    # Pasture / livestock
    open_ac = ac * (pasture_pct / 100)
    timber_ac = ac - open_ac
    lines.append(f"Est. {open_ac:.0f} ac open / {timber_ac:.0f} ac wooded")
    # Livestock estimate: 1 cow per 1.5 ac (Southeast pasture avg)
    cows = int(open_ac / 1.5)
    # Garden estimate
    garden_ac = min(open_ac * 0.3, 15)
    lines.append(f"~{cows} cow/calf pairs possible; ~{garden_ac:.0f} ac crop/garden potential")
    if timber_ac >= 30:
        cords = int(timber_ac * 1.5)  # rough: 1.5 cords/ac sustainable harvest
        lines.append(f"~{timber_ac:.0f} ac timber: firewood, building material, wildlife habitat")

    # Water
    if water_score >= 8: lines.append("✓ High water access score — streams/springs typical for region")
    elif water_score >= 5: lines.append("△ Moderate water access — verify on-property water source")
    else: lines.append("⚠ Lower water score — well + cistern likely needed, verify depth")

    # NF / public land
    if nf: lines.append(f"✓ {nf_name} nearby — hunting, foraging, watershed buffer, recreation")

    # Affordability for group
    if val > 0:
        per_person_cost = val / HOMESTEAD_GROUP
        lines.append(f"Cost split {HOMESTEAD_GROUP} ways: ${per_person_cost:,.0f}/person at appraised value")
        mao_pp = (val * 0.65) / HOMESTEAD_GROUP
        lines.append(f"At 65% MAO: ${mao_pp:,.0f}/person target acquisition cost")

    return lines

# ── Soil data per county ──────────────────────────────────────────────────────
# Added water_score (1-10) and climate_score (1-10) for homestead scoring
SOIL_DATA = {
    "TN_Blount": {
        "base_soil": "Whiteside-Soco Complex (Class II–III)",
        "drainage": "Well drained ridges, moderately well drained hollows",
        "base_pasture_pct": 50,
        "nf_nearby": True, "nf_name": "Cherokee Nat'l Forest",
        "climate": "800–1,400 ft. Avg 52°F. ~55 in/yr. 175-day growing season.",
        "water_score": 9,   # high rainfall + mountain streams
        "climate_score": 7, # good growing season, slightly cooler
    },
    "TN_Hamilton": {
        "base_soil": "Sequatchie-Hamblen Silt Loam (Class II)",
        "drainage": "Well drained, Tennessee River valley alluvial",
        "base_pasture_pct": 58,
        "nf_nearby": False, "nf_name": "",
        "climate": "600–900 ft. Avg 59°F. ~54 in/yr. 200-day growing season.",
        "water_score": 7,
        "climate_score": 8,
    },
    "TN_Knox": {
        "base_soil": "Corryton-Muskingum Loam (Class II–III)",
        "drainage": "Well drained to moderately well drained ridges",
        "base_pasture_pct": 48,
        "nf_nearby": False, "nf_name": "",
        "climate": "900–1,200 ft. Avg 57°F. ~48 in/yr. 195-day growing season.",
        "water_score": 6,
        "climate_score": 7,
    },
    "TN_Madison": {
        "base_soil": "Memphis-Loring Silt Loam (Class I–II, prime farmland)",
        "drainage": "Well drained, West TN loess uplands",
        "base_pasture_pct": 65,
        "nf_nearby": False, "nf_name": "",
        "climate": "350–450 ft. Avg 62°F. ~50 in/yr. 220-day season. Tornado corridor.",
        "water_score": 6,
        "climate_score": 9,  # prime farmland, long season
    },
    "TN_Sevier": {
        "base_soil": "Spivey-Santeetlah Complex (Class III–IV, steep terrain)",
        "drainage": "Well drained, shallow-to-moderate mountain soils",
        "base_pasture_pct": 28,
        "nf_nearby": True, "nf_name": "Great Smoky Mtns Nat'l Park",
        "climate": "1,200–3,000 ft. Avg 49°F. ~65 in/yr. 160-day season.",
        "water_score": 10,  # abundant streams
        "climate_score": 5, # shorter season at elevation
    },
    "AL_DeKalb": {
        "base_soil": "Hartsells-Albertville Loam (Class II–III)",
        "drainage": "Well drained, cherty Sand Mountain plateau",
        "base_pasture_pct": 55,
        "nf_nearby": False, "nf_name": "",
        "climate": "900–1,400 ft. Avg 59°F. ~56 in/yr. 195-day season.",
        "water_score": 7,
        "climate_score": 7,
    },

"AL_Cherokee": {
    "base_soil": "Hartsells-Townley Fine Sandy Loam (Class III, sandstone ridges)",
    "drainage": "Well drained, Coosa River watershed headwaters",
    "base_pasture_pct": 50,
    "nf_nearby": False, "nf_name": "",
    "climate": "800–1,800 ft. Avg 59°F. ~56 in/yr. 195-day season.",
    "water_score": 8,
    "climate_score": 8,
},
"AL_DeKalb": {
    "base_soil": "Decatur-Hartsells Silt Loam (Class I–III, varied terrain)",
    "drainage": "Well drained, Little River canyon area, excellent bottomland",
    "base_pasture_pct": 55,
    "nf_nearby": False, "nf_name": "",
    "climate": "700–1,800 ft. Avg 59°F. ~57 in/yr. 200-day season.",
    "water_score": 8,
    "climate_score": 8,
},
"AL_Etowah": {
    "base_soil": "Holston-Steekee Complex (Class II–III, limestone valley)",
    "drainage": "Well drained, Coosa River / Gadsden area, productive valley soils",
    "base_pasture_pct": 52,
    "nf_nearby": False, "nf_name": "",
    "climate": "600–1,400 ft. Avg 61°F. ~56 in/yr. 210-day season.",
    "water_score": 7,
    "climate_score": 8,
},
    "AL_Jackson": {
        "base_soil": "Dickson-Mimosa Silt Loam (Class II, limestone valley)",
        "drainage": "Moderately well drained, Paint Rock Valley bottomland",
        "base_pasture_pct": 67,
        "nf_nearby": False, "nf_name": "",
        "climate": "600–1,000 ft. Avg 60°F. ~53 in/yr. 210-day season.",
        "water_score": 8,   # Paint Rock River watershed, springs
        "climate_score": 8,
    },
    "AL_Madison": {
        "base_soil": "Decatur-Hartsells Silt Loam (Class I–II, prime)",
        "drainage": "Well drained, Tennessee Valley limestone floor",
        "base_pasture_pct": 60,
        "nf_nearby": False, "nf_name": "",
        "climate": "550–750 ft. Avg 61°F. ~53 in/yr. 215-day season. Huntsville metro.",
        "water_score": 7,
        "climate_score": 8,
    },
    "AL_Marshall": {
        "base_soil": "Dickson-Taft Silt Loam (Class II–III)",
        "drainage": "Well drained with Tennessee River / Guntersville Lake proximity",
        "base_pasture_pct": 55,
        "nf_nearby": False, "nf_name": "",
        "climate": "560–900 ft. Avg 60°F. ~55 in/yr. 205-day season.",
        "water_score": 9,   # lake county, abundant water
        "climate_score": 8,
    },
    
"GA_Fannin": {
    "base_soil": "Suches-Elf Complex (Class III–IV, high elevation Blue Ridge)",
    "drainage": "Well drained mountain soils, Toccoa/Conasauga headwaters",
    "base_pasture_pct": 30, "nf_nearby": True, "nf_name": "Chattahoochee-Oconee NF",
    "climate": "1,400–3,000 ft. Avg 55°F. ~65 in/yr. 155-day season.",
    "water_score": 9, "climate_score": 6,
},
"GA_Gilmer": {
    "base_soil": "Cullasaja-Saunook Complex (Class II–III, creek bottoms & ridges)",
    "drainage": "Well drained, Ellijay River valley, productive bottomland",
    "base_pasture_pct": 40, "nf_nearby": True, "nf_name": "Chattahoochee-Oconee NF",
    "climate": "1,100–2,400 ft. Avg 57°F. ~62 in/yr. 170-day season.",
    "water_score": 9, "climate_score": 7,
},
"GA_Union": {
    "base_soil": "Suches-Elf-Chestnut Complex (Class III–IV, high elevation)",
    "drainage": "Well drained, Nottely River watershed, excellent water",
    "base_pasture_pct": 30, "nf_nearby": True, "nf_name": "Chattahoochee-Oconee NF",
    "climate": "1,800–4,000 ft. Avg 53°F. ~68 in/yr. 145-day season.",
    "water_score": 10, "climate_score": 6,
},
"GA_Towns": {
    "base_soil": "Saunook-Tuckasegee Silt Loam (Class II–III, river valleys)",
    "drainage": "Well drained, Hiawassee River/Lake Chatuge, scenic basin",
    "base_pasture_pct": 35, "nf_nearby": True, "nf_name": "Chattahoochee-Oconee NF",
    "climate": "1,900–4,700 ft. Avg 52°F. ~70 in/yr. 140-day season.",
    "water_score": 10, "climate_score": 6,
},
"GA_Lumpkin": {
    "base_soil": "Madison-Pacolet Sandy Clay Loam (Class III, Piedmont meets Blue Ridge)",
    "drainage": "Well drained, Chestatee River/Lake Lanier watershed",
    "base_pasture_pct": 35, "nf_nearby": True, "nf_name": "Chattahoochee-Oconee NF",
    "climate": "1,200–3,500 ft. Avg 57°F. ~64 in/yr. 165-day season.",
    "water_score": 8, "climate_score": 7,
},
"NC_Watauga": {
        "base_soil": "Porters-Chestnut Silt Loam (Class III, high Blue Ridge)",
        "drainage": "Well drained, upper New River watershed headwaters",
        "base_pasture_pct": 30,
        "nf_nearby": True, "nf_name": "Pisgah Nat'l Forest / Blue Ridge Parkway",
        "climate": "3,000–5,500 ft. Avg 51°F. ~52 in/yr. 145-day season. Coolest summer in SE.",
        "water_score": 9, "climate_score": 10,
    },
    "NC_Avery": {
        "base_soil": "Porters-Chestnut Channery Silt Loam (Class III–IV, steep high elevation)",
        "drainage": "Well drained, Toe River/Linville watershed, rocky high slopes",
        "base_pasture_pct": 20,
        "nf_nearby": True, "nf_name": "Pisgah Nat'l Forest / Roan Highlands",
        "climate": "2,500–6,000 ft. Avg 49°F. ~60 in/yr. 135-day season. Roan Mountain balds.",
        "water_score": 9, "climate_score": 10,
    },
    "NC_Ashe": {
        "base_soil": "Clifton-Hayesville Clay Loam (Class II–III, New River valley)",
        "drainage": "Well drained, New River valley, excellent bottomland",
        "base_pasture_pct": 45,
        "nf_nearby": True, "nf_name": "Jefferson Nat'l Forest / New River State Park",
        "climate": "2,800–4,900 ft. Avg 51°F. ~48 in/yr. 150-day season.",
        "water_score": 10, "climate_score": 9,
    },
    "NC_Alleghany": {
        "base_soil": "Hayesville-Ashe Silt Loam (Class II–III, plateau farmland)",
        "drainage": "Well drained, New River headwaters, excellent plateau farmland",
        "base_pasture_pct": 50,
        "nf_nearby": True, "nf_name": "Jefferson Nat'l Forest",
        "climate": "2,900–4,600 ft. Avg 51°F. ~45 in/yr. 150-day season. High country plateau.",
        "water_score": 9, "climate_score": 9,
    },
    "NC_Cherokee": {
        "base_soil": "Tusquitee-Saunook Silt Loam (Class II–III)",
        "drainage": "Well drained mountain valley soils",
        "base_pasture_pct": 35,
        "nf_nearby": True, "nf_name": "Nantahala Nat'l Forest",
        "climate": "1,600–3,000 ft. Avg 54°F. ~60 in/yr. 170-day season.",
        "water_score": 9,
        "climate_score": 6,
    },
    "NC_Macon": {
        "base_soil": "Evard-Cowee Complex (Class III–IV, steep slopes)",
        "drainage": "Well drained to excessively drained mountain soils",
        "base_pasture_pct": 25,
        "nf_nearby": True, "nf_name": "Nantahala Nat'l Forest",
        "climate": "2,000–4,000 ft. Avg 51°F. ~65 in/yr. 155-day season.",
        "water_score": 10,
        "climate_score": 5,
    },
    "NC_Swain": {
        "base_soil": "Spivey-Santeetlah Complex (Class IV, very steep)",
        "drainage": "Well drained, deep mountain soils with rocky outcrops",
        "base_pasture_pct": 15,
        "nf_nearby": True, "nf_name": "Great Smoky Mtns Nat'l Park",
        "climate": "1,800–5,000 ft. Avg 49°F. ~75 in/yr. 140-day season.",
        "water_score": 10,
        "climate_score": 4, # high elevation, short season
    },
"SC_Oconee": {
        "base_soil": "Dellwood-Tate Sandy Loam (Class III–IV, mountain terrain)",
        "drainage": "Well drained, Chattooga/Keowee watershed headwaters",
        "base_pasture_pct": 38,
        "nf_nearby": True, "nf_name": "Sumter Nat'l Forest / Ellicott Rock Wilderness",
        "climate": "1,200–3,200 ft. Avg 55F. ~65 in/yr. 175-day season.",
        "water_score": 10,
        "climate_score": 6,
    },
    "SC_Pickens": {
        "base_soil": "Saluda-Pacolet Sandy Clay Loam (Class II–III)",
        "drainage": "Well drained, Blue Ridge foothills piedmont",
        "base_pasture_pct": 45,
        "nf_nearby": True, "nf_name": "Sumter Nat'l Forest",
        "climate": "900–2,000 ft. Avg 58F. ~60 in/yr. 190-day season.",
        "water_score": 8,
        "climate_score": 7,
    },

"VA_Lee": {
    "base_soil": "Whitesburg-Bays Silt Loam (Class II–III, river bottoms)",
    "drainage": "Well drained, Powell/Clinch River valley bottomland",
    "base_pasture_pct": 55,
    "nf_nearby": True, "nf_name": "Cherokee Nat'l Forest / Jefferson NF",
    "climate": "1,200–3,500 ft. Avg 55°F. ~46 in/yr. 175-day season.",
    "water_score": 8,
    "climate_score": 7,
},
"VA_Scott": {
    "base_soil": "Sequoia-Muskingum Silt Loam (Class III, ridge and valley)",
    "drainage": "Well drained mountain valley, Clinch River watershed",
    "base_pasture_pct": 48,
    "nf_nearby": True, "nf_name": "Jefferson National Forest",
    "climate": "1,600–4,000 ft. Avg 53°F. ~48 in/yr. 165-day season.",
    "water_score": 8,
    "climate_score": 6,
},
"VA_Wise": {
    "base_soil": "Dekalb-Clymer Channery Silt Loam (Class III–IV, steep)",
    "drainage": "Well drained, high elevation plateau, coal country",
    "base_pasture_pct": 35,
    "nf_nearby": True, "nf_name": "Jefferson National Forest",
    "climate": "1,800–4,400 ft. Avg 51°F. ~49 in/yr. 155-day season.",
    "water_score": 7,
    "climate_score": 6,
},
"VA_Washington": {
    "base_soil": "Dunmore-Frederick Silt Loam (Class II, Great Valley limestone)",
    "drainage": "Well drained, Great Valley floor, excellent agricultural land",
    "base_pasture_pct": 65,
    "nf_nearby": True, "nf_name": "Jefferson National Forest / Mount Rogers NRA",
    "climate": "1,700–3,000 ft. Avg 54°F. ~43 in/yr. 175-day season.",
    "water_score": 8,
    "climate_score": 7,
},
"VA_Russell": {
    "base_soil": "Atkins-Monongalia Silt Loam (Class II–III, creek bottoms)",
    "drainage": "Well drained river valleys, Clinch/Copper Creek watershed",
    "base_pasture_pct": 55,
    "nf_nearby": True, "nf_name": "Jefferson National Forest",
    "climate": "1,500–3,200 ft. Avg 54°F. ~44 in/yr. 175-day season.",
    "water_score": 8,
    "climate_score": 7,
},
    "SC_Anderson": {
        "base_soil": "Cecil-Pacolet Sandy Clay Loam (Class II–III, piedmont)",
        "drainage": "Well drained, piedmont red clay, Lake Hartwell watershed",
        "base_pasture_pct": 50,
        "nf_nearby": False, "nf_name": "",
        "climate": "700–1,000 ft. Avg 60F. ~51 in/yr. 200-day season.",
        "water_score": 7,
        "climate_score": 8,
    },
    "SC_Greenville": {
        "base_soil": "Cecil-Pacolet Complex (Class II, upper piedmont)",
        "drainage": "Well drained, Reedy/Saluda River watershed",
        "base_pasture_pct": 48,
        "nf_nearby": True, "nf_name": "Sumter Nat'l Forest (nearby)",
        "climate": "800–1,400 ft. Avg 59F. ~54 in/yr. 200-day season.",
        "water_score": 8,
        "climate_score": 8,
    },
    "SC_Spartanburg": {
        "base_soil": "Madison-Pacolet Sandy Loam (Class II–III)",
        "drainage": "Well drained to moderately well drained, Broad River headwaters",
        "base_pasture_pct": 52,
        "nf_nearby": False, "nf_name": "",
        "climate": "700–1,100 ft. Avg 59F. ~52 in/yr. 200-day season.",
        "water_score": 7,
        "climate_score": 8,
    },
}  # end SOIL_DATA
def soil_col_data(lead):
    """Returns structured dict for the dedicated soil/pasture column."""
    key = f"{lead['state']}_{lead['county']}"
    d = SOIL_DATA.get(key, {})
    ac = lead["acres"]
    lu = (lead.get("lu_class") or "").lower()
    base = d.get("base_pasture_pct", 40)

    # Per-parcel pasture estimate
    if "pasture" in lu or "grass" in lu or "range" in lu:
        est = min(base + 15, 92)
    elif "timber" in lu or "forest" in lu:
        est = max(base - 20, 5)
    elif "agri" in lu or "farm" in lu or "crop" in lu:
        est = min(base + 10, 88)
    else:
        est = base
    lead["est_pasture_pct"] = est

    open_ac = round(ac * est / 100)
    wooded_ac = round(ac - open_ac)

    return {
        "soil": d.get("base_soil", "—"),
        "drainage": d.get("drainage", "—"),
        "open_ac": open_ac,
        "wooded_ac": wooded_ac,
        "pasture_pct": est,
        "climate": d.get("climate", "—"),
        "nf_nearby": d.get("nf_nearby", False),
        "nf_name": d.get("nf_name", ""),
        "water_score": d.get("water_score", 5),
        "climate_score": d.get("climate_score", 5),
    }

def soil_analysis_for_parcel(lead):
    """Legacy text field — now just a summary string."""
    s = soil_col_data(lead)
    parts = [
        f"Soil: {s['soil']}",
        f"Est. {s['pasture_pct']}% open ({s['open_ac']} ac) / {s['wooded_ac']} ac wooded",
        s['climate'],
    ]
    if s['nf_nearby']:
        parts.append(f"Near: {s['nf_name']}")
    return " | ".join(parts)

# ── Tennessee ─────────────────────────────────────────────────────────────────

# ── Tennessee ─────────────────────────────────────────────────────────────────
TN_STATEWIDE_URL = ("https://tnmap.tn.gov/arcgis/rest/services/ENVIRONMENTAL/"
                    "COMPTROLLER_OLG_LANDUSE/MapServer/2/query")

# LU_CLASSIFICATION values to EXCLUDE — non-land-investment parcel types
# TN Comptroller property class codes — NUMERIC
# These are confirmed from running --diagnose-tn
TN_EXCLUDE_LU_CODES = {
    "11",  # Single Family Residential
    "12",  # Duplex/Multi-family
    "13",  # Apartment/Condo
    "14",  # Mobile Home Park
    "15",  # Other Residential
    "21",  # Commercial/Retail/Office
    "22",  # Industrial/Warehouse
    "23",  # Utility
    "91",  # Exempt (church, charity)
    "92",  # Exempt (government, school)
    "93",  # Exempt other
}

def is_excluded_tn_lu(lu_class):
    """Return True if this TN numeric LU code is NOT a land investment target."""
    if not lu_class: return False
    code = str(lu_class).strip()
    return code in TN_EXCLUDE_LU_CODES

TN_COUNTY_IDS = {
    "Blount":"05","Hamilton":"33","Knox":"47","Madison":"57","Sevier":"78",
}

def fetch_tn_owner(gislink, county_id, verbose=False):
    """Fetch TN owner via TPAD API + HTML fallback. Called for all TN parcels."""
    if not gislink or not county_id:
        return ""
    try:
        # TPAD GIS lookup endpoint
        url = "https://assessment.cot.tn.gov/TPAD/Parcel/GIS"
        r = GET(url, {"gislink": gislink}, timeout=15, retries=2)
        if not r:
            return ""
        text = r.text

        # Try JSON response first (TPAD sometimes returns JSON)
        try:
            d = r.json()
            if isinstance(d, dict):
                for k in ("OwnerName","ownerName","owner_name","Owner","owner",
                          "TaxpayerName","PropertyOwner"):
                    v = d.get(k)
                    if v and str(v).strip():
                        return str(v).strip()
        except: pass

        # Parse HTML
        for pattern in [
            r'(?i)owner\s*name[^:]*:\s*<[^>]+>([^<]{3,60})<',
            r'(?i)<td[^>]*>owner[^<]*</td>\s*<td[^>]*>([^<]{3,60})</td>',
            r'(?i)id=["\']owner["\'][^>]*>([^<]{3,60})<',
            r'(?i)OwnerName["\s:]+([A-Z][A-Z &,.\-\']{2,50})',
        ]:
            m = re.search(pattern, text)
            if m:
                name = m.group(1).strip()
                if len(name) > 2 and not name.lower().startswith("owner"):
                    return name
    except Exception as e:
        if verbose: print(f"      [TPAD scrape error: {e}]")
    return ""


# LU codes most likely to be real land investment parcels
TN_LAND_LU_CODES = {'61','62','63','64','71','72','31','32','2','4','5','6','7'}

# Keywords that strongly suggest government/institutional ownership
TN_GOVT_ADDRESS_SIGNALS = [
    'state of tn','state of tenn','dept of','department of','tva ','tennessee valley',
    'us forest','usfs','national park','city of ','county of ','town of ',
    'school district','board of education','school board','habitat for','conservation district',
    'state park','wildlife management','corps of engineers','army corps',
]

def tn_land_score(lead):
    """
    Score a TN lead for likelihood of being a real private land opportunity.
    Higher = more likely genuine private land worth contacting.
    Used to select top-N per county before expensive owner lookups.
    """
    score = 0
    lu = str(lead.get('lu_class','') or '').strip()
    addr = (lead.get('address','') or '').lower()
    val = lead.get('appr_value', 0) or 0
    acres = lead.get('acres', 0) or 0

    # LU code signals
    if lu in TN_LAND_LU_CODES:
        score += 30
    elif lu == '':
        score += 5  # unknown — possible but uncertain

    # Address signals — govt addresses tank the score
    for sig in TN_GOVT_ADDRESS_SIGNALS:
        if sig in addr:
            score -= 60
            break

    # No address at all is suspicious
    if not addr:
        score -= 10

    # Appraised value > 0 means it's on the tax rolls as private
    if val > 0:
        score += 20
    # Very low value for large acreage can mean exempt/govt
    if acres > 0 and val > 0:
        vpa = val / acres
        if vpa < 50:   score -= 15  # suspiciously cheap
        elif vpa < 200: score += 5
        elif vpa < 2000: score += 15
        else: score += 8

    # Greenbelt codes are almost always private farm/forest
    if lu in ('61','62','63','64'):
        score += 15

    return score

def tn_top_n_per_county(leads, n=50):
    """
    From a list of TN leads, return the top-N per county by land score.
    Used to limit expensive TPAD owner lookups to most promising candidates.
    """
    from collections import defaultdict
    by_county = defaultdict(list)
    for lead in leads:
        if lead.get('state') == 'TN':
            lead['_tn_score'] = tn_land_score(lead)
            by_county[lead['county']].append(lead)

    result = []
    for county, county_leads in by_county.items():
        county_leads.sort(key=lambda l: l['_tn_score'], reverse=True)
        kept = county_leads[:n]
        dropped = len(county_leads) - len(kept)
        print(f"    TN/{county}: keeping top {len(kept)} of {len(county_leads)} "
              f"(dropped {dropped} low-score parcels)")
        result.extend(kept)
    return result

def enrich_tn_owners(leads, verbose=False):
    """
    Fetch owner for ALL TN parcels missing it via TPAD.
    Uses threads (8 workers) to run lookups concurrently.
    """
    tn_no_owner = [l for l in leads if l["state"] == "TN" and not l.get("owner")]
    if not tn_no_owner: return
    if verbose: print(f"  → Fetching TN owner data for {len(tn_no_owner)} parcels via TPAD (8 threads)...")
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    lock = threading.Lock()
    done = [0]

    def fetch_one(lead):
        gislink = lead.get("parcel_id","")
        cid = TN_COUNTY_IDS.get(lead["county"],"")
        owner, mail, sale = scrape_owner_tn(gislink)
        if not owner:
            owner = fetch_tn_owner(gislink, cid, False)
        with lock:
            if owner:
                lead["owner"] = owner
                if mail and not lead.get("mail_addr"): lead["mail_addr"] = mail
                if sale and not lead.get("sale_year"): lead["sale_year"] = sale
            done[0] += 1
            if verbose and done[0] % 50 == 0:
                pct = done[0] * 100 // len(tn_no_owner)
                print(f"      {done[0]}/{len(tn_no_owner)} ({pct}%) TN owners fetched...")
        sleep(0.05)  # light throttle per thread

    with ThreadPoolExecutor(max_workers=8) as ex:
        list(as_completed([ex.submit(fetch_one, l) for l in tn_no_owner]))

    got = sum(1 for l in tn_no_owner if l.get("owner"))
    if verbose: print(f"  → TN owners: {got}/{len(tn_no_owner)} found")


def scrape_tn(counties, min_acres, max_vpa, max_per, verbose):
    """
    Scrape TN statewide OLG_LANDUSE service with pagination.
    Fetches ALL pages of results (not just first 500), then filters
    by land use class. Orders by LU_ACRES DESC so largest parcels first.
    """
    leads, gaps = [], []
    PAGE_SIZE = 500  # ArcGIS max per request
    for county in counties:
        if verbose: print(f"  TN/{county}...", end=" ", flush=True)
        all_features = []
        offset = 0
        while True:
            data, err = jget(TN_STATEWIDE_URL, {
                # Exclude residential/commercial/exempt using numeric LU codes
                "where": (
                    f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres}"
                    " AND LU_CLASSIFICATION IN ('2','4','5','6','7','10','31','32','61','62','63','64','71','72')"
                ),
                "outFields": ("LU_ACRES,COUNTY,APPRAISAL,LANDVALUE,ADDRESS,CITY,"
                              "NUMBUILDINGS,GISLINK,LU_CLASSIFICATION"),
                "orderByFields": "LU_ACRES DESC",
                "resultRecordCount": PAGE_SIZE,
                "resultOffset": offset,
                "returnGeometry": "false", "f": "json",
            })
            if data is None:
                if offset == 0:
                    gaps.append(f"TN/{county} — {err}")
                    if verbose: print(f"✗ {err}")
                break
            page_feats = data.get("features", [])
            all_features.extend(page_feats)
            # If fewer than PAGE_SIZE returned, we have everything
            if len(page_feats) < PAGE_SIZE:
                break
            offset += PAGE_SIZE
            sleep(0.2)

        if verbose: print(f"  {len(all_features)} raw →", end=" ", flush=True)

        found = 0
        for feat in all_features:
            p = feat.get("attributes", {})
            acres = pick_num(p, ["LU_ACRES"] + ACRE_KEYS)
            if acres < min_acres: continue
            lu_raw = clean(p.get("LU_CLASSIFICATION") or "")
            if is_excluded_tn_lu(lu_raw): continue
            val = pick_num(p, ["APPRAISAL","LANDVALUE"] + VALUE_KEYS)
            vpa = round(val / acres, 0) if acres > 0 and val > 0 else 0
            if max_vpa > 0 and 0 < vpa > max_vpa: continue
            gislink = clean(p.get("GISLINK") or "")
            if not gislink: continue  # skip parcels with no GISLINK (can't get owner)
            leads.append({
                "state":"TN", "county":county,
                "parcel_id": gislink,
                "owner": "",
                "address": clean(p.get("ADDRESS") or ""),
                "city": clean(p.get("CITY") or "") or county,
                "acres": acres, "appr_value": val, "vpa": vpa,
                "mail_addr":"","mail_city":"","mail_state":"","mail_zip":"",
                "sale_year": "",
                "oos": False,
                "has_structure": int(p.get("NUMBUILDINGS") or 0) > 0,
                "lu_class": lu_raw,
                "source_name": f"{county} County TN — OLG_LANDUSE statewide",
                "lat": None, "lng": None, "elevation_ft": None,
                "elev_min": None, "elev_max": None, "geom_rings": None,
            })
            found += 1

        if found == 0:
            gaps.append(f"TN/{county} — 0 results after LU filter")
        if verbose: print(f"✓ {found} after LU filter")
        sleep(0.3)
    return leads, gaps


# ── South Carolina — confirmed REST endpoints ─────────────────────────────────
SC_COUNTIES_CFG = {
    "Spartanburg": {
        "city": "Spartanburg",
        "viewer_url": "https://www.spartanburgcounty.org/288/Assessor-Property-Records-Search",
        "direct_urls": [
            # CONFIRMED: Acreage field exists but WHERE Acreage>=X fails.
            # SHAPE.STArea()>=4356000 confirmed working (same as Anderson).
            # Fields: OwnerName, MAPNUMBER(parcelID), Acreage, CurrentAppraisedLandValue,
            #         SaleDate, StreetAddress, State, Zip, PropertyType, LandUse
            {"url": "https://maps.spartanburgcounty.org/server/rest/services/GIS/CAMA_Parcels/FeatureServer/0/query",
             "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "StreetAddress",
             "parcel_f": "MAPNUMBER", "value_f": "CurrentAppraisedLandValue", "sale_f": "SaleDate",
             "where_tpl": "SHAPE.STArea()>={sqft}"},
            {"url": "https://maps.spartanburgcounty.org/server/rest/services/GIS/CAMA_Parcels/MapServer/0/query",
             "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "StreetAddress",
             "parcel_f": "MAPNUMBER", "value_f": "CurrentAppraisedLandValue", "sale_f": "SaleDate",
             "where_tpl": "SHAPE.STArea()>={sqft}"},
        ],
        "probe_urls": [],
    },
    "Anderson": {
        "city": "Anderson",
        "viewer_url": "https://propertyviewer.andersoncountysc.org/mapsjs/",
        "direct_urls": [
            # lyr5: TAXOWNSTR=owner(nullable), PHYS_ADDR, MRKT_VALUE, SALE_YEAR, TMS, SHAPE.STArea()
            {"url": "https://propertyviewer.andersoncountysc.org/arcgis/rest/services/NewPropertyViewer/MapServer/5/query",
             "acre_f": None, "owner_f": "TAXOWNSTR", "addr_f": "PHYS_ADDR",
             "parcel_f": "TMS", "value_f": "MRKT_VALUE", "sale_f": "SALE_YEAR",
             "where_tpl": "SHAPE.STArea() >= {sqft}"},
        ],
        "probe_urls": [
            "https://propertyviewer.andersoncountysc.org/arcgis/rest/services/NewPropertyViewer/MapServer",
        ],
    },
    "Greenville": {
        "city": "Greenville",
        "viewer_url": "https://www.gcgis.org/apps/greenvillenj/",
        "direct_urls": [
            # lyr1/2: sales transaction layers — PURNAME=owner, PIN=parcel, LOTSIZE=acres, SALEPRICE, SALEDATE
            {"url": "https://www.gcgis.org/arcgis/rest/services/GreenvilleJS/Map_Layers_JS/MapServer/1/query",
             "acre_f": "LOTSIZE", "owner_f": "PURNAME", "addr_f": "STREET",
             "parcel_f": "PIN", "value_f": "SALEPRICE", "sale_f": "SALEDATE",
             "where_tpl": "LOTSIZE >= {min_ac}"},
            {"url": "https://www.gcgis.org/arcgis/rest/services/GreenvilleJS/Map_Layers_JS/MapServer/2/query",
             "acre_f": "LOTSIZE", "owner_f": "PURNAME", "addr_f": "STREET",
             "parcel_f": "PIN", "value_f": "SALEPRICE", "sale_f": "SALEDATE",
             "where_tpl": "LOTSIZE >= {min_ac}"},
        ],
        "probe_urls": [
            "https://www.gcgis.org/arcgis/rest/services/GreenvilleJS/Map_Layers_JS/MapServer",
            "https://www.gcgis.org/arcgis/rest/services/RealProperty/MapServer",
        ],
    },
    "Oconee": {
        "city": "Walhalla",
        "viewer_url": "https://data-oconeesc.opendata.arcgis.com/",
        "direct_urls": [
            {
                # Oconee County ArcGIS Online public parcel FeatureServer
                "url": "https://services1.arcgis.com/ACpN9sxwFvuFNGEB/arcgis/rest/services/Parcels/FeatureServer/0/query",
                "where": "Acreage >= {min_acres}",
                "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "SiteAddress",
                "parcel_f": "ParcelID", "value_f": "TotalValue", "sale_f": "SaleDate",
            },
            {
                # Fallback: opendata hub
                "url": "https://opendata-ocga-gis.hub.arcgis.com/datasets/OCGA-GIS::parcels/FeatureServer/0/query",
                "where": "Acreage >= {min_acres}",
                "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "SiteAddress",
                "parcel_f": "ParcelID", "value_f": "TotalValue", "sale_f": None,
            },
        ],
        "probe_urls": [
            "https://gis.oconeesc.com/arcgis/rest/services/Public/MapServer",
            "https://gis.oconeesc.com/arcgis/rest/services/Parcels/MapServer",
            "https://services1.arcgis.com/ACpN9sxwFvuFNGEB/arcgis/rest/services/Parcels/FeatureServer",
        ],
    },
    "Pickens": {
        "city": "Pickens",
        "viewer_url": "https://pcgis-pickenscosc.opendata.arcgis.com/",
        "direct_urls": [
            {
                # Pickens County ArcGIS Online public parcel FeatureServer
                "url": "https://services.arcgis.com/2Ef1pNFLcLyG1Lhd/arcgis/rest/services/Pickens_Parcels/FeatureServer/0/query",
                "where": "Acreage >= {min_acres}",
                "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "SiteAddress",
                "parcel_f": "ParcelID", "value_f": "TotalValue", "sale_f": "SaleDate",
            },
            {
                "url": "https://services.arcgis.com/2Ef1pNFLcLyG1Lhd/arcgis/rest/services/Pickens_Parcels/MapServer/0/query",
                "where": "Acreage >= {min_acres}",
                "acre_f": "Acreage", "owner_f": "OwnerName", "addr_f": "SiteAddress",
                "parcel_f": "ParcelID", "value_f": "TotalValue", "sale_f": None,
            },
        ],
        "probe_urls": [
            "https://services.arcgis.com/2Ef1pNFLcLyG1Lhd/arcgis/rest/services/Pickens_Parcels/FeatureServer",
            "https://gis.co.pickens.sc.us/arcgis/rest/services/Parcels/MapServer",
        ],
    },
}

def _sc_make_lead(p, county, city, source, min_acres, max_vpa,
                  acre_f=None, owner_f=None, addr_f=None, parcel_f=None,
                  value_f=None, sale_f=None, geometry=None):
    acres = pick_num(p, ([acre_f] if acre_f else []) + ACRE_KEYS)
    if acres == 0:
        st = flt(p.get("Shape.STArea()") or p.get("SHAPE.STArea()") or
                 p.get("SHAPE.STAREA()") or p.get("shape.starea()") or 0)
        if st > 0: acres = round(st / 43560.0, 2)
    if acres < min_acres: return None
    val = pick_num(p, ([value_f] if value_f else []) + VALUE_KEYS)
    vpa = round(val/acres, 0) if acres > 0 and val > 0 else 0
    if max_vpa > 0 and 0 < vpa > max_vpa: return None
    owner = (clean(p.get(owner_f, "")) if owner_f else "") or pick(p, OWNER_KEYS)
    if is_government(owner): return None
    ms = pick(p, MAIL_STATE)
    oos = bool(ms) and ms.upper() not in ("SC", "")
    parcel = (clean(p.get(parcel_f, "")) if parcel_f else "") or pick(p, PARCEL_K)
    sale_raw = (p.get(sale_f) if sale_f and p.get(sale_f) is not None else "") or pick(p, SALE_YR_K)
    return {
        "state":"SC","county":county,"parcel_id":parcel,"owner":owner,
        "address":(clean(p.get(addr_f,"")) if addr_f else "") or pick(p,ADDR_KEYS),
        "city":city,"acres":acres,"appr_value":val,"vpa":vpa,
        "mail_addr":pick(p,MAIL_ADDR),"mail_city":pick(p,MAIL_CITY),
        "mail_state":ms,"mail_zip":pick(p,MAIL_ZIP),
        "sale_year":parse_sale_year(sale_raw),
        "oos":oos,"has_structure":False,
        "lu_class":pick(p,["LANDUSE","landuse","land_use","lu_class","useclass"]),
        "source_name":source,
        "lat": polygon_centroid_wgs84(geometry)[0] if geometry else None,
        "lng": polygon_centroid_wgs84(geometry)[1] if geometry else None,
        "elevation_ft":None,"elev_min":None,"elev_max":None,
        "geom_rings": (geometry or {}).get("rings"),
        "streams": None, "waterbodies": None,
    }

def _sc_probe_service(base_url, county, city, min_acres, max_per, max_vpa):
    svc, _ = jget(base_url, {"f":"json"}, timeout=12)
    if not svc: return []
    layers = svc.get("layers",[]) or [{"id":0,"name":"parcels","type":"Feature Layer"}]
    def pri(l):
        n=(l.get("name") or "").lower()
        return 0 if "parcel" in n and "line" not in n and "anno" not in n else 9
    for lyr in sorted([l for l in layers if l.get("type","").startswith("Feature")],key=pri)[:6]:
        lid=lyr["id"]; url=f"{base_url}/{lid}/query"
        probe,_=jget(url,{"where":"1=1","outFields":"*","resultRecordCount":3,"returnGeometry":"false","f":"json"},timeout=10)
        if not probe: continue
        pf=probe.get("features",[])
        if not pf or pick_num(pf[0].get("attributes",{}),ACRE_KEYS)==0: continue
        data,_=jget(url,{"where":"1=1","outFields":"*","resultRecordCount":max_per,"returnGeometry":"false","f":"json"},timeout=20)
        if not data: continue
        leads=[l for l in [_sc_make_lead(f["attributes"],county,city,
               f"{county} SC lyr{lid}",min_acres,max_vpa) for f in data.get("features",[])] if l]
        if leads: return leads
        sleep(0.2)
    return []


# ── Virginia county profiles ─────────────────────────────────────────────────
VA_COUNTY_PROFILES = {
    # All SW Virginia counties use interactivegis.com — confirmed host for Washington.
    # Probe multiple URL paths per county since exact service names vary.
    # outFields=* lets pick() find whatever field names each county uses.
    "Lee":        {"city": "Jonesville",  "fips": "51105",
                   "gis_urls": [
                       "https://leecova.interactivegis.com/arcgis/rest/services/LeeCounty/Parcels/MapServer/0/query",
                       "https://leecova.interactivegis.com/arcgis/rest/services/Lee/Parcels/MapServer/0/query",
                       "https://leecova.interactivegis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                   ], "gis_where": "ACREAGE>={min_acres}", "gis_fields": "*"},
    "Scott":      {"city": "Gate City",   "fips": "51169",
                   "gis_urls": [
                       "https://scottcova.interactivegis.com/arcgis/rest/services/ScottCounty/Parcels/MapServer/0/query",
                       "https://scottcova.interactivegis.com/arcgis/rest/services/Scott/Parcels/MapServer/0/query",
                       "https://scottcova.interactivegis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                   ], "gis_where": "ACREAGE>={min_acres}", "gis_fields": "*"},
    "Wise":       {"city": "Wise",        "fips": "51195",
                   "gis_urls": [
                       "https://wisecova.interactivegis.com/arcgis/rest/services/WiseCounty/Parcels/MapServer/0/query",
                       "https://wisecova.interactivegis.com/arcgis/rest/services/Wise/Parcels/MapServer/0/query",
                       "https://wisecova.interactivegis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                   ], "gis_where": "ACREAGE>={min_acres}", "gis_fields": "*"},
    "Washington": {"city": "Abingdon",    "fips": "51191",
                   "gis_urls": [
                       "https://washcova.interactivegis.com/arcgis/rest/services/WashingtonCounty/Parcels/MapServer/0/query",
                   ], "gis_where": "ACREAGE>={min_acres}", "gis_fields": "*"},
    "Russell":    {"city": "Lebanon",     "fips": "51167",
                   "gis_urls": [
                       "https://russellcova.interactivegis.com/arcgis/rest/services/RussellCounty/Parcels/MapServer/0/query",
                       "https://russellcova.interactivegis.com/arcgis/rest/services/Russell/Parcels/MapServer/0/query",
                       "https://russellcova.interactivegis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                   ], "gis_where": "ACREAGE>={min_acres}", "gis_fields": "*"},
}

VA_STATEWIDE_FS = "https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/MapServer/0/query"
# DCR VFRIS: has owner (DESC1/DESC2), acreage, land value — county-specific layers
VA_DCR_BASE = "https://consappsrpt.dcr.virginia.gov/arcgis/rest/services/VFRIS/Parcels/MapServer"
VA_STATEWIDE_MS = "https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/MapServer/0/query"

def _build_va_lead(p, county, city, min_acres, max_vpa, **kwargs):
    # VGIN: no Acreage field — compute from polygon geometry (passed as kwarg)
    geom_arg = kwargs.get("geometry") if kwargs else None
    if geom_arg:
        ct = polygon_centroid_wgs84(geom_arg)
        # Estimate area from bounding box as rough acreage filter
        rings = (geom_arg.get("rings") or [])
        if rings:
            lats = [pt[1] for ring in rings for pt in ring]
            lngs = [pt[0] for ring in rings for pt in ring]
            lat_span = (max(lats) - min(lats)) * 111320
            lng_span = (max(lngs) - min(lngs)) * 111320 * _math.cos(_math.radians(sum(lats)/len(lats)))
            acres = round(lat_span * lng_span / 4046.86, 1)
        else:
            acres = 0
    else:
        acres = 0
    if acres < min_acres:
        return None
    val = pick_num(p, VALUE_KEYS + ["totalvalue","landvalue","improvvalue"])
    vpa = round(val / acres, 0) if acres > 0 and val > 0 else 0
    if max_vpa > 0 and 0 < vpa > max_vpa:
        return None
    owner = pick(p, OWNER_KEYS + ["ownname1","owner1","taxpayer_name"])
    if is_government(owner):
        return None
    ms = pick(p, MAIL_STATE)
    oos = bool(ms) and ms.upper() not in ("VA", "")
    parcel = pick(p, ["PARCELID","parcelid","VGIN_QPID"] + PARCEL_K)
    return {
        "state": "VA", "county": county,
        "parcel_id": parcel,
        "owner": owner,
        "address": pick(p, ADDR_KEYS),
        "city": city,
        "acres": acres, "appr_value": val, "vpa": vpa,
        "mail_addr": pick(p, MAIL_ADDR),
        "mail_city": pick(p, MAIL_CITY),
        "mail_state": ms,
        "mail_zip": pick(p, MAIL_ZIP),
        "sale_year": parse_sale_year(pick(p, SALE_YR_K)),
        "oos": oos,
        "has_structure": False,
        "lu_class": pick(p, ["landuse","lu_code","property_class","use_code"]),
        "source_name": f"{county} County VA",
        "lat": None, "lng": None, "elevation_ft": None,
        "elev_min": None, "elev_max": None, "geom_rings": None,
        "streams": None, "waterbodies": None,
    }


def _scrape_va_dcr(county, city, min_acres, max_vpa, max_per, verbose=False):
    """VA DCR VFRIS disabled — service is slow and returns no owner data.
    VA relies on VGIN geometry + county GIS fallback only."""
    return []

def scrape_va(target_counties, min_acres, max_vpa, max_per, verbose):
    """
    Scrape VA counties using VGIN statewide parcel FeatureServer.
    Tries statewide service first, then county-specific fallbacks.
    """
    all_leads, gaps = [], []
    sqft_min = min_acres * 43560

    for county in target_counties:
        cfg = VA_COUNTY_PROFILES.get(county)
        if not cfg:
            gaps.append(f"VA {county}: no profile")
            continue
        city = cfg["city"]
        if verbose: print(f"  VA/{county} ({city})...", end=" ", flush=True)

        leads = []

        # Try statewide VGIN FeatureServer
        # Try multiple where clauses — UPPER() not always supported
        # CONFIRMED: VGIN VA_Parcels MapServer
        # County field: LOCALITY (e.g. "Lee County")
        # Area field: SHAPE.STArea() in sq ft (1 acre = 43560)
        # No owner/value/mail — geometry+parcel ID only
        sqft_min = int(min_acres * 43560)
        # MapServer does NOT support SHAPE.STArea() in WHERE clause.
        # Filter by LOCALITY only; compute acreage from polygon geometry in Python.
        where_options = [
            f"LOCALITY='{county} County'",
            f"LOCALITY='{county.upper()} COUNTY'",
            # No 1=1 fallback — would return random VA parcels from any county
        ]
        params = {
            "where": where_options[0],
            # CONFIRMED fields: PARCELID, LOCALITY, FIPS, VGIN_QPID
            # No owner/value — parcel skeleton only. Geometry for acreage calc.
            "outFields": "PARCELID,LOCALITY,FIPS,VGIN_QPID",
            "resultRecordCount": max_per,
            "returnGeometry": "true",
            "outSR": "4326",
            "geometryPrecision": "5",
            "resultRecordCount": max_per,
            "f": "json",
        }
        data, err = None, None
        for where_opt in where_options:
            params["where"] = where_opt
            data, err = jget(VA_STATEWIDE_FS, params, timeout=10)
            if data and data.get("features"): break
            if data and "error" in str(data).lower():
                sleep(0.2); continue
        if data and data.get("features"):
            for feat in data["features"]:
                p = feat.get("attributes", {})
                geom = feat.get("geometry")
                lead = _build_va_lead(p, county, city, min_acres, max_vpa, geometry=geom)
                if lead:
                    if geom:
                        ct = polygon_centroid_wgs84(geom)
                        if ct[0]: lead["lat"], lead["lng"] = ct[0], ct[1]
                    leads.append(lead)

        if not leads:
            # Try county GIS services (has owner + acreage + value)
            gis_urls = cfg.get("gis_urls") or ([cfg["gis_url"]] if cfg.get("gis_url") else [])
            gis_where = cfg.get("gis_where", "")
            gis_fields = cfg.get("gis_fields", "*")
            for gis_url in gis_urls:
                gis_data, gis_err = jget(gis_url, {
                    "where": gis_where.format(min_acres=min_acres),
                    "outFields": gis_fields,
                    "resultRecordCount": min(200, max_per),
                    "returnGeometry": "true", "outSR": "4326",
                    "f": "json",
                }, timeout=12)
                if gis_data and gis_data.get("features"):
                    for feat in gis_data["features"]:
                        p = feat.get("attributes", {})
                        acres = flt(pick_num(p, ACRE_KEYS + ["ACREAGE","Acreage","TotalAcres"]))
                        if acres < min_acres: continue
                        val = flt(pick_num(p, VALUE_KEYS + ["TOTALVALUE","LANDVALUE","TotalValue"]))
                        vpa = round(val/acres) if acres and val else 0
                        owner = clean(pick(p, OWNER_KEYS + ["OWNERNAME","OwnerName","OWNER","Owner"]))
                        if is_government(owner): continue
                        pid = str(pick(p, PARCEL_K + ["PARCELID","ParcelID"]) or "")
                        ms = (pick(p, MAIL_STATE + ["MAILINGSTATE","MailState"]) or "").upper().strip()
                        geom = feat.get("geometry")
                        lat, lng = None, None
                        if geom:
                            ct = polygon_centroid_wgs84(geom) if geom.get("rings") else (geom.get("y"), geom.get("x"))
                            if ct and ct[0]: lat, lng = ct[0], ct[1]
                        sale_raw = str(pick(p, SALE_YR_K + ["SALEDATE","SaleDate"]) or "")
                        leads.append({
                            "state":"VA","county":county,"parcel_id":pid,
                            "owner":owner,"address":clean(p.get("PHYSADDR") or ""),
                            "city":city,"acres":acres,"appr_value":int(val),
                            "vpa":int(vpa),"mail_addr":"","mail_city":"",
                            "mail_state":ms,"mail_zip":"",
                            "sale_year":parse_sale_year(sale_raw),
                            "oos":bool(ms) and ms not in ("VA",""),
                            "has_structure":flt(p.get("TOTALVALUE",0))>flt(p.get("LANDVALUE",0)),
                            "lu_class":"","source_name":f"{county} County VA — county GIS",
                            "lat":lat,"lng":lng,"elevation_ft":None,
                            "elev_min":None,"elev_max":None,"geom_rings":None,
                        })
        if not leads:
            gaps.append(f"VA {county}: no data from VGIN or county GIS")

        if leads:
            if verbose: print(f"✓ {len(leads)}")
            all_leads.extend(leads)
        else:
            gaps.append(f"VA {county}: no accessible GIS service (try county assessor directly)")
            if verbose: print("✗ no data")
        sleep(0.5)

    return all_leads, gaps


# ── Georgia county profiles ──────────────────────────────────────────────────
GA_COUNTY_PROFILES = {
    "Fannin":  {"city": "Blue Ridge",  "fips": "13111",
                "gis_urls": [
                    "https://services1.arcgis.com/eFKqf8uANXd0jT6D/arcgis/rest/services/Parcels/FeatureServer/0/query",
                    "https://fannincountyga.maps.arcgis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                ]},
    "Gilmer":  {"city": "Ellijay",    "fips": "13123",
                "gis_urls": [
                    "https://services.arcgis.com/NN0eWPnkJYBwOCgf/arcgis/rest/services/Parcel_View/FeatureServer/0/query",
                    "https://gilmercountyga.maps.arcgis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                ]},
    "Union":   {"city": "Blairsville", "fips": "13291",
                "gis_urls": [
                    "https://services.arcgis.com/eDMNiKzGPV9rGxT1/arcgis/rest/services/Parcels/FeatureServer/0/query",
                    "https://unioncountyga.maps.arcgis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                ]},
    "Towns":   {"city": "Hiawassee",   "fips": "13281",
                "gis_urls": [
                    "https://services.arcgis.com/eFKqf8uANXd0jT6D/arcgis/rest/services/Towns_Parcels/FeatureServer/0/query",
                    "https://townscountyga.maps.arcgis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                ]},
    "Lumpkin": {"city": "Dahlonega",   "fips": "13187",
                "gis_urls": [
                    "https://services.arcgis.com/sEjGKTa5pnX5BFXJ/arcgis/rest/services/Parcels/FeatureServer/0/query",
                    "https://lumpkincountyga.maps.arcgis.com/arcgis/rest/services/Parcels/MapServer/0/query",
                ]},
}

def scrape_ga(target_counties, min_acres, max_vpa, max_per, verbose):
    """Scrape North Georgia counties via ArcGIS parcel services."""
    all_leads, gaps = [], []
    for county in target_counties:
        cfg = GA_COUNTY_PROFILES.get(county)
        if not cfg:
            gaps.append(f"GA {county}: no profile"); continue
        city = cfg["city"]
        if verbose: print(f"  GA/{county}...", end=" ", flush=True)
        leads = []
        for url in cfg.get("gis_urls", []):
            # Try SHAPE.STArea() style where first, then Acreage
            for where in [f"Acreage>={min_acres}", f"ACREAGE>={min_acres}",
                          f"SHAPE.STArea()>={int(min_acres*43560)}"]:
                data, err = jget(url, {
                    "where": where, "outFields": "*",
                    "resultRecordCount": min(200, max_per),
                    "returnGeometry": "true", "outSR": "4326",
                    "geometryPrecision": "5", "f": "json",
                }, timeout=12)
                if data and data.get("features"):
                    for feat in data["features"]:
                        p = feat.get("attributes", {})
                        acres = flt(pick_num(p, ACRE_KEYS + ["Acreage","ACREAGE","TotalAcres"]))
                        if acres < min_acres: continue
                        val = flt(pick_num(p, VALUE_KEYS + ["TotalValue","TOTALVALUE","AssessedValue"]))
                        vpa = round(val/acres) if acres and val else 0
                        if max_vpa > 0 and 0 < vpa > max_vpa: continue
                        owner = clean(pick(p, OWNER_KEYS + ["OwnerName","OWNERNAME","Owner"]))
                        if is_government(owner): continue
                        pid = str(pick(p, PARCEL_K + ["ParcelID","PARCELID","Parcel_ID"]) or "")
                        ms = (pick(p, MAIL_STATE + ["MailState","MAILSTATE"]) or "").upper().strip()
                        geom = feat.get("geometry")
                        lat, lng = None, None
                        if geom:
                            ct = polygon_centroid_wgs84(geom) if geom.get("rings")                                  else (geom.get("y"), geom.get("x"))
                            if ct and ct[0]: lat, lng = ct[0], ct[1]
                        leads.append({
                            "state":"GA","county":county,"parcel_id":pid,
                            "owner":owner,"address":clean(pick(p,ADDR_KEYS+["SiteAddress","SITEADDRESS"])),
                            "city":city,"acres":acres,"appr_value":int(val),"vpa":int(vpa),
                            "mail_addr":"","mail_city":"","mail_state":ms,"mail_zip":"",
                            "sale_year":parse_sale_year(pick(p,SALE_YR_K+["SaleDate","SALEDATE"])),
                            "oos":bool(ms) and ms not in ("GA",""),
                            "has_structure":flt(pick_num(p,["ImpValue","IMPVALUE","ImprovValue"]))>0,
                            "lu_class":pick(p,["LandUse","LANDUSE","UseCode"]),
                            "source_name":f"{county} County GA",
                            "lat":lat,"lng":lng,"elevation_ft":None,
                            "elev_min":None,"elev_max":None,"geom_rings":None,
                        })
                    if leads: break
                sleep(0.1)
            if leads: break
            sleep(0.3)
        if leads:
            all_leads.extend(leads)
            if verbose: print(f"✓ {len(leads)}")
        else:
            gaps.append(f"GA/{county} — no accessible GIS service")
            if verbose: print("✗ no results")
        sleep(0.3)
    return all_leads, gaps

def scrape_sc(target_counties, min_acres, max_vpa, max_per, verbose):
    all_leads, gaps = [], []
    for county in target_counties:
        cfg = SC_COUNTIES_CFG.get(county)
        if not cfg:
            gaps.append(f"SC/{county} — not in config"); continue
        if verbose: print(f"  SC/{county}...", end=" ", flush=True)
        leads = []
        for d in cfg.get("direct_urls", []):
            where_clause = d.get("where_tpl", "1=1").format(
                sqft=int(min_acres * 43560),
                min_ac=min_acres)
            q_params = {"where": where_clause, "outFields": "*",
                        "resultRecordCount": max_per,
                        "returnGeometry": "true", "outSR": "4326",
                        "geometryPrecision": "5",
                        "f": "json"}
            if d.get("order_by"):
                q_params["orderByFields"] = d["order_by"]
            data, _ = jget(d["url"], q_params, timeout=15)
            if not data: continue
            leads = [l for l in [_sc_make_lead(
                f["attributes"], county, cfg["city"],
                f"{county} SC direct", min_acres, max_vpa,
                d["acre_f"], d["owner_f"], d["addr_f"],
                d["parcel_f"], d["value_f"], d["sale_f"],
                geometry=f.get("geometry"))
                for f in data.get("features", [])] if l]
            if leads: break
            sleep(0.3)
        if not leads:
            for url in cfg.get("probe_urls", []):
                leads = _sc_probe_service(url, county, cfg["city"], min_acres, max_per, max_vpa)
                if leads: break
                sleep(0.3)
        if leads:
            all_leads.extend(leads)
            if verbose: print(f"✓ {len(leads)}")
        else:
            gaps.append(f"SC/{county} — no queryable service found")
            if verbose: print("✗ no results")
        sleep(0.3)
    return all_leads, gaps


# ── North Carolina — confirmed statewide REST service ─────────────────────────
NC_URL_FS = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/FeatureServer/1/query"
NC_URL_MS = "https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1/query"
NC_CHEROKEE_DIRECT = "https://maps.cherokeecounty-nc.gov/ccgis/rest/services/LandRecords/MapServer/0/query"
# Confirmed fields from live service — note: ownname NOT owner
NC_FIELDS = ("cntyname,gisacres,recareano,ownname,parval,mailadd,mcity,mstate,"
             "mzip,siteadd,scity,saledatetx,struct,parno")
NC_COUNTY_NAMES = {
    "Cherokee": ["Cherokee","CHEROKEE"],
    "Clay":     ["Clay","CLAY"],
    "Graham":   ["Graham","GRAHAM"],
    "Haywood":  ["Haywood","HAYWOOD"],
    "Jackson":  ["Jackson","JACKSON"],
    "Macon":    ["Macon","MACON"],
    "Madison":  ["Madison","MADISON"],
    "Watauga":   ["Watauga","WATAUGA"],
    "Avery":     ["Avery","AVERY"],
    "Ashe":      ["Ashe","ASHE"],
    "Alleghany": ["Alleghany","ALLEGHANY"],
    "Mitchell": ["Mitchell","MITCHELL"],
    "Swain":    ["Swain","SWAIN"],
    "Yancey":   ["Yancey","YANCEY"],
}


def _parse_nc(feats, county, min_acres, max_vpa, source):
    out = []
    for feat in feats:
        p = feat.get("attributes", {})
        _geom = feat.get("geometry")
        _lat, _lng = polygon_centroid_wgs84(_geom) if _geom else (None, None)
        _rings = (_geom or {}).get("rings") if _geom else None
        # BUG FIX: use gisacres (reliable) — recareano can be null
        acres = pick_num(p, ["gisacres","recareano"] + ACRE_KEYS)
        if acres < min_acres: continue
        val = pick_num(p, ["parval"] + VALUE_KEYS)
        vpa = round(val / acres, 0) if acres > 0 and val > 0 else 0
        if max_vpa > 0 and 0 < vpa > max_vpa: continue
        # BUG FIX: field is ownname not owner
        owner = pick(p, ["ownname"] + OWNER_KEYS)
        if is_government(owner): continue
        ms = pick(p, ["mstate"] + MAIL_STATE)
        oos = bool(ms) and ms.upper() not in ("NC","")
        struct_raw = str(p.get("struct") or "0").lower().strip()
        has_struct = struct_raw not in ("0","","null","no","false","none","<null>")
        out.append({
            "state":"NC","county":county,
            "parcel_id": format_nc_pin(pick(p, ["parno"] + PARCEL_K)),
            "owner": owner,
            "address": pick(p, ["siteadd"] + ADDR_KEYS),
            "city": pick(p, ["scity","sitecity"]) or county,
            "acres": acres, "appr_value": val, "vpa": vpa,
            "mail_addr": pick(p, ["mailadd"] + MAIL_ADDR),
            "mail_city": pick(p, ["mcity"] + MAIL_CITY),
            "mail_state": ms,
            "mail_zip": pick(p, ["mzip"] + MAIL_ZIP),
            "sale_year": parse_sale_year(pick(p, ["saledatetx"] + SALE_YR_K)),
            "oos": oos,
            "has_structure": has_struct,
            "lu_class": "",
            "source_name": source,
            "lat": _lat, "lng": _lng, "elevation_ft": None,
            "elev_min": None, "elev_max": None, "geom_rings": _rings,
        })
    return out

def scrape_nc(counties, min_acres, max_vpa, max_per, verbose):
    leads, gaps = [], []
    for county in counties:
        if verbose: print(f"  NC/{county}...", end=" ", flush=True)
        county_leads = []

        for url in [NC_URL_FS, NC_URL_MS]:
            for name_fmt in NC_COUNTY_NAMES.get(county, [county]):
                # BUG FIX: orderByFields=gisacres DESC ensures largest parcels come first
                d, err = jget(url, {
                    "where": f"cntyname='{name_fmt}'",
                    "outFields": NC_FIELDS,
                    "orderByFields": "gisacres DESC",
                    "resultRecordCount": max_per,
                    "returnGeometry": "true", "outSR": "4326",
                    "geometryPrecision": "5",
                    "f": "json",
                })
                if d is None: continue
                feats = d.get("features")
                if feats is None: continue
                parsed = _parse_nc(feats, county, min_acres, max_vpa,
                                   f"{county} County NC — NC1Map")
                if parsed:
                    county_leads = parsed
                    break
            if county_leads: break
            sleep(0.4)

        # Cherokee county-direct fallback
        if not county_leads and county == "Cherokee":
            d2, _ = jget(NC_CHEROKEE_DIRECT, {
                "where": "1=1", "outFields": "*",
                "resultRecordCount": max_per,
                "returnGeometry": "true", "outSR": "4326",
                "geometryPrecision": "5", "f": "json",
            })
            if d2 and d2.get("features"):
                parsed2 = _parse_nc(d2["features"], county, min_acres, max_vpa,
                                    "Cherokee County NC — County GIS")
                if parsed2: county_leads = parsed2

        if not county_leads:
            gaps.append(f"NC/{county} — no qualifying parcels >= {min_acres} ac")
            if verbose: print("✗ no results")
        else:
            leads.extend(county_leads)
            if verbose: print(f"✓ {len(county_leads)}")
        sleep(0.3)
    return leads, gaps

# ── Alabama — KCS GAMAWeb ──────────────────────────────────────────────────────
# CONFIRMED endpoints found through live web research:
#
#  Madison (AL47): web3.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer
#                  Layer 141 "Parcel View"
#                  Fields: Acres, PropertyOwner, MailingAddress, TotalAppraisedValue,
#                          DeedDate, PIN, PropertyAddress
#
#  Marshall (AL50): web2.kcsgis.com/kcsgis/rest/services/Marshall/AL50_VAM_MS/MapServer
#                   Layer 9 "Parcels" — fields discovered with outFields=*
#
#  DeKalb (AL28): Same GAMAWeb pattern → DeKalb/AL28_GAMAWeb on web3/web5/web2
#  Jackson (AL49): Same GAMAWeb pattern → Jackson/AL49_GAMAWeb on web3/web5/web2
#
# All GAMAWeb services have a "Parcel View" layer with identical field schema.
# Field normalization handles both confirmed fields and any variations.


def _build_al_lead(p, county, city, assess_url, source, min_acres, max_vpa):
    """Parse a single AL GIS feature attributes dict into a lead dict."""
    acres = pick_num(p, ACRE_KEYS)
    if acres < min_acres:
        return None
    val = pick_num(p, VALUE_KEYS)
    vpa = round(val / acres, 0) if acres > 0 and val > 0 else 0
    if max_vpa > 0 and 0 < vpa > max_vpa:
        return None
    owner = pick(p, OWNER_KEYS)
    if is_government(owner):
        return None
    ms = pick(p, MAIL_STATE)
    oos = bool(ms) and ms.upper() not in ("AL", "")
    parcel = pick(p, PARCEL_K)
    return {
        "state": "AL", "county": county,
        "parcel_id": parcel,
        "owner": owner,
        "address": pick(p, ADDR_KEYS),
        "city": city,
        "acres": acres, "appr_value": val, "vpa": vpa,
        "mail_addr": pick(p, MAIL_ADDR),
        "mail_city": pick(p, MAIL_CITY),
        "mail_state": ms,
        "mail_zip": pick(p, MAIL_ZIP),
        "sale_year": parse_sale_year(pick(p, SALE_YR_K)),
        "oos": oos,
        "has_structure": False,
        "lu_class": pick(p, ["landuse","land_use","lu_class","luc",
                              "propertyuse","property_use","useclass"]),
        "source_name": source,
        "lat": None, "lng": None, "elevation_ft": None,
    }


def _probe_gama_service(base_url, county, city, assess_url, min_acres, max_per, max_vpa, verbose, use_kcs=True):
    """
    Probe a KCS GAMAWeb service: list layers, find parcel layer, query it.
    Looks for 'Parcel View' or 'Parcels' layer with acres/owner fields.
    """
    # Get layer listing
    svc_data, err = jget(base_url, {"f":"json"}, timeout=15, kcs=use_kcs)
    if svc_data is None:
        if verbose: print(f"      [{base_url.split('/')[2][:25]}] ✗ {err}")
        return [], None  # service not up
    layers = svc_data.get("layers", [])
    if not layers:
        return [], None

    # Priority: "Parcel View" (GAMAWeb) > "Parcels" > any polygon layer
    def layer_priority(lyr):
        name = (lyr.get("name") or "").lower()
        if "parcel view" in name: return 0
        if name == "parcels": return 1
        if "parcel" in name and "anno" not in name and "line" not in name: return 2
        return 99

    candidates = sorted(
        [l for l in layers if l.get("type") == "Feature Layer"],
        key=layer_priority
    )

    for lyr_info in candidates[:8]:
        lid = lyr_info["id"]
        url = f"{base_url}/{lid}/query"

        # Quick probe to check fields
        probe, _ = jget(url, {
            "where": "1=1", "outFields": "*",
            "resultRecordCount": 3, "returnGeometry": "true", "outSR": "4326", "f": "json",
        }, timeout=12, kcs=use_kcs)
        if probe is None: continue
        pfeats = probe.get("features", [])
        if not pfeats: continue
        sample = pfeats[0].get("attributes", {})

        # Verify this is a parcel layer
        has_acres = pick_num(sample, ACRE_KEYS) > 0
        has_owner_or_value = bool(pick(sample, OWNER_KEYS)) or pick_num(sample, VALUE_KEYS) > 0
        if not has_acres and not has_owner_or_value:
            continue  # not a parcel layer

        # Full query
        data, _ = jget(url, {
            "where": "1=1", "outFields": "*",
            "resultRecordCount": max_per, "returnGeometry": "false", "f": "json",
        }, timeout=25, kcs=use_kcs)
        if data is None: continue
        feats = data.get("features", [])
        if not feats: continue

        leads = []
        for feat in feats:
            p = feat.get("attributes", {})
            lead = _build_al_lead(p, county, city, assess_url,
                                  f"{county} County AL — {base_url.split('/')[2][:20]} lyr{lid}",
                                  min_acres, max_vpa)
            if lead: leads.append(lead)

        if leads:
            if verbose: print(f"[{lyr_info.get('name','?')} lyr{lid} @ {base_url.split('/')[2][:16]}]")
            return leads, None
        elif verbose:
            # Layer found but all parcels < min_acres — might just be small county
            pass
        sleep(0.2)

    return [], None  # no useful layer found


# All 4 AL counties use the same probe strategy.
# GAMAWeb (AL##_GAMAWeb) and Public services tried across web2-web5.
# Layer 141 "Parcel View" fields confirmed for Madison; others use probe.
AL_ALL_COUNTIES = {
    "Madison": {
        "city": "Huntsville",
        "assess_url": "https://isv.kcsgis.com/al.madison_revenue/",
        # Layer 141 confirmed direct; other URLs as probe fallback
        "direct": {
            "url": "https://web3.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer/141/query",
            "where": "Acres >= {min_acres}",
            "fields": "PIN,PropertyAddress,PropertyOwner,MailingAddress,TotalAppraisedValue,Acres,DeedDate",
        },
        "base_urls": [
            # emapsplus: confirmed OWNER + ACREAGE, no Referer needed
            "http://emapsplus.com/arcgis/rest/services/Alabama/MadisonAnalyst/MapServer",
            "http://emapsplus.com/arcgis/rest/services/Alabama/MadisonEmapsDMO/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer",
            "https://web4.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer",
            "https://web5.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer",
            "https://web2.kcsgis.com/kcsgis/rest/services/Madison/AL47_GAMAWeb/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Madison/Public/MapServer",
            "https://web5.kcsgis.com/kcsgis/rest/services/Madison/Public/MapServer",
            "https://maps.madisoncountyal.gov/arcgis/rest/services/Public/Parcels/MapServer",
        ],
    },
    "Marshall": {
        "city": "Guntersville",
        "assess_url": "https://isv.kcsgis.com/al.marshall_revenue/",
        # CONFIRMED: iWorQ/MapServer lyr1 "Parcels" on web4
        # Acres: DeededAcres / CalcAcres  Value: TTV / CLandValue
        # Owner: Owner  Mail: MailAdd1, MailCity, MailState, MailZip1
        # DO NOT use orderByFields — server returns non-JSON gzip response
        "direct": {
            "url": "https://web4.kcsgis.com/kcsgis/rest/services/Marshall/iWorQ/MapServer/1/query",
            "where": "DeededAcres >= {min_acres} OR CalcAcres >= {min_acres}",
            "fields": "GIS_PARCELID,PARCELID,Owner,MailAdd1,MailAdd2,MailCity,MailState,MailZip1,TTV,CLandValue,DeededAcres,CalcAcres,DeedRecorded,SitusAddName,SitusAddNumber,SitusAddCity,PropertyClass",
        },
        "base_urls": [
            "https://web4.kcsgis.com/kcsgis/rest/services/Marshall/iWorQ/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Marshall/iWorQ/MapServer",
            "https://web5.kcsgis.com/kcsgis/rest/services/Marshall/iWorQ/MapServer",
        ],
    },
    "Cherokee": {
        "city": "Centre",
        "assess_url": "https://isv.kcsgis.com/al.cherokee_revenue/",
        "direct": None,
        "base_urls": [
            "https://web4.kcsgis.com/kcsgis/rest/services/Cherokee/AL14_GAMAWeb/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Cherokee/AL14_GAMAWeb/MapServer",
            "https://web2.kcsgis.com/kcsgis/rest/services/Cherokee/AL14_GAMAWeb/MapServer",
            "https://web4.kcsgis.com/kcsgis/rest/services/Cherokee/Public/MapServer",
        ],
    },
    "Etowah": {
        "city": "Gadsden",
        "assess_url": "https://isv.kcsgis.com/al.etowah_revenue/",
        "direct": None,
        "base_urls": [
            "https://web4.kcsgis.com/kcsgis/rest/services/Etowah/AL28_GAMAWeb/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Etowah/AL28_GAMAWeb/MapServer",
            "https://web4.kcsgis.com/kcsgis/rest/services/Etowah/Public/MapServer",
            "https://web3.kcsgis.com/kcsgis/rest/services/Etowah/Public/MapServer",
        ],
    },
    "DeKalb": {
        "city": "Fort Payne",
        "assess_url": "https://isv.kcsgis.com/al.dekalb_revenue/",
        "direct": None,
        "base_urls": [
            # AL28_GAMAWeb is stopped on web4. Use PublicF and Public
            "https://web4.kcsgis.com/kcsgis/rest/services/DeKalb/PublicF/MapServer",
            "https://web4.kcsgis.com/kcsgis/rest/services/DeKalb/Public/MapServer",
        ],
    },
    # Jackson AL: CONFIRMED no bulk parcel REST service.
    # Only annotation layers found (Public_ISV_Jackson_Anno, Public_ISV_Jackson).
    # Removed from scraper — county has no queryable GIS data.

}

def _scrape_al_county(county, cfg, min_acres, max_vpa, max_per, verbose):
    if verbose: print(f"  AL/{county}...", end=" ", flush=True)
    city = cfg["city"]
    assess_url = cfg["assess_url"]

    # Try confirmed direct URL first (Madison)
    direct = cfg.get("direct")
    if direct:
        data, err = jget(direct["url"], {
            "where": direct["where"].format(min_acres=min_acres),
            "outFields": direct["fields"],
            "resultRecordCount": max_per,
            "returnGeometry": "true", "outSR": "4326", "f": "json",
        }, kcs=True)
        if data is not None:
            leads = []
            for feat in data.get("features", []):
                p = feat.get("attributes", {})
                lead = _build_al_lead(p, county, city, assess_url,
                                      f"{county} County AL — direct lyr141",
                                      min_acres, max_vpa)
                if lead:
                    geom = feat.get("geometry")
                    if geom:
                        ct = polygon_centroid_wgs84(geom) if geom.get("rings") else (geom.get("y"), geom.get("x"))
                        if ct and ct[0]: lead["lat"], lead["lng"] = ct[0], ct[1]
                    leads.append(lead)
            if leads:
                if verbose: print(f"✓ {len(leads)} [direct]")
                return leads, None
            # Direct hit but 0 qualifying — still try probe for other layers
        sleep(0.3)

    # Probe all base_urls
    for base_url in cfg.get("base_urls", []):
        use_kcs = "kcsgis" in base_url
        leads, _ = _probe_gama_service(
            base_url, county, city, assess_url,
            min_acres, max_per, max_vpa, verbose, use_kcs=use_kcs
        )
        if leads:
            if verbose: print(f" ✓ {len(leads)}")
            sleep(0.2)
            return leads, None
        sleep(0.3)

    if verbose: print("✗ all services failed")
    return [], f"AL/{county} — no queryable service found"

def scrape_al(target_counties, min_acres, max_vpa, max_per, verbose):
    all_leads, gaps = [], []
    for county in target_counties:
        if county not in AL_ALL_COUNTIES:
            gaps.append(f"AL/{county} — not in config")
            continue
        leads, gap = _scrape_al_county(county, AL_ALL_COUNTIES[county],
                                        min_acres, max_vpa, max_per, verbose)
        all_leads.extend(leads)
        if gap: gaps.append(gap)
    return all_leads, gaps


# ── Dedup + Finalize ──────────────────────────────────────────────────────────

COUNTY_TEMPS = {
    "TN_Blount":    (87, 28), "TN_Hamilton":  (89, 31), "TN_Knox":      (87, 29),
    "TN_Madison":   (91, 30), "TN_Sevier":    (86, 27),
    "AL_DeKalb":    (88, 27), "AL_Jackson":   (89, 27),
    "AL_Madison":   (91, 31), "AL_Marshall":  (90, 28),
    "NC_Watauga":   (78, 18), "NC_Avery":     (76, 17),
    "NC_Ashe":      (79, 18), "NC_Alleghany": (79, 18),
    "NC_Cherokee":  (83, 22), "NC_Macon":     (81, 21), "NC_Swain":     (80, 20),
    "SC_Oconee":    (87, 28), "SC_Pickens":   (88, 28),
    "SC_Anderson":  (90, 29), "SC_Greenville":(89, 29), "SC_Spartanburg":(89, 28),
}

def get_county_temps(lead):
    key = f"{lead.get('state','')}_{lead.get('county','')}"
    return COUNTY_TEMPS.get(key, (None, None))


import math as _math

# Pre-computed town data for our target counties
# (name, lat, lng, population) — nearest towns of each size tier
NEARBY_TOWNS = {
    "TN_Blount":    [("Alcoa TN",35.789,-83.974,9600),("Maryville TN",35.757,-84.000,29000),("Knoxville TN",35.960,-83.921,190000)],
    "TN_Hamilton":  [("Soddy-Daisy TN",35.235,-85.181,9000),("Cleveland TN",35.160,-84.877,45000),("Chattanooga TN",35.045,-85.309,181000)],
    "TN_Knox":      [("Powell TN",36.031,-83.989,9000),("Maryville TN",35.757,-84.000,29000),("Knoxville TN",35.960,-83.921,190000)],
    "TN_Madison":   [("Humboldt TN",35.817,-88.912,8000),("Jackson TN",35.615,-88.814,67000),("Memphis TN",35.149,-90.048,633000)],
    "TN_Sevier":    [("Gatlinburg TN",35.714,-83.512,4000),("Sevierville TN",35.868,-83.562,17000),("Knoxville TN",35.960,-83.921,190000)],
    "AL_DeKalb":    [("Rainsville AL",34.494,-85.849,5000),("Fort Payne AL",34.443,-85.720,14000),("Huntsville AL",34.730,-86.586,190000)],
    "AL_Jackson":   [("Stevenson AL",34.869,-85.834,2000),("Scottsboro AL",34.673,-86.034,15000),("Huntsville AL",34.730,-86.586,190000)],
    "AL_Madison":   [("Owens Cross Roads AL",34.622,-86.499,1600),("Madison AL",34.699,-86.748,48000),("Huntsville AL",34.730,-86.586,190000)],
    "AL_Marshall":  [("Guntersville AL",34.354,-86.294,8000),("Albertville AL",34.267,-86.209,21000),("Huntsville AL",34.730,-86.586,190000)],
    "NC_Cherokee":  [("Murphy NC",35.088,-84.016,1600),("Dalton GA",34.770,-84.970,34000),("Chattanooga TN",35.045,-85.309,181000)],
    "NC_Macon":     [("Franklin NC",35.183,-83.381,4000),("Waynesville NC",35.489,-82.986,10000),("Atlanta GA",33.749,-84.388,498000)],
    "NC_Swain":     [("Bryson City NC",35.432,-83.446,1500),("Waynesville NC",35.489,-82.986,10000),("Atlanta GA",33.749,-84.388,498000)],
    "SC_Spartanburg":[("Glendale SC",34.949,-81.850,900),("Spartanburg SC",34.949,-81.932,38000),("Charlotte NC",35.227,-80.843,874000)],
    "SC_Anderson":  [("Williamston SC",34.619,-82.477,4000),("Anderson SC",34.503,-82.650,27000),("Charlotte NC",35.227,-80.843,874000)],
    "SC_Greenville":[("Taylors SC",34.918,-82.294,5000),("Greenville SC",34.852,-82.394,70000),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Clay":     [("Hayesville NC",35.049,-83.818,300),("Dalton GA",34.770,-84.970,34000),("Atlanta GA",33.749,-84.388,498000)],
    "NC_Graham":   [("Robbinsville NC",35.323,-83.804,600),("Murphy NC",35.088,-84.016,1600),("Dalton GA",34.770,-84.970,34000),("Atlanta GA",33.749,-84.388,498000)],
    "NC_Haywood":  [("Lake Junaluska NC",35.526,-82.960,2600),("Waynesville NC",35.489,-82.986,10000),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Jackson":  [("Sylva NC",35.374,-83.222,2600),("Waynesville NC",35.489,-82.986,10000),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Madison":  [("Marshall NC",35.790,-82.682,800),("Asheville NC",35.579,-82.551,94000),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Watauga":   [("Boone NC",36.210,-81.675,20000),("Blowing Rock NC",36.134,-81.677,1300),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Avery":     [("Banner Elk NC",36.163,-81.872,1200),("Newland NC",36.087,-81.923,700),("Johnson City TN",36.313,-82.353,71000)],
    "NC_Ashe":      [("West Jefferson NC",36.402,-81.498,1300),("Jefferson NC",36.422,-81.477,1600),("Boone NC",36.210,-81.675,20000)],
    "NC_Alleghany": [("Sparta NC",36.506,-81.119,1800),("West Jefferson NC",36.402,-81.498,1300),("Boone NC",36.210,-81.675,20000)],
    "NC_Mitchell": [("Spruce Pine NC",35.917,-82.064,2000),("Asheville NC",35.579,-82.551,94000),("Charlotte NC",35.227,-80.843,874000)],
    "NC_Yancey":   [("Burnsville NC",35.912,-82.299,1600),("Asheville NC",35.579,-82.551,94000),("Charlotte NC",35.227,-80.843,874000)],
    "VA_Lee":        [("Jonesville VA",36.683,-83.109,900),("Kingsport TN",36.548,-82.562,54000),("Bristol VA/TN",36.595,-82.188,17000)],
    "VA_Scott":      [("Gate City VA",36.637,-82.576,2000),("Kingsport TN",36.548,-82.562,54000),("Bristol VA/TN",36.595,-82.188,17000)],
    "VA_Wise":       [("Norton VA",36.933,-82.628,3700),("Kingsport TN",36.548,-82.562,54000),("Roanoke VA",37.271,-79.941,99000)],
    "VA_Washington": [("Abingdon VA",36.709,-81.977,8600),("Bristol VA/TN",36.595,-82.188,17000),("Roanoke VA",37.271,-79.941,99000)],
    "VA_Russell":    [("Lebanon VA",36.898,-82.077,3100),("Kingsport TN",36.548,-82.562,54000),("Bristol VA/TN",36.595,-82.188,17000)],
    "AL_Cherokee":   [("Centre AL",34.152,-85.678,3400),("Rome GA",34.257,-85.165,37000),("Chattanooga TN",35.045,-85.309,181000)],
    "AL_Etowah":     [("Attalla AL",34.016,-86.087,6000),("Gadsden AL",34.013,-86.003,32000),("Huntsville AL",34.730,-86.586,190000)],
    "SC_Oconee":   [("Walhalla SC",34.765,-83.069,3500),("Greenville SC",34.852,-82.394,70000),("Charlotte NC",35.227,-80.843,874000)],
    "SC_Pickens":  [("Pickens SC",34.884,-82.707,3200),("Easley SC",34.830,-82.601,20000),("Charlotte NC",35.227,-80.843,874000)],
}

def haversine_mi(lat1, lng1, lat2, lng2):
    """Straight-line distance in miles."""
    R = 3958.8
    dlat = _math.radians(lat2 - lat1)
    dlng = _math.radians(lng2 - lng1)
    a = (_math.sin(dlat/2)**2 +
         _math.cos(_math.radians(lat1)) * _math.cos(_math.radians(lat2)) * _math.sin(dlng/2)**2)
    return R * 2 * _math.asin(_math.sqrt(a))

def road_dist_estimate(straight_mi):
    """Estimate road distance from straight-line using 1.35x rural factor."""
    return round(straight_mi * 1.35, 1)

def nearest_towns(lead):
    """
    Returns dict with nearest town of each size tier (road distance estimate in miles).
    Tiers: small (< 10k), medium (10k–99k), large (100k+)
    Uses parcel lat/lng if available, else county centroid from NEARBY_TOWNS list.
    """
    key = f"{lead.get('state','')}_{lead.get('county','')}"
    towns = NEARBY_TOWNS.get(key, [])
    if not towns:
        return {}
    lat = lead.get("lat")
    lng = lead.get("lng")
    result = {}
    for name, tlat, tlng, pop in towns:
        if lat and lng:
            sl = haversine_mi(lat, lng, tlat, tlng)
        else:
            sl = None
        rd = road_dist_estimate(sl) if sl is not None else None
        tier = "small" if pop < 10000 else ("medium" if pop < 100000 else "large")
        if tier not in result or (rd is not None and rd < result[tier]["dist_mi"]):
            result[tier] = {"name": name, "pop": pop, "dist_mi": rd}
    return result

PROBATE_WORDS = ["ESTATE OF","HEIRS OF","DECD","DECEASED","IN RE ","PROBATE",
                 "ADMINISTRATR","EXECUTOR","ET AL","ESTATE"]

def distressed_signals(lead):
    """
    Returns list of (label, description, score_pts) tuples.
    Score components:
      - Probate/estate: +25
      - OOS absentee: +15
      - Long hold (age of last sale): +5 to +25
      - Entity type (LLC/Trust/Corp): +7 to +12
      - No structure: +3
      - No owner in GIS: -10
    """
    sigs = []
    owner = (lead.get("owner") or "").upper()
    ms    = (lead.get("mail_state") or "").upper()
    state = lead.get("state","").upper()
    yr    = lead.get("sale_year","")
    lu    = (lead.get("lu_class") or "").lower()
    oos   = lead.get("oos", False)

    # ── Ownership type ─────────────────────────────────────────────────────────
    # Active probate: flagged by scraper cross-referencing court records or
    # by the user via the dashboard court lookup tool
    if lead.get("active_probate"):
        sigs.append(("ACTIVE PROBATE",
                     "CONFIRMED open probate case in county court — estate not yet settled, heirs eager to liquidate",
                     60))
    if any(k in owner for k in PROBATE_WORDS):
        sigs.append(("PROBATE/ESTATE",
                     "Heir or estate ownership — highly motivated, no emotional attachment, act quickly",
                     40))
    elif "TRUST" in owner:
        sigs.append(("TRUST",
                     "Trust-held — motivated when trustees managing for multiple heirs",
                     12))
    elif "LLC" in owner:
        sigs.append(("LLC",
                     "Entity-owned — verify if inactive or dissolved LLC",
                     10))
    elif any(k in owner for k in ["CORP","INC.","CO.","LTD"]):
        sigs.append(("CORP",
                     "Corporate owner — likely peripheral asset, may respond to offers",
                     7))

    # ── Absentee / out-of-state ────────────────────────────────────────────────
    if oos and ms and ms != state:
        sigs.append((f"OOS {ms}",
                     f"Absentee owner mails to {ms} — not managing land locally, classic distressed signal",
                     15))

    # ── Last sale recency (hold period) ───────────────────────────────────────
    hold_score = 0
    hold_label = ""
    if yr:
        try:
            age = 2026 - int(str(yr)[:4])
            if age >= 50:
                hold_score, hold_label = 25, f"HELD {age} YRS"
                sigs.append((hold_label,
                             f"Purchased ~{yr} — {age} years ago. Multi-generational hold, no mortgage, heirs likely unaligned.",
                             hold_score))
            elif age >= 35:
                hold_score, hold_label = 20, f"HELD {age} YRS"
                sigs.append((hold_label,
                             f"Purchased ~{yr} — {age} years ago. Long hold, equity rich, owner may welcome liquidity.",
                             hold_score))
            elif age >= 20:
                hold_score, hold_label = 14, f"HELD {age} YRS"
                sigs.append((hold_label,
                             f"Purchased ~{yr} — {age} years ago. Substantial equity, owner likely open.",
                             hold_score))
            elif age >= 10:
                hold_score, hold_label = 7, f"HELD {age} YRS"
                sigs.append((hold_label,
                             f"Purchased ~{yr} — {age} years ago. Moderate hold.",
                             hold_score))
            elif age >= 0:
                sigs.append((f"RECENT {yr}",
                             f"Sold in {yr} — recent acquisition, likely not distressed.",
                             -5))
        except Exception:
            pass
    else:
        sigs.append(("NO SALE YR",
                     "No last-sale year in GIS — could be very old inherited hold or data gap.",
                     5))

    # ── Land type / structure ──────────────────────────────────────────────────
    if not lead.get("has_structure"):
        if "vacant" in lu or "undevel" in lu:
            sigs.append(("VACANT LAND", "Classified vacant/undeveloped — no attachment to structure", 6))
        else:
            sigs.append(("RAW LAND", "No recorded structure — pure land play", 3))

    # ── Low value-per-acre (motivated seller signal) ──────────────────────────
    vpa = lead.get("vpa") or 0
    acres = lead.get("acres") or 0
    if vpa > 0 and acres >= 100:
        if vpa < 500:
            sigs.append(("DISTRESSED VPA",
                         f"Listed at ${vpa}/ac — far below market, implies distress or motivated seller",
                         18))
        elif vpa < 1000:
            sigs.append(("LOW VPA",
                         f"${vpa}/ac — below-market pricing, possible motivated seller",
                         10))

    # ── Multiple heirs / heir fragments ───────────────────────────────────────
    if any(k in owner for k in ["ET UX", "ET AL", "HEIRS", "& HEIRS", "MULTIPLE"]):
        sigs.append(("MULTI-HEIR",
                     "Multiple heirs on title — divided ownership creates pressure to liquidate",
                     22))

    # ── Very long hold without ANY sale record ────────────────────────────────
    if not yr or str(yr).strip() in ("", "0", "None"):
        if "ESTATE" in owner or "HEIRS" in owner:
            sigs.append(("UNDATED ESTATE",
                         "Estate owner with no recorded sale date — likely inherited, never sold",
                         20))

    # ── Out-of-state + entity combo ───────────────────────────────────────────
    if oos and ("LLC" in owner or "TRUST" in owner or "CORP" in owner):
        sigs.append(("OOS ENTITY",
                     "Out-of-state entity ownership — absentee investor, often unaware of local value",
                     15))

    # ── Data quality flag ──────────────────────────────────────────────────────
    if not lead.get("owner"):
        sigs.append(("NO OWNER",
                     "Owner not found in GIS — VERIFY at county assessor before contacting. Could be exempt/govt.",
                     -10))

    return sigs

def distress_score_total(sigs):
    return max(0, sum(s[2] for s in sigs))


def compute_county_medians(leads):
    from statistics import median
    cv = {}
    for l in leads:
        if l["vpa"] > 0:
            k = f"{l['state']}_{l['county']}"
            cv.setdefault(k, []).append(l["vpa"])
    return {k: median(v) for k, v in cv.items() if v}


def _soil_score(lead):
    """
    Derive a 1-10 soil quality score from existing soil/water/pasture data.
    Used in the dashboard land column.
    """
    score = 5  # base
    soil = (lead.get("soil") or lead.get("base_soil") or "").lower()
    water = lead.get("water_score") or 0
    pasture = lead.get("pasture_pct") or 0

    # Soil class/name signals
    if any(x in soil for x in ["class ii", "class i ", "silt loam", "saunook", "tusquitee",
                                 "cecilina", "pacolet", "madison"]): score += 2
    if any(x in soil for x in ["class iii", "sandy loam", "clay loam"]): score += 1
    if any(x in soil for x in ["class iv", "class v", "rocky", "stony", "ledge"]): score -= 1
    if any(x in soil for x in ["well drained", "well-drained"]): score += 1
    if "poorly drained" in soil: score -= 2

    # Water score contribution
    if water >= 8: score += 1
    elif water <= 4: score -= 1

    # Pasture % — high pasture implies workable soil
    if pasture >= 50: score += 1
    elif pasture >= 30: score += 0

    return max(1, min(10, score))

def finalize(all_leads, top_n):
    seen, deduped = set(), []
    for l in all_leads:
        pid = l.get("parcel_id") or ""
        key = f"{l['state']}_{pid}" if pid else f"{l['state']}_{l['county']}_{l.get('address')}_{l['acres']:.0f}"
        if key not in seen:
            seen.add(key)
            deduped.append(l)

    deduped = [l for l in deduped if not is_government(l.get("owner",""))]
    medians = compute_county_medians(deduped)

    for lead in deduped:
        med = medians.get(f"{lead['state']}_{lead['county']}", 0)
        lead["county_vpa_median"] = med
        lead["soil_col"]    = soil_col_data(lead)  # sets est_pasture_pct first
        lead["soil_analysis"] = soil_analysis_for_parcel(lead)
        lead["score"]       = score_investment(lead, med)
        lead["h_score"]     = score_homestead(lead)
        # Combined score: 60% homestead + 40% investment
        lead["combined"]    = round(lead["h_score"] * 0.6 + lead["score"] * 0.4)
        lead["mao"]         = round(lead["appr_value"] * 0.65, 0)
        lead["confidence"], lead["conf_score"], lead["conf_notes"] = confidence(lead)
        lead["analysis"]    = analyze_property(lead, med)
        lead["h_analysis"]  = homestead_analysis(lead)
        lead["signals"]     = distressed_signals(lead)
        lead["d_score"]     = distress_score_total(lead["signals"])
        # combined = homestead(50%) + distress(30%) + investment(20%)
        h = lead.get("h_score", 0)
        inv = lead.get("i_score", 0)
        d = lead.get("d_score", 0)
        lead["combined"] = min(99, round(h * 0.50 + d * 0.30 + inv * 0.20))
        lead["summer_high"], lead["winter_low"] = get_county_temps(lead)
        lead["towns"] = nearest_towns(lead)

    deduped.sort(key=lambda x: x["combined"], reverse=True)
    return deduped  # no cap — return all qualifying leads

# ── HTML Dashboard ────────────────────────────────────────────────────────────
def esc(s): return _html.escape(str(s or ""))

def build_html(leads, gaps, args, run_ts):
    # Precompute stats (avoids f-string dict literal bug with {{}})
    n_owner   = sum(1 for l in leads if l.get("owner"))
    n_oos     = sum(1 for l in leads if l.get("oos"))
    n_nf      = sum(1 for l in leads if l.get("soil_col",{}).get("nf_nearby"))
    n_total   = len(leads)
    # Serialise leads to JSON for JS filter engine
    leads_json = json.dumps([{
        "rank": i+1,
        "state": l["state"],
        "county": l["county"],
        "acres": l["acres"],
        "vpa": l["vpa"],
        "appr_value": l["appr_value"],
        "mao": l["mao"],
        "score": l["score"],
        "h_score": l["h_score"],
        "combined": l["combined"],
        "confidence": l["confidence"],
        "conf_score": l["conf_score"],
        "conf_notes": "; ".join(l.get("conf_notes",[])),
        "owner": l.get("owner",""),
        "address": l.get("address","") or l.get("city",""),
        "parcel_id": l.get("parcel_id",""),
        "lu_class": l.get("lu_class",""),
        "sale_year": l.get("sale_year",""),
        "oos": l.get("oos", False),
        "has_structure": l.get("has_structure", False),
        "has_owner": bool(l.get("owner")),
        "is_vacant": not bool(l.get("has_structure")),
        "soil_score": _soil_score(l),
        "has_sale_year": bool(l.get("sale_year")),
        "mail_state": l.get("mail_state",""),
        "mail_addr": l.get("mail_addr",""),
        "mail_city": l.get("mail_city",""),
        "mail_zip": l.get("mail_zip",""),
        "analysis": [s[:60] for s in (l.get("analysis") or [])[:3]],
        "h_analysis": [s[:60] for s in (l.get("h_analysis") or [])[:4]],
        "soil": l["soil_col"].get("soil",""),
        "drainage": l["soil_col"].get("drainage",""),
        "open_ac": l["soil_col"].get("open_ac",0),
        "wooded_ac": l["soil_col"].get("wooded_ac",0),
        "pasture_pct": l["soil_col"].get("pasture_pct",0),
        "climate": l["soil_col"].get("climate",""),
        "nf_nearby": l["soil_col"].get("nf_nearby", False),
        "nf_name": l["soil_col"].get("nf_name",""),
        "water_score": l["soil_col"].get("water_score",5),
        "climate_score": l["soil_col"].get("climate_score",5),
        "source_name": l.get("source_name",""),
        "map_url": parcel_map_urls(l)[0],
        "google_url": parcel_map_urls(l)[1],
        "regrid_url": regrid_url(l),
        "qpublic_url": qpublic_url(l) or "",
        # Trim text fields to keep JSON small (signals: label only, no description)
        "signals": [[s[0]] for s in l.get("signals",[])],
        "d_score": l.get("d_score",0),
        "has_owner": bool(l.get("owner")),
        "lat": l.get("lat"), "lng": l.get("lng"),
        "elevation_ft": l.get("elevation_ft"),
        "elev_min": l.get("elev_min"), "elev_max": l.get("elev_max"),
        "summer_high": l.get("summer_high"), "winter_low": l.get("winter_low"),
        "towns": {k: {"name": v["name"], "dist_mi": v["dist_mi"]}
                  for k, v in (l.get("towns") or {}).items()},
    } for i, l in enumerate(leads[:500])], ensure_ascii=False)  # cap 500 for browser perf

    gap_html = "".join(f'<li>⚠ {esc(g)}</li>' for g in gaps) if gaps else "<li>All counties returned data</li>"

    raw = args.raw_count if hasattr(args,'raw_count') else len(leads)
    filt = args.after_filter if hasattr(args,'after_filter') else len(leads)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Land Scout v75 · {run_ts}</title>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=Space+Mono:wght@400;700&family=Outfit:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
:root{{
  --bg:#0a0f0a;--surface:#111811;--surface2:#182018;--border:#1f2d1f;
  --panel:#0e160e;--text:#d6e8d2;--muted:#6b8a67;--dim:#3a5036;
  --accent:#4ade80;--amber:#fbbf24;--red:#f87171;--sky:#67e8f9;
  --purple:#c084fc;--earth:#c9a96e;
  --font-head:'Syne',sans-serif;--font-mono:'Space Mono',monospace;
  --font-body:'Outfit',sans-serif;
  --radius:6px;
  --h-green:linear-gradient(135deg,#16a34a,#065f46);
  --i-blue:linear-gradient(135deg,#1d4ed8,#4338ca);
  --c-gold:linear-gradient(135deg,#b45309,#78350f);
}}
*{{box-sizing:border-box;margin:0;padding:0;transition:background .15s,color .15s}}
html{{scroll-behavior:smooth}}
body{{background:var(--bg);color:var(--text);font-family:var(--font-body);font-size:13px;line-height:1.5;min-height:100vh}}

/* ─ Layout ─ */
.app{{display:flex;height:100vh;overflow:hidden}}
.sidebar{{width:300px;min-width:300px;background:var(--panel);border-right:1px solid var(--border);
  display:flex;flex-direction:column;overflow:hidden}}
.main-area{{flex:1;display:flex;flex-direction:column;overflow:hidden}}
.topbar{{background:var(--surface);border-bottom:1px solid var(--border);
  padding:12px 20px;display:flex;align-items:center;gap:16px;flex-shrink:0}}

/* ─ Sidebar ─ */
.sidebar-header{{padding:16px;border-bottom:1px solid var(--border);flex-shrink:0}}
.sidebar-header h2{{font-family:var(--font-head);font-size:15px;font-weight:800;
  color:var(--accent);letter-spacing:.5px;display:flex;align-items:center;gap:8px}}
.sidebar-header h2 span{{font-size:18px}}
.filter-scroll{{flex:1;overflow-y:auto;padding:12px}}
.filter-scroll::-webkit-scrollbar{{width:4px}}
.filter-scroll::-webkit-scrollbar-thumb{{background:var(--border);border-radius:2px}}
.filter-group{{margin-bottom:20px}}
.filter-label{{font-family:var(--font-mono);font-size:9px;letter-spacing:1px;
  text-transform:uppercase;color:var(--muted);margin-bottom:8px;
  display:flex;align-items:center;gap:6px}}
.filter-label::before{{content:'';flex:1;height:1px;background:var(--border)}}

/* Range sliders */
.range-wrap{{display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-top:6px}}
.range-input-wrap{{display:flex;flex-direction:column;gap:3px}}
.range-input-wrap label{{font-size:10px;color:var(--muted)}}
input[type=range]{{-webkit-appearance:none;width:100%;height:4px;
  background:var(--border);border-radius:2px;outline:none}}
input[type=range]::-webkit-slider-thumb{{-webkit-appearance:none;width:14px;height:14px;
  border-radius:50%;background:var(--accent);cursor:pointer;box-shadow:0 0 0 3px rgba(74,222,128,.2)}}
.range-val{{font-family:var(--font-mono);font-size:10px;color:var(--accent);text-align:center;
  background:var(--surface2);padding:2px 6px;border-radius:3px}}

/* Checkboxes + toggles */
.check-grid{{display:grid;grid-template-columns:1fr 1fr;gap:4px}}
.check-item{{display:flex;align-items:center;gap:6px;padding:4px 6px;
  border-radius:4px;cursor:pointer;border:1px solid transparent}}
.check-item:hover{{background:var(--surface2);border-color:var(--border)}}
.check-item input{{accent-color:var(--accent);width:13px;height:13px}}
.check-item label{{font-size:11px;color:var(--text);cursor:pointer}}

/* Sort + sort-by selects */
select{{background:var(--surface2);color:var(--text);border:1px solid var(--border);
  padding:5px 8px;border-radius:4px;font-size:11px;font-family:var(--font-body);width:100%}}
select:focus{{outline:none;border-color:var(--accent)}}

/* Toggle pills */
.pill-group{{display:flex;gap:4px;flex-wrap:wrap}}
.pill{{padding:4px 10px;border-radius:20px;font-size:10px;font-weight:600;
  border:1px solid var(--border);color:var(--muted);cursor:pointer;
  font-family:var(--font-mono);letter-spacing:.3px}}
.pill.active{{background:var(--accent);color:#0a0f0a;border-color:var(--accent)}}
.pill:hover:not(.active){{border-color:var(--accent);color:var(--accent)}}

/* Buttons */
.status-btn{{width:28px;height:28px;border-radius:4px;border:2px solid var(--border);
  cursor:pointer;font-size:15px;display:inline-flex;align-items:center;justify-content:center;
  background:var(--surface2);transition:all .15s;line-height:1}}
.status-btn.great{{background:#14532d;border-color:#4ade80;box-shadow:0 0 8px #4ade8066}}
.status-btn.good{{background:#1e3a5f;border-color:#38bdf8;box-shadow:0 0 6px #38bdf855}}
.status-btn.avg{{background:#78350f;border-color:#f59e0b;box-shadow:0 0 6px #f59e0b55}}
.status-btn.bad{{background:#7f1d1d;border-color:#dc2626;box-shadow:0 0 8px #dc262666}}
.status-btn:hover{{border-color:var(--accent);transform:scale(1.1)}}
.copy-btn{{padding:5px 12px;background:var(--surface2);border:1px solid var(--border);
  border-radius:4px;color:var(--accent);font-size:11px;font-weight:600;cursor:pointer;
  font-family:var(--font-mono);letter-spacing:.5px;white-space:nowrap}}
.copy-btn:hover{{background:var(--accent);color:var(--bg)}}
.copy-btn.flash{{background:#166534;color:#fff;border-color:#166534}}
.btn-reset{{width:100%;padding:8px;background:transparent;border:1px solid var(--border);
  color:var(--muted);border-radius:4px;cursor:pointer;font-size:11px;margin-top:6px;
  font-family:var(--font-mono);letter-spacing:.5px}}
.btn-reset:hover{{border-color:var(--red);color:var(--red)}}

/* ─ Topbar ─ */
.logo{{font-family:var(--font-head);font-weight:800;font-size:18px;color:var(--accent)}}
.logo small{{font-size:11px;font-weight:400;color:var(--muted);margin-left:6px}}
.stats-strip{{display:flex;gap:20px;margin-left:auto;flex-wrap:wrap}}
.stat-item{{display:flex;flex-direction:column;align-items:center}}
.stat-n{{font-family:var(--font-mono);font-size:16px;font-weight:700;color:var(--accent)}}
.stat-l{{font-size:9px;color:var(--muted);letter-spacing:.5px;text-transform:uppercase}}
.result-count{{font-family:var(--font-mono);font-size:12px;color:var(--muted)}}

/* ─ Table area ─ */
.table-wrap{{flex:1;overflow:auto}}
.table-wrap::-webkit-scrollbar{{width:6px;height:6px}}
.table-wrap::-webkit-scrollbar-thumb{{background:var(--border);border-radius:3px}}
table{{width:100%;border-collapse:collapse;font-size:12px;min-width:1100px}}
thead th{{background:var(--surface);padding:9px 10px;text-align:left;
  font-family:var(--font-mono);font-size:9px;letter-spacing:.8px;
  text-transform:uppercase;color:var(--muted);border-bottom:2px solid var(--border);
  position:sticky;top:0;z-index:40;white-space:nowrap;cursor:pointer;user-select:none}}
thead th:hover{{color:var(--accent)}}
thead th .sort-arrow{{margin-left:4px;opacity:.4}}
thead th.sorted .sort-arrow{{opacity:1;color:var(--accent)}}
tbody tr{{border-bottom:1px solid var(--border)}}
tbody tr:hover td{{background:var(--surface2)}}
td{{padding:8px 10px;vertical-align:top}}
td.num{{text-align:right;font-family:var(--font-mono);white-space:nowrap}}
.pid-col{{max-width:110px;font-size:10px;word-break:break-all;line-height:1.3}}

/* Score badges */
.score-wrap{{display:flex;flex-direction:column;gap:3px;align-items:center;min-width:75px}}
.score-ring{{position:relative;width:44px;height:44px;margin:0 auto}}
.score-ring svg{{transform:rotate(-90deg)}}
.score-ring .ring-bg{{fill:none;stroke:var(--border);stroke-width:4}}
.score-ring .ring-val{{fill:none;stroke-width:4;stroke-linecap:round;transition:stroke-dashoffset .4s}}
.score-num{{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);
  font-family:var(--font-mono);font-size:12px;font-weight:700}}
.badge-row{{display:flex;gap:3px;justify-content:center;flex-wrap:wrap}}
.badge{{padding:2px 6px;border-radius:10px;font-size:9px;font-weight:700;font-family:var(--font-mono)}}
.badge-h{{background:#14532d;color:#4ade80}}
.badge-i{{background:#1e3a8a;color:#93c5fd}}
.badge-c{{background:#78350f;color:#fcd34d}}
.badge-conf{{color:#fff}}
.badge-oos{{background:#581c87;color:#e9d5ff}}
.badge-nf{{background:#164e63;color:#67e8f9}}

/* Location cell */
.loc-county{{font-family:var(--font-head);font-weight:700;color:var(--accent);font-size:11px}}
.loc-addr{{color:var(--text);margin:2px 0;font-size:12px}}
.loc-src{{color:var(--dim);font-size:10px;font-family:var(--font-mono)}}

/* Owner cell */
.owner-name{{font-weight:600;color:#86efac;font-size:12px;margin-bottom:2px}}
.owner-mail{{color:var(--muted);font-size:11px}}
.owner-sale{{color:var(--earth);font-size:10px;font-family:var(--font-mono)}}

/* Parcel cell */
.parcel-id{{font-family:var(--font-mono);font-size:11px;color:var(--amber)}}
.lu-tag{{display:inline-block;margin-top:3px;padding:2px 6px;border-radius:3px;
  background:var(--surface2);font-size:10px;color:var(--muted);border:1px solid var(--border)}}

/* Soil column */
.soil-cell{{min-width:180px}}
.soil-bars{{margin-bottom:6px}}
.soil-bar-row{{display:flex;align-items:center;gap:6px;margin-bottom:4px;font-size:10px}}
.soil-bar-label{{width:50px;color:var(--muted);text-align:right;flex-shrink:0}}
.soil-bar-track{{flex:1;height:6px;background:var(--border);border-radius:3px;overflow:hidden}}
.soil-bar-fill{{height:100%;border-radius:3px;transition:width .3s}}
.soil-bar-num{{width:30px;color:var(--text);font-family:var(--font-mono)}}
.soil-name{{font-size:10px;color:var(--muted);margin-top:4px;line-height:1.4}}
.nf-chip{{display:inline-flex;align-items:center;gap:4px;padding:2px 7px;
  border-radius:10px;background:#0c4a6e;color:var(--sky);font-size:10px;margin-top:4px}}
.water-dots{{display:flex;gap:2px;margin-top:2px}}
.water-dot{{width:7px;height:7px;border-radius:50%}}

/* Analysis accordion */
.analysis-wrap details{{margin-top:4px}}
.analysis-wrap summary{{cursor:pointer;font-size:10px;color:var(--accent);
  font-family:var(--font-mono);letter-spacing:.3px;user-select:none}}
.analysis-wrap summary:hover{{color:var(--sky)}}
.analysis-list{{list-style:none;margin-top:4px;border-left:2px solid var(--border);padding-left:8px}}
.analysis-list li{{padding:2px 0;font-size:11px;color:var(--text);line-height:1.5}}
.analysis-list li.good{{color:#4ade80}}
.analysis-list li.warn{{color:var(--amber)}}
.analysis-list li.bad{{color:var(--red)}}

/* No results */
.map-btn{{display:inline-block;padding:4px 8px;margin:2px 1px;border-radius:4px;
  font-size:11px;font-weight:600;text-decoration:none;border:1px solid var(--border);
  background:var(--surface2);color:var(--accent);white-space:nowrap}}
.map-btn:hover{{background:var(--accent);color:var(--bg);border-color:var(--accent)}}
.sat-btn{{background:#0c2340;border-color:#1e4080;color:var(--sky)}}
.sat-btn:hover{{background:var(--sky);color:var(--bg);border-color:var(--sky)}}
.no-results{{padding:60px;text-align:center;color:var(--muted)}}
.no-results h3{{font-family:var(--font-head);font-size:20px;color:var(--dim);margin-bottom:8px}}

/* Gaps panel */
.gaps-bar{{background:var(--surface);border-top:1px solid var(--border);
  padding:8px 20px;font-size:11px;color:var(--muted);cursor:pointer;flex-shrink:0}}
.gaps-bar summary{{display:flex;align-items:center;gap:8px;list-style:none}}
.gaps-panel{{padding:10px 20px 14px;border-top:1px solid var(--border);
  background:var(--panel);font-size:11px}}
.gaps-panel ul{{margin-left:14px;color:var(--muted);line-height:2}}

.sbadge{{display:inline-block;padding:1px 5px;margin:1px;border-radius:3px;font-size:10px;font-weight:700;color:#fff;cursor:default;white-space:nowrap;vertical-align:middle}}
.sig-cell{{min-width:120px;font-size:11px;line-height:1.6}}
@keyframes fadeIn{{from{{opacity:0;transform:translateY(4px)}}to{{opacity:1;transform:none}}}}
tbody tr{{animation:fadeIn .2s ease both}}

/* ─ Mobile ─ */
@media (max-width: 768px) {{
  .app {{ flex-direction: column; height: auto; overflow: auto; }}
  .sidebar {{ width: 100%; min-width: unset; border-right: none; border-bottom: 1px solid var(--border); max-height: 50vh; }}
  .main-area {{ flex: 1; overflow: auto; }}
  .topbar {{ flex-wrap: wrap; gap: 8px; padding: 8px 12px; }}
  .logo {{ display: none; }}
  .stats-strip {{ flex-wrap: wrap; gap: 6px; }}
  .table-wrap {{ overflow-x: auto; -webkit-overflow-scrolling: touch; }}
  table {{ min-width: 900px; font-size: 11px; }}
  th, td {{ padding: 5px 6px; }}
  .copy-btn {{ font-size: 10px; padding: 4px 8px; }}
}}
</style>
</head>
<body>
<div class="app">

<!-- ─ SIDEBAR FILTERS ─ -->
<aside class="sidebar">
  <div class="sidebar-header">
    <h2><span>🌿</span> Filters</h2>
  </div>
  <div class="filter-scroll">

    <div class="filter-group">
      <div class="filter-label">Sort by</div>
      <select id="sortBy" onchange="applyFilters()">
        <option value="combined">Combined Score ↓</option>
        <option value="d_score" selected>Motivated Owner Score ↓</option>
        <option value="h_score">Homestead Score</option>
        <option value="score">Investment Score</option>
        <option value="acres">Acres (largest)</option>
        <option value="vpa">$/ac (lowest)</option>
        <option value="pasture_pct">Pasture % (highest)</option>
        <option value="water_score">Water Score</option>
        <option value="appr_value">Appraised Value</option>
      </select>
    </div>

    <div class="filter-group" style="background:rgba(220,38,38,.08);border:1px solid #7f1d1d;border-radius:6px;padding:8px">
      <div class="filter-label" style="color:#fca5a5;font-weight:700">🔥 Min Motivated Owner Score</div>
      <input type="range" id="minDistress" min="0" max="80" step="5" value="0"
             oninput="syncRange(this,'minDistressVal');applyFilters()">
      <div class="range-val" id="minDistressVal">Any</div>
    </div>

    <div class="filter-group">
      <div class="filter-label">State / County</div>
      <div id="stateButtons" style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:6px"></div>
      <div class="check-grid" id="countyChecks"></div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Min Acres</div>
      <input type="range" id="minAcres" min="100" max="1000" step="25" value="100" oninput="syncRange(this,'minAcresVal');applyFilters()">
      <div class="range-val" id="minAcresVal">100 ac+</div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Max $/acre</div>
      <input type="range" id="maxVpa" min="0" max="20000" step="250" value="20000" oninput="syncRange(this,'maxVpaVal');applyFilters()">
      <div class="range-val" id="maxVpaVal">Any</div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Min Climate Score (1–10)</div>
      <input type="range" id="minClimate" min="0" max="10" step="1" value="0" oninput="syncRange(this,'minClimateVal');applyFilters()">
      <div class="range-val" id="minClimateVal">Any</div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Min Homestead Score</div>
      <input type="range" id="minHScore" min="0" max="95" step="5" value="0" oninput="syncRange(this,'minHScoreVal');applyFilters()">
      <div class="range-val" id="minHScoreVal">Any</div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Min Investment Score</div>
      <input type="range" id="minIScore" min="0" max="95" step="5" value="0" oninput="syncRange(this,'minIScoreVal');applyFilters()">
      <div class="range-val" id="minIScoreVal">Any</div>
    </div>


    <div class="filter-group">
      <div class="filter-label">Confidence Level</div>
      <div class="pill-group" id="confPills">
        <div class="pill active" data-val="" onclick="togglePill(this,'confPills')">All</div>
        <div class="pill" data-val="HIGH" onclick="togglePill(this,'confPills')">HIGH</div>
        <div class="pill" data-val="MED" onclick="togglePill(this,'confPills')">MED</div>
        <div class="pill" data-val="LOW" onclick="togglePill(this,'confPills')">LOW</div>
      </div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Manual Review Rating</div>
      <div class="pill-group" id="ratingPills">
        <div class="pill active" data-val="" onclick="togglePill(this,'ratingPills')">All</div>
        <div class="pill" data-val="great" onclick="togglePill(this,'ratingPills')">⭐ Great</div>
        <div class="pill" data-val="good" onclick="togglePill(this,'ratingPills')">✅ Good</div>
        <div class="pill" data-val="avg" onclick="togglePill(this,'ratingPills')">〜 Avg</div>
        <div class="pill" data-val="bad" onclick="togglePill(this,'ratingPills')">❌ Bad</div>
        <div class="pill" data-val="unrated" onclick="togglePill(this,'ratingPills')">Unrated</div>
      </div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Toggles</div>
      <div class="check-grid">
        <div class="check-item">
          <input type="checkbox" id="togOOS" onchange="applyFilters()">
          <label for="togOOS">OOS owners only</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togOwner" onchange="applyFilters()">
          <label for="togOwner">Has owner data</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togNF" onchange="applyFilters()">
          <label for="togNF">Nat'l Forest nearby</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togNoStruct" onchange="applyFilters()">
          <label for="togNoStruct">No structures</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togSaleYr" onchange="applyFilters()">
          <label for="togSaleYr">Has sale year</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togTrust" onchange="applyFilters()">
          <label for="togTrust">Estate/Trust/LLC</label>
        </div>
        <div class="check-item">
          <input type="checkbox" id="togReviewed" onchange="applyFilters()">
          <label for="togReviewed">Manually reviewed</label>
        </div>
      </div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Land Use Class</div>
      <div class="pill-group" id="luPills">
        <div class="pill active" data-val="" onclick="togglePill(this,'luPills')">All</div>
        <div class="pill" data-val="pasture" onclick="togglePill(this,'luPills')">Pasture</div>
        <div class="pill" data-val="timber" onclick="togglePill(this,'luPills')">Timber</div>
        <div class="pill" data-val="agri" onclick="togglePill(this,'luPills')">Ag/Farm</div>
        <div class="pill" data-val="vacant" onclick="togglePill(this,'luPills')">Vacant</div>
      </div>
    </div>

    <div class="filter-group">
      <div class="filter-label">Min Appraised Value</div>
      <input type="range" id="minVal" min="0" max="2000000" step="25000" value="0" oninput="syncRange(this,'minValDisp');applyFilters()">
      <div class="range-val" id="minValDisp">Any</div>
    </div>
    <div class="filter-group">
      <div class="filter-label">Max Appraised Value</div>
      <input type="range" id="maxVal" min="50000" max="5000000" step="50000" value="5000000" oninput="syncRange(this,'maxValDisp');applyFilters()">
      <div class="range-val" id="maxValDisp">Any</div>
    </div>

    <button class="btn-reset" onclick="resetFilters()">↺ Reset All Filters</button>
  </div>
</aside>

<!-- ─ MAIN AREA ─ -->
<div class="main-area">
  <div class="topbar">
    <button id="btnSaved" onclick="openSaved()" style="padding:6px 14px;background:#166534;border:2px solid #16a34a;border-radius:5px;color:#4ade80;font-size:12px;font-weight:700;cursor:pointer;font-family:var(--font-mono);letter-spacing:.5px" title="View saved properties">
      ★ SAVED PROPERTIES <span id="savedCount" style="background:#14532d;border-radius:3px;padding:1px 5px;margin-left:4px">0</span>
    </button>
    <button class="copy-btn" id="btnCopyHeaders" onclick="copyHeaders()" title="Copy column headers to clipboard for Excel">
      📋 Copy Headers
    </button>
    <button class="copy-btn" id="btnViewMap" onclick="openViewMapPanel()" title="Open selected parcel in Regrid to view property boundary lines">🗺 View Map</button>
    <div id="viewMapNote" style="font-size:9px;color:var(--muted);white-space:nowrap">← paste parcel # in Regrid search</div>
    <button class="copy-btn" id="btnFetchElev" onclick="fetchElevationAll()" title="Fetch USGS elevation for parcels with GPS coordinates">
      ⛰ Fetch Elevation
    </button>
    <span id="elevStatus" style="font-size:11px;color:var(--accent);margin-left:6px"></span>
    <span id="copyMsg" style="font-size:11px;color:var(--accent);margin-left:8px;opacity:0;transition:opacity .5s"></span>
    <button class="copy-btn" onclick="exportRatings()" title="Download your saved ratings as a backup file">⬇ Export Ratings</button>
    <label class="copy-btn" style="cursor:pointer" title="Restore ratings from a backup file">⬆ Import Ratings<input type="file" accept=".json" onchange="importRatings(event)" style="display:none"></label>
    <div class="logo">LAND SCOUT <small>v75 · {run_ts}</small><span id="capNote" style="font-size:9px;color:var(--muted);margin-left:8px"></span></div>
    <div class="stats-strip">
      <div class="stat-item"><span class="stat-n">{n_total}</span><span class="stat-l">Total</span></div>
      <div class="stat-item"><span class="stat-n" id="visCount">{n_total}</span><span class="stat-l">Shown</span></div>
      <div class="stat-item"><span class="stat-n">{raw}</span><span class="stat-l">Raw</span></div>
      <div class="stat-item"><span class="stat-n">{n_owner}</span><span class="stat-l">w/ Owner</span></div>
      <div class="stat-item"><span class="stat-n">{n_oos}</span><span class="stat-l">OOS</span></div>
      <div class="stat-item"><span class="stat-n">{n_nf}</span><span class="stat-l">NF Nearby</span></div>
    </div>
  </div>

  <div style="background:var(--surface);border-bottom:1px solid var(--border);padding:10px 20px;flex-shrink:0;text-align:center">
    <div style="font-family:var(--font-head);font-size:15px;font-weight:800;color:var(--fg);letter-spacing:.3px">Off Market Large Property Search for Southern Appalachian Mountains</div>
    <div style="font-family:var(--font-mono);font-size:10px;color:var(--muted);margin-top:3px;letter-spacing:.5px">⏱ Data Refresh Runs Overnight</div>
  </div>
  <div class="table-wrap">
    <table id="leadsTable">
    <thead>
    <tr>
      <th onclick="sortTable('combined')">Score<span class="sort-arrow">↕</span></th>
      <th style="width:64px;text-align:center;font-size:8px;line-height:1.3" title="⭐ GREAT = Make offer | ✅ GOOD = Review further | 〜 AVG = Some potential | ❌ BAD = Deal breaker">Manual<br>Review<br>Rating</th>
      <th onclick="sortTable('state')">Location<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('owner')">Owner / Contact<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('parcel_id')" style="min-width:0">Parcel ID<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('acres')">Acres<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('appr_value')">Appraised<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('vpa')">$/ac<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('mao')" class="num" style="min-width:0;padding:6px 4px">MAO 65%<span class="sort-arrow">↕</span></th>
      <th onclick="sortTable('pasture_pct')">Land &amp; Pasture<span class="sort-arrow">↕</span></th>
      <th>Investment Analysis</th>
      <th>Homestead Analysis</th>
      <th>GPS / Elev · Temps</th>
      <th>Road Distance</th>
      <th>Signals <button onclick="event.stopPropagation();openSignalDefs()" style="padding:1px 5px;font-size:8px;background:#1e3a5f;border:1px solid #38bdf8;border-radius:3px;cursor:pointer;color:#93c5fd;font-family:var(--font-mono)" title="What do these signals mean?">?</button></th>
    </tr>
    </thead>
    <tbody id="tbody"></tbody>
    </table>
    <div class="no-results" id="noResults" style="display:none">
      <h3>🌾 No properties match</h3>
      <p>Try relaxing your filters</p>
    </div>
  </div>

  <details class="gaps-bar">
    <summary>⚠ Data Gaps ({len(gaps)}) — click to expand</summary>
    <div class="gaps-panel"><ul>{gap_html}</ul></div>
  </details>
</div>
</div>

<script>
const DATA = {leads_json};

// ── Nearby towns data + haversine ──────────────────────────────────────
const NEARBY_TOWNS_DATA = {{
  "AL_DeKalb": [["Rainsville AL",34.494,-85.849,5000], ["Fort Payne AL",34.443,-85.72,14000], ["Huntsville AL",34.73,-86.586,190000]],
  "AL_Jackson": [["Stevenson AL",34.869,-85.834,2000], ["Scottsboro AL",34.673,-86.034,15000], ["Huntsville AL",34.73,-86.586,190000]],
  "AL_Madison": [["Owens Cross Roads AL",34.622,-86.499,1600], ["Madison AL",34.699,-86.748,48000], ["Huntsville AL",34.73,-86.586,190000]],
  "AL_Cherokee": [["Centre AL",34.154,-85.679,3400],["Gadsden AL",34.014,-85.999,33000],["Birmingham AL",33.52,-86.803,212000]],
  "AL_Etowah":   [["Gadsden AL",34.014,-85.999,33000],["Attalla AL",34.017,-86.087,6000],["Birmingham AL",33.52,-86.803,212000]],
  "AL_Marshall": [["Guntersville AL",34.354,-86.294,8000], ["Albertville AL",34.267,-86.209,21000], ["Huntsville AL",34.73,-86.586,190000]],
  "GA_Fannin":  [["Blue Ridge GA",34.866,-84.326,1300],["Ellijay GA",34.694,-84.478,1600],["Chattanooga TN",35.045,-85.309,181000]],
  "GA_Gilmer":  [["Ellijay GA",34.694,-84.478,1600],["Blue Ridge GA",34.866,-84.326,1300],["Chattanooga TN",35.045,-85.309,181000]],
  "GA_Union":   [["Blairsville GA",34.876,-83.958,700],["Hiawassee GA",34.950,-83.755,900],["Gainesville GA",34.298,-83.824,42000]],
  "GA_Towns":   [["Hiawassee GA",34.950,-83.755,900],["Blairsville GA",34.876,-83.958,700],["Gainesville GA",34.298,-83.824,42000]],
  "GA_Lumpkin": [["Dahlonega GA",34.532,-83.985,7000],["Gainesville GA",34.298,-83.824,42000],["Atlanta GA",33.749,-84.388,498000]],
  "NC_Cherokee": [["Murphy NC",35.088,-84.016,1600], ["Dalton GA",34.77,-84.97,34000], ["Chattanooga TN",35.045,-85.309,181000]],
  "NC_Clay": [["Hayesville NC",35.049,-83.818,300], ["Dalton GA",34.77,-84.97,34000], ["Atlanta GA",33.749,-84.388,498000]],
  "NC_Graham": [["Robbinsville NC",35.323,-83.804,600], ["Murphy NC",35.088,-84.016,1600], ["Dalton GA",34.77,-84.97,34000], ["Atlanta GA",33.749,-84.388,498000]],
  "NC_Haywood": [["Lake Junaluska NC",35.526,-82.96,2600], ["Waynesville NC",35.489,-82.986,10000], ["Charlotte NC",35.227,-80.843,874000]],
  "NC_Jackson": [["Sylva NC",35.374,-83.222,2600], ["Waynesville NC",35.489,-82.986,10000], ["Charlotte NC",35.227,-80.843,874000]],
  "NC_Macon": [["Franklin NC",35.183,-83.381,4000], ["Waynesville NC",35.489,-82.986,10000], ["Atlanta GA",33.749,-84.388,498000]],
  "NC_Madison": [["Marshall NC",35.79,-82.682,800], ["Asheville NC",35.579,-82.551,94000], ["Charlotte NC",35.227,-80.843,874000]],
  "NC_Watauga":   [["Boone NC",36.210,-81.675,20000],["Blowing Rock NC",36.134,-81.677,1300],["Charlotte NC",35.227,-80.843,874000]],
  "NC_Avery":     [["Banner Elk NC",36.163,-81.872,1200],["Newland NC",36.087,-81.923,700],["Johnson City TN",36.313,-82.353,71000]],
  "NC_Ashe":      [["West Jefferson NC",36.402,-81.498,1300],["Jefferson NC",36.422,-81.477,1600],["Boone NC",36.210,-81.675,20000]],
  "NC_Alleghany": [["Sparta NC",36.506,-81.119,1800],["West Jefferson NC",36.402,-81.498,1300],["Boone NC",36.210,-81.675,20000]],
  "NC_Mitchell": [["Spruce Pine NC",35.917,-82.064,2000], ["Asheville NC",35.579,-82.551,94000], ["Charlotte NC",35.227,-80.843,874000]],
  "NC_Swain": [["Bryson City NC",35.432,-83.446,1500], ["Waynesville NC",35.489,-82.986,10000], ["Atlanta GA",33.749,-84.388,498000]],
  "NC_Yancey": [["Burnsville NC",35.912,-82.299,1600], ["Asheville NC",35.579,-82.551,94000], ["Charlotte NC",35.227,-80.843,874000]],
  "SC_Anderson": [["Williamston SC",34.619,-82.477,4000], ["Anderson SC",34.503,-82.65,27000], ["Charlotte NC",35.227,-80.843,874000]],
  "SC_Greenville": [["Taylors SC",34.918,-82.294,5000], ["Greenville SC",34.852,-82.394,70000], ["Charlotte NC",35.227,-80.843,874000]],
  "SC_Oconee": [["Walhalla SC",34.765,-83.069,3500], ["Greenville SC",34.852,-82.394,70000], ["Charlotte NC",35.227,-80.843,874000]],
  "SC_Pickens": [["Pickens SC",34.884,-82.707,3200], ["Easley SC",34.83,-82.601,20000], ["Charlotte NC",35.227,-80.843,874000]],
  "SC_Spartanburg": [["Glendale SC",34.949,-81.85,900], ["Spartanburg SC",34.949,-81.932,38000], ["Charlotte NC",35.227,-80.843,874000]],
  "VA_Lee":        [["Jonesville VA",36.687,-83.108,1000],["Middlesboro KY",36.608,-83.717,10000],["Kingsport TN",36.548,-82.562,54000]],
  "VA_Scott":      [["Gate City VA",36.643,-82.576,2000],["Kingsport TN",36.548,-82.562,54000],["Johnson City TN",36.328,-82.353,66000]],
  "VA_Wise":       [["Wise VA",36.976,-82.574,3000],["Norton VA",36.933,-82.628,3700],["Kingsport TN",36.548,-82.562,54000]],
  "VA_Washington": [["Abingdon VA",36.709,-81.977,8000],["Bristol TN",36.595,-82.188,27000],["Johnson City TN",36.328,-82.353,66000]],
  "VA_Russell":    [["Lebanon VA",36.899,-82.079,3200],["Abingdon VA",36.709,-81.977,8000],["Kingsport TN",36.548,-82.562,54000]],
  "TN_Blount": [["Alcoa TN",35.789,-83.974,9600], ["Maryville TN",35.757,-84.0,29000], ["Knoxville TN",35.96,-83.921,190000]],
  "TN_Knox": [["Powell TN",36.031,-83.989,9000], ["Maryville TN",35.757,-84.0,29000], ["Knoxville TN",35.96,-83.921,190000]],
  "TN_Sevier": [["Gatlinburg TN",35.714,-83.512,4000], ["Sevierville TN",35.868,-83.562,17000], ["Knoxville TN",35.96,-83.921,190000]]
}};

function haversineMi(lat1,lng1,lat2,lng2) {{
  const R=3958.8,dLat=(lat2-lat1)*Math.PI/180,dLng=(lng2-lng1)*Math.PI/180;
  const sL=Math.sin(dLat/2),sG=Math.sin(dLng/2);
  return R*2*Math.asin(Math.sqrt(sL*sL+Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*sG*sG));
}}

function calcTownDistances() {{
  let n=0;
  DATA.forEach(d => {{
    if (!d.lat || !d.lng) return;
    const key = d.state+'_'+d.county;
    const list = NEARBY_TOWNS_DATA[key];
    if (!list) return;
    const tiers = {{}};
    list.forEach(([name,tlat,tlng,pop]) => {{
      const rd = Math.round(haversineMi(d.lat,d.lng,tlat,tlng)*1.35*10)/10;
      const tier = pop<10000?'small':pop<100000?'medium':'large';
      if (!tiers[tier] || rd<tiers[tier].dist_mi) tiers[tier]={{name,dist_mi:rd}};
    }});
    d.towns = tiers; n++;
  }});
  return n;
}}

// Rebuild Regrid URL for a record after lat/lng becomes available
function updateRegridUrl(d) {{
  if (!d.lat || !d.lng) return;
  const base = (d.regrid_url||'').split('/@')[0].split('?')[0];
  const search = d.parcel_id ? '?search=' + encodeURIComponent(d.parcel_id) : '';
  d.regrid_url = base + '/@' + d.lat.toFixed(5) + ',' + d.lng.toFixed(5) + ',17z' + search;
  // Also update Google Earth URL with real coords
  d.google_url = 'https://earth.google.com/web/@' + d.lat.toFixed(5) + ',' + d.lng.toFixed(5) + ',300a,1200d,35y,0h,0t,0r';
}}

// ── On-demand elevation fetch (USGS EPQS) ────────────────────────────────────
async function fetchElevationAll() {{
  const btn = document.getElementById('btnFetchElev');
  const status = document.getElementById('elevStatus');
  // Get ONLY rows currently visible in viewport, max 5
  const allTrs = Array.from(document.querySelectorAll('#tbody tr'));
  const vpTrs = allTrs.filter(tr => {{
    const r = tr.getBoundingClientRect();
    return r.top >= 0 && r.bottom <= (window.innerHeight || document.documentElement.clientHeight);
  }}).slice(0, 5);
  if (!vpTrs.length) {{ status.textContent='⚠ Scroll to rows first'; return; }}
  // Get data for visible rows via ROWS index
  const vpLeads = vpTrs.map(tr => {{
    const idx = tr.dataset.rowidx;
    return (idx !== undefined) ? ROWS[idx] : null;
  }}).filter(d => d && d.lat && d.lng);
  if (!vpLeads.length) {{ status.textContent='⚠ No GPS on visible rows'; return; }}
  btn.disabled=true; btn.textContent='⛰ Fetching...'; status.textContent='Fetching...';
  for (let i=0; i<vpLeads.length; i++) {{
    const d = vpLeads[i];
    const tr = vpTrs[i];
    status.textContent = `Fetching ${{i+1}}/${{vpLeads.length}}...`;
    try {{
      const url = `https://epqs.nationalmap.gov/v1/json?x=${{d.lng}}&y=${{d.lat}}&units=Feet&includeDate=false`;
      const ft = parseFloat((await (await fetch(url)).json())?.value);
      if (!isNaN(ft) && ft > -1000) {{
        d.elevation_ft = Math.round(ft);
        d.elev_min = Math.round(ft);
        d.elev_max = Math.round(ft);
        updateRegridUrl(d);
        reapplyHighlights();
        // Update GPS cell inline — find the GPS cell and inject elevation
        const gpsCell = tr.querySelector('.gps-cell');
        if (gpsCell) {{
          const elevSpan = document.createElement('span');
          elevSpan.textContent = ` ▲${{Math.round(ft)}}ft`;
          elevSpan.style.cssText = 'display:inline-block;font-size:15px;font-weight:700;color:#22c55e;background:#052e16;padding:2px 6px;border-radius:4px;margin-left:4px';
          gpsCell.appendChild(elevSpan);
          // Flash highlight for 3 seconds
          elevSpan.style.animation = 'none';
          elevSpan.style.outline = '2px solid #22c55e';
          setTimeout(()=>{{ elevSpan.style.outline=''; elevSpan.style.color='#86efac'; elevSpan.style.fontSize='11px'; elevSpan.style.fontWeight='500'; }}, 3000);
        }}
      }}
    }} catch(e) {{ /* silent fail */ }}
    await new Promise(r=>setTimeout(r,120));
  }}
  btn.disabled=false;
  btn.textContent='⛰ Fetch Elevation';
  status.textContent = `✓ Done (${{vpLeads.length}} rows)`;
}}


// ── State ──────────────────────────────────────────────────────────
let sortKey = 'd_score', sortDir = 1;  // 1=desc (highest first)

// ── Init ───────────────────────────────────────────────────────────
function init() {{
  // Cap display at 2000 rows to prevent browser hang on large datasets
  // JSON already capped at 500 records server-side for browser performance
  calcTownDistances();
  buildCountyChecks();
  applyFilters();
  updateSavedCount();
  // Restore STATUSES from SAVED on load
  Object.entries(SAVED).forEach(([pid,e]) => {{ STATUSES[pid] = e.status; }});
  // Re-render so status buttons show saved state
  applyFilters();
}}

function filterByState(st) {{
  const checks = document.querySelectorAll('#countyChecks input');
  checks.forEach(cb => {{
    const cbState = cb.value.split('/')[0];
    cb.checked = st === 'ALL' || cbState === st;
  }});
  applyFilters();
}}

function buildCountyChecks() {{
  // Build state filter buttons
  const stateDiv = document.getElementById('stateButtons');
  const states = [...new Set(DATA.map(d => d.state))].sort();
  stateDiv.innerHTML = ['ALL',...states].map(function(st){{
    var label = st==='ALL'?'All':st;
    return '<button class="copy-btn" style="padding:3px 8px;font-size:10px" '
      +'data-st="'+st+'" '
      +'onclick="filterByState(this.dataset.st)" '
      +'title="Filter to '+label+'">'+label+'</button>';
  }}).join('');

  const container = document.getElementById('countyChecks');
  const counties = [...new Set(DATA.map(d => d.state + '/' + d.county))].sort();
  container.innerHTML = counties.map(c => `
    <div class="check-item">
      <input type="checkbox" id="c_${{c.replace('/','_')}}" value="${{c}}" checked onchange="applyFilters()">
      <label for="c_${{c.replace('/','_')}}">${{c}}</label>
    </div>`).join('');
}}

// ── Climate score helper ──────────────────────────────────────────────────
function effectiveClimateScore(d) {{
  var cs = d.climate_score || 5;
  var e = d.elev_min || d.elevation_ft;
  if (e && e > 0) {{
    var bonus = Math.min(3, Math.max(0, ((e - 1000) / 1000) * 0.5));
    cs = Math.min(10, Math.round((cs + bonus) * 10) / 10);
  }}
  return cs;
}}

function reapplyHighlights() {{
  var trs = document.querySelectorAll('#tbody tr');
  (VISIBLE_DATA || []).forEach(function(d, i) {{
    if (!trs[i]) return;
    var cs = effectiveClimateScore(d);
    var st = STATUSES[d.parcel_id] || '';
    if (st === 'great' && cs >= 9) {{
      trs[i].style.outline = '2px solid #f59e0b';
      trs[i].style.outlineOffset = '-1px';
      trs[i].style.boxShadow = '0 0 10px rgba(245,158,11,0.35)';
    }} else {{
      trs[i].style.outline = '';
      trs[i].style.boxShadow = '';
    }}
  }});
}}

// ── Filter engine ──────────────────────────────────────────────────
function applyFilters() {{
  const minAcres   = +document.getElementById('minAcres').value;
  const maxVpa     = +document.getElementById('maxVpa').value;
  const minClimate = +document.getElementById('minClimate').value;
    const minHScore  = +document.getElementById('minHScore').value;
  const minIScore  = +document.getElementById('minIScore').value;
  const minVal     = +document.getElementById('minVal').value;
  const maxVal     = +document.getElementById('maxVal').value;
  const minDistress= +document.getElementById('minDistress').value;
  const togOOS     = document.getElementById('togOOS').checked;
  const togOwner   = document.getElementById('togOwner').checked;
  const togNF      = document.getElementById('togNF').checked;
  const togNoStruct= document.getElementById('togNoStruct').checked;
  const togSaleYr  = document.getElementById('togSaleYr').checked;
  const togTrust   = document.getElementById('togTrust').checked;
  const togReviewed = document.getElementById('togReviewed').checked;

  const confPill = document.querySelector('#confPills .pill.active')?.dataset.val || '';
  const ratingPill = document.querySelector('#ratingPills .pill.active')?.dataset.val || '';
  const luPill   = document.querySelector('#luPills .pill.active')?.dataset.val   || '';
  const sortSel  = document.getElementById('sortBy').value;

  const activeCounies = new Set(
    [...document.querySelectorAll('#countyChecks input:checked')].map(e => e.value)
  );

  let rows = DATA.filter(d => {{
    if (!activeCounies.has(d.state + '/' + d.county)) return false;
    if (d.acres < minAcres) return false;
    if (maxVpa < 20000 && d.vpa > 0 && d.vpa > maxVpa) return false;
    if (minClimate > 0 && effectiveClimateScore(d) < minClimate) return false;
    if (d.h_score < minHScore) return false;
    if (d.score < minIScore) return false;
    if (d.appr_value < minVal) return false;
    if (maxVal < 5000000 && d.appr_value > maxVal) return false;
    if (minDistress > 0 && (d.d_score||0) < minDistress) return false;
    if (togOOS && !d.oos) return false;
    if (togOwner && !d.has_owner) return false;
    if (togNF && !d.nf_nearby) return false;
    if (togNoStruct && d.has_structure) return false;
    if (togSaleYr && !d.has_sale_year) return false;
    if (togReviewed) {{
      const st = STATUSES[d.parcel_id];
      if (!st || !['great','good','avg','bad'].includes(st)) return false;
    }}
    if (togTrust) {{
      const o = d.owner.toUpperCase();
      if (!o.includes('LLC') && !o.includes('TRUST') && !o.includes('ESTATE') && !o.includes('HEIRS')) return false;
    }}
    if (confPill && d.confidence !== confPill) return false;
    if (ratingPill) {{
      const st = STATUSES[d.parcel_id] || '';
      if (ratingPill === 'unrated') {{ if (st) return false; }}
      else {{ if (st !== ratingPill) return false; }}
    }}
    if (luPill) {{
      const lu = (d.lu_class || '').toLowerCase();
      if (luPill === 'pasture' && !lu.includes('past') && !lu.includes('grass') && !lu.includes('range')) return false;
      if (luPill === 'timber' && !lu.includes('timber') && !lu.includes('forest')) return false;
      if (luPill === 'agri' && !lu.includes('agri') && !lu.includes('farm') && !lu.includes('crop')) return false;
      if (luPill === 'vacant' && !lu.includes('vacant') && !lu.includes('undevel')) return false;
    }}
    return true;
  }});

  // Sort
  const sk = sortSel || sortKey;
  rows.sort((a, b) => {{
    let av = a[sk]??0, bv = b[sk]??0;
    if (typeof av === 'string') {{ av = av.toLowerCase(); bv = (bv||"").toLowerCase(); }}
    // sortDir=1 → descending (highest first, default for scores)
    // sortDir=-1 → ascending (for price, vpa lowest-first)
    if (av < bv) return sortDir;   // sortDir=1: b before a → highest first
    if (av > bv) return -sortDir;
    return 0;
  }});

  renderRows(rows);
  document.getElementById('visCount').textContent = rows.length;
  document.getElementById('noResults').style.display = rows.length ? 'none' : 'flex';
  reapplyHighlights();
}}

function sortTable(key) {{
  if (sortKey === key) sortDir *= -1; else {{ sortKey = key; sortDir = 1; }}
  document.querySelectorAll('thead th').forEach(th => th.classList.remove('sorted'));
  applyFilters();
}}

// ── Render rows ────────────────────────────────────────────────────
function fmt(n) {{ return n >= 1e6 ? '$'+( n/1e6).toFixed(1)+'M' : n >= 1e3 ? '$'+(n/1e3).toFixed(0)+'K' : '$'+n.toFixed(0); }}
function fmtAc(n) {{ return n >= 1000 ? (n/1000).toFixed(1)+'K' : n.toFixed(0); }}

function scoreRing(val, color) {{
  const r = 17, c = 2*Math.PI*r;
  const pct = val/99;
  return `<div class="score-ring">
    <svg width="44" height="44" viewBox="0 0 44 44">
      <circle class="ring-bg" cx="22" cy="22" r="${{r}}"/>
      <circle class="ring-val" cx="22" cy="22" r="${{r}}"
        style="stroke:${{color}};stroke-dasharray:${{c}};stroke-dashoffset:${{c*(1-pct)}}" />
    </svg>
    <span class="score-num" style="color:${{color}}">${{val}}</span>
  </div>`;
}}

function waterDots(score) {{
  return Array.from({{length:10}}, (_,i) =>
    `<div class="water-dot" style="background:${{i<score?'#67e8f9':'var(--border)'}};opacity:${{i<score?1:.3}}"></div>`
  ).join('');
}}

function renderRows(rows) {{
  const tb = document.getElementById('tbody');
  VISIBLE_DATA = rows.slice();
  if (!rows.length) {{ tb.innerHTML = ''; return; }}
  ROWS = {{}};
  tb.innerHTML = rows.map((d,i) => {{
    ROWS[i] = d; // i = row index into ROWS
    const confColor = {{HIGH:'#22c55e',MED:'#f59e0b',LOW:'#ef4444'}}[d.confidence] || '#888';
    const mailParts = [d.mail_addr, d.mail_city, d.mail_state, d.mail_zip].filter(Boolean);
    const luTag = d.lu_class ? '<span class="lu-tag">'+esc(d.lu_class)+'</span>' : '';

    // Trust/LLC/Estate tag
    const o = d.owner.toUpperCase();
    const ownerType = o.includes('LLC') ? 'LLC' : o.includes('TRUST') ? 'TRUST'
      : o.includes('ESTATE') || o.includes('HEIRS') ? 'ESTATE' : o.includes('CORP') ? 'CORP' : '';

    const analysisList = d.analysis.map(a => {{
      const cls = a.startsWith('✓') ? 'good' : a.startsWith('⚠') || a.startsWith('✗') ? 'bad'
        : a.startsWith('△') ? 'warn' : '';
      return `<li class="${{cls}}">${{esc(a)}}</li>`;
    }}).join('');

    const hList = d.h_analysis.map(a => {{
      const cls = a.startsWith('✓') ? 'good' : a.startsWith('✗') ? 'bad' : a.startsWith('△') ? 'warn' : '';
      return `<li class="${{cls}}">${{esc(a)}}</li>`;
    }}).join('');

    return `<tr data-rowidx="${{i}}">
<td>
  <div class="score-wrap">
    ${{scoreRing(d.combined,'#fbbf24')}}
    <div class="badge-row">
      <span class="badge badge-h">H${{d.h_score}}</span>
      <span class="badge badge-i">I${{d.score}}</span>
      <span class="badge" style="background:#7f1d1d;color:#fca5a5">M${{d.d_score||0}}</span>
    </div>
    <span class="badge badge-conf" style="background:${{confColor}}">${{d.confidence}}</span>
    <div style="font-size:9px;margin-top:3px;line-height:1.5">${{(d.signals||[]).map(s=>'<span class="sbadge" style="font-size:8px">'+(s[0]||s)+'</span>').join(' ')}}</div>
  </div>
</td>
<td style="text-align:center;vertical-align:middle;padding:6px 4px">
  ${{(function(){{var st=STATUSES[d.parcel_id]||'';
    var cls='status-btn'+(st?' '+st:'');
    var lbl=STATUS_ICON[st]||'○';
    return '<button class="'+cls+'" data-pid="'+esc(d.parcel_id)+'" '
      +'onclick="var b=this;var p=b.dataset.pid;toggleStatus(p,b)" '
      +'title="'+(st?STATUS_LABEL[st]:'Click to rate: ⭐GREAT ✅GOOD 〜AVG ❌BAD')+'">' +lbl+'</button>';
  }})()}}
</td><td>
  <button class="copy-btn" style="padding:2px 7px;font-size:9px;margin-bottom:4px"
    onclick="copyRow(${{i}})" title="Copy row to clipboard (paste into Excel)">📋 Copy</button>
  <div class="loc-county">${{esc(d.state)}} · ${{esc(d.county)}}</div>
  <div class="loc-addr">${{esc(d.address||d.county)}}</div>
  ${{d.city ? '<div style="color:var(--sky);font-size:9px">'+esc(d.city)+'</div>' : ''}}
  ${{d.oos ? '<span class="badge badge-oos">OOS: '+esc(d.mail_state)+'</span>' : ''}}
  ${{d.nf_nearby ? '<span class="badge badge-nf">🌲 '+esc(d.nf_name)+'</span>' : ''}}
  <div class="loc-src">${{esc(d.source_name)}}</div>
</td>
<td>
  <div class="owner-name">${{d.owner ? esc(d.owner) : '<span style="color:var(--dim)">— see assessor —</span>'}}</div>
  ${{ownerType ? '<span class="badge badge-h" style="font-size:9px">'+ownerType+'</span>' : ''}}
  ${{mailParts.length ? '<div class="owner-mail">'+esc(mailParts.join(', '))+'</div>' : ''}}
  <div class="owner-sale">Acquired: ${{d.sale_year||'—'}}</div>
</td>
<td class="pid-col">
  <div class="parcel-id" style="font-size:10px">${{esc(d.parcel_id)||'—'}}</div>
  <button data-cpid="${{esc(d.parcel_id||'')}}" onclick="var p=this.dataset.cpid;navigator.clipboard.writeText(p).then(()=>{{this.textContent='✓';setTimeout(()=>this.textContent='📋',1200)}});" style="padding:1px 5px;font-size:9px;background:var(--surface2);border:1px solid var(--border);border-radius:3px;cursor:pointer;color:var(--sky);font-family:var(--font-mono);margin-top:3px" title="Copy parcel ID">📋</button>
  ${{luTag}}
</td>
<td class="num" style="padding:6px 4px">${{fmtAc(d.acres)}}</td>
<td class="num" style="padding:6px 4px">${{d.appr_value > 0 ? fmt(d.appr_value) : '—'}}</td>
<td class="num" style="padding:6px 4px">${{d.vpa > 0 ? '$'+d.vpa.toFixed(0) : '—'}}</td>
<td class="num" style="padding:6px 4px">${{d.mao > 0 ? fmt(d.mao) : '—'}}</td>
<td class="soil-cell">
  <div class="soil-bars">
    <div class="soil-bar-row">
      <span class="soil-bar-label">Open</span>
      <div class="soil-bar-track"><div class="soil-bar-fill" style="width:${{d.pasture_pct}}%;background:linear-gradient(90deg,#4ade80,#16a34a)"></div></div>
      <span class="soil-bar-num">${{d.pasture_pct}}%</span>
    </div>
    <div class="soil-bar-row">
      <span class="soil-bar-label">Wooded</span>
      <div class="soil-bar-track"><div class="soil-bar-fill" style="width:${{100-d.pasture_pct}}%;background:linear-gradient(90deg,#713f12,#a16207)"></div></div>
      <span class="soil-bar-num">${{100-d.pasture_pct}}%</span>
    </div>
    <div class="soil-bar-row">
      <span class="soil-bar-label">Water</span>
      <div class="water-dots">${{waterDots(d.water_score)}}</div>
      <span class="soil-bar-num">${{d.water_score}}/10</span>
    </div>
  </div>
  <div class="soil-name">${{esc(d.soil)}}</div>
  ${{d.soil_score ? '<span class="badge" style="background:#1c4532;color:#6ee7b7;font-size:9px">🌱Soil '+d.soil_score+'/10</span>' : ''}}
  <div style="font-size:10px;color:var(--muted);margin-top:2px">${{esc(d.climate)}}</div>
  <div style="font-size:10px;margin-top:3px">
    ${{d.streams>0?'<span style="color:#38bdf8">💧'+d.streams+' stream'+(d.streams>1?'s':'')+'</span> ':''}}${{d.waterbodies>0?'<span style="color:#818cf8">🏞 '+d.waterbodies+' pond'+(d.waterbodies>1?'s':'')+'</span>':''}}${{d.streams===0&&d.waterbodies===0&&d.lat?'<span style="color:#6b7280;font-size:9px">no water</span>':''}}${{d.is_vacant?'<br><span style="color:#a3e635;font-size:9px">✓ Vacant</span>':'<br><span style="color:#f97316;font-size:9px">⚑ Structure</span>'}}
  </div>
</td>
<td class="analysis-wrap">
  <details>
    <summary>Investment</summary>
    <ul class="analysis-list">${{analysisList}}</ul>
  </details>
</td>
<td class="analysis-wrap">
  <details>
    <summary>Homestead</summary>
    <ul class="analysis-list">${{hList}}</ul>
  </details>
</td>
<td class="gps-cell" style="font-size:9px;line-height:1.4;white-space:nowrap;min-width:0">
  ${{d.lat
    ? '<a href="https://www.google.com/maps/place/'+d.lat+','+d.lng+'/@'+d.lat+','+d.lng+',14z" target="_blank" style="color:var(--sky)">'+d.lat+','+d.lng+'</a>'
    : '<span style="color:#666">—</span>'}}<br>
  ${{d.elev_min != null ? '<span style="color:#86efac">▼'+d.elev_min.toLocaleString()+' ft</span>' : ''}}
  ${{d.elev_max != null ? '<span style="color:#fde68a"> ▲'+d.elev_max.toLocaleString()+' ft</span>' : ''}}<br>
  ${{d.summer_high ? '<span style="color:#f97316;font-size:10px">☀'+d.summer_high+'° </span>' : ''}}
  ${{d.winter_low != null ? '<span style="color:#93c5fd;font-size:10px">❄'+d.winter_low+'°</span>' : ''}}<br>
  <span style="color:#4ade80;font-size:10px;font-weight:600">🌡 Climate ${{effectiveClimateScore(d)}}/10</span>
</td>
<td style="font-size:9px;line-height:1.5;white-space:nowrap;min-width:80px">
  ${{(d.towns&&d.towns.small)
    ? '<span style="color:#a3e635">🏘 '+esc(d.towns.small.name)+'</span><br><span style="color:var(--muted)">~'+d.towns.small.dist_mi+' mi road</span><br>' : ''}}
  ${{(d.towns&&d.towns.medium)
    ? '<span style="color:#fbbf24">🏙 '+esc(d.towns.medium.name)+'</span><br><span style="color:var(--muted)">~'+d.towns.medium.dist_mi+' mi road</span><br>' : ''}}
  ${{(d.towns&&d.towns.large)
    ? '<span style="color:#60a5fa">🌆 '+esc(d.towns.large.name)+'</span><br><span style="color:var(--muted)">~'+d.towns.large.dist_mi+' mi road</span>' : ''}}
</td>
<td class="sig-cell" style="min-width:0;max-width:100px">${{(d.signals||[]).map(s => {{
  const t = s[0]; const bg =
    t.startsWith('PROBATE') || t.startsWith('ESTATE') ? '#7f1d1d' :
    t.startsWith('OOS') || t.startsWith('LONG') || t.startsWith('TRUST') ? '#78350f' :
    t.startsWith('LLC') || t.startsWith('CORP') ? '#713f12' :
    t.startsWith('NO OWNER') ? '#500' : '#1e3a5f';
  return '<span class="sbadge" style="background:'+bg+'">'+esc(t)+'</span>';
}}).join(' ')}}${{!d.has_owner ? ' <span class="sbadge" style="background:#7f1d1d">NO OWNER</span>' : ''}}</td>

</tr>`;
  }}).join('');
}}

// ── Saved Properties Panel ──────────────────────────────────────────────────
// PERMANENT key — never changes across versions
const SAVED_KEY = 'land_scout_saved';
let SAVED = {{}};
// Recover data from any old versioned key
(function(){{
  const candidates = ['land_scout_saved','land_scout_saved_v45','land_scout_saved_v46',
    'land_scout_saved_v47','land_scout_saved_v48','land_scout_saved_v49',
    'land_scout_saved_v50','land_scout_saved_v51','land_scout_saved_v52',
    'land_scout_saved_v53','land_scout_saved_v54','land_scout_saved_v55',
    'land_scout_saved_v56','land_scout_saved_v57','land_scout_saved_v58'];
  for (const _k of candidates) {{
    try {{ const d=JSON.parse(localStorage.getItem(_k)||'{{}}'); Object.assign(SAVED,d); }} catch(e){{}}
  }}
  // Seed: saved properties keyed by parcel_id (matches runtime)
  const _SEED={{"1":{{"status":"bad","data":{{"state":"SC","county":"Spartanburg","owner":"ENOREE RAPIDS LLC ETAL","acres":466.96,"parcel_id":"1","signals":[]}},"ts":1700000000000}},"57":{{"status":"bad","data":{{"state":"SC","county":"Spartanburg","owner":"MICHELIN TIRE CORP","acres":147.74,"parcel_id":"57","signals":[]}},"ts":1700000000000}},"452100845603000":{{"status":"good","data":{{"state":"NC","county":"Cherokee","owner":"TATUM MICHELLE ET AL","acres":324.02,"parcel_id":"452100845603000","signals":[]}},"ts":1700000000000}},"454300637265000":{{"status":"great","data":{{"state":"NC","county":"Cherokee","owner":"DICKEY HELEN MILLSAPS ET AL","acres":509.15,"parcel_id":"454300637265000","signals":[]}},"ts":1700000000000}},"453200719036000":{{"status":"avg","data":{{"state":"NC","county":"Cherokee","owner":"VULTURES ROW LLC","acres":186.59,"parcel_id":"453200719036000","signals":[]}},"ts":1700000000000}},"453100383010000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"HORNETS NEST LLC","acres":736.36,"parcel_id":"453100383010000","signals":[]}},"ts":1700000000000}},"451200919107000":{{"status":"good","data":{{"state":"NC","county":"Cherokee","owner":"MOUNTAIN FARMS LLC","acres":916.68,"parcel_id":"451200919107000","signals":[]}},"ts":1700000000000}},"452100801226000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"SEIME ELIZABETH ANN TRUSTEE","acres":107.99,"parcel_id":"452100801226000","signals":[]}},"ts":1700000000000}},"454400356625000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"MATHESON MICHAEL M","acres":125.12,"parcel_id":"454400356625000","signals":[]}},"ts":1700000000000}},"443900595528000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"APAC-TENNESSEE INC","acres":229.6,"parcel_id":"443900595528000","signals":[]}},"ts":1700000000000}},"458600501685000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"NORTH RIDGE MOUNTAINS LLC","acres":101.61,"parcel_id":"458600501685000","signals":[]}},"ts":1700000000000}},"455600120978000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"METAL GROUP LLC","acres":280.16,"parcel_id":"455600120978000","signals":[]}},"ts":1700000000000}},"458000450045000":{{"status":"good","data":{{"state":"NC","county":"Cherokee","owner":"HARTNESS EDWARD L TRUSTEE","acres":311.46,"parcel_id":"458000450045000","signals":[]}},"ts":1700000000000}},"7518-46-6726":{{"status":"good","data":{{"state":"NC","county":"Jackson","owner":"SMISSON REAL ESTATE INV LLC","acres":542.51,"parcel_id":"7518-46-6726","signals":[]}},"ts":1700000000000}},"457600259472000":{{"status":"good","data":{{"state":"NC","county":"Cherokee","owner":"ADAMS REAL ESTATE HOLDINGS NC LLC","acres":107.24,"parcel_id":"457600259472000","signals":[]}},"ts":1700000000000}},"453500272277000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"LAKE APALACHIA LLC","acres":179.52,"parcel_id":"453500272277000","signals":[]}},"ts":1700000000000}},"6548456266":{{"status":"good","data":{{"state":"NC","county":"Macon","owner":"GROVE FAMILY TRUST","acres":204.89,"parcel_id":"6548456266","signals":[]}},"ts":1700000000000}},"459102488021000":{{"status":"bad","data":{{"state":"NC","county":"Cherokee","owner":"WILDCAT RIDGE INVESTMENTS LLC","acres":151.15,"parcel_id":"459102488021000","signals":[]}},"ts":1700000000000}},"7575-38-7176":{{"status":"bad","data":{{"state":"NC","county":"Jackson","owner":"KENNEDY J PATRICK TRUSTEE","acres":592.95,"parcel_id":"7575-38-7176","signals":[]}},"ts":1700000000000}},"7574-93-7688":{{"status":"avg","data":{{"state":"NC","county":"Jackson","owner":"CARLTON PATRICK E TRUSTEE","acres":413.86,"parcel_id":"7574-93-7688","signals":[]}},"ts":1700000000000}},"8507-82-8235":{{"status":"bad","data":{{"state":"NC","county":"Jackson","owner":"WHITAKER KENNETH A TRUSTEE","acres":305.12,"parcel_id":"8507-82-8235","signals":[]}},"ts":1700000000000}},"8743270779":{{"status":"good","data":{{"state":"NC","county":"Madison","owner":"FERGUSON BERNARD W TRUST","acres":417.34,"parcel_id":"8743270779","signals":[]}},"ts":1700000000000}},"7575-17-4368":{{"status":"bad","data":{{"state":"NC","county":"Jackson","owner":"KENNEDY J PATRICK TRUSTEE","acres":252.65,"parcel_id":"7575-17-4368","signals":[]}},"ts":1700000000000}},"7584-55-6183":{{"status":"bad","data":{{"state":"NC","county":"Jackson","owner":"CARLTON PATRICK E TRUSTEE","acres":230.98,"parcel_id":"7584-55-6183","signals":[]}},"ts":1700000000000}},"7569-37-5777":{{"status":"good","data":{{"state":"NC","county":"Jackson","owner":"ELMO TRUST LLC","acres":215.9,"parcel_id":"7569-37-5777","signals":[]}},"ts":1700000000000}},"8742872813":{{"status":"good","data":{{"state":"NC","county":"Madison","owner":"JOHN C ADLER REVOCABLE LIVING TRUST","acres":457.71,"parcel_id":"8742872813","signals":[]}},"ts":1700000000000}},"0835-00-58-2710":{{"status":"avg","data":{{"state":"NC","county":"Mitchell","owner":"JOHNSON ZACHARY TRUSTEES","acres":440.91,"parcel_id":"0835-00-58-2710","signals":[]}},"ts":1700000000000}},"9719506848":{{"status":"great","data":{{"state":"NC","county":"Madison","owner":"BERRY CAROLYN TRUSTEE","acres":236.03,"parcel_id":"9719506848","signals":[]}},"ts":1700000000000}},"7546-57-7596":{{"status":"avg","data":{{"state":"NC","county":"Jackson","owner":"RSJ REAL ESTATE LLC","acres":117.57,"parcel_id":"7546-57-7596","signals":[]}},"ts":1700000000000}},"8779925020":{{"status":"great","data":{{"state":"NC","county":"Madison","owner":"THE HEIRS OF EVELYN SAWYER","acres":593.19,"parcel_id":"8779925020","signals":[]}},"ts":1700000000000}},"083100165854000":{{"status":"good","data":{{"state":"NC","county":"Yancey","owner":"APPLEBY REAL ESTATE HOLDINGS LLLP","acres":334.68,"parcel_id":"083100165854000","signals":[]}},"ts":1700000000000}},"0870-00-48-9404":{{"status":"good","data":{{"state":"NC","county":"Mitchell","owner":"SLAY RONALD ET AL","acres":124.2,"parcel_id":"0870-00-48-9404","signals":[]}},"ts":1700000000000}},"8794089196":{{"status":"avg","data":{{"state":"NC","county":"Madison","owner":"ROBERTS GLORIA F LIFE ESTATE","acres":138.66,"parcel_id":"8794089196","signals":[]}},"ts":1700000000000}},"6527739065":{{"status":"great","data":{{"state":"NC","county":"Macon","owner":"WATERS S J JR LIFE ESTATE","acres":188.5,"parcel_id":"6527739065","signals":[]}},"ts":1700000000000}},"071900530417000":{{"status":"good","data":{{"state":"NC","county":"Yancey","owner":"TURNER C L ESTATE","acres":123.5,"parcel_id":"071900530417000","signals":[]}},"ts":1700000000000}},"082200964024000":{{"status":"good","data":{{"state":"NC","county":"Yancey","owner":"BAILEY J RAY ESTATE","acres":120.9,"parcel_id":"082200964024000","signals":[]}},"ts":1700000000000}},"550102983778000":{{"status":"great","data":{{"state":"NC","county":"Cherokee","owner":"MCKEON LEE WELLS WEST CHARLES H JR ET AL","acres":389.51,"parcel_id":"550102983778000","signals":[]}},"ts":1700000000000}},"0866-00-91-0792":{{"status":"avg","data":{{"state":"NC","county":"Mitchell","owner":"131 OF CHATHAM LLC ET AL","acres":286.69,"parcel_id":"0866-00-91-0792","signals":[]}},"ts":1700000000000}},"0895-00-20-9875":{{"status":"good","data":{{"state":"NC","county":"Mitchell","owner":"BEAM DAVID C ET AL","acres":201.78,"parcel_id":"0895-00-20-9875","signals":[]}},"ts":1700000000000}},"0789-00-27-3282":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"POTEAT PROPERTY LLC ET AL","acres":186.77,"parcel_id":"0789-00-27-3282","signals":[]}},"ts":1700000000000}},"0891-00-02-7941":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"GP SULLINS LAND COMPANY LLC ET AL","acres":184.51,"parcel_id":"0891-00-02-7941","signals":[]}},"ts":1700000000000}},"0826-00-19-3634":{{"status":"good","data":{{"state":"NC","county":"Mitchell","owner":"GRIFFITH JOSEPH ET AL","acres":133.0,"parcel_id":"0826-00-19-3634","signals":[]}},"ts":1700000000000}},"0816-00-82-6572":{{"status":"avg","data":{{"state":"NC","county":"Mitchell","owner":"GRAHAM LOUISE ET AL","acres":132.7,"parcel_id":"0816-00-82-6572","signals":[]}},"ts":1700000000000}},"0833-00-47-3580":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"TIPTON STEPHEN ET AL","acres":101.1,"parcel_id":"0833-00-47-3580","signals":[]}},"ts":1700000000000}},"0855-00-95-3760":{{"status":"avg","data":{{"state":"NC","county":"Mitchell","owner":"REDMON RACHEL ET AL","acres":112.29,"parcel_id":"0855-00-95-3760","signals":[]}},"ts":1700000000000}},"0835-00-44-4785":{{"status":"good","data":{{"state":"NC","county":"Mitchell","owner":"FARUQ KIFU ET AL","acres":142.64,"parcel_id":"0835-00-44-4785","signals":[]}},"ts":1700000000000}},"0798-00-32-3181":{{"status":"avg","data":{{"state":"NC","county":"Mitchell","owner":"BALLEW JOHN TRUSTEE ET AL","acres":138.75,"parcel_id":"0798-00-32-3181","signals":[]}},"ts":1700000000000}},"0882-00-52-1389":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"PENLAND BAILEY ESTATE LLC","acres":2909.5,"parcel_id":"0882-00-52-1389","signals":[]}},"ts":1700000000000}},"0881-00-43-2281":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"PENLAND BAILEY ESTATE LLC","acres":248.65,"parcel_id":"0881-00-43-2281","signals":[]}},"ts":1700000000000}},"0882-00-54-8571":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"PENLAND BAILEY ESTATE LLC","acres":151.52,"parcel_id":"0882-00-54-8571","signals":[]}},"ts":1700000000000}},"7552-81-7340":{{"status":"good","data":{{"state":"NC","county":"Jackson","owner":"WHITESIDE ESTATES INC","acres":171.38,"parcel_id":"7552-81-7340","signals":[]}},"ts":1700000000000}},"9709899947":{{"status":"good","data":{{"state":"NC","county":"Madison","owner":"RICE EDMOND JR LIFE ESTATE","acres":117.55,"parcel_id":"9709899947","signals":[]}},"ts":1700000000000}},"0881-00-62-6742":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"PENLAND BAILEY ESTATE LLC","acres":184.21,"parcel_id":"0881-00-62-6742","signals":[]}},"ts":1700000000000}},"0880-00-29-4892":{{"status":"bad","data":{{"state":"NC","county":"Mitchell","owner":"PENLAND BAILEY ESTATE LLC","acres":101.19,"parcel_id":"0880-00-29-4892","signals":[]}},"ts":1700000000000}},"076700512301000":{{"status":"good","data":{{"state":"NC","county":"Yancey","owner":"NANCY BRADSHAW ESTATE","acres":134.35,"parcel_id":"076700512301000","signals":[]}},"ts":1700000000000}},"6516673857":{{"status":"good","data":{{"state":"NC","county":"Macon","owner":"BEECH COVE BRANCH LLC","acres":494.07,"parcel_id":"6516673857","signals":[]}},"ts":1700000000000}},"6563418893":{{"status":"bad","data":{{"state":"NC","county":"Macon","owner":"FRANKLIN HOLDINGS LLC","acres":191.36,"parcel_id":"6563418893","signals":[]}},"ts":1700000000000}},"6506214926":{{"status":"avg","data":{{"state":"NC","county":"Macon","owner":"MARIE ROSE ROBERT LLC","acres":160.0,"parcel_id":"6506214926","signals":[]}},"ts":1700000000000}},"7535379187":{{"status":"avg","data":{{"state":"NC","county":"Macon","owner":"TREASURED TRANQUILITY LLC","acres":141.55,"parcel_id":"7535379187","signals":[]}},"ts":1700000000000}},"7576-22-5611":{{"status":"good","data":{{"state":"NC","county":"Jackson","owner":"AMAZING GRACE PROPERTIES LLC","acres":592.2,"parcel_id":"7576-22-5611","signals":[]}},"ts":1700000000000}}}};
  for(const [k,v] of Object.entries(_SEED)){{if(!SAVED[k])SAVED[k]=v;}}
  // Re-save under permanent key so future loads always find it
  try {{ localStorage.setItem(SAVED_KEY, JSON.stringify(SAVED)); }} catch(e){{}}
}})();
function savePersist() {{ try {{ localStorage.setItem(SAVED_KEY, JSON.stringify(SAVED)); }} catch(e){{}} }}

function updateSavedCount() {{
  const n = Object.keys(SAVED).length;
  const el = document.getElementById('savedCount');
  if(el) el.textContent = n;
}}

function openViewMapPanel() {{
  // Open Regrid — user pastes parcel # in search box to highlight boundary
  window.open("https://app.regrid.com", "_blank");
}}

function openSignalDefs() {{ document.getElementById('signalDefsPanel').style.display='block'; }}

function openSaved() {{
  renderSavedList();
  document.getElementById('savedPanel').style.display = 'block';
}}

function closeSaved() {{
  document.getElementById('savedPanel').style.display = 'none';
}}

function renderSavedList() {{
  const list = document.getElementById('savedList');
  const empty = document.getElementById('savedEmpty');
  const entries = Object.entries(SAVED);
  if (!entries.length) {{ list.innerHTML=''; empty.style.display='block'; return; }}
  empty.style.display = 'none';
  // Sort: good first, then bad
  // Sort: GREAT(0)→GOOD(1)→AVG(2)→BAD(3)
  const _so = {{great:0,good:1,avg:2,bad:3}};
  entries.sort((a,b) => {{
    const as = _so[a[1].status] ?? 9;
    const bs = _so[b[1].status] ?? 9;
    if (as !== bs) return as - bs;
    return effectiveClimateScore(b[1].data||{{}}) - effectiveClimateScore(a[1].data||{{}});
  }});
  list.innerHTML = entries.map(([pid, entry]) => {{
    const d = entry.data || {{}};
    const st = entry.status;
    const stColors = {{great:['#14532d','#4ade80'],good:['#1e3a5f','#38bdf8'],avg:['#78350f','#f59e0b'],bad:['#7f1d1d','#dc2626']}};
    const [stBg,stBorder] = stColors[st]||['#1e3a5f','#6b7280'];
    const stIcon = STATUS_ICON[st]||st;
    const stLabel = STATUS_LABEL[st]||st;
    return `<div style="background:var(--surface2);border:1px solid ${{stBorder}};border-radius:6px;padding:12px;display:grid;grid-template-columns:auto 1fr auto;gap:12px;align-items:start">
      <div style="background:${{stBg}};border:2px solid ${{stBorder}};border-radius:4px;padding:4px 8px;font-size:12px;white-space:nowrap">${{stIcon}} ${{stLabel}}</div>
      <div>
        <div style="font-weight:600;color:var(--accent);font-size:13px">${{esc(d.owner||'Unknown Owner')}}</div>
        <div style="font-size:11px;color:var(--sky)">${{esc(d.state)}}/${{esc(d.county)}} — ${{esc(d.address||'')}}</div>
        <div style="font-size:11px;margin-top:4px">
          <span style="color:var(--accent)">${{Math.round(d.acres||0)}} ac</span> &nbsp;
          <span style="color:var(--muted)">Appraised: ${{d.appr_value>0?fmt(d.appr_value):'—'}}</span> &nbsp;
          <span style="color:var(--muted)">${{d.vpa>0?'$'+d.vpa+'/ac':'—'}}</span> &nbsp;
          <span style="color:#fbbf24">MAO: ${{d.mao>0?fmt(d.mao):'—'}}</span>
        </div>
        <div style="font-size:11px;color:var(--muted);margin-top:2px">Parcel: ${{esc(d.parcel_id||'—')}} &nbsp; Sale yr: ${{d.sale_year||'—'}} &nbsp; Mail: ${{esc(d.mail_state||'—')}}</div>
        <div style="font-size:10px;margin-top:4px;display:flex;gap:6px;flex-wrap:wrap">
          ${{d.regrid_url?'<a href="'+esc(d.regrid_url)+'" target="_blank" style="color:#4ade80">🗺 Lines</a>':''}} &nbsp;
          ${{d.google_url?'<a href="'+esc(d.google_url)+'" target="_blank" style="color:#60a5fa">🌍 Earth</a>':''}} &nbsp;
          ${{(d.signals||[]).map(s=>'<span style="background:#1e3a5f;padding:1px 5px;border-radius:3px;font-size:9px">'+esc(s[0]||s)+'</span>').join(' ')}}
        </div>
      </div>
      <button data-rpid="${{pid}}" onclick="removeSaved(this.dataset.rpid)" style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:4px 8px;color:var(--muted);cursor:pointer;font-size:11px" title="Remove">✕</button>
    </div>`;
  }}).join('');
}}

function removeSaved(pid) {{
  delete SAVED[pid];
  savePersist();
  updateSavedCount();
  renderSavedList();
}}

function clearSaved() {{
  if (!confirm('Clear all saved properties?')) return;
  SAVED = {{}};
  savePersist();
  updateSavedCount();
  renderSavedList();
}}

function exportSaved() {{
  const rows = Object.entries(SAVED).map(([pid,e]) => {{
    const d = e.data||{{}};
    return [e.status,d.state,d.county,d.owner,d.address,d.acres,d.appr_value,d.vpa,d.mao,d.sale_year,d.mail_state,d.parcel_id,d.regrid_url].join('\\t');
  }});
  const header = 'Status\\tState\\tCounty\\tOwner\\tAddress\\tAcres\\tAppraised\\t$/ac\\tMAO\\tSale Yr\\tMail State\\tParcel ID\\tGIS Link';
  navigator.clipboard.writeText(header+'\\n'+rows.join('\\n'))
    .then(()=>alert('Copied! Paste into Excel.'))
    .catch(()=>alert('Copy failed — try again'));
}}

// ── Property status (green=contact/offer, red=skip) ─────────────────────────
// PERMANENT status key — never changes
const STATUS_KEY = 'land_scout_status';
let STATUSES = {{}};
(function(){{
  const sk=['land_scout_status','lead_status_v30','lead_status_v45','lead_status_v46',
    'lead_status_v47','lead_status_v48','lead_status_v49',
    'lead_status_v50','lead_status_v51','lead_status_v52','lead_status_v53',
    'lead_status_v54','lead_status_v55','lead_status_v56','lead_status_v57',
    'lead_status_v58'];
  for (const _k of sk) {{
    try {{ Object.assign(STATUSES, JSON.parse(localStorage.getItem(_k)||'{{}}')); }} catch(e){{}}
  }}

  const _SEEDST={{"1":"bad","57":"bad","452100845603000":"good","454300637265000":"great","453200719036000":"avg","453100383010000":"bad","451200919107000":"good","452100801226000":"bad","454400356625000":"bad","443900595528000":"bad","458600501685000":"bad","455600120978000":"bad","458000450045000":"good","7518-46-6726":"good","457600259472000":"good","453500272277000":"bad","6548456266":"good","459102488021000":"bad","7575-38-7176":"bad","7574-93-7688":"avg","8507-82-8235":"bad","8743270779":"good","7575-17-4368":"bad","7584-55-6183":"bad","7569-37-5777":"good","8742872813":"good","0835-00-58-2710":"avg","9719506848":"great","7546-57-7596":"avg","8779925020":"great","083100165854000":"good","0870-00-48-9404":"good","8794089196":"avg","6527739065":"great","071900530417000":"good","082200964024000":"good","550102983778000":"great","0866-00-91-0792":"avg","0895-00-20-9875":"good","0789-00-27-3282":"bad","0891-00-02-7941":"bad","0826-00-19-3634":"good","0816-00-82-6572":"avg","0833-00-47-3580":"bad","0855-00-95-3760":"avg","0835-00-44-4785":"good","0798-00-32-3181":"avg","0882-00-52-1389":"bad","0881-00-43-2281":"bad","0882-00-54-8571":"bad","7552-81-7340":"good","9709899947":"good","0881-00-62-6742":"bad","0880-00-29-4892":"bad","076700512301000":"good","6516673857":"good","6563418893":"bad","6506214926":"avg","7535379187":"avg","7576-22-5611":"good"}};
  for(const [k,v] of Object.entries(_SEEDST)){{if(!STATUSES[k])STATUSES[k]=v;}}
  // Bridge: index any URL-keyed legacy SAVED entries by parcel_id
  Object.values(SAVED).forEach(v=>{{ if(v.data?.parcel_id && !STATUSES[v.data.parcel_id]) STATUSES[v.data.parcel_id]=v.status; }});
  try {{ localStorage.setItem(STATUS_KEY, JSON.stringify(STATUSES)); }} catch(e){{}}
}})();

function saveStatuses() {{
  try {{ localStorage.setItem(STATUS_KEY, JSON.stringify(STATUSES)); }} catch(e){{}}
}}

const STATUS_CYCLE = ['','great','good','avg','bad'];
const STATUS_ICON  = {{'great':'⭐','good':'✅','avg':'〜','bad':'❌'}};
const STATUS_LABEL = {{'great':'GREAT — top priority','good':'GOOD — contact','avg':'AVERAGE — maybe','bad':'BAD — skip'}};

function toggleStatus(pid, btn) {{
  const cur = STATUSES[pid] || '';
  const idx = STATUS_CYCLE.indexOf(cur);
  const next = STATUS_CYCLE[(idx + 1) % STATUS_CYCLE.length];
  if (next) STATUSES[pid] = next; else delete STATUSES[pid];
  saveStatuses();
  btn.className = 'status-btn' + (next ? ' '+next : '');
  btn.title = next ? STATUS_LABEL[next] : 'Click to rate: ⭐ GREAT → ✅ GOOD → 〜 AVG → ❌ BAD';
  btn.textContent = next ? STATUS_ICON[next] : '○';
  // Save/remove from SAVED panel
  const rec = DATA.find(d => d.parcel_id === pid);
  if (next && rec) {{
    SAVED[pid] = {{ status: next, data: rec, ts: Date.now() }};
  }} else {{
    delete SAVED[pid];
  }}
  savePersist();
  updateSavedCount();
  reapplyHighlights();
}}

// ── Excel copy helpers ─────────────────────────────────────────────────────
const EXCEL_COLS = [
  ["Status",          d => (STATUSES[d.parcel_id]||'')],  // great/good/avg/bad
  ["State/County",    d => d.state+"/"+d.county],
  ["Parcel ID",       d => d.parcel_id||""],
  ["Owner",           d => d.owner||""],
  ["Address",         d => d.address||""],
  ["City",            d => d.city||""],
  ["Acres",           d => d.acres],
  ["Appraised $",     d => d.appr_value||0],
  ["$/ac",            d => d.vpa||0],
  ["MAO 65%",         d => d.mao||0],
  ["Sale Year",       d => d.sale_year||""],
  ["Mail State",      d => d.mail_state||""],
  ["OOS",             d => d.oos ? "YES" : ""],
  ["Has Structure",   d => d.has_structure ? "YES" : ""],
  ["LU Class",        d => d.lu_class||""],
  ["Combined Score",  d => d.combined||0],
  ["Motivated Owner", d => d.d_score||0],
  ["Homestead Score", d => d.h_score||0],
  ["Invest Score",    d => d.score||0],
  ["Pasture %",       d => d.pasture_pct||0],
  ["Water Score",     d => d.water_score||0],
  ["Soil",            d => d.base_soil||""],
  ["Climate",         d => d.climate||""],
  ["NF Nearby",       d => d.nf_nearby ? "YES" : ""],
  ["NF Name",         d => d.nf_name||""],
  ["Lat",             d => d.lat||""],
  ["Lng",             d => d.lng||""],
  ["Elev Min ft",     d => d.elev_min||""],
  ["Elev Max ft",     d => d.elev_max||""],
  ["Summer High °F",  d => d.summer_high||""],
  ["Winter Low °F",   d => d.winter_low||""],
  ["Nearest Small Town", d => (d.towns&&d.towns.small) ? d.towns.small.name : ""],
  ["Small Town Mi",   d => (d.towns&&d.towns.small) ? d.towns.small.dist_mi : ""],
  ["Nearest Med Town",d => (d.towns&&d.towns.medium) ? d.towns.medium.name : ""],
  ["Med Town Mi",     d => (d.towns&&d.towns.medium) ? d.towns.medium.dist_mi : ""],
  ["Nearest City",    d => (d.towns&&d.towns.large) ? d.towns.large.name : ""],
  ["City Mi",         d => (d.towns&&d.towns.large) ? d.towns.large.dist_mi : ""],
  ["Signals",         d => (d.signals||[]).map(s=>s[0]).join("; ")],
  ["Regrid (Lines)",  d => d.regrid_url||""],
  ["Sat Link",        d => d.google_url||""],
  ["qPublic Record",  d => d.qpublic_url||""],
  ["County GIS",      d => d.map_url||""],
  ["Source",          d => d.source_name||""],
];

function rowToTsv(d) {{
  return EXCEL_COLS.map(([,fn]) => {{
    const v = fn(d);
    const s = String(v === null || v === undefined ? "" : v);
    return s.includes('\\t') || s.includes('\\n') ? '"'+s.replace(/"/g,'""')+'"' : s;
  }}).join('\\t');
}}

function flashMsg(txt) {{
  const el = document.getElementById('copyMsg');
  el.textContent = txt;
  el.style.opacity = '1';
  setTimeout(() => {{ el.style.opacity = '0'; }}, 2500);
}}

function copyHeaders() {{
  const headers = EXCEL_COLS.map(([h]) => h).join('\\t');
  navigator.clipboard.writeText(headers).then(() => {{
    flashMsg('✓ Headers copied — paste into Excel row 1');
    const btn = document.getElementById('btnCopyHeaders');
    btn.classList.add('flash');
    setTimeout(() => btn.classList.remove('flash'), 1200);
  }}).catch(() => {{
    // Fallback for older browsers
    const ta = document.createElement('textarea');
    ta.value = headers;
    document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    flashMsg('✓ Headers copied');
  }});
}}

function copyRow(idx) {{
  const d = ROWS[idx];
  if (!d) return;
  const tsv = rowToTsv(d);
  navigator.clipboard.writeText(tsv).then(() => {{
    flashMsg('✓ Row copied — paste into Excel');
  }}).catch(() => {{
    const ta = document.createElement('textarea');
    ta.value = tsv;
    document.body.appendChild(ta);
    ta.select(); document.execCommand('copy');
    document.body.removeChild(ta);
    flashMsg('✓ Row copied');
  }});
}}

// Store rendered rows by index for copyRow()
let ROWS = {{}};
function esc(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

// ── Slider sync ────────────────────────────────────────────────────
function syncRange(el, labelId) {{
  const v = +el.value;
  const id = el.id;
  const label = document.getElementById(labelId);
  if (id === 'minAcres') label.textContent = v + ' ac+';
  else if (id === 'maxVpa') label.textContent = v >= 20000 ? 'Any' : '$'+v.toLocaleString()+'/ac';
  else if (id === 'minClimate') label.textContent = v === 0 ? 'Any' : v+'+ /10';
    else if (id === 'minHScore') label.textContent = v === 0 ? 'Any' : v+'+';
  else if (id === 'minIScore') label.textContent = v === 0 ? 'Any' : v+'+';
  else if (id === 'minVal') label.textContent = v === 0 ? 'Any' : '$'+v.toLocaleString()+'+';
  else if (id === 'maxVal') label.textContent = v >= 5000000 ? 'Any' : '$'+v.toLocaleString();
}}

function togglePill(el, groupId) {{
  document.querySelectorAll(`#${{groupId}} .pill`).forEach(p => p.classList.remove('active'));
  el.classList.add('active');
  applyFilters();
}}

function resetFilters() {{
  document.getElementById('minAcres').value  = 100;   syncRange(document.getElementById('minAcres'),'minAcresVal');
  document.getElementById('maxVpa').value    = 20000; syncRange(document.getElementById('maxVpa'),'maxVpaVal');
  document.getElementById('minHScore').value = 0;     syncRange(document.getElementById('minHScore'),'minHScoreVal');
  document.getElementById('minIScore').value = 0;     syncRange(document.getElementById('minIScore'),'minIScoreVal');
  document.getElementById('minVal').value    = 0;     syncRange(document.getElementById('minVal'),'minValDisp');
  document.getElementById('maxVal').value    = 5000000;syncRange(document.getElementById('maxVal'),'maxValDisp');
  ['togOOS','togOwner','togNF','togNoStruct','togSaleYr','togTrust','togReviewed'].forEach(id =>
    document.getElementById(id).checked = false);
  document.querySelectorAll('.pill').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('#confPills .pill[data-val=""], #luPills .pill[data-val=""]')
    .forEach(p => p.classList.add('active'));
  document.querySelectorAll('#countyChecks input').forEach(c => c.checked = true);
  document.getElementById('sortBy').value = 'combined';
  sortKey = 'combined'; sortDir = -1;
  var capNote = document.getElementById('capNote');
  if (capNote) capNote.textContent = '(top 500 shown — re-run with filters to explore more)';
  applyFilters();
}}

function exportRatings() {{
  const data = {{
    saved:   JSON.parse(localStorage.getItem('land_scout_saved')  || '{{}}'),
    statuses: JSON.parse(localStorage.getItem('land_scout_status') || '{{}}'),
    exported: new Date().toISOString()
  }};
  const blob = new Blob([JSON.stringify(data, null, 2)], {{type: 'application/json'}});
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'land_scout_ratings_' + new Date().toISOString().slice(0,10) + '.json';
  a.click();
  URL.revokeObjectURL(a.href);
}}

function importRatings(event) {{
  const file = event.target.files[0];
  if (!file) return;
  const reader = new FileReader();
  reader.onload = function(e) {{
    try {{
      const data = JSON.parse(e.target.result);
      if (data.saved) {{
        const existing = JSON.parse(localStorage.getItem('land_scout_saved') || '{{}}');
        const merged = Object.assign({{}}, data.saved, existing); // existing wins on conflict
        localStorage.setItem('land_scout_saved', JSON.stringify(merged));
        Object.assign(SAVED, merged);
      }}
      if (data.statuses) {{
        const existing = JSON.parse(localStorage.getItem('land_scout_status') || '{{}}');
        const merged = Object.assign({{}}, data.statuses, existing);
        localStorage.setItem('land_scout_status', JSON.stringify(merged));
        Object.assign(STATUSES, merged);
      }}
      alert('Ratings imported successfully! ' + Object.keys(data.statuses||{{}}).length + ' statuses loaded.');
      applyFilters();
      renderSavedList();
    }} catch(err) {{
      alert('Import failed: ' + err.message);
    }}
  }};
  reader.readAsText(file);
  event.target.value = ''; // reset so same file can be re-imported
}}

init();
</script>

<!-- ── SAVED PROPERTIES PANEL ─── -->
<div id="savedPanel" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.85);z-index:1000;overflow:auto;padding:20px">
  <div style="max-width:1200px;margin:0 auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:20px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <h2 style="color:var(--accent);margin:0">★ Saved Properties</h2>
      <div style="display:flex;gap:8px">
        <button onclick="exportSaved()" style="padding:6px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--accent);cursor:pointer;font-size:11px">📋 Copy to Excel</button>
        <button onclick="clearSaved()" style="padding:6px 12px;background:#7f1d1d;border:1px solid #dc2626;border-radius:4px;color:#fca5a5;cursor:pointer;font-size:11px">🗑 Clear All</button>
        <button onclick="closeSaved()" style="padding:6px 14px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;color:var(--fg);cursor:pointer;font-size:12px">✕ Close</button>
      </div>
    </div>
    <div style="font-size:11px;color:var(--muted);margin-bottom:12px">
      <span style="color:#4ade80">✅ Contact/Offer</span> &nbsp; <span style="color:#f87171">❌ Skip</span>
    </div>
    <div id="savedList" style="display:grid;gap:8px"></div>
    <div id="savedEmpty" style="color:var(--muted);text-align:center;padding:40px;display:none">No saved properties yet. Click ○ on any row to save it.</div>
  </div>
</div>

<!-- ── SIGNAL DEFINITIONS PANEL ── -->
<div id="signalDefsPanel" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.85);z-index:1001;overflow:auto;padding:20px">
  <div style="max-width:760px;margin:0 auto;background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:24px">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
      <h2 style="color:var(--sky);font-size:16px">📋 Distress Signal Definitions</h2>
      <button onclick="document.getElementById('signalDefsPanel').style.display='none'" style="padding:5px 12px;background:var(--surface2);border:1px solid var(--border);border-radius:4px;cursor:pointer;color:var(--fg)">✕ Close</button>
    </div>
    <div style="display:grid;gap:10px;font-size:11px">
      <div style="background:#4a003322;border:1px solid #9f0068;border-radius:5px;padding:10px">
        <strong style="color:#f0abfc">⚡ ACTIVE PROBATE (+60 pts)</strong> — Confirmed open probate case in county court. Estate actively administered — heirs legally required to settle assets. Flag via BatchLeads/PropStream import or scraper active_probate field.</div>
      <div style="background:#7f1d1d22;border:1px solid #7f1d1d;border-radius:5px;padding:10px">
        <strong style="color:#fca5a5">PROBATE/ESTATE (+40 pts)</strong> — Owner name contains "ESTATE OF", "HEIRS OF", "DECD", "DECEASED", "EXECUTOR", "ADMINISTRATR", or "PROBATE". These are inherited properties where heirs have no emotional attachment to the land and are often motivated to liquidate quickly. Highest single signal.
      </div>
      <div style="background:#78350f22;border:1px solid #78350f;border-radius:5px;padding:10px">
        <strong style="color:#fcd34d">MULTI-HEIR (+22 pts)</strong> — Owner contains "ET AL", "ET UX", "HEIRS", or "MULTIPLE". Multiple heirs on title creates fractured ownership — heirs often can't agree on usage, creating pressure to sell.
      </div>
      <div style="background:#78350f22;border:1px solid #78350f;border-radius:5px;padding:10px">
        <strong style="color:#fcd34d">UNDATED ESTATE (+20 pts)</strong> — Estate or heirs owner with NO recorded sale date. Property was never sold — it was inherited and passed down, likely multiple times. Often no mortgage, no basis tracking, highly motivated.
      </div>
      <div style="background:#78350f22;border:1px solid #78350f;border-radius:5px;padding:10px">
        <strong style="color:#fcd34d">HELD 50+ YRS (+25 pts) / HELD 35+ YRS (+20 pts)</strong> — Last sale was 35–50+ years ago. Multi-generational hold, no mortgage, high equity. Owner (or heirs) may welcome liquidity and have no price anchoring from recent purchase.
      </div>
      <div style="background:#1e3a5f22;border:1px solid #1e3a5f;border-radius:5px;padding:10px">
        <strong style="color:#93c5fd">OOS (Out-of-State) (+15 pts)</strong> — Mailing address is in a different state than the property. Absentee owner who is not managing the land locally — classic distressed signal. More likely to respond to a letter offer.
      </div>
      <div style="background:#1e3a5f22;border:1px solid #1e3a5f;border-radius:5px;padding:10px">
        <strong style="color:#93c5fd">OOS ENTITY (+15 pts)</strong> — Combines out-of-state mailing address with entity ownership (LLC/Trust/Corp). The absentee investor double-signal — likely a peripheral asset in a portfolio.
      </div>
      <div style="background:#71320022;border:1px solid #713200;border-radius:5px;padding:10px">
        <strong style="color:#fde68a">TRUST (+12 pts)</strong> — Property held in a trust. When trustees manage land for multiple beneficiaries, the complexity often drives motivation to sell. Especially strong if also OOS.
      </div>
      <div style="background:#71320022;border:1px solid #713200;border-radius:5px;padding:10px">
        <strong style="color:#fde68a">LLC (+10 pts)</strong> — Entity-owned land. Verify if the LLC is active or dissolved (inactive LLCs can't legally hold title, creating urgency). LLC land is often bought speculatively and later abandoned.
      </div>
      <div style="background:#71320022;border:1px solid #713200;border-radius:5px;padding:10px">
        <strong style="color:#fde68a">DISTRESSED VPA (+18 pts) / LOW VPA (+10 pts)</strong> — Price per acre is far below market ($500/ac or less for 100+ acre tracts). Implies the owner either doesn't know current land values or needs to sell fast. Cross-reference with county median VPA for context.
      </div>
      <div style="background:#50005022;border:1px solid #500050;border-radius:5px;padding:10px">
        <strong style="color:#e879f9">NO SALE YR (+5 pts)</strong> — No recorded last-sale year in GIS. Could be a very old inherited hold predating digital records, or a data gap. Worth investigating at the county assessor.
      </div>
      <div style="background:#1a3a1a22;border:1px solid #1a3a1a;border-radius:5px;padding:10px">
        <strong style="color:#86efac">VACANT LAND (+6 pts) / RAW LAND (+3 pts)</strong> — No structure recorded. Pure land with no emotional attachment to a home. Easier negotiation — no one is "living there."
      </div>
      <div style="background:#7f1d1d22;border:1px solid #7f1d1d;border-radius:5px;padding:10px">
        <strong style="color:#fca5a5">NO OWNER (-10 pts)</strong> — Owner field blank in GIS. VERIFY at county assessor before contacting. Could be government-owned, exempt property, or data error.
      </div>
    </div>
    <div style="margin-top:14px;font-size:10px;color:var(--muted)">Combined Motivated Owner score = sum of all applicable signal points, capped at 100. Higher = more motivated seller signals present.</div>
  </div>
</div>
</body>
</html>"""

# ── Main ──────────────────────────────────────────────────────────────────────


def diagnose_tn_filter_test(county, min_acres=100):
    """
    Test different WHERE clause formats on TN OLG_LANDUSE service to find
    which LU_CLASSIFICATION filter syntax actually works.
    """
    print(f"\n── TN filter syntax test for {county} ────────────────────────────")
    tests = [
        ("No filter (baseline)",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres}"),
        ("IN whitelist numeric",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION IN (2,4,5,6,7,10,31,32,61,62,63,64,71,72)"),
        ("IN whitelist strings",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION IN ('61','62','71','72','31','32')"),
        ("NOT IN numeric",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION NOT IN (11,12,13,21,22,91,92)"),
        ("= single value numeric",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION=62"),
        ("= single value string",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION='62'"),
        ("<= comparison",
         f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres} AND LU_CLASSIFICATION<=72"),
    ]
    for label, where in tests:
        data, err = jget(TN_STATEWIDE_URL, {
            "where": where,
            "outFields": "LU_CLASSIFICATION,LU_ACRES",
            "resultRecordCount": 5,
            "resultOffset": 0, "f": "json",
        })
        if data is None:
            print(f"  ✗ {label}: {err[:60]}")
        else:
            feats = data.get("features", [])
            codes = [f["attributes"].get("LU_CLASSIFICATION") for f in feats]
            exceeded = data.get("exceededTransferLimit", False)
            print(f"  ✓ {label}: {len(feats)} results, codes={codes}, exceeded={exceeded}")
        sleep(0.3)

def diagnose_tn(counties, min_acres=100):
    """
    Sample LU_CLASSIFICATION values from TN OLG_LANDUSE to see what's coming back.
    Helps identify what land use types are inflating parcel counts.
    """
    print("\n── TN LU_CLASSIFICATION diagnostic ──────────────────────────────")
    print(f"Sampling parcels >= {min_acres} ac per county (no LU filter)\n")
    from collections import Counter
    for county in counties:
        data, err = jget(TN_STATEWIDE_URL, {
            "where": f"COUNTY='{county.upper()}' AND LU_ACRES>={min_acres}",
            "outFields": "LU_ACRES,LU_CLASSIFICATION,GISLINK",
            "resultRecordCount": 500,
            "resultOffset": 0,
            "f": "json",
        })
        if not data:
            print(f"  {county}: ERROR — {err}")
            continue
        feats = data.get("features", [])
        total = data.get("exceededTransferLimit", False)
        lu_counts = Counter()
        no_gislink = 0
        for feat in feats:
            p = feat.get("attributes", {})
            lu = (p.get("LU_CLASSIFICATION") or "BLANK/NULL").strip().upper()
            lu_counts[lu] += 1
            if not p.get("GISLINK"):
                no_gislink += 1
        print(f"  {county}: {len(feats)} sampled {'(MORE EXIST — exceeds 500)' if total else ''}")
        print(f"    No GISLINK: {no_gislink}")
        TN_LU_LEGEND = {
            "2":"Ag","4":"Rural","5":"Ag","6":"Farm","7":"Timber","10":"Rural Res",
            "11":"SFR ✗","12":"Duplex ✗","13":"Apt/Condo ✗","14":"Mobile Home ✗",
            "15":"Other Res ✗","21":"Commercial ✗","22":"Industrial ✗","23":"Utility ✗",
            "31":"Farmland","32":"Farm Improv","61":"Greenbelt Farm","62":"Greenbelt Forest",
            "63":"Greenbelt Other","64":"Greenbelt","71":"Agricultural","72":"Timber",
            "91":"Exempt ✗","92":"Exempt Govt ✗","93":"Exempt Other ✗",
        }
        print(f"    LU breakdown (top 15):")
        kept_total = 0
        for lu, ct in lu_counts.most_common(15):
            excluded = is_excluded_tn_lu(lu)
            legend = TN_LU_LEGEND.get(lu, "?")
            tag = f" ← EXCLUDED ({legend})" if excluded else f" ← KEPT ({legend})"
            if not excluded: kept_total += ct
            print(f"      {ct:4d}  {lu[:6]}{tag}")
        print(f"    After exclusion: ~{kept_total} of {len(feats)} sample would remain")
        print()

def diagnose():
    """Quick endpoint health check. Run: python script.py --diagnose"""
    import urllib.request, urllib.parse, json, ssl

    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    def probe(label, url, kcs=False, params=None, limit=6000):
        try:
            p = params or {"where":"1=1","outFields":"*","resultRecordCount":3,
                           "returnGeometry":"false","f":"json"}
            sess = KCS_SESSION if kcs else SESSION
            resp = sess.get(url, params=p, timeout=12)
            body = resp.text
            try:
                d = json.loads(body)
                if "error" in d:
                    print(f"  ✗ {label}: {d['error'].get('message','err')[:100]}")
                elif "features" in d:
                    feats = d["features"]
                    flds = list(feats[0]["attributes"].keys()) if feats else []
                    samp = {k:feats[0]["attributes"][k] for k in flds[:6]} if feats else {}
                    print(f"  ✓ {label}: {len(feats)} feat  fields={flds}")
                    print(f"    sample={samp}")
                elif "layers" in d:
                    print(f"  ✓ {label}: layers={[(l['id'],l['name'][:24]) for l in d['layers'][:8]]}")
                elif "currentVersion" in d:
                    import re as _re
                    ids = _re.findall(r'"id"\s*:\s*(\d+)', body)
                    nms = _re.findall(r'"name"\s*:\s*"([^"]{2,28})"', body)
                    print(f"  ~ {label}: alive, layers={list(zip(ids,nms))[:10]}")
                else:
                    print(f"  ~ {label}: 200 keys={list(d.keys())[:5]}")
            except (json.JSONDecodeError, ValueError):
                print(f"  ~ {label}: non-JSON={body[:80]}")
        except Exception as e:
            print(f"  ✗ {label}: {type(e).__name__}: {str(e)[:90]}")

    print("─── Alabama ───")
    probe("Marshall iWorQ lyr1 (web4) CONFIRMED", kcs=True,
          url="https://web4.kcsgis.com/kcsgis/rest/services/Marshall/iWorQ/MapServer/1/query")
    # Jackson AL: CONFIRMED no bulk parcel service — only annotation layers
    print("  Jackson AL: no bulk parcel GIS service (confirmed by discovery)")

    print("\n─── South Carolina ───")
    probe("Spartanburg SHAPE.STArea()>=4356000",
          url="https://maps.spartanburgcounty.org/server/rest/services/GIS/CAMA_Parcels/FeatureServer/0/query",
          params={"where":"SHAPE.STArea()>=4356000","outFields":"MAPNUMBER,OwnerName,StreetAddress,Acreage,CurrentAppraisedLandValue,SaleDate",
                  "resultRecordCount":"3","returnGeometry":"false","f":"json"})
    probe("Anderson lyr5 SHAPE.STArea()>=4356000",
          url="https://propertyviewer.andersoncountysc.org/arcgis/rest/services/NewPropertyViewer/MapServer/5/query",
          params={"where":"SHAPE.STArea() >= 4356000","outFields":"TMS,TAXOWNSTR,PHYS_ADDR,MRKT_VALUE,SALE_YEAR,SHAPE.STArea()",
                  "resultRecordCount":"3","returnGeometry":"false","f":"json"})
    probe("Greenville lyr1 LOTSIZE>=100",
          url="https://www.gcgis.org/arcgis/rest/services/GreenvilleJS/Map_Layers_JS/MapServer/1/query",
          params={"where":"LOTSIZE >= 100","outFields":"PIN,PURNAME,LOTSIZE,SALEPRICE,SALEDATE,STREET,LANDUSE",
                  "resultRecordCount":"3","returnGeometry":"false","f":"json"})
    print("\nPaste output if any ✗ remain.")
    print("\n── VA diagnostic ──────────────────────────────────────────")
    for county, cfg in VA_COUNTY_PROFILES.items():
        print(f"  VA/{county}...")
        # Test statewide FeatureServer
        params = {"where": f"LOCALITY='{county} County'",
                  "outFields": "PARCELID,LOCALITY,FIPS",
                  "resultRecordCount": 3, "returnGeometry": "false", "f": "json"}
        data, err = jget(VA_STATEWIDE_FS, params, timeout=15)
        if data and data.get("features"):
            print(f"    ✓ VGIN statewide FS: {len(data['features'])} test records")
        else:
            print(f"    ✗ VGIN statewide FS: {err}")
    print("\nDiagnostic complete.")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--enable-tn", action="store_true",
                    help="Include Tennessee scraping (disabled by default)")
    ap.add_argument("--diagnose", action="store_true",
                    help="Test all AL/SC endpoints and exit — no scraping")
    ap.add_argument("--diagnose-tn", action="store_true",
                    help="Sample TN LU_CLASSIFICATION values to debug bloat")
    ap.add_argument("--min-acres", type=float, default=100)
    ap.add_argument("--max-vpa",   type=float, default=0)
    ap.add_argument("--max-per",   type=int,   default=500)
    ap.add_argument("--top",       type=int,   default=9999)
    ap.add_argument("--out",       default="land_scout.html")
    ap.add_argument("--no-owner-scrape", action="store_true")
    ap.add_argument("--tn-counties", nargs="+",
                    default=["Blount","Knox","Sevier"])
    ap.add_argument("--al-counties", nargs="+",
                    # Jackson removed: no bulk parcel GIS service exists
                    default=["Marshall","Cherokee","DeKalb","Etowah"])
    ap.add_argument("--nc-counties", nargs="+",
                    default=["Cherokee","Clay","Graham","Haywood","Jackson","Macon","Madison","Mitchell","Swain","Yancey","Watauga","Avery","Ashe","Alleghany"])
    ap.add_argument("--sc-counties", nargs="+",
                    default=["Spartanburg","Anderson","Greenville","Oconee","Pickens"])
    ap.add_argument("--va-counties", nargs="+",
                    default=["Lee","Scott","Wise","Washington","Russell"])
    ap.add_argument("--ga-counties", nargs="+",
                    default=["Fannin","Gilmer","Union","Towns","Lumpkin"])
    args = ap.parse_args()

    if args.diagnose:
        diagnose()
        return

    if args.diagnose_tn:
        diagnose_tn(args.tn_counties, args.min_acres)
        diagnose_tn_filter_test(args.tn_counties[0] if args.tn_counties else "Blount", args.min_acres)
        return

    verbose = True
    run_ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    print("=" * 60)
    print(f"Land Lead Finder v75 — {run_ts}")
    print(f"Min acres: {args.min_acres}  Max per county: {args.max_per}")
    print("=" * 60)

    all_leads, all_gaps = [], []

    # TN disabled — scraper returns bloated results, TPAD owner lookup never works
    # Re-enable with --enable-tn flag when fixed
    if getattr(args, "enable_tn", False):
        print("── Tennessee ──────────────────────────────────────────────")
        tn_l, tn_g = scrape_tn(args.tn_counties, args.min_acres, args.max_vpa,
                                args.max_per, verbose)
        all_leads.extend(tn_l); all_gaps.extend(tn_g)

    print("── Alabama ────────────────────────────────────────────────")
    al_l, al_g = scrape_al(args.al_counties, args.min_acres, args.max_vpa,
                            args.max_per, verbose)
    all_leads.extend(al_l); all_gaps.extend(al_g)

    print("── North Carolina ─────────────────────────────────────────")
    nc_l, nc_g = scrape_nc(args.nc_counties, args.min_acres, args.max_vpa,
                            args.max_per, verbose)
    all_leads.extend(nc_l); all_gaps.extend(nc_g)

    print("── South Carolina ─────────────────────────────────────────")
    sc_l, sc_g = scrape_sc(args.sc_counties, args.min_acres, args.max_vpa,
                            args.max_per, verbose)
    all_leads.extend(sc_l); all_gaps.extend(sc_g)

    print("── Virginia ───────────────────────────────────────────────")
    va_l, va_g = scrape_va(args.va_counties, args.min_acres, args.max_vpa,
                            min(200, args.max_per), verbose)  # cap VA to 200/county
    all_leads.extend(va_l); all_gaps.extend(va_g)

    print("── Georgia ─────────────────────────────────────────────────────────")
    ga_l, ga_g = scrape_ga(args.ga_counties, args.min_acres, args.max_vpa,
                            args.max_per, verbose)
    all_leads.extend(ga_l); all_gaps.extend(ga_g)

    print("─" * 60)
    print(f"Raw parcels scraped: {len(all_leads)}")
    args.raw_count = len(all_leads)

    top = finalize(all_leads, args.top)
    args.after_filter = len(top)
    print(f"After dedup + filter: {args.after_filter}")

    if not args.no_owner_scrape:
        print("Enriching owner data from county assessors...")
        enrich_owners_web(top, max_scrape=9999, verbose=verbose)
        # Recompute scores/analysis after owner enrichment
        for l in top:
            l["confidence"],l["conf_score"],l["conf_notes"] = confidence(l)
            l["analysis"] = analyze_property(l, l.get("county_vpa_median",0))
            l["h_analysis"] = homestead_analysis(l)

    # Water features fetched on-demand via dashboard 💧 button
    # Elevation fetched on-demand via dashboard ⛰ button

    print(f"Top {len(top)} selected")
    if all_gaps:
        print(f"\nGaps ({len(all_gaps)}):")
        for g in all_gaps: print(f"  ⚠ {g}")

    html_out = build_html(top, all_gaps, args, run_ts)
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html_out)
    print(f"\n✓ Saved → {args.out}")
    print(f"  Owner data:  {sum(1 for l in top if l.get('owner'))}/{len(top)}")
    print(f"  Sale year:   {sum(1 for l in top if l.get('sale_year'))}/{len(top)}")
    print(f"  OOS owners:  {sum(1 for l in top if l.get('oos'))}/{len(top)}")
    nf_count = sum(1 for l in top if SOIL_DATA.get(l['state']+'_'+l['county'], {}).get('nf_nearby'))
    print(f"  NF nearby:   {nf_count}/{len(top)}")

if __name__ == "__main__":
    main()
