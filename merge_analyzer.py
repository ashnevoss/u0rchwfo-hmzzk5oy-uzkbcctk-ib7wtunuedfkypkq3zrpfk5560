import streamlit as st
import pandas as pd
import requests
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from apify_client import ApifyClient
from openai import OpenAI
import json
import time
from datetime import datetime, timedelta, timezone

# ================= CONFIGURATION =================
IG_SCRAPER_ACTOR = "apify/instagram-reel-scraper"
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe"
YT_TRANSCRIBER_ACTOR = "f3uGksrII7QnHi8oD"
# =================================================

st.set_page_config(page_title="Universal Viral Analyzer", page_icon="🚀", layout="wide")

# --- SIDEBAR: PLATFORM SELECTION & UPLOADS ---
st.sidebar.title("⚙️ Settings")
platform = st.sidebar.radio("Select Platform:", ["YouTube Shorts", "Instagram Reels"])

st.sidebar.divider()
st.sidebar.write("📂 **Upload Credentials**")
uploaded_google_key = st.sidebar.file_uploader("1. Google Sheets JSON", type="json")
uploaded_api_keys = st.sidebar.file_uploader("2. API Keys JSON", type="json")

# --- LOAD CREDENTIALS ---
apify_client = None
openai_client = None
YOUTUBE_API_KEY = None
valid_api_config = False

if uploaded_api_keys:
    try:
        keys = json.load(uploaded_api_keys)
        APIFY_TOKEN = keys.get("APIFY_TOKEN")
        OPENAI_API_KEY = keys.get("OPENAI_API_KEY")
        
        # YouTube key is only strictly needed if on YouTube mode
        YOUTUBE_API_KEY = keys.get("YOUTUBE_API_KEY")
        
        if APIFY_TOKEN and OPENAI_API_KEY:
            apify_client = ApifyClient(APIFY_TOKEN)
            openai_client = OpenAI(api_key=OPENAI_API_KEY)
            valid_api_config = True
            st.sidebar.success("✅ API Keys Loaded")
        else:
            st.sidebar.error("❌ Missing APIFY or OPENAI keys in JSON.")
    except Exception as e:
        st.sidebar.error(f"Error reading keys: {e}")

# ================= CORE AI FUNCTIONS (SHARED) =================

def extract_hook_with_ai(transcript):
    if not transcript or transcript == "N/A": return "N/A"
    preview_text = transcript[:800]
    prompt = (
        "You are a Viral Content Analyst. Analyze the transcript below.\n"
        "Goal: Extract the 'HOOK' (the very first 3-5 seconds of audio).\n"
        "Rules: Return ONLY the exact text of the hook. Do NOT translate.\n\n"
        f"Transcript Start:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.1
        )
        return response.choices[0].message.content.strip()
    except Exception:
        return transcript[:50] + "..."

def generate_topic_with_ai(transcript):
    if not transcript or transcript == "N/A": return "Unknown Topic"
    preview_text = transcript[:1000]
    prompt = (
        "Analyze this video transcript. \n"
        "Generate a short, 3-5 word 'Topic Label' or 'Category'.\n"
        "Return ONLY the topic label.\n\n"
        f"Transcript:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.3
        )
        return response.choices[0].message.content.strip().replace('"', '')
    except Exception:
        return "General"

# ================= YOUTUBE SPECIFIC FUNCTIONS =================

def get_yt_channel_id(handle, api_key):
    url = "https://youtube.googleapis.com/youtube/v3/channels"
    params = {"key": api_key, "forHandle": handle, "part": "id"}
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if "items" in data and data["items"]:
            return data["items"][0]["id"]
    except Exception:
        pass
    return None

