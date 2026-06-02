"""
DZ4P Campaign - Daily Solidarity Tech Sync

Auto-pulls from Google Sheets, parses contacts, uploads new people to
Solidarity Tech, and surfaces messages/comments for Dave to read.

Usage:
    pip install streamlit requests
    streamlit run dz4p_solidarity_sync.py
"""

import streamlit as st
import requests
import time
import re
import csv
import io
import json

# ── Config ────────────────────────────────────────────────────────────────
ST_BASE_URL = "https://api.solidarity.tech/v1"
CHAPTER_ID = 2160
DELAY = 0.3

# Google Sheets CSV export URLs
SHEET1_URL = "https://docs.google.com/spreadsheets/d/1RRcaInrEYke6mccW7sVYLFwFJfvjhNN0fRso-_BE4Mk/export?format=csv&gid=0"
SHEET2_URL = "https://docs.google.com/spreadsheets/d/1tlPnpIn4fP_6BuDI9Cv9MXoJwXZRQTtrXo7cOZx2Hm4/export?format=csv&gid=0"

# The compound tag with commas in its name
COMPOUND_TAG = "Talk to Your Family, Friends, & Neighbors in Ward 4"

KNOWN_TAGS = [
    "dz4p-website-signup",
    "email-list-signup",
    "burns-park-senior-center-jan-2026",
    "canvassing-volunteer",
    "ward4-mailing-list",
    "Canvassing & Knocking on Doors",
    "Put up a Yard Sign",
    "Host a Meet & Greet",
    "Host a Fundraiser",
    "Join the Campaign Team",
    COMPOUND_TAG,
    "avail-mon-morning", "avail-mon-afternoon", "avail-mon-evening",
    "avail-tue-morning", "avail-tue-afternoon", "avail-tue-evening",
    "avail-wed-morning", "avail-wed-afternoon", "avail-wed-evening",
    "avail-thu-morning", "avail-thu-afternoon", "avail-thu-evening",
    "avail-fri-morning", "avail-fri-afternoon", "avail-fri-evening",
    "avail-sat-morning", "avail-sat-afternoon", "avail-sat-evening",
    "avail-sun-morning", "avail-sun-afternoon", "avail-sun-evening",
]

_PARSER_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-3-flash-preview:generateContent"

_PARSER_PROMPT = """You are a data parsing assistant for a political campaign.
You will receive raw CSV or spreadsheet text from an unknown source.
Your job is to extract contact records and return them as a JSON array.

Each record must have these fields:
  first    - first name (string, may be empty)
  last     - last name (string, may be empty)
  email    - email address (string, required - skip rows with no email)
  phone    - phone number (string, may be empty)
  address  - full mailing address as a single string (string, may be empty)
  message  - any notes, comments, or freetext the person wrote (string, may be empty)
  tags     - list of strings. Always include "email-list-signup". Add any other tags
             that make sense based on column names or values.

Return ONLY a valid JSON array with no explanation, no markdown, no code fences.
If you cannot find any usable records, return an empty array [].
"""


# ── Tag parsing (handles compound tag with commas) ────────────────────────

def parse_checkbox_tags(raw):
    """Parse comma-separated tags, preserving the compound tag name."""
    if not raw or not raw.strip():
        return []
    tags = []
    remaining = raw.strip()
    if COMPOUND_TAG in remaining:
        tags.append(COMPOUND_TAG)
        remaining = remaining.replace(COMPOUND_TAG, "").strip().strip(",").strip()
    compound_variant = COMPOUND_TAG.rstrip() + " "
    if compound_variant in remaining:
        if COMPOUND_TAG not in tags:
            tags.append(COMPOUND_TAG)
        remaining = remaining.replace(compound_variant, "").strip().strip(",").strip()
    if remaining:
        for t in remaining.split(","):
            t = t.strip()
            if t and t not in tags:
                tags.append(t)
    return tags


# ── Parsing helpers ───────────────────────────────────────────────────────

def split_name(full):
    parts = full.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


def clean_phone(val):
    if not val or not val.strip():
        return ""
    digits = re.sub(r"\D", "", str(val))
    if len(digits) == 11 and digits[0] == "1":
        digits = digits[1:]
    return digits if len(digits) == 10 else str(val).strip()


# ── Sheet fetching ────────────────────────────────────────────────────────

