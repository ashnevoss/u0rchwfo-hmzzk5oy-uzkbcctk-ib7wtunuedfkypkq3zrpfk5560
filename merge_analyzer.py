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
import re

# ================= CONFIGURATION =================
# 1. TRANSCRIBERS (Using Apify Actors)
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe" 
YT_TRANSCRIBER_ACTOR = "bbqmsPr0r519A0ZaV"

# 2. LANGUAGE SETTINGS
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
    """
    Fallback: Ensures the raw title is in English if transcript is missing.
    """
    if not title or title == "N/A":
        return "Unknown Title"
    if all(ord(c) < 128 for c in title.replace(" ", "")):
        return title
    prompt = (
        f"Translate the following video title into English. \n"
        f"Return ONLY the English translation, no other text.\n\n"
        f"Original Title: {title}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role": "user", "content": prompt}], 
            temperature=0.1
        )
        return response.choices[0].message.content.strip().replace('"', '')
    except Exception:
        return title

def generate_title_from_transcript(transcript):
    """
    Generates a concise title based on the video content.
    """
    if not transcript or len(transcript) < 20 or "N/A" in transcript: 
        return None  # Return None so we can fallback to raw title
    
    preview_text = transcript[:1500] # Give AI enough context
    
    prompt = (
        "Read the following video transcript and generate a CONCISE English title (3-6 words).\n"
        "The title should summarize exactly what the video is about.\n"
        "Do not use clickbait. Just state the topic clearly.\n"
        "Return ONLY the title text.\n\n"
        f"Transcript:\n{preview_text}"
    )
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role": "user", "content": prompt}], 
            temperature=0.3 
        )
        title_text = response.choices[0].message.content.strip().replace('"', '')
        return title_text
    except Exception:
        return None

def extract_hook_with_ai(transcript):
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
        return "N/A"
    
    preview_text = transcript[:1000]
    prompt = (
        "I will provide a video transcript below.\n"
        "Identify the HOOK. The hook is the first 1 or 2 sentences that grab the viewer's attention.\n"
        "Instructions:\n"
        "1. Extract ONLY the hook text.\n"
        "2. Do not add any explanation or labels.\n"
        "3. Quote the text exactly from the transcript.\n\n"
        f"Transcript:\n{preview_text}"
    )
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role": "user", "content": prompt}], 
            temperature=0.2 
        )
        hook_text = response.choices[0].message.content.strip().replace('"', '')
        return hook_text
    except Exception:
        return transcript[:100] + "..."

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
    
    stats = {"total_fetched": 0, "date_filtered": 0, "valid": 0}
    
    url = "https://youtube.googleapis.com/youtube/v3/search"
    params = {
        "key": api_key, "channelId": channel_id, "part": "snippet,id",
        "order": "date", "publishedAfter": published_after,
        "videoDuration": "short", "maxResults": 50, "type": "video"
    }
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        items = data.get('items', [])
        stats['total_fetched'] = len(items)
        
        video_ids = [item['id']['videoId'] for item in items if 'videoId' in item['id']]
        
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
        
        stats['valid'] = len(shorts)
        return sorted(shorts, key=lambda x: x['views'], reverse=True), stats
    except Exception as e:
        st.error(f"YT API Error: {e}")
        return [], stats

