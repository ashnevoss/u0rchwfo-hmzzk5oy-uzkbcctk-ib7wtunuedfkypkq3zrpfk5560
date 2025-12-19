import streamlit as st
import pandas as pd
import requests
import gspread
from google.oauth2.service_account import Credentials # FIXED: Backend Auth
from apify_client import ApifyClient
from openai import OpenAI
import json
import time
from datetime import datetime, timedelta, timezone
import concurrent.futures
import re
import isodate # REQUIRED: pip install isodate

# ================= CONFIGURATION =================
# 1. TRANSCRIBERS (Using Apify Actors)
IG_TRANSCRIBER_ACTOR = "QDd59HBnZaQ89Rghe" 
YT_TRANSCRIBER_ACTOR = "f3uGksrII7QnHi8oD"

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
    """Fallback: Ensures the raw title is in English if transcript is missing."""
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
    """Generates a concise title based on the video content."""
    # UPDATED: More aggressive generation even for short transcripts
    if not transcript or len(transcript.strip()) < 2: return None
    if transcript.startswith("N/A"): return None
    
    preview_text = transcript[:2000] 
    
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
    """
    Master Hook Extractor.
    """
    if not transcript or len(transcript) < 5 or transcript.startswith("N/A"): 
        return "N/A"
    
    preview_text = transcript[:800]
    
    prompt = (
        "You are a Viral Script Engineer. Your job is to extract the 'Curiosity Setup' (The Hook) and strictly remove the 'Process' (The Recipe/Steps).\n\n"
        "PHASE 1: ANALYZE THE STRUCTURE\n"
        "Determine if the transcript is **Type A (Process)** or **Type B (Statement)**.\n\n"
        "--- TYPE A: THE 'PROCESS' HOOK ---\n"
        "(Starts with: 'Agar tum...', 'If you...', 'Kisi ne bataya...', 'Did you know...')\n"
        "* **The Logic:** These hooks follow a specific formula: [Intro] + [If You] + [Subject] + [Action 1] + [Connector].\n"
        "* **The Subject:** Identify the main object (e.g., 'Kaju', 'Mirchi', 'Paneer', 'Settings button').\n"
        "* **The Cut Point:** You must STOP immediately after the **First Action** and its **Connector**.\n"
        "* **The 'Ke Baad' Rule (Hindi):** If you see the phrase 'ke baad' (after), STOP exactly there.\n"
        "* **The 'To' Rule:** NEVER reach the word 'to' (then). If you see 'to', you have gone too far.\n\n"
        "**Type A Examples (Study the Cut Point):**\n"
        "1.  *Input:* 'Kisi ne tumhe bataya agar tum **toote hue kaju** ko **garam pani mein soak karne ke baad** paste bana loge...'\n"
        "    *Output:* 'Kisi ne tumhe bataya agar tum **toote hue kaju** ko **garam pani mein soak karne ke baad** (process...)'\n"
        "    *(Reason: Stopped exactly at 'ke baad'. Deleted 'paste bana loge'.)*\n\n"
        "2.  *Input:* 'Agar tum **badi wali mirchi** ka **stem katne ke baad** uske beej nikal loge...'\n"
        "    *Output:* 'Agar tum **badi wali mirchi** ka **stem katne ke baad** (process...)'\n"
        "    *(Reason: Stopped at 'ke baad'. Did not list step 2 'beej nikal loge'.)*\n\n"
        "3.  *Input:* 'Agar tum **iPhone settings** mein **privacy par click karoge** to tumhe ek secret button milega.'\n"
        "    *Output:* 'Agar tum **iPhone settings** mein **privacy par click karoge** (process...)'\n\n"
        "--- TYPE B: THE 'STATEMENT' HOOK ---\n"
        "(Starts with: A fact, a bold claim, or a question. No 'If' condition.)\n"
        "* **The Logic:** Extract the first 1-2 sentences verbatim.\n"
        "* **Example:** 'Stop using ChatGPT. Here is why.' -> Output: 'Stop using ChatGPT. Here is why.'\n\n"
        "--- YOUR OUTPUT FORMAT ---\n"
        "Return ONLY the extracted text followed by `(process...)` if it was Type A. Do not add labels.\n\n"
        f"Transcript:\n{preview_text}"
    )
    
    try:
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini", 
            messages=[{"role": "user", "content": prompt}], 
            temperature=0.0, 
            max_tokens=85    
        )
        hook_text = response.choices[0].message.content.strip().replace('"', '')

        if "ke baad" in hook_text:
            parts = hook_text.split("ke baad")
            hook_text = parts[0] + "ke baad (process...)"
        if "to tum" in hook_text:
            hook_text = hook_text.split("to tum")[0].strip() + " (process...)"
        if "toh tum" in hook_text:
             hook_text = hook_text.split("toh tum")[0].strip() + " (process...)"
        if "(process...)" in hook_text:
            hook_text = hook_text.replace("(process...)", "").strip() + " (process...)"

        return hook_text
    except Exception:
        return " ".join(transcript.split()[:10]) + "..."

