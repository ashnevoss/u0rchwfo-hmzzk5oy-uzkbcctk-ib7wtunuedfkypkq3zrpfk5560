import streamlit as st
import pandas as pd
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
from datetime import datetime, timedelta, timezone
import json

# ================= CONFIGURATION =================
# The specific Apidojo actor you want to test
APIDOJO_ACTOR_ID = "culc72xb7MP3EbaeX"  # apidojo/instagram-scraper
# =================================================

st.set_page_config(page_title="Apidojo Cost Tester", page_icon="🧪")

st.title("🧪 Apidojo Scraper Test")
st.markdown("""
This tool tests the `apidojo/instagram-scraper` API to verify costs and data quality.
**Safety Mode:** Limits are calculated automatically, but you can override them in the sidebar.
""")

# --- SIDEBAR: CREDENTIALS & SETTINGS ---
st.sidebar.header("🔑 Credentials")
apify_token = st.sidebar.text_input("Enter Apify API Token", type="password")
uploaded_google_key = st.sidebar.file_uploader("Upload Google Sheets JSON", type="json")

st.sidebar.markdown("---")
st.sidebar.header("💰 Cost Control")
# SAFETY FEATURE: Allow manual override of max items
manual_limit = st.sidebar.number_input(
    "Max Items Safety Limit", 
    min_value=5, 
    max_value=500, 
    value=50, 
    help="Hard limit on how many posts to fetch to prevent wasting credits."
)

# --- MAIN INPUTS ---
col1, col2 = st.columns(2)
with col1:
    target_handle = st.text_input("Instagram Username (No @)", "sanjay_nuthra")
with col2:
    time_frame = st.selectbox("Select Timeframe", ["Last 7 Days", "Last 15 Days", "Last 30 Days"])

sheet_name = st.text_input("Google Sheet Name", "Test_Data_Scrape")

# --- HELPER FUNCTIONS ---

def get_date_cutoff(frame_str):
    days = int(frame_str.split(" ")[1])
    return datetime.now(timezone.utc) - timedelta(days=days)

def connect_to_sheets(json_file, sheet_name):
    try:
        scope = ['https://spreadsheets.google.com/feeds', 'https://www.googleapis.com/auth/drive']
        # Load JSON from the uploaded file object properly
        json_file.seek(0)
        creds_dict = json.load(json_file)
        creds = ServiceAccountCredentials.from_json_keyfile_dict(creds_dict, scope)
        client = gspread.authorize(creds)
        
        # Open sheet or create if not exists
        try:
            sheet = client.open(sheet_name).sheet1
        except:
            st.warning(f"Sheet '{sheet_name}' not found. Creating it...")
            sh = client.create(sheet_name)
            sh.share(creds_dict['client_email'], perm_type='user', role='owner')
            sheet = sh.sheet1
            
        return sheet
    except Exception as e:
        st.error(f"Google Sheets Error: {e}")
        return None

def run_scraper(token, username, cutoff_date, hard_limit):
    client = ApifyClient(token)
    
    # Clean username
    username = username.strip().replace("@", "").replace("/", "")
    reels_url = f"https://www.instagram.com/{username}/reels/"
    
    # Format date for 'until' parameter (YYYY-MM-DD)
    until_date_str = cutoff_date.strftime("%Y-%m-%d")
    
    # 1. Date Math Limit: (Days * 5 posts/day buffer)
    days_diff = (datetime.now(timezone.utc) - cutoff_date).days
    calculated_limit = (days_diff * 5) + 10 
    
    # 2. Final Limit: Use the smaller of 'Calculated' vs 'User Hard Limit' to save money
    final_limit = min(calculated_limit, hard_limit)
    
    run_input = {
        "startUrls": [reels_url],
        "maxItems": final_limit, # Hard stop to save credits
        "until": until_date_str, # Soft stop based on date
        "customMapFunction": "(object) => { return {...object} }",
    }
    
    st.info(f"🚀 Starting scraper for **{username}**...")
    st.caption(f"⚙️ Config: Fetching max **{final_limit}** items or until **{until_date_str}**.")
    
    try:
        run = client.actor(APIDOJO_ACTOR_ID).call(run_input=run_input)
        
        if not run:
            st.error("Scraper failed to start.")
            return []
            
        dataset_items = client.dataset(run["defaultDatasetId"]).list_items().items
        return dataset_items
        
    except Exception as e:
        st.error(f"Apify Error: {e}")
        return []