def get_yt_shorts(channel_id, api_key, days_ago=30):
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    published_after = cutoff_date.strftime('%Y-%m-%dT%H:%M:%SZ')
    url = "https://youtube.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key, "channelId": channel_id, "part": "snippet,id",
        "order": "date", "publishedAfter": published_after,
        "videoDuration": "short", "maxResults": 50, "type": "video"
    }
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
    except Exception as e:
        st.error(f"YouTube API Error: {e}")
        return []

    video_ids = [item['id']['videoId'] for item in data.get('items', []) if 'videoId' in item['id']]
    shorts = []
    if video_ids:
        details_url = "https://youtube.googleapis.com/youtube/v3/videos"
        details_params = {"key": api_key, "id": ",".join(video_ids), "part": "statistics,snippet"}
        details_resp = requests.get(details_url, params=details_params)
        for v in details_resp.json().get('items', []):
            views = int(v['statistics'].get('viewCount', 0))
            shorts.append({
                "title": v['snippet']['title'],
                "views": views,
                "url": f"https://www.youtube.com/watch?v={v['id']}",
                "published": v['snippet']['publishedAt'][:10],
                "id": v['id']
            })
    return sorted(shorts, key=lambda x: x['views'], reverse=True)

def transcribe_yt(video_url):
    run_input = {"videoUrl": video_url, "downloadSubtitles": True, "saveSubsToKvs": False}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            run = apify_client.actor(YT_TRANSCRIBER_ACTOR).call(run_input=run_input)
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if not dataset_items: return "N/A"
            for item in dataset_items:
                if item.get("subtitles"):
                    subs = item.get("subtitles")
                    if isinstance(subs, list): return " ".join([l.get('text', '') for l in subs])
                    if isinstance(subs, str): return subs
                if item.get("transcript"): return item.get("transcript")
            return "N/A"
        except Exception:
            if attempt == max_retries - 1: return "N/A"
            time.sleep(2)
    return "N/A"

# ================= INSTAGRAM SPECIFIC FUNCTIONS =================

def get_ig_reels(username, days_ago=30):
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    run_input = {"username": [username], "resultsLimit": 50, "searchType": "hashtag", "searchLimit": 1}
    try:
        run = apify_client.actor(IG_SCRAPER_ACTOR).call(run_input=run_input)
        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        valid_reels = []
        for item in dataset_items:
            timestamp = item.get("timestamp")
            if not timestamp: continue
            try:
                reel_date = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            except ValueError:
                try:
                    reel_date = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                except: continue
            if reel_date < cutoff_date: continue
            
            plays = item.get("playCount") or item.get("videoViewCount") or item.get("likesCount") or 0
            valid_reels.append({
                "title": item.get("caption", "")[:50] + "...", 
                "views": int(plays),
                "url": item.get("url"),
                "published": reel_date.strftime('%Y-%m-%d'),
                "id": item.get("id")
            })
        return sorted(valid_reels, key=lambda x: x['views'], reverse=True)
    except Exception as e:
        st.error(f"Apify Discovery Error: {e}")
        return []

def transcribe_ig(reel_url):
    run_input = {"instagramUrl": reel_url, "url": reel_url, "openaiApiKey": openai_client.api_key, "settings": {"language": "en"}}
    max_retries = 3
    for attempt in range(max_retries):
        try:
            run = apify_client.actor(IG_TRANSCRIBER_ACTOR).call(run_input=run_input)
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if not dataset_items: return "N/A"
            item = dataset_items[0]
            candidate = item.get("text") or item.get("transcript") or item.get("result")
            if isinstance(candidate, dict):
                candidate = candidate.get("text") or candidate.get("transcript") or str(candidate)
            if candidate: return str(candidate)
            return "N/A"
        except Exception:
            if attempt == max_retries - 1: return "N/A"
            time.sleep(2)
    return "N/A"

# ================= MAIN UI LAYOUT =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    handle_label = "YouTube Handle (@name)"
    default_handle = "@BusyFunda"
else:
    st.title("📸 Instagram Viral Analyzer")
    handle_label = "Instagram Username"
    default_handle = "sanjay_nuthra"

# Inputs Row
col1, col2 = st.columns(2)
with col1:
    target_handle = st.text_input(handle_label, value=default_handle)
    sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

# Settings Row
st.divider()
st.subheader("⚙️ Filter Logic")
c1, c2, c3 = st.columns(3)
with c1:
    time_options = ["30 Days", "60 Days", "90 Days", "180 Days", "365 Days"]
    time_filter = st.selectbox("Scan Last:", time_options, index=0)
    days_ago = int(time_filter.split(" ")[0])
