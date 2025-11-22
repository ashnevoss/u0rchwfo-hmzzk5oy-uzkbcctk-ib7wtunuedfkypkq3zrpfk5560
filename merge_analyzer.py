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
# 1. SCOUT: Official Apify Instagram Scraper
IG_SCOUT_ACTOR = "apify/instagram-scraper"

# 2. TRANSCRIBERS: 
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe" 
YT_TRANSCRIBER_ACTOR = "akash9078/youtube-transcript-extractor"
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
    """
    Classifies the video into one of the user's specific formats.
    """
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
        return "Unknown"
        
    preview_text = transcript[:2000] # Analyze slightly more text for context
    
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

def get_ig_reels(username, days_ago):
    username = username.strip().replace("@", "")
    profile_url = f"https://www.instagram.com/{username}/"

    st.info(f"🕵️ DEBUG: Scouting via Apify Official Scraper for '{username}'...")
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    
    run_input = {
        "directUrls": [profile_url],
        "resultsType": "posts", 
        "resultsLimit": 100 if days_ago < 60 else 200,
        "searchType": "hashtag", 
        "searchLimit": 1,
    }
    
    try:
        run = apify_client.actor(IG_SCOUT_ACTOR).call(run_input=run_input)
        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        
        st.write(f"📦 **Raw Items Scouted:** {len(dataset_items)}")
        
        valid_reels = []
        skipped_count = 0
        
        for item in dataset_items:
            item_type = item.get("type", "Unknown")
            if item_type not in ["Video", "Reel", "IGTV"]:
                if not item.get("videoPlayCount") and not item.get("videoViewCount"):
                    skipped_count += 1
                    continue

            ts_str = item.get("timestamp")
            reel_date = None
            if ts_str:
                try:
                    reel_date = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                except ValueError:
                    try:
                        reel_date = datetime.strptime(ts_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    except:
                        pass
            
            if not reel_date or reel_date < cutoff_date:
                skipped_count += 1
                continue

            plays = item.get("videoPlayCount")
            if plays is None: plays = item.get("videoViewCount")
            if plays is None: plays = item.get("playCount")
            if plays is None: plays = item.get("viewCount")
            if plays is None: plays = 0
            
            short_code = item.get("shortCode") or item.get("code")
            post_url = item.get("url")
            
            if not post_url and short_code:
                post_url = f"https://www.instagram.com/reel/{short_code}/"
            elif post_url and "/p/" in post_url:
                 post_url = post_url.replace("/p/", "/reel/")
            
            if not post_url:
                continue

            valid_reels.append({
                "title": (item.get("caption") or "")[:50] + "...",
                "full_caption": item.get("caption") or "",
                "views": int(plays),
                "url": post_url,
                "published": reel_date.strftime('%Y-%m-%d'),
                "id": item.get("id") or short_code
            })
            
        st.write(f"✅ **Valid Reels Found:** {len(valid_reels)} (Skipped {skipped_count})")
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
            clean_url = url.replace("/p/", "/reel/")
            
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
            if item.get("transcription"): return str(item["transcription"])
            if item.get("result"): return str(item["result"])
            
            for key, value in item.items():
                if isinstance(value, str) and len(value) > 50:
                    return value
                if isinstance(value, dict):
                    for sub_key, sub_value in value.items():
                        if isinstance(sub_value, str) and len(sub_value) > 50:
                            return sub_value
            return str(item)

    except Exception as e:
        print(f"Transcribe Error: {e}")
        return "N/A"

# ================= THREADED PROCESSING =================

def process_single_video(video, platform_type, baseline):
    calc_log = []
    calc_log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    
    multiplier = video['views'] / baseline
    calc_log.append(f"   • 🧮 **Math:** {video['views']:,} views ÷ {baseline:,} baseline = **{multiplier:.2f}x**")
    
    # Try to transcribe
    transcript_data = transcribe_video(video['url'], platform_type)
    
    # Safety Force String
    if isinstance(transcript_data, dict):
        transcript = str(transcript_data)
    else:
        transcript = str(transcript_data)
    
    # Fallback Logic
    is_caption_fallback = False
    if not transcript or len(transcript) < 5 or transcript == "N/A":
        caption = video.get("full_caption", "")
        if caption and len(str(caption)) > 5:
            transcript = str(caption)
            is_caption_fallback = True
            calc_log.append("   • ⚠️ **Warning:** Audio Transcript failed. Using Instagram Caption as fallback.")
        else:
            calc_log.append("   • ❌ **Status:** Transcription Failed & No Caption. Skipping.")
            return None, "\n".join(calc_log)
    else:
        calc_log.append(f"   • 📝 **Transcript:** Found ({len(transcript)} chars)")
    
    # --- AI ANALYSIS (Updated with Format) ---
    hook = extract_hook_with_ai(transcript)
    topic = generate_topic_with_ai(transcript)
    video_format = identify_format_with_ai(transcript) # <--- NEW FUNCTION CALLED HERE
    
    calc_log.append(f"   • 🧠 **AI Analysis:** Topic: '{topic}' | Format: '{video_format}'")
    calc_log.append(f"   • ✅ **Success:** Row generated.")

    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript

    # Updated Row Structure: Topic, Format, Views...
    row = [topic, video_format, video['views'], f"{round(multiplier, 1)}x", video['published'], video['url'], hook, final_transcript[:40000]]
    
    return row, "\n".join(calc_log)

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    handle_label = "YouTube Handle"
    default_handle = "@BusyFunda"
else:
    st.title("📸 Instagram Viral Analyzer (Format Edition)")
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
                status.update(label="❌ No videos found.", state="error")
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
            # Updated Headers with 'Format'
            if not existing:
                sheet.append_row(['Topic', 'Format', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript'])
            sheet.append_rows(results_to_save)
            st.success(f"🎉 Analysis Complete! Saved {len(results_to_save)} rows to Google Sheets.")
            
            # Display DataFrame with new column
            st.dataframe(pd.DataFrame(results_to_save, columns=['Topic', 'Format', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript']))

    except Exception as e:
        st.error(f"Critical Error: {e}")