def process_results(items, cutoff_date):
    processed_data = []
    
    for item in items:
        # 1. Parse Date (UPDATED to include 'createdAt')
        ts = item.get("taken_at_timestamp") or item.get("taken_at") or item.get("date") or item.get("createdAt")
        post_date = None
        
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    post_date = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                elif isinstance(ts, str):
                    # Handle "2025-11-28T14:09:54.000Z" -> "2025-11-28"
                    clean_ts = ts.split("T")[0]
                    post_date = datetime.strptime(clean_ts, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                post_date = None

        # 2. Safety Check: Skip if no date found
        if post_date is None:
            continue

        # 3. Filter by Date
        if post_date < cutoff_date:
            continue
            
        # 4. Extract Data
        # Caption
        caption = item.get("caption", "")
        if isinstance(caption, dict):
             caption = caption.get("text", "")
        
        # Views (UPDATED: Fallback to playCount or video_view_count, else 0)
        # Note: The JSON you showed only has 'likeCount'. 
        # If views are missing in JSON, this will default to 0.
        views = item.get("video_view_count") or item.get("playCount") or item.get("play_count") or item.get("view_count") or 0
        
        # Link
        # Your JSON has "code", so we construct the URL manually to ensure it works
        code = item.get("code")
        if code:
            url = f"https://www.instagram.com/reel/{code}/"
        else:
            url = item.get("url")

        # 5. Add to list
        # We removed the "is_video" check because your JSON snippet didn't have that key, 
        # and we don't want to skip valid data.
        processed_data.append([
            post_date.strftime("%Y-%m-%d"),
            caption,
            views,
            url
        ])
            
    return processed_data
    processed_data = []
    
    for item in items:
        # 1. Parse Date safely
        ts = item.get("taken_at_timestamp") or item.get("taken_at") or item.get("date")
        post_date = None
        
        if ts:
            try:
                if isinstance(ts, (int, float)):
                    post_date = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                elif isinstance(ts, str):
                    # Take first part of ISO string "2023-10-01T12:00:00" -> "2023-10-01"
                    clean_ts = ts.split("T")[0]
                    post_date = datetime.strptime(clean_ts, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except Exception:
                post_date = None

        # --- CRITICAL FIX: Skip items with no valid date ---
        if post_date is None:
            continue

        # 2. Filter by Date (Cutoff)
        if post_date < cutoff_date:
            continue
            
        # 3. Extract Data
        # Caption
        caption = ""
        if item.get("caption"):
            if isinstance(item["caption"], dict):
                caption = item["caption"].get("text", "")
            else:
                caption = str(item["caption"])
        
        # Views
        views = item.get("video_view_count") or item.get("play_count") or item.get("view_count") or 0
        
        # Link
        code = item.get("code") or item.get("short_code")
        url = f"https://www.instagram.com/reel/{code}/" if code else item.get("url", "N/A")

        # Only add valid video items
        # "is not False" allows None to pass (some scrapers don't set this key for all videos)
        if item.get("is_video") is not False: 
            processed_data.append([
                post_date.strftime("%Y-%m-%d"),
                caption,
                views,
                url
            ])
            
    return processed_data

# --- RUN BUTTON ---
if st.button("Start Scrape & Save", type="primary"):
    if not apify_token or not uploaded_google_key:
        st.error("❌ Please provide both Apify Token and Google Sheets JSON.")
    else:
        cutoff = get_date_cutoff(time_frame)
        
        # 1. Run Scraper with Cost Controls
        with st.spinner("Scraping data... (This may take 1-2 minutes)"):
            raw_data = run_scraper(apify_token, target_handle, cutoff, manual_limit)
            
        if raw_data:
            # 2. Process Data
            clean_rows = process_results(raw_data, cutoff)
            
            if clean_rows:
                st.success(f"✅ Scraped {len(clean_rows)} relevant videos from {time_frame}.")
                
                # 3. Write to Sheets
                sheet = connect_to_sheets(uploaded_google_key, sheet_name)
                if sheet:
                    # Add headers if sheet is empty
                    existing_data = sheet.get_all_values()
                    if not existing_data:
                        sheet.append_row(["Date", "Caption", "Views", "Link"])
                    
                    sheet.append_rows(clean_rows)
                    st.success(f"📝 Successfully wrote {len(clean_rows)} rows to **{sheet_name}**.")
                    st.dataframe(pd.DataFrame(clean_rows, columns=["Date", "Caption", "Views", "Link"]))
            else:
                st.warning(f"Apify finished, but found no videos after {cutoff.strftime('%Y-%m-%d')}. Try increasing the Timeframe.")
        else:
            st.warning("No data returned from Apify. Check your token or if the account is private.")