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
# 1. SCOUT (Metadata): Used to check counts cheaply
IG_PROFILE_ACTOR = "apify/instagram-profile-scraper" 

# 2. DEEP SCRAPE (Content): Used to get actual reels
IG_REEL_ACTOR = "xMc5Ga1oCONPmWJIa" 

# 3. TRANSCRIBERS: 
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe" 
YT_TRANSCRIBER_ACTOR = "akash9078/youtube-transcript-extractor"
# =================================================

st.set_page_config(page_title="Universal Viral Analyzer Pro", page_icon="🚀", layout="wide")

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
OPENAI_API_KEY_VAL = None 
valid_api_config = False

if uploaded_api_keys:
    try:
        keys = json.load(uploaded_api_keys)
        APIFY_TOKEN = keys.get("APIFY_TOKEN")
        OPENAI_API_KEY_VAL = keys.get("OPENAI_API_KEY")
        YOUTUBE_API_KEY = keys.get("YOUTUBE_API_KEY")
        
        if APIFY_TOKEN and OPENAI_API_KEY_VAL:
            apify_client = ApifyClient(APIFY_TOKEN)
            openai_client = OpenAI(api_key=OPENAI_API_KEY_VAL)
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

def identify_format_with_ai(transcript):
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
        return "Unknown"
        
    preview_text = transcript[:2000]
    
    format_list = """
    1. Celebrity Format (Discussing or using a celebrity hook)
    2. Beginner vs expert (Comparison of skill levels)
    3. Problem Vs solution (Identifies a pain point and solves it)
    4. Multiple Characters (Skits with one person playing multiple roles)
    5. Q&A / Public Review (Answering questions or street interviews)
    6. Visual Dual Character (Split screen or visual dialogue)
    7. Podcast Style (Talking head with mic, interview vibe)
    8. Before Vs After (Transformation results)
    9. Choose one / This Vs That (Comparison or choice)
    10. Contrast method (Highlighting differences to prove a point)
    11. Storytelling (Narrative arc, personal story)
    12. Replace with (Alternative recommendations)
    13. Normal (Standard talking head or vlog)
    """
    
    prompt = (
        "Analyze the transcript below and classify it into exactly ONE of the following content formats:\n"
        f"{format_list}\n\n"
        "Rules:\n"
        "- Return ONLY the name of the format (e.g., 'Storytelling' or 'Problem Vs solution').\n"
        "- Do not write sentences, just the label.\n\n"
        f"Transcript:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.1
        )
        return response.choices[0].message.content.strip().replace('"', '').replace('.', '')
    except Exception:
        return "Normal"

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

# --- NEW FUNCTION: SMART PROFILE CHECK ---
def get_ig_profile_stats(username):
    """
    Quickly fetches just the profile metadata to get the total posts count.
    Cost: Very low (~$0.01 or free tier).
    """
    try:
        run_input = { "usernames": [username] }
        # Uses the cheaper 'instagram-profile-scraper'
        run = apify_client.actor(IG_PROFILE_ACTOR).call(run_input=run_input)
        
        if not run: return None
        
        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        if dataset_items:
            item = dataset_items[0]
            # postsCount is usually the total media count (Photos + Reels)
            return item.get("postsCount") or item.get("mediaCount")
    except Exception as e:
        print(f"Profile Stat Check Failed: {e}")
    return None

