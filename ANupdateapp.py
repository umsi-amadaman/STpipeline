"""
DZ4P Campaign - Streamlit app for uploading contacts to Action Network.

Accepts uploaded CSV/text files, parses them (with known-format detection or
LLM fallback via Gemini), previews the data, and uploads to Action Network.
"""

import streamlit as st
import requests
import time
import re
import csv
import io
import json

# ── Config ────────────────────────────────────────────────────────────────
BASE_URL = "https://actionnetwork.org/api/v2"
FORM_ID = "ea2c4775-10d6-464b-bd48-ecd6a6161fbb"
DELAY = 0.3

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
    "Talk to Your Family, Friends, & Neighbors in Ward 4",
    "avail-mon-morning", "avail-mon-afternoon", "avail-mon-evening",
    "avail-tue-morning", "avail-tue-afternoon", "avail-tue-evening",
    "avail-wed-morning", "avail-wed-afternoon", "avail-wed-evening",
    "avail-thu-morning", "avail-thu-afternoon", "avail-thu-evening",
    "avail-fri-morning", "avail-fri-afternoon", "avail-fri-evening",
    "avail-sat-morning", "avail-sat-afternoon", "avail-sat-evening",
    "avail-sun-morning", "avail-sun-afternoon", "avail-sun-evening",
]

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-04-17:generateContent"

LLM_SYSTEM = """You are a data parsing assistant for a political campaign.
You will receive raw CSV or spreadsheet text from an unknown source.
Your job is to extract contact records and return them as a JSON array.

Each record must have these fields:
  first    - first name (string, may be empty)
  last     - last name (string, may be empty)
  email    - email address (string, required - skip rows with no email)
  address  - full mailing address as a single string (string, may be empty)
  message  - any notes, comments, or freetext the person wrote (string, may be empty)
  tags     - list of strings. Always include "email-list-signup". Add any other tags
             that make sense based on column names or values (e.g. checkbox columns,
             event names, group memberships, availability slots like "avail-sat-morning").
             If a cell contains comma-separated values that look like tag options, split them.
             For columns named like "checkboxes_Something", if the value is 1 or true,
             add "Something" as a tag.

Return ONLY a valid JSON array with no explanation, no markdown, no code fences.
If you cannot find any usable records, return an empty array [].
"""


# ── Parsing helpers ───────────────────────────────────────────────────────

def parse_address(raw):
    if not raw:
        return None
    parts = [p.strip() for p in raw.split(",")]
    if len(parts) < 3:
        return {"address_lines": [raw], "country": "US"}
    addr = {"country": "US", "address_lines": [parts[0]]}
    if parts[-1].strip().lower() in ("united states", "us", "usa"):
        parts = parts[:-1]
    state_zip = re.compile(r'^([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)$')
    zip_only = re.compile(r'^(\d{5}(?:-\d{4})?)$')
    state_only = re.compile(r'^([A-Za-z]{2})$')
    state_map = {"michigan": "MI", "georgia": "GA", "ohio": "OH", "california": "CA"}
    if len(parts) >= 2:
        addr["locality"] = parts[1].strip()
    for part in parts[2:]:
        part = part.strip()
        m = state_zip.match(part)
        if m:
            addr["region"] = m.group(1).upper()
            addr["postal_code"] = m.group(2)
            continue
        m = zip_only.match(part)
        if m:
            addr["postal_code"] = m.group(1)
            continue
        m = state_only.match(part)
        if m:
            addr["region"] = m.group(1).upper()
            continue
        if part.lower() in state_map:
            addr["region"] = state_map[part.lower()]
    return addr


def parse_address_from_columns(row):
    """Build an address dict from individual columns (Address, City, State, Zip)."""
    parts = {}
    address_line = (row.get("Address") or "").strip()
    city = (row.get("City") or "").strip()
    state = (row.get("State/Province Abbreviated") or row.get("State/Province") or "").strip()
    zipcode = (row.get("Zip code") or row.get("Zip") or "").strip()
    country = (row.get("Country") or "US").strip()

    if not any([address_line, city, state, zipcode]):
        return None

    addr = {"country": country or "US"}
    if address_line:
        addr["address_lines"] = [address_line]
    if city:
        addr["locality"] = city
    if state:
        addr["region"] = state[:2].upper() if len(state) > 2 else state.upper()
    if zipcode:
        addr["postal_code"] = str(zipcode)
    return addr