# ================= DATA FETCHING FUNCTIONS =================

def is_actually_shorts(video_id):
    """The 'DNA Test': Checks if a video exists on the /shorts/ shelf."""
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
    """
    NEW: Parallel DNA Logic (Backend Only)
    """
    shorts = []
    stats = {"total_fetched": 0, "valid": 0, "skipped_low_views": 0}
    
    if days_ago > 3650: cutoff_date = datetime(2005, 1, 1, tzinfo=timezone.utc)
    else: cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)
    
    shorts_change_date = datetime(2024, 10, 15, tzinfo=timezone.utc)

    base_url = "https://youtube.googleapis.com/youtube/v3/playlistItems"
    params = {"key": api_key, "playlistId": playlist_id, "part": "snippet,contentDetails", "maxResults": 50}
    
    video_ids_batch = []
    next_page_token = None
    
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
                    # 1. View Check
                    views = int(v['statistics'].get('viewCount', 0))
                    if views < min_view_threshold:
                        stats['skipped_low_views'] += 1
                        continue 

                    # 2. Duration Check
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

                # 3. Parallel DNA Check
                if potential_candidates:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
                        future_to_vid = {executor.submit(is_actually_shorts, p['id']): p for p in potential_candidates}
                        for future in concurrent.futures.as_completed(future_to_vid):
                            if future.result(): 
                                shorts.append(future_to_vid[future])
            
            stats['total_fetched'] += len(items)
            if stop_scan: break
            
            next_page_token = data.get("nextPageToken")
            if not next_page_token: break
                
        except Exception as e:
            st.error(f"API Error: {e}")
            break
            
    stats['valid'] = len(shorts)
    return sorted(shorts, key=lambda x: x['views'], reverse=True), stats

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
    
    # --- FIXED: Define final_transcript HERE so AI can see it ---
    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript

    # --- AI ANALYSIS: HOOK & TITLE GENERATION ---
    final_title = "N/A"
    hook = "N/A"

    if final_transcript and not final_transcript.startswith("N/A"):
        # 1. Extract Hook (Using MASTER PROMPT)
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
    
    is_youtube = "Yes" if platform_type == "YouTube Shorts" else ""

    # --- CONSTRUCT ROW (UPDATED 9 COLUMNS) ---
    # Title, Link, Format, Subcategory, views, Hooks, Transcription, Made with Growingli, Is this youtube
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
    
    return row, "\n".join(calc_log), result_stats

# ================= MAIN UI =================