def fetch_csv(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except Exception as e:
        return None


# ── Known-format parsers ─────────────────────────────────────────────────

def detect_and_parse(csv_text):
    """Auto-detect format and parse."""
    reader = csv.DictReader(io.StringIO(csv_text))
    try:
        headers = reader.fieldnames or []
    except:
        return [], "empty"

    lower_headers = [h.lower() for h in headers]

    if "message optional" in lower_headers:
        return parse_website_signup1(csv_text), "website-signup-1"

    if "address optional" in lower_headers:
        return parse_website_signup2(csv_text), "website-signup-2"

    if "first name" in lower_headers and any("checkboxes_" in h for h in headers):
        return parse_fundraiser_checkin(csv_text), "fundraiser-checkin"

    return [], "unknown"


def parse_website_signup1(csv_text):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        name = (row.get("Name") or "").strip()
        first, last = split_name(name) if name else ("", "")
        address = (row.get("Address") or "").strip()
        message = (row.get("Message optional") or "").strip()
        phone = clean_phone(row.get("phone", ""))
        checkbox_col = next((k for k in row if "check the boxes" in k.lower()), None)
        extra_tags = parse_checkbox_tags(row.get(checkbox_col, "")) if checkbox_col else []
        tags = list(dict.fromkeys(["dz4p-website-signup", "email-list-signup"] + extra_tags))
        people.append({"first": first, "last": last, "email": email,
                        "address": address, "message": message, "phone": phone, "tags": tags})
    return people


def parse_website_signup2(csv_text):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        name = (row.get("Name") or "").strip()
        first, last = split_name(name) if name else ("", "")
        address = (row.get("Address optional") or row.get("Address") or "").strip()
        message = (row.get("Message") or "").strip()
        phone = clean_phone(row.get("phone", ""))
        checkbox_col = next((k for k in row if "check the boxes" in k.lower()), None)
        extra_tags = parse_checkbox_tags(row.get(checkbox_col, "")) if checkbox_col else []
        tags = list(dict.fromkeys(["dz4p-website-signup", "email-list-signup"] + extra_tags))
        people.append({"first": first, "last": last, "email": email,
                        "address": address, "message": message, "phone": phone, "tags": tags})
    return people


def parse_fundraiser_checkin(csv_text):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        first = (row.get("First name") or "").strip()
        last = (row.get("Last name") or "").strip()
        textarea = (row.get("textarea") or "").strip()
        addr_parts = []
        for col in ["Address", "City", "State/Province Abbreviated", "Zip code"]:
            val = (row.get(col) or "").strip()
            if val:
                addr_parts.append(val)
        address_str = ", ".join(addr_parts)
        phone = clean_phone(row.get("Phone", "") or row.get("phone", ""))
        tags = ["email-list-signup"]
        for key, val in row.items():
            if key.startswith("checkboxes_") and val and val.strip() in ("1", "true", "True"):
                tag_name = key[len("checkboxes_"):]
                if tag_name not in tags:
                    tags.append(tag_name)
        people.append({"first": first, "last": last, "email": email,
                        "address": address_str, "message": textarea, "phone": phone, "tags": tags})
    return people


# ── Fallback parser for unknown CSV formats ──────────────────────────────

def parse_unknown(raw_text, parser_key):
    payload = {
        "contents": [{"parts": [{"text": _PARSER_PROMPT}, {"text": raw_text[:12000]}]}],
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": 4000},
    }
    try:
        r = requests.post(f"{_PARSER_URL}?key={parser_key}",
                          headers={"Content-Type": "application/json"},
                          json=payload, timeout=60)
        if r.status_code != 200:
            return [], f"Parser error: {r.status_code}"
        result = r.json()
        parts = result["candidates"][0]["content"]["parts"]
        text = None
        for p in reversed(parts):
            if "text" in p:
                text = p["text"]
                break
        if not text:
            return [], "No results from parser"
        text = text.strip().strip("`").removeprefix("json").strip()
        people = json.loads(text)
        for p in people:
            if "email-list-signup" not in p.get("tags", []):
                p.setdefault("tags", []).insert(0, "email-list-signup")
        return people, f"Parsed {len(people)} records"
    except Exception as e:
        return [], f"Parser error: {e}"


# ── Solidarity Tech API ──────────────────────────────────────────────────

def st_headers(api_key):
    return {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}


def upload_person_st(api_key, person):
    if not person.get("email"):
        return None, "no email"

    user_data = {
        "first_name": person["first"],
        "last_name": person["last"],
        "email": person["email"],
        "tags": person.get("tags", ["email-list-signup"]),
        "chapter_id": CHAPTER_ID,
    }

    if person.get("address"):
        user_data["address"] = person["address"]
    if person.get("phone"):
        user_data["phone_number"] = person["phone"]

    payload = {"user": user_data}
    resp = requests.post(f"{ST_BASE_URL}/users",
                         headers=st_headers(api_key), json=payload)
    return resp.status_code, resp.text[:300]


# ── Streamlit App ─────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="DZ4P Daily Sync", layout="wide")
    st.title("DZ4P — Solidarity Tech Sync")
    st.caption("Pull website signups, upload to Solidarity Tech, review messages")

    # Sidebar
    st.sidebar.header("API Keys")
    st_key = st.sidebar.text_input("Solidarity Tech API Key", type="password")
    parser_key = st.sidebar.text_input("Parser Key (for unknown CSVs)", type="password")

    st.sidebar.markdown("---")
    st.sidebar.header("Settings")
    auto_fetch = st.sidebar.checkbox("Auto-fetch Google Sheets", value=True)

    # ── Step 1: Fetch data ────────────────────────────────────────────────
    st.header("1. Pull Contacts")

    all_people = []

    if auto_fetch:
        col1, col2 = st.columns(2)
        with col1:
            if st.button("Fetch Google Sheets", type="primary", use_container_width=True):
                with st.spinner("Fetching Website_Signup1..."):
                    csv1 = fetch_csv(SHEET1_URL)
                if csv1:
                    p1, fmt1 = detect_and_parse(csv1)
                    st.success(f"Sheet 1: {len(p1)} records")
                    all_people.extend(p1)
                else:
                    st.error("Could not fetch Sheet 1")

                with st.spinner("Fetching Website_Signup2..."):
                    csv2 = fetch_csv(SHEET2_URL)
                if csv2:
                    p2, fmt2 = detect_and_parse(csv2)
                    st.success(f"Sheet 2: {len(p2)} records")
                    all_people.extend(p2)
                else:
                    st.error("Could not fetch Sheet 2")

                st.session_state["all_people"] = all_people

        with col2:
            uploaded_file = st.file_uploader("Or upload a CSV", type=["csv", "txt", "tsv"])
            if uploaded_file:
                raw = uploaded_file.read().decode("utf-8-sig")
                people, fmt = detect_and_parse(raw)
                if not people and parser_key:
                    people, info = parse_unknown(raw, parser_key)
                    st.info(info)
                if people:
                    st.success(f"File: {len(people)} records")
                    prev = st.session_state.get("all_people", [])
                    st.session_state["all_people"] = prev + people
                else:
                    st.warning("No records found in file")

    # Get people from session
    all_people = st.session_state.get("all_people", [])

    if not all_people:
        st.info("Click 'Fetch Google Sheets' to pull the latest signups.")
        return

    # ── Dedupe ────────────────────────────────────────────────────────────
    seen = {}
    deduped = []
    for p in all_people:
        email = p.get("email", "").lower().strip()
        if not email:
            continue
        if email in seen:
            existing = seen[email]
            for tag in p.get("tags", []):
                if tag not in existing.get("tags", []):
                    existing["tags"].append(tag)
            if not existing.get("address") and p.get("address"):
                existing["address"] = p["address"]
            if not existing.get("phone") and p.get("phone"):
                existing["phone"] = p["phone"]
            if not existing.get("message") and p.get("message"):
                existing["message"] = p["message"]
        else:
            seen[email] = p
            deduped.append(p)

    # ── Step 2: Messages for Dave ─────────────────────────────────────────
    messages = [p for p in deduped if p.get("message") and p["message"].strip()]

    if messages:
        st.header(f"2. Messages for Dave ({len(messages)})")
        st.caption("These people left a note when they signed up:")
        for p in messages:
            with st.expander(f"{p['first']} {p['last']} -- {p['email']}" +
                             (f" -- {p.get('phone', '')}" if p.get('phone') else "")):
                st.markdown(f"> {p['message']}")
                if p.get("address"):
                    st.caption(p['address'])
                if p.get("tags"):
                    st.caption(', '.join(p['tags']))
    else:
        st.header("2. Messages for Dave")
        st.info("No messages this batch.")

    # ── Step 3: Preview ──────────────────────────────────────────────────
    st.header(f"3. Preview ({len(deduped)} contacts)")
    preview = []
    for p in deduped:
        preview.append({
            "Name": f"{p['first']} {p['last']}",
            "Email": p["email"],
            "Phone": p.get("phone", ""),
            "Address": (p.get("address") or "")[:50],
            "Tags": ", ".join(p.get("tags", [])),
            "Has Message": ("yes" if p.get("message") else ""),
        })
    st.dataframe(preview, use_container_width=True, height=400)

    # ── Step 4: Upload ───────────────────────────────────────────────────
    st.header("4. Upload to Solidarity Tech")

    if not st_key:
        st.warning("Enter your Solidarity Tech API key in the sidebar.")
        return

    if st.button("Upload to Solidarity Tech", type="primary"):
        uploadable = [p for p in deduped if p.get("email")]
        progress = st.progress(0)
        status = st.empty()
        log_lines = []
        success = errors = 0

        for i, person in enumerate(uploadable):
            label = f"{person['first']} {person['last']} <{person['email']}>"
            try:
                code, body = upload_person_st(st_key, person)
                if code in (200, 201):
                    success += 1
                    log_lines.append(f"OK  [{i+1}/{len(uploadable)}] {label}")
                else:
                    errors += 1
                    log_lines.append(f"ERR [{i+1}/{len(uploadable)}] {label} -- HTTP {code}: {body[:100]}")
            except Exception as e:
                errors += 1
                log_lines.append(f"ERR [{i+1}/{len(uploadable)}] {label} -- {e}")

            progress.progress((i + 1) / len(uploadable))
            status.text(f"Uploaded {i+1}/{len(uploadable)} -- {success} OK, {errors} errors")
            time.sleep(DELAY)

        st.markdown("---")
        if errors == 0:
            st.success(f"Done. {success} people uploaded.")
        else:
            st.warning(f"Done. {success} uploaded, {errors} errors.")

        with st.expander("Upload log"):
            st.code("\n".join(log_lines))


if __name__ == "__main__":
    main()