def split_name(full):
    parts = full.strip().split(None, 1)
    return (parts[0], parts[1]) if len(parts) == 2 else (parts[0], "")


# ── Known-format parser: fundraiser check-in CSV ─────────────────────────

def detect_fundraiser_checkin(headers):
    """Detect the fundraiser check-in CSV format by column names."""
    lower = [h.lower() for h in headers]
    return "first name" in lower and "email" in lower and any("checkboxes_" in h.lower() for h in headers)


def parse_fundraiser_checkin(csv_text):
    """Parse the fundraiser check-in CSV format with checkboxes_ columns."""
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        email = (row.get("Email") or "").strip()
        if not email:
            continue

        first = (row.get("First name") or "").strip()
        last = (row.get("Last name") or "").strip()
        textarea = (row.get("textarea") or "").strip()

        # Build address from columns
        addr_parts = []
        for col in ["Address", "City", "State/Province Abbreviated", "Zip code"]:
            val = (row.get(col) or "").strip()
            if val:
                addr_parts.append(val)
        address_str = ", ".join(addr_parts)

        # Extract tags from checkboxes_ columns
        tags = ["email-list-signup"]
        for key, val in row.items():
            if key.startswith("checkboxes_") and val and val.strip() in ("1", "true", "True"):
                tag_name = key[len("checkboxes_"):]
                if tag_name not in tags:
                    tags.append(tag_name)

        people.append({
            "first": first,
            "last": last,
            "email": email,
            "address": address_str,
            "message": textarea,
            "tags": tags,
        })
    return people


# ── Known-format parser: website signup sheets ────────────────────────────

def detect_website_signup1(headers):
    lower = [h.lower() for h in headers]
    return "name" in lower and "email" in lower and "address" in lower and "message optional" in lower


def parse_website_signup1(csv_text):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        name = (row.get("Name") or "").strip()
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        first, last = split_name(name) if name else ("", "")
        address = (row.get("Address") or "").strip()
        message = (row.get("Message optional") or "").strip()
        checkbox_col = next((k for k in row if "check the boxes" in k.lower()), None)
        extra_tags = [t.strip() for t in (row.get(checkbox_col, "") or "").split(",") if t.strip()] if checkbox_col else []
        tags = list(dict.fromkeys(["dz4p-website-signup", "email-list-signup"] + extra_tags))
        people.append({"first": first, "last": last, "email": email,
                        "address": address, "message": message, "tags": tags})
    return people


def detect_website_signup2(headers):
    lower = [h.lower() for h in headers]
    return "name" in lower and "email" in lower and "message" in lower and "address optional" in lower


def parse_website_signup2(csv_text):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        name = (row.get("Name") or "").strip()
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        first, last = split_name(name) if name else ("", "")
        address = (row.get("Address optional") or row.get("Address") or "").strip()
        message = (row.get("Message") or "").strip()
        checkbox_col = next((k for k in row if "check the boxes" in k.lower()), None)
        extra_tags = [t.strip() for t in (row.get(checkbox_col, "") or "").split(",") if t.strip()] if checkbox_col else []
        tags = list(dict.fromkeys(["dz4p-website-signup", "email-list-signup"] + extra_tags))
        people.append({"first": first, "last": last, "email": email,
                        "address": address, "message": message, "tags": tags})
    return people


# ── LLM parser (Gemini) ──────────────────────────────────────────────────