def get_ig_reels(username, days_ago):
    # 1. CLEAN USERNAME
    username = username.strip().replace("@", "")
    username = username.strip().replace("https://www.instagram.com/", "").replace("/", "")
    
    # 2. SMART PRE-FLIGHT CHECK
    # Check how many posts actually exist before we commit to scraping
    st.info(f"🕵️ Phase 1: Checking Profile Stats for '{username}'...")
    total_posts = get_ig_profile_stats(username)
    
    dynamic_limit = 50 # Default fallback
    
    if total_posts:
        st.success(f"✅ Profile Found: User has **{total_posts}** total posts.")
        
        if days_ago > 365:
            # Full History Strategy:
            # Set limit to exactly the total posts (plus small buffer) to get EVERYTHING.
            dynamic_limit = total_posts + 20
            st.write(f"🚀 Strategy: **Full History Mode** | Setting Limit to **{dynamic_limit}** to capture all reels.")
        else:
            # Recent History Strategy:
            # Estimate 3 posts/day max for the time period.
            estimated_posts = days_ago * 3
            dynamic_limit = min(estimated_posts, total_posts)
            st.write(f"📉 Strategy: **Recent Mode** ({days_ago} days) | Setting Limit to **{dynamic_limit}**.")
    else:
        st.warning("⚠️ Could not verify total posts count. Using fallback estimation.")
        dynamic_limit = 1000 if days_ago > 365 else days_ago * 3

    # Safety floor
    if dynamic_limit < 50: dynamic_limit = 50

    # 3. RUN THE DEEP SCRAPER
    st.info(f"🕵️ Phase 2: Deep Scraping via Actor ({IG_REEL_ACTOR})...")
    
    run_input = {
        "username": [username],
        "resultsLimit": dynamic_limit,
        "includeSharesCount": False,
        "skipPinnedPosts": False, 
    }
    
    try:
        run = apify_client.actor(IG_REEL_ACTOR).call(run_input=run_input)
        
        if not run:
            st.error("❌ Apify run failed to start.")
            return []

        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        st.write(f"📦 **Raw Items Retrieved:** {len(dataset_items)}")
        
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
        
        valid_reels = []
        skipped_old = 0
        skipped_error = 0
        
        for item in dataset_items:
            # --- DATE PARSING ---
            ts = item.get("timestamp") or item.get("takenAt") or item.get("date")
            reel_date = None
            
            try:
                if ts:
                    if isinstance(ts, (int, float)):
                        reel_date = datetime.fromtimestamp(ts, tz=timezone.utc)
                    elif isinstance(ts, str):
                        if ts.isdigit():
                             reel_date = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                        else:
                            formats = ["%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"]
                            for fmt in formats:
                                try:
                                    reel_date = datetime.strptime(ts, fmt).replace(tzinfo=timezone.utc)
                                    break
                                except ValueError:
                                    continue
            except Exception:
                pass
            
            if not reel_date:
                skipped_error += 1
                continue
                
            if reel_date < cutoff_date:
                skipped_old += 1
                continue

            # --- VIEWS & URL ---
            views = item.get("playCount") or item.get("viewCount") or item.get("videoViewCount")
            if views is None: views = 0
            
            post_url = item.get("url") or item.get("videoUrl")
            if not post_url:
                code = item.get("shortCode") or item.get("code")
                if code:
                    post_url = f"https://www.instagram.com/reel/{code}/"
            
            if not post_url:
                skipped_error += 1
                continue

            valid_reels.append({
                "title": (item.get("caption") or "")[:50] + "...",
                "full_caption": item.get("caption") or "",
                "views": int(views),
                "url": post_url,
                "published": reel_date.strftime('%Y-%m-%d'),
                "id": item.get("id")
            })
            
        st.write(f"✅ **Valid Reels Filtered:** {len(valid_reels)}")
        if skipped_old > 0: st.info(f"ℹ️ Filtered out {skipped_old} older posts.")
        
        return sorted(valid_reels, key=lambda x: x['views'], reverse=True)
        
    except Exception as e:
        st.error(f"Scout Error: {e}")
        return []

def transcribe_video(url, platform_type):
    try:
        if platform_type == "YouTube Shorts":
            run_input = {"videoUrl": url} 
            run = apify_client.actor(YT_TRANSCRIBER_ACTOR).call(run_input=run_input, timeout_secs=180)
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if not dataset_items: return "N/A"
            item = dataset_items[0]
            t = item.get("transcript")
            if isinstance(t, list): return " ".join([seg.get('text', '') for seg in t])
            return str(t) if t else "N/A"
            
        else:
            clean_url = url.split("?")[0] # Remove query params
            if "/p/" in clean_url: clean_url = clean_url.replace("/p/", "/reel/")
            
            run_input = {
                "instagramUrl": clean_url,
                "openaiApiKey": OPENAI_API_KEY_VAL,
                "task": "transcription",
                "model": "gpt-4o-mini-transcribe",
                "response_format": "json"
            }
            
            run = apify_client.actor(IG_TRANSCRIBER_ACTOR).call(run_input=run_input, timeout_secs=240)
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            
            if not dataset_items: return "N/A"
            
            item = dataset_items[0]
            
            if item.get("text"): return str(item["text"])
            if item.get("transcript"): return str(item["transcript"])
            
            def extract_text(data):
                if isinstance(data, dict):
                    for k, v in data.items():
                        if k in ["text", "transcript", "result"] and isinstance(v, str) and len(v) > 10: 
                            return v
                        res = extract_text(v)
                        if res: return res
                return None
                
            found_text = extract_text(item)
            return found_text if found_text else "N/A"

    except Exception as e:
        return "N/A"

