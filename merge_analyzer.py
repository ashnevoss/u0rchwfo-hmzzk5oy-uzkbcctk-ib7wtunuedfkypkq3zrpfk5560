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
import concurrent.futures

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

# ================= CORE AI FUNCTIONS =================

def extract_hook_with_ai(transcript):
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
        return "N/A"
    
    preview_text = transcript[:1000]
    prompt = (
        "You are a Viral Content Analyst. Analyze the transcript below.\n"
        "Goal: Extract the 'HOOK' (the very first sentence or 3-5 seconds of audio).\n"
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
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
        return "Unknown"
        
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

# ================= DATA FETCHING FUNCTIONS =================

def get_yt_channel_id(handle, api_key):
    url = "https://youtube.googleapis.com/youtube/v3/channels"
    if not handle.startswith("@"): handle = "@" + handle
    params = {"key": api_key, "forHandle": handle, "part": "id"}
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if "items" in data and data["items"]:
            return data["items"][0]["id"]
    except Exception:
        pass
    return None

def get_yt_shorts(channel_id, api_key, days_ago):
    # Calculate cutoff date based on days_ago
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
    except Exception as e:
        return []

def get_ig_reels(username, days_ago):
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    run_input = {"username": [username], "resultsLimit": 50, "resultsType": "posts"}
    try:
        run = apify_client.actor(IG_SCRAPER_ACTOR).call(run_input=run_input)
        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        valid_reels = []
        for item in dataset_items:
            timestamp = item.get("timestamp") or item.get("date")
            if not timestamp: continue
            try:
                if isinstance(timestamp, int):
                    reel_date = datetime.fromtimestamp(timestamp, timezone.utc)
                else:
                    reel_date = datetime.strptime(timestamp, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
            except: continue
            
            if reel_date < cutoff_date: continue
            
            # Check for video type
            if item.get("typeName") != "GraphVideo" and not item.get("isVideo", False):
                continue

            plays = item.get("playCount") or item.get("videoViewCount") or item.get("likesCount") or 0
            valid_reels.append({
                "title": (item.get("caption") or "")[:50] + "...", 
                "views": int(plays),
                "url": f"https://www.instagram.com/p/{item.get('shortCode')}/",
                "published": reel_date.strftime('%Y-%m-%d'),
                "id": item.get("id")
            })
        return sorted(valid_reels, key=lambda x: x['views'], reverse=True)
    except Exception:
        return []

def transcribe_video(url, platform_type):
    try:
        if platform_type == "YouTube Shorts":
            run = apify_client.actor(YT_TRANSCRIBER_ACTOR).call(run_input={"videoUrl": url, "downloadSubtitles": True}, timeout_secs=180)
        else:
            run = apify_client.actor(IG_TRANSCRIBER_ACTOR).call(run_input={"url": url, "openaiApiKey": openai_client.api_key}, timeout_secs=180)
            
        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        if not dataset_items: return "N/A"
        
        item = dataset_items[0]
        if platform_type == "YouTube Shorts":
            if item.get("subtitles"): return " ".join([l.get('text', '') for l in item.get("subtitles")])
            return item.get("transcript", "N/A")
        else:
            candidate = item.get("text") or item.get("transcript") or item.get("result")
            if isinstance(candidate, dict): return candidate.get("text", "N/A")
            return str(candidate) if candidate else "N/A"
    except:
        return "N/A"

# ================= THREADED PROCESSING =================

def process_single_video(video, platform_type, baseline):
    """
    Returns a tuple: (Result Row, Log String)
    """
    calc_log = []
    calc_log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    
    # 1. Calculation Logic
    multiplier = video['views'] / baseline
    calc_log.append(f"   • 🧮 **Math:** {video['views']:,} views ÷ {baseline:,} baseline = **{multiplier:.2f}x**")
    
    transcript = transcribe_video(video['url'], platform_type)
    
    if transcript == "N/A":
        calc_log.append("   • ❌ **Status:** Transcription Failed. Skipping.")
        return None, "\n".join(calc_log)
    
    calc_log.append(f"   • 📝 **Transcript:** Found ({len(transcript)} chars)")
    
    # 2. AI Logic
    hook = extract_hook_with_ai(transcript)
    topic = generate_topic_with_ai(transcript)
    
    calc_log.append(f"   • 🧠 **AI Analysis:** Topic identified as '{topic}'")
    calc_log.append(f"   • ✅ **Success:** Row generated.")

    row = [topic, video['views'], f"{round(multiplier, 1)}x", video['published'], video['url'], hook, transcript[:40000]]
    
    return row, "\n".join(calc_log)

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    handle_label = "YouTube Handle"
    default_handle = "@BusyFunda"
else:
    st.title("📸 Instagram Viral Analyzer")
    handle_label = "Instagram Username"
    default_handle = "sanjay_nuthra"

col1, col2 = st.columns(2)
with col1:
    target_handle = st.text_input(handle_label, value=default_handle)
    sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

st.divider()
st.subheader("⚙️ Filter & Math Logic")
c1, c2, c3 = st.columns(3)

# ✅ UPDATED: Added your requested time options and parsing logic
with c1:
    time_options = [
        "30 Days", "60 Days", "90 Days", "180 Days", "365 Days", 
        "1.5 Years", "2 Years", "Full History"
    ]
    selected_time = st.selectbox("Scan Last:", time_options, index=0)
    
    # Parse the selected string into integer 'days_ago'
    if selected_time == "Full History":
        days_ago = 10000 # ~27 years (covers entire history of YT/IG)
    elif "Year" in selected_time:
        # "1.5 Years" -> 1.5
        years = float(selected_time.split(" ")[0])
        days_ago = int(years * 365)
    else:
        # "30 Days" -> 30
        days_ago = int(selected_time.split(" ")[0])

with c2:
    manual_baseline = st.number_input("Baseline Views (Avg):", min_value=1000, value=10000, step=1000)
with c3:
    viral_multiplier = st.slider("Viral Multiplier:", 2, 50, 3)

target_views = manual_baseline * viral_multiplier

st.success(f"📊 **Viral Formula:** Any video with views > **{manual_baseline:,}** (Baseline) × **{viral_multiplier}** (Multiplier) = **{target_views:,} Views**")

if st.button("🚀 Start Deep Analysis", type="primary"):
    if not uploaded_google_key or not valid_api_config:
        st.error("❌ Missing Credentials.")
        st.stop()
        
    try:
        # Auth Sheets
        json_creds = json.load(uploaded_google_key)
        scope = ['https://spreadsheets.google.com/feeds','https://www.googleapis.com/auth/drive']
        creds = ServiceAccountCredentials.from_json_keyfile_dict(json_creds, scope)
        client = gspread.authorize(creds)
        sheet = client.open(sheet_name).sheet1

        # Main Progress Container
        with st.status("🔍 **Phase 1: Scouting Content...**", expanded=True) as status:
            
            # Fetch Logic
            if platform == "YouTube Shorts":
                channel_id = get_yt_channel_id(target_handle, YOUTUBE_API_KEY)
                if not channel_id: st.stop()
                videos = get_yt_shorts(channel_id, YOUTUBE_API_KEY, days_ago)
            else:
                videos = get_ig_reels(target_handle, days_ago)

            if not videos:
                status.update(label="❌ No videos found.", state="error")
                st.stop()

            # Filter Logic Visualization
            st.write(f"📉 Found {len(videos)} total videos. Filtering for viral hits...")
            targets = []
            for v in videos:
                is_viral = v['views'] >= target_views
                if is_viral:
                    targets.append(v)
                    st.write(f"🔥 **HIT:** {v['title'][:40]}... | {v['views']:,} views (>{target_views:,})")
            
            if not targets:
                status.update(label="❌ No viral videos found meeting your criteria.", state="error")
                st.stop()

            status.update(label=f"✅ Found {len(targets)} Viral Hits. Starting Deep Analysis...", state="running", expanded=False)

        # Phase 2: Deep Analysis with Real-Time Logs
        st.subheader("📝 Live Calculation Logs")
        log_container = st.container()
        results_to_save = []
        
        progress_bar = st.progress(0)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_video = {
                executor.submit(process_single_video, vid, platform, manual_baseline): vid 
                for vid in targets
            }
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_video):
                row, log_text = future.result()
                
                # Update the Log UI immediately
                with log_container:
                    with st.expander(f"Processed: {future_to_video[future]['title'][:50]}...", expanded=False):
                        st.markdown(log_text)

                if row:
                    results_to_save.append(row)
                
                completed += 1
                progress_bar.progress(completed / len(targets))

        # Final Save
        if results_to_save:
            existing = sheet.get_all_values()
            if not existing:
                sheet.append_row(['Topic', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript'])
            sheet.append_rows(results_to_save)
            st.success(f"🎉 Analysis Complete! Saved {len(results_to_save)} rows to Google Sheets.")
            st.dataframe(pd.DataFrame(results_to_save, columns=['Topic', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript']))

    except Exception as e:
        st.error(f"Critical Error: {e}")