def parse_with_llm(raw_text, gemini_key):
    payload = {
        "contents": [{
            "parts": [{"text": LLM_SYSTEM}, {"text": raw_text[:12000]}]
        }],
        "generationConfig": {
            "temperature": 0.1,
            "maxOutputTokens": 4000,
        },
    }
    try:
        r = requests.post(
            f"{GEMINI_URL}?key={gemini_key}",
            headers={"Content-Type": "application/json"},
            json=payload,
            timeout=60,
        )
        if r.status_code != 200:
            return [], f"Gemini API error: {r.status_code} - {r.text[:200]}"

        result = r.json()
        usage = result.get("usageMetadata", {})

        try:
            finish_reason = result["candidates"][0].get("finishReason")
            if finish_reason == "MAX_TOKENS":
                return [], "Gemini response truncated (MAX_TOKENS)"
        except (KeyError, IndexError):
            pass

        parts = result["candidates"][0]["content"]["parts"]
        text = None
        for p in reversed(parts):
            if "text" in p:
                text = p["text"]
                break
        if not text:
            return [], "No text in Gemini response"

        text = text.strip()
        if text.startswith("```json"):
            text = text[7:]
        if text.startswith("```"):
            text = text[3:]
        if text.endswith("```"):
            text = text[:-3]

        people = json.loads(text.strip())
        for p in people:
            if "email-list-signup" not in p.get("tags", []):
                p.setdefault("tags", []).insert(0, "email-list-signup")

        token_info = (f"Tokens -- prompt:{usage.get('promptTokenCount', 0)} "
                      f"output:{usage.get('candidatesTokenCount', 0)} "
                      f"total:{usage.get('totalTokenCount', 0)}")
        return people, token_info

    except (KeyError, IndexError, json.JSONDecodeError) as e:
        return [], f"Error parsing Gemini response: {e}"
    except Exception as e:
        return [], f"Error calling Gemini: {e}"


# ── Auto-detect and parse ────────────────────────────────────────────────

def parse_file(csv_text, gemini_key=None):
    """Try known parsers first, fall back to LLM."""
    reader = csv.reader(io.StringIO(csv_text))
    try:
        headers = next(reader)
    except StopIteration:
        return [], "Empty file", "empty"

    if detect_fundraiser_checkin(headers):
        people = parse_fundraiser_checkin(csv_text)
        return people, f"Detected fundraiser check-in format. Found {len(people)} records.", "fundraiser-checkin"

    if detect_website_signup1(headers):
        people = parse_website_signup1(csv_text)
        return people, f"Detected Website Signup 1 format. Found {len(people)} records.", "website-signup-1"

    if detect_website_signup2(headers):
        people = parse_website_signup2(csv_text)
        return people, f"Detected Website Signup 2 format. Found {len(people)} records.", "website-signup-2"

    # Unknown format -- use LLM
    if not gemini_key:
        return [], "Unknown CSV format and no Gemini API key provided for LLM parsing.", "unknown"

    people, info = parse_with_llm(csv_text, gemini_key)
    return people, f"LLM parsed {len(people)} records. {info}", "llm"


# ── Action Network helpers ────────────────────────────────────────────────

def an_headers(api_key):
    return {"Content-Type": "application/json", "OSDI-API-Token": api_key}


def ensure_tags(api_key, extra_tags, progress_callback=None):
    headers = an_headers(api_key)
    tags_to_create = list(KNOWN_TAGS)
    for t in extra_tags:
        if t not in tags_to_create:
            tags_to_create.append(t)

    results = []
    for i, tag in enumerate(tags_to_create):
        resp = requests.post(f"{BASE_URL}/tags", headers=headers, json={"name": tag})
        ok = resp.status_code in (200, 201)
        results.append((tag, ok, resp.status_code))
        if progress_callback:
            progress_callback((i + 1) / len(tags_to_create))
        time.sleep(DELAY)
    return results


def upload_person(api_key, person):
    if not person.get("email"):
        return None, "no email"
    person_data = {
        "given_name": person["first"],
        "family_name": person["last"],
        "email_addresses": [{"address": person["email"]}],
    }

    # Handle address -- either from string or structured
    addr = parse_address(person.get("address", ""))
    if addr:
        person_data["postal_addresses"] = [addr]

    if person.get("message"):
        person_data["custom_fields"] = {"signup_message": person["message"]}

    payload = {
        "person": person_data,
        "add_tags": person.get("tags", ["email-list-signup"]),
    }
    resp = requests.post(
        f"{BASE_URL}/forms/{FORM_ID}/submissions",
        headers=an_headers(api_key),
        json=payload,
    )
    return resp.status_code, resp.text[:200]