# ================= THREADED PROCESSING =================

def process_single_video(video, platform_type, baseline):
    calc_log = []
    calc_log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    
    multiplier = video['views'] / baseline
    calc_log.append(f"   • 🧮 **Math:** {video['views']:,} views ÷ {baseline:,} baseline = **{multiplier:.2f}x**")
    
    # Try to transcribe
    transcript_data = transcribe_video(video['url'], platform_type)
    transcript = str(transcript_data)
    
    # Fallback Logic
    is_caption_fallback = False
    if not transcript or len(transcript) < 10 or "N/A" in transcript or "{" in transcript:
        caption = video.get("full_caption", "")
        if caption and len(str(caption)) > 10:
            transcript = str(caption)
            is_caption_fallback = True
            calc_log.append("   • ⚠️ **Warning:** Audio Transcript failed. Using Instagram Caption as fallback.")
        else:
            calc_log.append("   • ❌ **Status:** Transcription Failed & No Caption. Skipping.")
            return None, "\n".join(calc_log)
    else:
        calc_log.append(f"   • 📝 **Transcript:** Found ({len(transcript)} chars)")
    
    # --- AI ANALYSIS ---
    hook = extract_hook_with_ai(transcript)
    topic = generate_topic_with_ai(transcript)
    video_format = identify_format_with_ai(transcript) 
    
    calc_log.append(f"   • 🧠 **AI Analysis:** Topic: '{topic}' | Format: '{video_format}'")
    calc_log.append(f"   • ✅ **Success:** Row generated.")

    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript

    row = [topic, video_format, video['views'], f"{round(multiplier, 1)}x", video['published'], video['url'], hook, final_transcript[:40000]]
    
    return row, "\n".join(calc_log)

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    handle_label = "YouTube Handle"
    default_handle = "@BusyFunda"
else:
    st.title("📸 Instagram Viral Analyzer (Smart Edition)")
    handle_label = "Instagram Username"
    default_handle = "sanjay_nuthra"

col1, col2 = st.columns(2)
with col1:
    target_handle = st.text_input(handle_label, value=default_handle)
    sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

st.divider()
st.subheader("⚙️ Filter & Math Logic")
c1, c2, c3 = st.columns(3)

with c1:
    time_options = [
        "30 Days", "60 Days", "90 Days", "180 Days", "365 Days", 
        "1.5 Years", "2 Years", "Full History"
    ]
    selected_time = st.selectbox("Scan Last:", time_options, index=0)
    
    if selected_time == "Full History":
        days_ago = 10000 
    elif "Year" in selected_time:
        years = float(selected_time.split(" ")[0])
        days_ago = int(years * 365)
    else:
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

        with st.status("🔍 **Phase 1: Scouting Content...**", expanded=True) as status:
            
            if platform == "YouTube Shorts":
                channel_id = get_yt_channel_id(target_handle, YOUTUBE_API_KEY)
                if not channel_id: st.stop()
                videos = get_yt_shorts(channel_id, YOUTUBE_API_KEY, days_ago)
            else:
                videos = get_ig_reels(target_handle, days_ago)

            if not videos:
                status.update(label="❌ No videos found (Check permissions/username).", state="error")
                st.stop()

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
                
                with log_container:
                    with st.expander(f"Processed: {future_to_video[future]['title'][:50]}...", expanded=False):
                        st.markdown(log_text)

                if row:
                    results_to_save.append(row)
                
                completed += 1
                progress_bar.progress(completed / len(targets))

        if results_to_save:
            existing = sheet.get_all_values()
            if not existing:
                sheet.append_row(['Topic', 'Format', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript'])
            sheet.append_rows(results_to_save)
            st.success(f"🎉 Analysis Complete! Saved {len(results_to_save)} rows to Google Sheets.")
            
            st.dataframe(pd.DataFrame(results_to_save, columns=['Topic', 'Format', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript']))

    except Exception as e:
        st.error(f"Critical Error: {e}")