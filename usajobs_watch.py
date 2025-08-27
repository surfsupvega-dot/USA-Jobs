# usajobs_watch.py
import os, json, time, hashlib, re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, Optional

import requests
from requests.exceptions import RequestException

# ===================== QUERY CONFIG =====================
BASE = "https://data.usajobs.gov/api/Search"
PARAMS = {
    "JobCategoryCode": "1176:1173",    # Series 1176 and 1173
    "LocationName": "92055",           # Camp Pendleton ZIP
    "Radius": "25",                    # 25 miles
    "PayGradeLow": "09",               # GS-09
    "PayGradeHigh": "12",              # GS-12
    "Fields": "All",
    "WhoMayApply": "all",
    "SortField": "openingdate",
    "SortDirection": "desc",
    "ResultsPerPage": "50",
}
# ========================================================

# ===================== NOTIFY/BEHAVIOR ==================
ENFORCE_LOCAL_8PM = os.getenv("ENFORCE_LOCAL_8PM", "1") == "1"

NOTIFY_FETCH_FAILURE = True
NOTIFY_ZERO_RESULTS = True
NOTIFY_NO_NEW_ITEMS = True

TIMEOUT_SECONDS = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 5
# ========================================================

# ===================== AUTH / HEADERS ===================
USER_AGENT = os.getenv("USAJOBS_USER_AGENT")
API_KEY    = os.getenv("USAJOBS_API_KEY")
DISCORD_WH = os.getenv("DISCORD_WEBHOOK")

HEADERS = {
    "User-Agent": USER_AGENT or "",
    "Authorization-Key": API_KEY or "",
    "Accept": "application/json",
}
# ========================================================

SEEN_PATH = "seen_usajobs.json"

# ===================== HELPERS ==========================
def load_seen(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}

def save_seen(path: str, data: Dict[str, Any]) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)

def norm(s: Optional[str]) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()

def jid(rec: Dict[str, Any]) -> str:
    key = f"{rec.get('MatchedObjectId','')}|{rec.get('PositionTitle','')}|{(rec.get('ApplyURI') or [None])[0] or rec.get('PositionURI','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]

def send_discord(message: str) -> None:
    if not DISCORD_WH:
        print("[Discord disabled] " + message)
        return
    try:
        r = requests.post(DISCORD_WH, json={"content": message}, timeout=15)
        if r.status_code == 204:
            print("[OK] Discord accepted message (204).")
        elif 200 <= r.status_code < 300:
            print(f"[OK] Discord HTTP {r.status_code}.")
        else:
            body = (r.text or "")[:200]
            print(f"[WARN] Discord webhook HTTP {r.status_code}: {body}")
    except Exception as e:
        print(f"[WARN] Discord send failed: {e}")

def fetch_with_retries(url: str, params: Dict[str, str], headers: Dict[str, str],
                       attempts: int = 3, backoff: int = 5, timeout: int = 30) -> Dict[str, Any]:
    last_err = None
    for i in range(1, attempts + 1):
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=timeout)
            if resp.status_code == 200:
                return resp.json()
            else:
                last_err = f"HTTP {resp.status_code}: {resp.text[:200]}"
                print(f"[ERROR] Attempt {i}/{attempts} failed: {last_err}")
        except RequestException as e:
            last_err = str(e)
            print(f"[ERROR] Attempt {i}/{attempts} exception: {last_err}")
        if i < attempts:
            sleep_for = backoff * (2 ** (i - 1))
            print(f"[INFO] Backing off {sleep_for}s before retry...")
            time.sleep(sleep_for)
    raise RuntimeError(last_err or "Unknown fetch error")