# ── Streamlit app ─────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="DZ4P Action Network Uploader", layout="wide")
    st.title("DZ4P -- Action Network Uploader")

    # Sidebar: API keys
    st.sidebar.header("API Keys")
    an_key = st.sidebar.text_input("Action Network API Key", type="password",
                                    help="Your Action Network API key")
    gemini_key = st.sidebar.text_input("Gemini API Key", type="password",
                                        help="Required only for unknown CSV formats")

    st.sidebar.markdown("---")
    st.sidebar.header("Additional Tags")
    extra_tags_input = st.sidebar.text_area(
        "Extra tags to add to all records (one per line)",
        help="These tags will be added to every uploaded person, in addition to file-detected tags."
    )
    extra_global_tags = [t.strip() for t in extra_tags_input.strip().split("\n") if t.strip()]

    # File upload
    st.header("1. Upload File")
    uploaded_file = st.file_uploader(
        "Upload a CSV file with contact data",
        type=["csv", "txt", "tsv"],
        help="Supported formats: fundraiser check-in CSVs, website signup sheets, or any CSV (parsed via LLM)."
    )

    if uploaded_file is None:
        st.info("Upload a CSV file to get started.")
        return

    # Read and parse
    raw_text = uploaded_file.read().decode("utf-8-sig")
    st.text(f"File: {uploaded_file.name} ({len(raw_text):,} bytes)")

    people, parse_msg, fmt = parse_file(raw_text, gemini_key)
    st.header("2. Parse Results")
    st.write(parse_msg)

    if not people:
        st.warning("No records found. Check the file format or provide a Gemini API key for LLM parsing.")
        return

    # Add global extra tags
    if extra_global_tags:
        for p in people:
            for t in extra_global_tags:
                if t not in p.get("tags", []):
                    p.setdefault("tags", []).append(t)

    # Preview table
    st.header("3. Preview")
    preview_data = []
    for p in people:
        preview_data.append({
            "First": p["first"],
            "Last": p["last"],
            "Email": p["email"],
            "Address": p.get("address", "")[:50],
            "Message": (p.get("message") or "")[:50],
            "Tags": ", ".join(p.get("tags", [])),
        })
    st.dataframe(preview_data, use_container_width=True, height=400)
    st.write(f"**{len(people)} records** ready to upload.")

    # Upload
    st.header("4. Upload to Action Network")
    if not an_key:
        st.warning("Enter your Action Network API key in the sidebar to upload.")
        return

    if st.button("Upload to Action Network", type="primary"):
        # Collect all tags
        all_tags_seen = set()
        for p in people:
            all_tags_seen.update(p.get("tags", []))

        # Step 1: Ensure tags
        st.subheader("Ensuring tags...")
        tag_progress = st.progress(0)
        tag_results = ensure_tags(an_key, list(all_tags_seen), progress_callback=tag_progress.progress)
        tag_errors = [r for r in tag_results if not r[1]]
        if tag_errors:
            st.warning(f"{len(tag_errors)} tag(s) had errors: {', '.join(r[0] for r in tag_errors)}")
        else:
            st.success(f"All {len(tag_results)} tags OK.")

        # Step 2: Upload people
        st.subheader("Uploading people...")
        progress_bar = st.progress(0)
        status_area = st.empty()
        log_lines = []

        success = 0
        errors = 0
        uploadable = [p for p in people if p.get("email")]

        for i, person in enumerate(uploadable):
            label = f"{person['first']} {person['last']} <{person['email']}>"
            try:
                status_code, body = upload_person(an_key, person)
                if status_code in (200, 201):
                    success += 1
                    log_lines.append(f"OK  [{i+1}/{len(uploadable)}] {label}")
                else:
                    errors += 1
                    log_lines.append(f"ERR [{i+1}/{len(uploadable)}] {label} -- HTTP {status_code}: {body[:80]}")
            except Exception as e:
                errors += 1
                log_lines.append(f"ERR [{i+1}/{len(uploadable)}] {label} -- {e}")

            progress_bar.progress((i + 1) / len(uploadable))
            status_area.text(f"Uploaded {i+1}/{len(uploadable)} -- {success} OK, {errors} errors")
            time.sleep(DELAY)

        # Final summary
        st.markdown("---")
        if errors == 0:
            st.success(f"Done. {success} people uploaded successfully.")
        else:
            st.warning(f"Done. {success} uploaded, {errors} errors.")

        with st.expander("Upload log"):
            st.code("\n".join(log_lines))


if __name__ == "__main__":
    main()
