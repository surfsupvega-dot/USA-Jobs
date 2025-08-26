# usajobs_watch.py
import os, json, time, hashlib, re
from datetime import datetime
from zoneinfo import ZoneInfo
import requests

# ---- Search config ----
BASE = "https://data.usajobs.gov/api/Search"
PARAMS = {
    # Multiple series: colon-separated per USAJOBS docs
    "JobCategoryCode": "1176:1173",
    # 92055 (Camp Pendleton) within 25 miles
    "LocationName": "92055",
    "Radius": "25",
    # Grade range GS-09 to GS-12
    "PayGradeLow": "09",
    "PayGradeHigh": "12",
    # Return full fields
    "Fields": "Full",
    # Include all hiring paths
    "WhoMayApply": "all",
    # Sort newest first
    "SortField": "openingdate",
    "SortDirection": "desc",
    # Return up to 50 results
    "ResultsPerPage": "50",
}

# ---- Headers from GitHub Secrets ----
USER_AGENT = os.getenv("USAJOBS_USER_AGENT")  # your email (required)
API_KEY    = os.getenv("USAJOBS_API_KEY")     # your_