def format_msg(obj: Dict[str, Any]) -> str:
    title = obj.get("PositionTitle", "Untitled")
    org   = obj.get("OrganizationName", "")
    loc_field = obj.get("PositionLocationDisplay")
    if isinstance(loc_field, list):
        locs = ", ".join(sorted({loc.get("LocationName","") for loc in loc_field if isinstance(loc, dict)}))
    else:
        locs = norm(str(loc_field or ""))
    url   = (obj.get("ApplyURI") or [None])[0] or obj.get("PositionURI") or ""
    grade = ", ".join(obj.get("JobGrade", []) or []) or ""
    closing = f" | Closes: {obj['ApplicationCloseDate']}" if obj.get("ApplicationCloseDate") else ""
    return f"🔔 **{title}** ({grade}) @ {org}\n📍 {locs}{closing}\n{url}"

def guard_local_8pm() -> bool:
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(tz)
    return now.hour == 20
# ========================================================

# ===================== MAIN =============================
def run_once() -> None:
    if not USER_AGENT or not API_KEY:
        msg = "❌ USAJOBS credentials missing: set USAJOBS_USER_AGENT and USAJOBS_API_KEY."
        print(msg)
        send_discord(msg)
        return

    try:
        data = fetch_with_retries(
            BASE, PARAMS, HEADERS,
            attempts=RETRY_ATTEMPTS,
            backoff=RETRY_BACKOFF,
            timeout=TIMEOUT_SECONDS,
        )
    except Exception as e:
        err = f"🚨 USAJOBS fetch failed after retries: {e}"
        print(err)
        if NOTIFY_FETCH_FAILURE:
            send_discord(err)
        return

    sr = (data.get("SearchResult", {}) or {})
    items = (sr.get("SearchResultItems", []) or [])
    total = sr.get("SearchResultCount", len(items))

    print(f"[INFO] Fetched {total} results; items array length={len(items)}.")

    if (total == 0 or len(items) == 0):
        info = ("ℹ️ No results for USAJOBS query today "
                f"(Series 1176/1173, 92055±25mi, GS09–GS12 + NF-02).")
        print(info)
        if NOTIFY_ZERO_RESULTS:
            send_discord(info)
        return

    seen = load_seen(SEEN_PATH)
    new_count = 0
    for item in items:
        mo = item.get("MatchedObjectDescriptor", {})
        if not mo:
            continue
        rec = {
            "MatchedObjectId": item.get("MatchedObjectId"),
            "PositionTitle": mo.get("PositionTitle"),
            "OrganizationName": mo.get("OrganizationName"),
            "PositionLocationDisplay": mo.get("PositionLocationDisplay"),
            "ApplyURI": mo.get("ApplyURI"),
            "PositionURI": mo.get("PositionURI"),
            "JobGrade": [g.get("Code") for g in (mo.get("JobGrade") or [])],
            "ApplicationCloseDate": mo.get("ApplicationCloseDate"),
        }
        key = jid(rec)
        if key in seen:
            continue

        # 🔎 Extra filters:
        title = (rec.get("PositionTitle") or "").lower()
        grades = [g.lower() for g in (rec.get("JobGrade") or [])]

        if (
            "building management" not in title
            and "housing management" not in title
            and not any("nf-02" in g or "nf-2" in g for g in grades)
        ):
            continue  # skip jobs that don't match desired filters

        # New item → alert + record
        send_discord(format_msg(rec))
        seen[key] = {
            "title": rec["PositionTitle"],
            "first_seen": int(time.time()),
            "uri": rec.get("ApplyURI") or rec.get("PositionURI"),
        }
        new_count += 1

    if new_count:
        save_seen(SEEN_PATH, seen)
        print(f"[INFO] Saved {new_count} new items.")
    else:
        msg = "🟦 No new items today (matches exist, but already seen)."
        print(msg)
        if NOTIFY_NO_NEW_ITEMS:
            send_discord(msg)

if __name__ == "__main__":
    if ENFORCE_LOCAL_8PM and not guard_local_8pm():
        print("[INFO] Skipping run (not 8 PM America/Los_Angeles).")
    else:
        run_once()
