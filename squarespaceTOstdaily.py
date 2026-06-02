"""
DZ4P Campaign - Daily Solidarity Tech Sync

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

# ── Config ────────────────────────────────────────────────────────────────
ST_BASE_URL = "https://api.solidarity.tech/v1"
CHAPTER_ID = 2160
DELAY = 0.3

SHEET1_URL = "https://docs.google.com/spreadsheets/d/1RRcaInrEYke6mccW7sVYLFwFJfvjhNN0fRso-_BE4Mk/export?format=csv&gid=0"
SHEET2_URL = "https://docs.google.com/spreadsheets/d/1tlPnpIn4fP_6BuDI9Cv9MXoJwXZRQTtrXo7cOZx2Hm4/export?format=csv&gid=0"

COMPOUND_TAG = "Talk to Your Family, Friends, & Neighbors in Ward 4"


# ── Helpers ───────────────────────────────────────────────────────────────

def parse_checkbox_tags(raw):
    if not raw or not raw.strip():
        return []
    tags = []
    remaining = raw.strip()
    for variant in [COMPOUND_TAG, COMPOUND_TAG.rstrip() + " "]:
        if variant in remaining:
            if COMPOUND_TAG not in tags:
                tags.append(COMPOUND_TAG)
            remaining = remaining.replace(variant, "").strip().strip(",").strip()
    if remaining:
        for t in remaining.split(","):
            t = t.strip()
            if t and t not in tags:
                tags.append(t)
    return tags


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


def fetch_csv(url):
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return r.text
    except:
        return None


def parse_sheet(csv_text, address_col="Address", message_col="Message optional"):
    people = []
    reader = csv.DictReader(io.StringIO(csv_text))
    for row in reader:
        email = (row.get("Email") or "").strip()
        if not email:
            continue
        name = (row.get("Name") or "").strip()
        first, last = split_name(name) if name else ("", "")
        address = (row.get(address_col) or "").strip()
        message = (row.get(message_col) or "").strip()
        phone = clean_phone(row.get("phone", ""))
        checkbox_col = next((k for k in row if "check the boxes" in k.lower()), None)
        extra_tags = parse_checkbox_tags(row.get(checkbox_col, "")) if checkbox_col else []
        tags = list(dict.fromkeys(["dz4p-website-signup", "email-list-signup"] + extra_tags))
        people.append({"first": first, "last": last, "email": email,
                        "address": address, "message": message, "phone": phone, "tags": tags})
    return people


def dedupe(all_people):
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
    return deduped


def upload_person(api_key, person):
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
    resp = requests.post(f"{ST_BASE_URL}/users",
                         headers={"Content-Type": "application/json",
                                  "Authorization": f"Bearer {api_key}"},
                         json={"user": user_data})
    return resp.status_code, resp.text[:300]


# ── App ───────────────────────────────────────────────────────────────────

def main():
    st.set_page_config(page_title="DZ4P Daily Sync", layout="wide")

    # Password gate
    if "authenticated" not in st.session_state:
        st.session_state["authenticated"] = False

    if not st.session_state["authenticated"]:
        st.title("DZ4P — Solidarity Tech Sync")
        pwd = st.text_input("Password", type="password")
        if st.button("Log in"):
            if pwd == st.secrets.get("app_password", ""):
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Incorrect password.")
        return

    st_key = st.secrets.get("st_api_key", "")
    if not st_key:
        st.error("Solidarity Tech API key not configured in secrets.")
        return

    st.title("DZ4P — Solidarity Tech Sync")

    # One button
    if st.button("Sync Now", type="primary"):
        # Fetch
        with st.spinner("Pulling signups..."):
            all_people = []
            csv1 = fetch_csv(SHEET1_URL)
            if csv1:
                all_people.extend(parse_sheet(csv1, "Address", "Message optional"))
            csv2 = fetch_csv(SHEET2_URL)
            if csv2:
                all_people.extend(parse_sheet(csv2, "Address optional", "Message"))
            people = dedupe(all_people)

        if not people:
            st.warning("No signups found.")
            return

        st.success(f"Found {len(people)} contacts.")

        # Messages
        messages = [p for p in people if p.get("message") and p["message"].strip()]
        if messages:
            st.header(f"Messages ({len(messages)})")
            for p in messages:
                with st.expander(f"{p['first']} {p['last']} -- {p['email']}" +
                                 (f" -- {p['phone']}" if p.get('phone') else "")):
                    st.markdown(f"> {p['message']}")
                    if p.get("address"):
                        st.caption(p["address"])
                    if p.get("tags"):
                        st.caption(", ".join(p["tags"]))

        # Upload
        st.header("Uploading...")
        progress = st.progress(0)
        status = st.empty()
        log_lines = []
        success = errors = 0

        for i, person in enumerate(people):
            label = f"{person['first']} {person['last']} <{person['email']}>"
            try:
                code, body = upload_person(st_key, person)
                if code in (200, 201):
                    success += 1
                    log_lines.append(f"OK  [{i+1}/{len(people)}] {label}")
                else:
                    errors += 1
                    log_lines.append(f"ERR [{i+1}/{len(people)}] {label} -- HTTP {code}: {body[:100]}")
            except Exception as e:
                errors += 1
                log_lines.append(f"ERR [{i+1}/{len(people)}] {label} -- {e}")

            progress.progress((i + 1) / len(people))
            status.text(f"{i+1}/{len(people)} -- {success} OK, {errors} errors")
            time.sleep(DELAY)

        st.markdown("---")
        if errors == 0:
            st.success(f"Done. {success} people synced.")
        else:
            st.warning(f"Done. {success} synced, {errors} errors.")

        with st.expander("Log"):
            st.code("\n".join(log_lines))


if __name__ == "__main__":
    main()