with c2:
    manual_baseline = st.number_input("Baseline Views (Normal):", min_value=1000, value=10000, step=1000)
with c3:
    viral_multiplier = st.slider("Viral Multiplier (x times):", 2, 50, 3)

threshold = manual_baseline * viral_multiplier
st.info(f"🎯 **Goal:** Find videos with > **{threshold:,} views**.")

# EXECUTION BUTTON
if st.button("🚀 Start Analysis", type="primary"):
    if not uploaded_google_key:
        st.error("❌ Please upload Google Sheets JSON in the Sidebar.")
    elif not valid_api_config:
        st.error("❌ Please upload API Keys JSON in the Sidebar.")
    elif platform == "YouTube Shorts" and not YOUTUBE_API_KEY:
        st.error("❌ API Keys JSON is missing 'YOUTUBE_API_KEY'.")
    else:
        # --- CONNECT SHEETS ---
        try:
            json_creds = json.load(uploaded_google_key)
            scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
            creds = ServiceAccountCredentials.from_json_keyfile_dict(json_creds, scope)
            client = gspread.authorize(creds)
            sheet = client.open(sheet_name).sheet1
            st.success("✅ Connected to Google Sheets")

            with st.status(f"Running {platform} Analysis...", expanded=True) as status:
                
                # --- FETCH DATA ---
                videos = []
                if platform == "YouTube Shorts":
                    status.write(f"🔍 Finding Channel ID for {target_handle}...")
                    channel_id = get_yt_channel_id(target_handle, YOUTUBE_API_KEY)
                    if not channel_id:
                        st.error("Could not find channel. Check handle.")
                        st.stop()
                    status.write(f"📊 Fetching Shorts...")
                    videos = get_yt_shorts(channel_id, YOUTUBE_API_KEY, days_ago)
                else:
                    status.write(f"🔍 Fetching Reels for {target_handle}...")
                    videos = get_ig_reels(target_handle, days_ago)

                if not videos:
                    status.update(label="No videos found.", state="error")
                    st.stop()

                # --- FILTER ---
                targets = []
                for v in videos:
                    v['multiplier_val'] = v['views'] / manual_baseline
                    if v['views'] >= threshold:
                        targets.append(v)
                
                if not targets:
                    status.update(label="No viral hits found. Lower the multiplier.", state="error")
                    st.stop()

                status.write(f"✅ Found {len(targets)} viral hits. Starting AI...")
                
                # --- PROCESS ---
                new_rows = []
                progress = st.progress(0)
                
                for idx, v in enumerate(targets):
                    try:
                        status.write(f"▶ Processing: {v['title'][:30]}...")
                        
                        # Select Transcriber based on platform
                        if platform == "YouTube Shorts":
                            transcript = transcribe_yt(v['url'])
                        else:
                            transcript = transcribe_ig(v['url'])

                        # AI Logic
                        hook = extract_hook_with_ai(transcript)
                        topic = generate_topic_with_ai(transcript)
                        
                        mult_str = f"{round(v['multiplier_val'], 1)}x"
                        new_rows.append([topic, v['views'], mult_str, v['published'], v['url'], hook, transcript[:40000]])
                        
                    except Exception as e:
                        st.warning(f"Skipped {v['title']} (Error: {e})")
                        continue
                    progress.progress((idx + 1) / len(targets))

                # --- SAVE ---
                if new_rows:
                    status.write("💾 Saving to Google Sheets...")
                    existing = sheet.get_all_values()
                    if not existing:
                        sheet.append_rows([['Topic', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript']] + new_rows)
                    else:
                        sheet.append_rows(new_rows)
                    status.update(label=f"Success! Added {len(new_rows)} rows.", state="complete", expanded=False)
                else:
                    status.update(label="Finished but no rows added.", state="error")

            st.dataframe(pd.DataFrame(new_rows, columns=['Topic', 'Views', 'Mult', 'Date', 'URL', 'Hook', 'Transcript']))

        except Exception as e:
            st.error(f"Connection Error: {e}")