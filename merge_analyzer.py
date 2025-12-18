import streamlit as st
import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials
from apify_client import ApifyClient
from openai import OpenAI
import json
import time
from datetime import datetime, timedelta, timezone
import concurrent.futures
import re
import isodate 

# ================= CONFIGURATION =================
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe" 
YT_TRANSCRIBER_ACTOR = "bbqmsPr0r519A0ZaV"
YT_LANGUAGE_PREFERENCE = "en" 
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

def translate_title_with_ai(title):
    if not title or title == "N/A": return "Unknown Title"
    if all(ord(c) < 128 for c in title.replace(" ", "")): return title
    prompt = f"Translate into English. Return ONLY the English text.\n\nOriginal: {title}"
    try:
        response = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.1)
        return response.choices[0].message.content.strip().replace('"', '')
    except: return title

def generate_title_from_transcript(transcript):
    """
    FORCED AI TITLE GENERATION
    """
    # Relaxed checks: Only fail if truly empty or explicit error string
    if not transcript or len(transcript.strip()) < 2: return None, "Empty Transcript"
    if transcript.startswith("N/A"): return None, "No Transcript Found"

    preview_text = transcript[:2000]
    prompt = (
        "Read the transcript below and write a VIRAL YouTube Short Title in english (3-6 words).\n"
        "It must describe the content exactly. Do not use generic words.\n"
        "Return ONLY the title text.\n\n"
        f"Transcript:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.4)
        title = response.choices[0].message.content.strip().replace('"', '')
        return title, None # Success
    except Exception as e:
        return None, str(e) # Return error for logging

def extract_hook_with_ai(transcript):
    if not transcript or len(transcript) < 5 or transcript.startswith("N/A"): return "N/A"
    preview_text = transcript[:800]
    prompt = (
        "You are a Viral Script Engineer. Extract the 'Curiosity Setup' (Hook) and remove the 'Process'.\n"
        "If it starts with 'If you/Agar tum' (Process Hook): STOP immediately after the first action/connector (e.g. 'ke baad').\n"
        "If it is a Statement/Fact: Extract first 2 sentences.\n"
        "Return ONLY the extracted text followed by `(process...)` if Type A.\n\n"
        f"Transcript:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(model="gpt-4o-mini", messages=[{"role": "user", "content": prompt}], temperature=0.0, max_tokens=85)
        hook_text = response.choices[0].message.content.strip().replace('"', '')
        if "ke baad" in hook_text: hook_text = hook_text.split("ke baad")[0] + "ke baad (process...)"
        if "to tum" in hook_text: hook_text = hook_text.split("to tum")[0].strip() + " (process...)"
        if "(process...)" in hook_text: hook_text = hook_text.replace("(process...)", "").strip() + " (process...)"
        return hook_text
    except: return " ".join(transcript.split()[:10]) + "..."

# ================= DATA FETCHING (OPTIMIZED) =================

def is_actually_shorts(video_id):
    try:
        r = requests.head(f"https://www.youtube.com/shorts/{video_id}", allow_redirects=False, timeout=3)
        return r.status_code == 200
    except: return False

def get_yt_channel_upload_playlist(handle, api_key):
    url = "https://youtube.googleapis.com/youtube/v3/channels"
    if not handle.startswith("@"): handle = "@" + handle
    params = {"key": api_key, "forHandle": handle, "part": "contentDetails"}
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if "items" in data: return data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except: pass
    return None

def get_yt_shorts_optimized(playlist_id, api_key, days_ago, min_view_threshold):
    shorts = []
    stats = {"total_scanned": 0, "valid_viral": 0, "skipped_low_views": 0}
    
    if days_ago > 3650: cutoff_date = datetime(2005, 1, 1, tzinfo=timezone.utc)
    else: cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    shorts_change_date = datetime(2024, 10, 15, tzinfo=timezone.utc)

    base_url = "https://youtube.googleapis.com/youtube/v3/playlistItems"
    params = {"key": api_key, "playlistId": playlist_id, "part": "snippet,contentDetails", "maxResults": 50}
    
    video_ids_batch = []
    next_page_token = None
    
    st.write("⚡ **Fast-Scanning Channel History...**")
    progress_bar = st.progress(0)
    
    while True:
        if next_page_token: params["pageToken"] = next_page_token
            
        try:
            resp = requests.get(base_url, params=params)
            data = resp.json()
            items = data.get('items', [])
            if not items: break
                
            current_batch_ids = []
            stop_scan = False
            for item in items:
                vid_date = datetime.strptime(item['snippet']['publishedAt'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                if days_ago < 3650 and vid_date < cutoff_date:
                    stop_scan = True
                    break
                current_batch_ids.append(item['contentDetails']['videoId'])

            if current_batch_ids:
                d_url = "https://youtube.googleapis.com/youtube/v3/videos"
                d_params = {"key": api_key, "id": ",".join(current_batch_ids), "part": "contentDetails,statistics,snippet"}
                d_resp = requests.get(d_url, params=d_params)
                d_items = d_resp.json().get('items', [])
                
                potential_candidates = []
                
                for v in d_items:
                    views = int(v['statistics'].get('viewCount', 0))
                    if views < min_view_threshold:
                        stats['skipped_low_views'] += 1
                        continue 

                    try:
                        seconds = isodate.parse_duration(v['contentDetails']['duration']).total_seconds()
                    except: continue
                    
                    vid_date = datetime.strptime(v['snippet']['publishedAt'], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    limit = 181 if vid_date >= shorts_change_date else 61
                    if seconds > limit: continue 

                    potential_candidates.append({
                        "title": v['snippet']['title'],
                        "views": views,
                        "url": f"https://www.youtube.com/watch?v={v['id']}",
                        "id": v['id'],
                        "published": v['snippet']['publishedAt'][:10]
                    })

                if potential_candidates:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        future_to_vid = {executor.submit(is_actually_shorts, p['id']): p for p in potential_candidates}
                        for future in concurrent.futures.as_completed(future_to_vid):
                            if future.result(): 
                                shorts.append(future_to_vid[future])
            
            stats['total_scanned'] += len(items)
            if stop_scan: break
            
            next_page_token = data.get("nextPageToken")
            if not next_page_token: break
                
        except Exception as e:
            st.error(f"API Error: {e}")
            break
            
    stats['valid_viral'] = len(shorts)
    progress_bar.empty()
    return sorted(shorts, key=lambda x: x['views'], reverse=True), stats

def load_sortfeed_data(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        videos = []
        for index, row in df.iterrows():
            url = str(row['Reel']).strip()
            views = int(''.join(filter(str.isdigit, str(row['Views'])))) if any(c.isdigit() for c in str(row['Views'])) else 0
            videos.append({"title": f"Reel", "full_caption": "", "views": views, "url": url})
        return sorted(videos, key=lambda x: x['views'], reverse=True)
    except Exception as e:
        st.error(f"CSV Error: {e}"); return []

def transcribe_video(url, platform_type):
    try:
        if platform_type == "YouTube Shorts":
            run = apify_client.actor(YT_TRANSCRIBER_ACTOR).call(run_input={"videoUrl": url, "language": YT_LANGUAGE_PREFERENCE, "shouldFetchSubtitles": True}, timeout_secs=180)
            if not run: return "N/A"
            item = apify_client.dataset(run["defaultDatasetId"]).list_items().items[0]
            if item.get("text"): return str(item["text"])
            t = item.get("transcript")
            return " ".join([seg.get('text', '') for seg in t]) if isinstance(t, list) else str(t)
        else:
            clean_url = url.split("?")[0].replace("/p/", "/reel/")
            run = apify_client.actor(IG_TRANSCRIBER_ACTOR).call(run_input={"instagramUrl": clean_url, "openaiApiKey": OPENAI_API_KEY_VAL, "task": "transcription", "model": "gpt-4o-mini-transcribe"}, timeout_secs=240)
            item = apify_client.dataset(run["defaultDatasetId"]).list_items().items[0]
            if item.get("text"): return str(item["text"])
            return str(item.get("transcript", "N/A"))
    except: return "N/A"

# ================= THREADED PROCESSING =================

def process_single_video(video, platform_type):
    log = []
    log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    
    # 1. Transcribe
    transcript = transcribe_video(video['url'], platform_type)
    final_transcript = transcript if len(transcript) > 5 else "N/A"
    
    if final_transcript == "N/A":
        log.append("   • ⚠️ **Audio:** No transcript found.")
    else:
        log.append(f"   • 🎙️ **Audio:** Transcribed ({len(final_transcript)} chars)")
    
    # 2. AI Analysis
    hook, final_title = "N/A", "N/A"
    
    # Force attempt to use transcript even if it looks weird, unless it's strictly "N/A"
    if final_transcript and not final_transcript.startswith("N/A"):
        # A. Hook
        hook = extract_hook_with_ai(final_transcript)
        
        # B. Title (STRICT PRIORITY: Generate from Transcript)
        generated_title, error_msg = generate_title_from_transcript(final_transcript)
        
        if generated_title:
            final_title = generated_title
            log.append(f"   • 🧠 **AI Title:** {final_title}")
        else:
            final_title = translate_title_with_ai(video['title'])
            if error_msg:
                log.append(f"   • ⚠️ **AI Title Error:** {error_msg}")
            else:
                log.append(f"   • ⚠️ **AI Title Failed:** Fallback to Translated Title")
    else:
        final_title = translate_title_with_ai(video['title'])
        log.append(f"   • ⚠️ **Mode:** Fallback to Raw Title (No Audio)")
    
    is_youtube = "Yes" if platform_type == "YouTube Shorts" else ""

    # 3. Format Row (9 Columns)
    row = [
        final_title,                # 1. Title
        video['url'],               # 2. Link
        "",                         # 3. Format
        "",                         # 4. Subcategory
        video['views'],             # 5. Views
        hook,                       # 6. Hooks
        final_transcript[:40000],   # 7. Transcription
        "",                         # 8. Made with Growingli
        is_youtube                  # 9. Is this youtube
    ]
    return row, "\n".join(log)

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("⚡ YouTube Viral Analyzer")
    st.caption("v4.5: High Speed + Smart Filtering + Forced AI Titles")
    col1, col2 = st.columns(2)
    with col1: target_handle = st.text_input("YouTube Handle", value="@BusyFunda")
    with col2: sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")
else:
    st.title("📸 Instagram Analyzer")
    col1, col2 = st.columns(2)
    with col1: uploaded_sortfeed = st.file_uploader("Upload CSV", type=["csv"])
    with col2: sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

st.divider()
c1, c2, c3 = st.columns(3)
with c1:
    if platform == "YouTube Shorts":
        time_options = ["7 Days", "30 Days", "90 Days", "All Time"]
        selected_time = st.selectbox("Scan Last:", time_options, index=1)
        days_ago = 5000 if selected_time == "All Time" else int(selected_time.split(" ")[0])
    else: days_ago = 9999
with c2: manual_baseline = st.number_input("Baseline Views:", min_value=1000, value=10000)
with c3: viral_multiplier = st.slider("Viral Multiplier:", 2, 50, 3)

target_views = manual_baseline * viral_multiplier
st.info(f"📊 **Target:** > {target_views:,} Views")

if st.button("🚀 Start Fast Analysis", type="primary"):
    if not valid_api_config: st.error("❌ Missing Keys"); st.stop()
    
    try:
        # Auth Sheets
        scopes = [
            'https://www.googleapis.com/auth/spreadsheets',
            'https://www.googleapis.com/auth/drive'
        ]
        creds = Credentials.from_service_account_info(json.load(uploaded_google_key), scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(sheet_name).sheet1
        
        # Check Existing
        existing_data = sheet.get_all_values()
        if not existing_data:
             sheet.append_row(['Title', 'Link', 'Format', 'Subcategory', 'views', 'Hooks', 'Transcription', 'Made with Growingli', 'Is this youtube'])
             existing_urls = set()
        else:
             existing_urls = set([row[1] for row in existing_data if len(row) > 1])
        
        # Scrape
        status = st.status("🔍 **Phase 1: Fast Scanning (Views First)...**", expanded=True)
        videos, stats = [], {}
        
        if platform == "YouTube Shorts":
            pid = get_yt_channel_upload_playlist(target_handle, YOUTUBE_API_KEY)
            if not pid: st.error("Channel not found."); st.stop()
            # We pass target_views to filter early!
            videos, stats = get_yt_shorts_optimized(pid, YOUTUBE_API_KEY, days_ago, target_views)
        else:
            if not uploaded_sortfeed: st.error("No CSV."); st.stop()
            uploaded_sortfeed.seek(0)
            videos = load_sortfeed_data(uploaded_sortfeed)
            videos = [v for v in videos if v['views'] >= target_views]

        # Filter Duplicates
        final_list = [v for v in videos if v['url'] not in existing_urls]
        
        status.update(label=f"✅ Found {len(final_list)} New Viral Videos (Skipped {stats.get('skipped_low_views', 0)} low views).", state="complete")
        
        if not final_list: st.warning("No new viral videos found."); st.stop()

        # Process
        st.divider()
        st.subheader("Phase 2: Deep Analysis & Saving")
        progress = st.progress(0)
        log_box = st.container()
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = {executor.submit(process_single_video, v, platform): v for v in final_list}
            for i, f in enumerate(concurrent.futures.as_completed(futures)):
                try:
                    row, log_text = f.result()
                    sheet.append_row(row)
                    time.sleep(1.0) # Rate limit safety
                    
                    with log_box: 
                        with st.expander(f"✅ Saved: {row[0][:50]}...", expanded=False): 
                            st.markdown(log_text)
                            
                except Exception as e:
                    st.error(f"Error saving row: {e}")
                progress.progress((i+1)/len(final_list))
                
        st.success("🎉 Done!")

    except Exception as e: st.error(f"Critical Error: {e}")