# usajobs_watch.py
import os, json, time, hashlib, re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

import requests
from requests.exceptions import RequestException

# ===================== QUERY CONFIG =====================
BASE = "https://data.usajobs.gov/api/Search"
PARAMS = {
    # Series 1176 and 1173 (USAJOBS allows multiple values with colon ':' )
    "JobCategoryCode": "1176:1173",
    # 92055 (Camp Pendleton) within 25 miles
    "LocationName": "92055",
    "Radius": "25",
    # Grade range (GS-09 to GS-12)
    "PayGradeLow": "09",
    "PayGradeHigh": "12",
    # Return all fields
    "Fields": "All",
    # Don't restrict who may apply (public/status/all)
    "WhoMayApply": "all",
    # Sort newest first
    "SortField": "openingdate",
    "SortDirection": "desc",
    # Up to 50 per page (raise if desired)
    "ResultsPerPage": "50",
}
# ========================================================

# ===================== NOTIFY/BEHAVIOR ==================
# Set to "1" (default) to enforce running only at 8 PM America/Los_Angeles
ENFORCE_LOCAL_8PM = os.getenv("ENFORCE_LOCAL_8PM", "1") == "1"

# Alert when the API/site fails or times out
NOTIFY_FETCH_FAILURE = True
# Alert when the query returns zero results
NOTIFY_ZERO_RESULTS = True
# Alert when there ARE results, but no NEW ones since last run (can be noisy)
NOTIFY_NO_NEW_ITEMS = True

# HTTP timeouts & retries
TIMEOUT_SECONDS = 30
RETRY_ATTEMPTS = 3
RETRY_BACKOFF = 5  # seconds; backoff is exponential: 5, 10, 20...
# ========================================================

# ===================== AUTH / HEADERS ===================
USER_AGENT = os.getenv("USAJOBS_USER_AGENT")  # your email (required by USAJOBS)
API_KEY    = os.getenv("USAJOBS_API_KEY")     # your USAJOBS API key
DISCORD_WH = os.getenv("DISCORD_WEBHOOK")     # your Discord webhook URL (for alerts)

HEADERS = {
    "User-Agent": USER_AGENT or "",
    "Authorization-Key": API_KEY or "",
    "Accept": "application/json",
}
# ========================================================

SEEN_PATH = "seen_usajobs.json"


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
    # Stable ID from announcement + title + URL
    key = f"{rec.get('MatchedObjectId','')}|{rec.get('PositionTitle','')}|{(rec.get('ApplyURI') or [None])[0] or rec.get('PositionURI','')}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]


def send_discord(message: str) -> None:
    if not DISCORD_WH:
        print("[Discord disabled] " + message)
        return
    try:
        r = requests.post(DISCORD_WH, json={"content": message}, timeout=15)
        # Discord returns 204 No Content on success
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
    # Exhausted attempts
    raise RuntimeError(last_err or "Unknown fetch error")


def format_msg(obj: Dict[str, Any]) -> str:
    title = obj.get("PositionTitle", "Untitled")
    org   = obj.get("OrganizationName", "")
    # USAJOBS can return either a list of dicts in PositionLocationDisplay or a string
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
    """Return True only if it is 8:00 PM America/Los_Angeles right now."""
    tz = ZoneInfo("America/Los_Angeles")
    now = datetime.now(tz)
    return now.hour == 20  # 8 PM local hour (minute-precision is fine via cron)


def run_once() -> None:
    # Sanity checks
    if not USER_AGENT or not API_KEY:
        msg = "❌ USAJOBS credentials missing: set USAJOBS_USER_AGENT and USAJOBS_API_KEY."
        print(msg)
        send_discord(msg)
        return

    # Fetch
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

    # Parse results
    sr = (data.get("SearchResult", {}) or {})
    items = (sr.get("SearchResultItems", []) or [])
    total = sr.get("SearchResultCount", len(items))

    print(f"[INFO] Fetched {total} results; items array length={len(items)}.")

    # Zero results alert
    if (total == 0 or len(items) == 0):
        info = ("ℹ️ No results for USAJOBS query today "
                f"(Series 1176/1173, 92055±25mi, GS09–GS12).")
        print(info)
        if NOTIFY_ZERO_RESULTS:
            send_discord(info)
        return

    # Build light records and de-dup
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
        # New item → alert + record
        send_discord(format_msg(rec))
        seen[key] = {
            "title": rec["PositionTitle"],
            "first_seen": int(time.time()),
            "uri": rec.get("ApplyURI") or rec.get("PositionURI"),
        }
        new_count += 1

    # Persist dedupe state
    if new_count:
        save_seen(SEEN_PATH, seen)
        print(f"[INFO] Saved {new_count} new items.")
    else:
        msg = "🟦 No new items today (matches exist, but already seen)."
        print(msg)
        if NOTIFY_NO_NEW_ITEMS:
            send_discord(msg)


if __name__ == "__main__":
    # When run from GitHub Actions, we schedule two UTC crons (03:00 and 04:00)
    # and only proceed if it's actually 8 PM America/Los_Angeles.
    if ENFORCE_LOCAL_8PM and not guard_local_8pm():
        print("[INFO] Skipping run (not 8 PM America/Los_Angeles).")
    else:
        run_once()