def load_sortfeed_data(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        required_cols = ['Reel', 'Views']
        if not all(col in df.columns for col in required_cols):
             st.error(f"❌ CSV format error. Missing required columns (Reel, Views). Found: {df.columns.tolist()}")
             return []
        
        videos = []
        for index, row in df.iterrows():
            url = str(row['Reel']).strip()
            short_code = "Unknown"
            match = re.search(r'/reel/([^/]+)', url)
            if match:
                short_code = match.group(1)
            
            views = row['Views']
            if isinstance(views, str):
                 views = int(''.join(filter(str.isdigit, views))) if any(c.isdigit() for c in views) else 0
            
            videos.append({
                "title": f"Reel {short_code}", 
                "full_caption": "", 
                "views": int(views),
                "url": url,
                "published": "N/A",
                "id": short_code
            })
        
        st.success(f"✅ Successfully loaded {len(videos)} videos from Sortfeed CSV.")
        return sorted(videos, key=lambda x: x['views'], reverse=True)

    except Exception as e:
        st.error(f"Error parsing Sortfeed CSV: {e}")
        return []

def transcribe_video(url, platform_type):
    try:
        if platform_type == "YouTube Shorts":
            run_input = {
                "videoUrl": url,
                "language": YT_LANGUAGE_PREFERENCE,
                "shouldFetchSubtitles": True
            }
            run = apify_client.actor(YT_TRANSCRIBER_ACTOR).call(run_input=run_input, timeout_secs=180)
            
            if not run: return "N/A"
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if not dataset_items: return "N/A"
            item = dataset_items[0]
            if item.get("text"): return str(item["text"])
            t = item.get("transcript")
            if isinstance(t, list): 
                return " ".join([seg.get('text', '') for seg in t])
            return str(t) if t else "N/A"
            
        else:
            clean_url = url.split("?")[0].replace("/p/", "/reel/")
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
        return f"N/A (Error: {str(e)})"

# ================= THREADED PROCESSING =================

def process_single_video(video, platform_type, baseline, enable_transcription):
    result_stats = {"transcribed": False, "caption_fallback": False, "failed": False}
    calc_log = []
    
    calc_log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    
    transcript = "N/A"
    
    # --- TRANSCRIPTION LOGIC ---
    if enable_transcription:
        transcript_data = transcribe_video(video['url'], platform_type)
        transcript = str(transcript_data)
    else:
        calc_log.append("   • 💰 **Economy Mode:** Skipped AI Transcription.")

    is_caption_fallback = False
    if not transcript or len(transcript) < 10 or "N/A" in transcript:
        caption = video.get("full_caption", "")
        if caption and len(str(caption)) > 5:
            transcript = str(caption)
            is_caption_fallback = True
            result_stats["caption_fallback"] = True
            calc_log.append("   • 📝 **Source:** Using Caption.")
        else:
            if not enable_transcription:
                calc_log.append("   • ⚠️ **Warning:** No transcription available.")
            else:
                calc_log.append("   • ❌ **Status:** Transcription Failed.")
            transcript = "N/A (No Audio/Caption)"
    else:
        result_stats["transcribed"] = True
        calc_log.append(f"   • 🎙️ **Source:** AI Transcript ({len(transcript)} chars)")
    
    # --- AI ANALYSIS: HOOK & TITLE GENERATION ---
    final_title = "N/A"
    hook = "N/A"

    if len(transcript) > 20 and "N/A" not in transcript[:5]:
        # 1. Extract Hook
        hook = extract_hook_with_ai(transcript)
        
        # 2. Generate Smart Title from Transcript
        generated_title = generate_title_from_transcript(transcript)
        
        if generated_title:
            final_title = generated_title
            calc_log.append(f"   • 🧠 **AI Title:** {final_title}")
        else:
            # Fallback to translation if AI title fails
            final_title = translate_title_with_ai(video['title'])
            calc_log.append(f"   • ⚠️ **AI Title Failed:** Using Translated Raw Title")
    else:
        # Fallback if no transcript
        final_title = translate_title_with_ai(video['title'])
    
    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript
    
    # --- CONSTRUCT ROW ---
    row = [
        final_title,                # Title (AI Generated from Transcript)
        video['url'],               # Link
        "",                         # Format
        video['views'],             # Views
        hook,                       # Hook
        final_transcript[:40000],   # Transcription
        "",                         # Made with Growingly
        ""                          # Is this Youtube
    ]
    
    return row, "\n".join(calc_log), result_stats

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    st.info("Uses YouTube API (Free/Cheap) for discovery.")
    handle_label = "YouTube Handle"
    default_handle = "@BusyFunda"
    
    col1, col2 = st.columns(2)
    with col1:
        target_handle = st.text_input(handle_label, value=default_handle)
    with col2:
        sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

else:
    st.title("📸 Instagram Viral Analyzer (Sortfeed Edition)")
    st.info("Upload your Sortfeed CSV export below to save on scraping costs.")
    
    col1, col2 = st.columns(2)
    with col1:
        uploaded_sortfeed = st.file_uploader("Upload Sortfeed CSV", type=["csv"])
    with col2:
        sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

st.divider()
st.subheader("⚙️ Filter & Math Logic")
c1, c2, c3 = st.columns(3)

with c1:
    if platform == "YouTube Shorts":
        time_options = ["7 Days", "14 Days", "30 Days", "60 Days", "90 Days", "180 Days"]
        selected_time = st.selectbox("Scan Last:", time_options, index=2)
        days_ago = int(selected_time.split(" ")[0])
    else:
        st.text_input("Scan Last:", value="Data from CSV", disabled=True, help="Date filtering is disabled because we are using uploaded CSV data.")
        days_ago = 9999 

with c2:
    manual_baseline = st.number_input("Baseline Views (Avg):", min_value=1000, value=10000, step=1000)
with c3:
    viral_multiplier = st.slider("Viral Multiplier:", 2, 50, 3)

target_views = manual_baseline * viral_multiplier

st.info(f"📊 **Viral Formula:** Videos > **{target_views:,} Views** (Baseline {manual_baseline} × {viral_multiplier})")

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

        # --- PHASE 1: DATA LOADING ---
        status_box = st.status("🔍 **Phase 1: Loading Data...**", expanded=True)
        with status_box:
            
            videos = []
            scrape_stats = {}
            
            if platform == "YouTube Shorts":
                channel_id = get_yt_channel_id(target_handle, YOUTUBE_API_KEY)
                if not channel_id: st.stop()
                videos, scrape_stats = get_yt_shorts(channel_id, YOUTUBE_API_KEY, days_ago)
            else:
                if not uploaded_sortfeed:
                    st.error("Please upload a Sortfeed CSV file.")
                    st.stop()
                
                uploaded_sortfeed.seek(0)
                videos = load_sortfeed_data(uploaded_sortfeed)
                scrape_stats = {"raw_fetched": len(videos)}

            if not videos:
                status_box.update(label="❌ No videos found.", state="error")
                st.stop()

            targets = []
            skipped_low_views = []
            
            for v in videos:
                if v['views'] >= target_views:
                    targets.append(v)
                else:
                    skipped_low_views.append({
                        "title": v['title'],
                        "views": v['views'],
                        "date": v['published'],
                        "shortfall": target_views - v['views']
                    })

            status_box.update(label=f"✅ Found {len(targets)} Viral Hits. Proceeding...", state="complete", expanded=False)

        # --- DATA DASHBOARD ---
        st.divider()
        st.subheader("📊 Data Inspector")
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Loaded", len(videos))
        m2.metric("Filtered (Low Views)", len(skipped_low_views))
        m3.metric("Ignored (Date)", "N/A" if platform == "Instagram Reels" else scrape_stats.get("date_filtered", 0))
        m4.metric("🔥 Viral Hits", len(targets))

        with st.expander("📉 View Rejected / Skipped Data (Click to Expand)"):
            if skipped_low_views:
                st.warning(f"These {len(skipped_low_views)} videos were skipped because they didn't hit {target_views:,} views.")
                st.dataframe(pd.DataFrame(skipped_low_views))
            else:
                st.success("No videos skipped due to low views.")

        if not targets:
            st.error("❌ No videos met your viral criteria. Try lowering the Multiplier or Baseline.")
            st.stop()

        # --- PHASE 2: PROCESSING ---
        st.divider()
        st.subheader(f"📝 Processing {len(targets)} Viral Videos")
        
        enable_ai_audio = st.checkbox("🎙️ Enable AI Audio Transcription", value=True, help="Uncheck to save money and use captions only. Check to use OpenAI transcription.")

        log_container = st.container()
        results_to_save = []
        
        proc_stats = {"audio_transcribed": 0, "caption_used": 0, "failed": 0}
        
        progress_bar = st.progress(0)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_video = {
                executor.submit(process_single_video, vid, platform, manual_baseline, enable_ai_audio): vid 
                for vid in targets
            }
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_video):
                row, log_text, p_stat = future.result()
                
                if p_stat["transcribed"]: proc_stats["audio_transcribed"] += 1
                if p_stat["caption_fallback"]: proc_stats["caption_used"] += 1
                if p_stat["failed"]: proc_stats["failed"] += 1

                with log_container:
                    with st.expander(f"Processed: {future_to_video[future]['title'][:50]}...", expanded=False):
                        st.markdown(log_text)

                if row:
                    results_to_save.append(row)
                
                completed += 1
                progress_bar.progress(completed / len(targets))

        # --- SUMMARY & SAVE ---
        st.divider()
        c_fin1, c_fin2 = st.columns(2)
        with c_fin1:
            st.caption("Processing Summary")
            st.write(f"🎙️ **Audio Transcribed:** {proc_stats['audio_transcribed']}")
            st.write(f"📝 **Caption Fallback:** {proc_stats['caption_used']}")
            st.write(f"❌ **Failed:** {proc_stats['failed']}")

        if results_to_save:
            existing = sheet.get_all_values()
            
            new_headers = ['Title', 'Link', 'Format', 'Views', 'Hook', 'Transcription', 'Made with Growingly', 'Is this Youtube']
            
            if not existing:
                sheet.append_row(new_headers)
            
            sheet.append_rows(results_to_save)
            st.success(f"🎉 Analysis Complete! Saved {len(results_to_save)} rows to Google Sheets.")
            
            with st.expander("📄 View Final Data"):
                st.dataframe(pd.DataFrame(results_to_save, columns=new_headers))

    except Exception as e:
        st.error(f"Critical Error: {e}")