if platform == "YouTube Shorts":
    st.title("🎥 YouTube Viral Analyzer")
    st.info("Uses DNA Test + Smart Filters for 100% Accuracy.")
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
        time_options = ["7 Days", "14 Days", "30 Days", "60 Days", "90 Days", "180 Days", "All Time"]
        selected_time = st.selectbox("Scan Last:", time_options, index=2)
        
        if selected_time == "All Time":
            days_ago = 5000 # ~13 years
        else:
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
        # Auth Sheets (FIXED: Uses updated Gspread Auth to stop crash)
        json_creds = json.load(uploaded_google_key)
        scopes = ['https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_info(json_creds, scopes=scopes)
        client = gspread.authorize(creds)
        sheet = client.open(sheet_name).sheet1

        # --- PHASE 1: DATA LOADING & DUPLICATE CHECK ---
        status_box = st.status("🔍 **Phase 1: Loading Data...**", expanded=True)
        with status_box:
            
            st.write("🛡️ Checking Google Sheet for existing videos...")
            existing_data = sheet.get_all_values()
            
            # HEADERS DEFINITION (UPDATED 9 COLS)
            new_headers = ['Title', 'Link', 'Format', 'Subcategory', 'views', 'Hooks', 'Transcription', 'Made with Growingli', 'Is this youtube']
            
            if not existing_data:
                sheet.append_row(new_headers)
                existing_urls = set()
                st.write("   • Sheet is empty. Added headers.")
            else:
                existing_urls = set([row[1] for row in existing_data if len(row) > 1])
                st.write(f"   • Found {len(existing_urls)} videos already in sheet.")

            # 2. Fetch New Videos
            videos = []
            scrape_stats = {}
            
            if platform == "YouTube Shorts":
                # === ENGINE SWAP START (Parallel DNA) ===
                pid = get_yt_channel_upload_playlist(target_handle, YOUTUBE_API_KEY)
                if not pid: 
                    st.error("Channel Uploads Playlist not found.")
                    st.stop()
                
                videos, scrape_stats = get_yt_shorts_optimized(pid, YOUTUBE_API_KEY, days_ago, target_views)
                # === ENGINE SWAP END ===
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

            # 3. Filter Viral + Check Duplicates
            viral_candidates = []
            skipped_low_views = []
            duplicates_count = 0
            
            for v in videos:
                if v['views'] >= target_views:
                    if v['url'] in existing_urls:
                        duplicates_count += 1
                    else:
                        viral_candidates.append(v)
                else:
                    skipped_low_views.append({
                        "title": v['title'],
                        "views": v['views'],
                        "date": v.get('published', 'N/A'),
                        "shortfall": target_views - v['views']
                    })

            status_box.update(label=f"✅ Found {len(viral_candidates)} New Viral Hits ({duplicates_count} skipped as duplicates).", state="complete", expanded=False)

        # --- DATA DASHBOARD ---
        st.divider()
        st.subheader("📊 Data Inspector")
        
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Total Fetched", scrape_stats.get('total_fetched', len(videos)))
        m2.metric("Low Views (Skipped)", len(skipped_low_views) + scrape_stats.get('skipped_low_views', 0))
        m3.metric("Already in Sheet (Skipped)", duplicates_count)
        m4.metric("🔥 New to Process", len(viral_candidates))

        with st.expander("📉 View Rejected / Skipped Data (Click to Expand)"):
            if skipped_low_views:
                st.warning(f"These {len(skipped_low_views)} videos were skipped because they didn't hit {target_views:,} views.")
                st.dataframe(pd.DataFrame(skipped_low_views))
            else:
                st.success("No videos skipped due to low views.")

        if not viral_candidates:
            st.warning("⚠️ All viral videos found are already in your Google Sheet! Nothing new to process.")
            st.stop()

        # --- PHASE 2: PROCESSING & INCREMENTAL SAVE ---
        st.divider()
        st.subheader(f"📝 Processing {len(viral_candidates)} Videos (Saving Instantly)")
        
        # MOVED PROGRESS BAR UP (Above Logs)
        progress_bar = st.progress(0)
        
        enable_ai_audio = True

        log_container = st.container()
        proc_stats = {"audio_transcribed": 0, "caption_used": 0, "failed": 0, "saved": 0}
        
        # === SECURITY UPDATE: BATCH PROCESSING (5 VIDEOS AT A TIME) ===
        BATCH_SIZE = 5
        total_videos = len(viral_candidates)
        completed_count = 0
        
        for i in range(0, total_videos, BATCH_SIZE):
            batch = viral_candidates[i : i + BATCH_SIZE]
            
            with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
                future_to_video = {
                    executor.submit(process_single_video, vid, platform, manual_baseline, enable_ai_audio): vid 
                    for vid in batch
                }
                
                for future in concurrent.futures.as_completed(future_to_video):
                    try:
                        row, log_text, p_stat = future.result()
                        
                        if p_stat["transcribed"]: proc_stats["audio_transcribed"] += 1
                        if p_stat["caption_fallback"]: proc_stats["caption_used"] += 1
                        if p_stat["failed"]: proc_stats["failed"] += 1

                        with log_container:
                            with st.expander(f"Processed: {future_to_video[future]['title'][:50]}...", expanded=False):
                                st.markdown(log_text)

                        if row:
                            try:
                                sheet.append_row(row)
                                proc_stats["saved"] += 1
                                time.sleep(1.5) 
                            except Exception as save_error:
                                st.error(f"❌ Failed to save row to Sheets: {save_error}")
                        
                        completed_count += 1
                        progress_bar.progress(completed_count / total_videos)
                        
                    except Exception as e:
                        st.error(f"Error processing a video: {e}")

        # --- SUMMARY ---
        st.divider()
        st.success(f"🎉 Analysis Complete! {proc_stats['saved']} new rows saved to Google Sheets.")
        
        c_fin1, c_fin2 = st.columns(2)
        with c_fin1:
            st.caption("Processing Summary")
            st.write(f"🎙️ **Audio Transcribed:** {proc_stats['audio_transcribed']}")
            st.write(f"📝 **Caption Fallback:** {proc_stats['caption_used']}")
            st.write(f"💾 **Saved to Sheet:** {proc_stats['saved']}")
            st.write(f"❌ **Failed:** {proc_stats['failed']}")

    except Exception as e:
        st.error(f"Critical Error: {e}")