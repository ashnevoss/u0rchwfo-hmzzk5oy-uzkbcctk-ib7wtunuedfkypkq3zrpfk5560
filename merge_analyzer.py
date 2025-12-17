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
import isodate  # Added for parsing YouTube duration

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
    if not transcript or len(transcript) < 20 or "N/A" in transcript: 
        return None 
    
    preview_text = transcript[:1500]
    
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
    """Master Hook Extractor."""
    if not transcript or len(transcript) < 5 or transcript == "N/A": 
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

        # --- PYTHON SAFETY GUILLOTINE ---
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
    """
    The 'DNA Test': Checks if a video exists on the /shorts/ shelf.
    This filters out short landscape videos.
    """
    url = f"https://www.youtube.com/shorts/{video_id}"
    try:
        # We use HEAD to save bandwidth. allow_redirects=False is key.
        # If it's a short, it stays 200. If not, it redirects 303 to /watch.
        r = requests.head(url, allow_redirects=False, timeout=5)
        if r.status_code == 200:
            return True
        return False
    except:
        # If network error, assume False to be safe
        return False

def get_yt_channel_upload_playlist(handle, api_key):
    url = "https://youtube.googleapis.com/youtube/v3/channels"
    if not handle.startswith("@"): handle = "@" + handle
    params = {"key": api_key, "forHandle": handle, "part": "contentDetails"}
    try:
        resp = requests.get(url, params=params)
        data = resp.json()
        if "items" in data and data["items"]:
            return data["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    except Exception as e:
        st.error(f"Error fetching channel details: {e}")
    return None

def get_yt_shorts(playlist_id, api_key, days_ago):
    """
    Fetches videos from Uploads playlist.
    FILTERS: 
    1. Duration (Smart Limit based on Date)
    2. Shorts Shelf Check (The DNA Test)
    """
    shorts = []
    stats = {"total_fetched": 0, "valid": 0}
    
    # Calculate cutoff date
    if days_ago > 3650:
        cutoff_date = datetime(2005, 1, 1, tzinfo=timezone.utc)
    else:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days_ago)

    base_url = "https://youtube.googleapis.com/youtube/v3/playlistItems"
    params = {
        "key": api_key,
        "playlistId": playlist_id,
        "part": "snippet,contentDetails",
        "maxResults": 50
    }
    
    video_ids_batch = []
    next_page_token = None
    
    st.write("🔄 Scanning Channel History...")
    progress_bar = st.progress(0)
    
    shorts_change_date = datetime(2024, 10, 15, tzinfo=timezone.utc)

    while True:
        if next_page_token:
            params["pageToken"] = next_page_token
            
        try:
            resp = requests.get(base_url, params=params)
            data = resp.json()
            items = data.get('items', [])
            
            if not items: break
                
            current_batch_ids = []
            for item in items:
                vid_date_str = item['snippet']['publishedAt']
                vid_date = datetime.strptime(vid_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                
                if days_ago < 3650 and vid_date < cutoff_date:
                    next_page_token = None
                    break
                
                current_batch_ids.append(item['contentDetails']['videoId'])

            video_ids_batch.extend(current_batch_ids)
            
            # Process batch
            if len(video_ids_batch) >= 50 or not data.get("nextPageToken") or (days_ago < 3650 and vid_date < cutoff_date):
                
                details_url = "https://youtube.googleapis.com/youtube/v3/videos"
                details_params = {
                    "key": api_key,
                    "id": ",".join(video_ids_batch),
                    "part": "contentDetails,statistics,snippet"
                }
                d_resp = requests.get(details_url, params=details_params)
                d_items = d_resp.json().get('items', [])
                
                # --- VALIDATION LOOP ---
                for v in d_items:
                    # 1. Parse Duration
                    duration_str = v['contentDetails']['duration']
                    try:
                        duration_obj = isodate.parse_duration(duration_str)
                        seconds = duration_obj.total_seconds()
                    except:
                        continue 
                    
                    # 2. Smart Duration Check (Date aware)
                    vid_date_str = v['snippet']['publishedAt']
                    vid_date = datetime.strptime(vid_date_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    
                    limit = 181 if vid_date >= shorts_change_date else 61
                    if seconds > limit:
                         continue # Too long

                    # 3. THE DNA TEST (Check if it's actually a Short)
                    # We only do this check if it passes duration, to save requests
                    if not is_actually_shorts(v['id']):
                        continue # It was a short landscape video!

                    # 4. Add to list
                    views = int(v['statistics'].get('viewCount', 0))
                    shorts.append({
                        "title": v['snippet']['title'],
                        "views": views,
                        "url": f"https://www.youtube.com/watch?v={v['id']}",
                        "published": v['snippet']['publishedAt'][:10],
                        "id": v['id']
                    })
                
                video_ids_batch = [] 

            stats['total_fetched'] += len(items)
            
            next_page_token = data.get("nextPageToken")
            if not next_page_token:
                break
                
        except Exception as e:
            st.error(f"API Error: {e}")
            break
            
    stats['valid'] = len(shorts)
    progress_bar.empty()
    return sorted(shorts, key=lambda x: x['views'], reverse=True), stats

def load_sortfeed_data(uploaded_file):
    try:
        df = pd.read_csv(uploaded_file)
        required_cols = ['Reel', 'Views']
        if not all(col in df.columns for col in required_cols):
             st.error(f"❌ CSV format error. Missing required columns (Reel, Views).")
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
        hook = extract_hook_with_ai(transcript)
        generated_title = generate_title_from_transcript(transcript)
        
        if generated_title:
            final_title = generated_title
            calc_log.append(f"   • 🧠 **AI Title:** {final_title}")
        else:
            final_title = translate_title_with_ai(video['title'])
            calc_log.append(f"   • ⚠️ **AI Title Failed:** Using Translated Raw Title")
    else:
        final_title = translate_title_with_ai(video['title'])
    
    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript
    
    is_youtube = "Yes" if platform_type == "YouTube Shorts" else ""

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
    st.info("Uses DNA Test to ensure 100% Shorts Accuracy.")
    handle_label = "YouTube Handle"
    default_handle = "@BusyFunda"
    
    col1, col2 = st.columns(2)
    with col1:
        target_handle = st.text_input(handle_label, value=default_handle)
    with col2:
        sheet_name = st.text_input("Google Sheet Name", value="ProjectO1")

else:
    st.title("📸 Instagram Viral Analyzer")
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
            days_ago = 5000 
        else:
            days_ago = int(selected_time.split(" ")[0])
    else:
        st.text_input("Scan Last:", value="Data from CSV", disabled=True)
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

        # --- PHASE 1: DATA LOADING & DUPLICATE CHECK ---
        status_box = st.status("🔍 **Phase 1: Loading Data...**", expanded=True)
        with status_box:
            
            st.write("🛡️ Checking Google Sheet for existing videos...")
            existing_data = sheet.get_all_values()
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
            if platform == "YouTube Shorts":
                playlist_id = get_yt_channel_upload_playlist(target_handle, YOUTUBE_API_KEY)
                if not playlist_id: 
                    st.error("Could not find uploads playlist.")
                    st.stop()
                videos, _ = get_yt_shorts(playlist_id, YOUTUBE_API_KEY, days_ago)
            else:
                if not uploaded_sortfeed:
                    st.error("Please upload a Sortfeed CSV file.")
                    st.stop()
                uploaded_sortfeed.seek(0)
                videos = load_sortfeed_data(uploaded_sortfeed)

            if not videos:
                status_box.update(label="❌ No videos found.", state="error")
                st.stop()

            # 3. Filter Viral + Check Duplicates
            viral_candidates = []
            duplicates_count = 0
            
            for v in videos:
                if v['views'] >= target_views:
                    if v['url'] in existing_urls:
                        duplicates_count += 1
                    else:
                        viral_candidates.append(v)
            
            status_box.update(label=f"✅ Found {len(viral_candidates)} New Viral Hits ({duplicates_count} skipped as duplicates).", state="complete", expanded=False)

        # --- PHASE 2: PROCESSING & INCREMENTAL SAVE ---
        if not viral_candidates:
            st.warning("⚠️ No new viral videos found to process.")
            st.stop()
            
        st.divider()
        st.subheader(f"📝 Processing {len(viral_candidates)} Videos")
        
        enable_ai_audio = True
        log_container = st.container()
        proc_stats = {"saved": 0}
        progress_bar = st.progress(0)
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            future_to_video = {
                executor.submit(process_single_video, vid, platform, manual_baseline, enable_ai_audio): vid 
                for vid in viral_candidates
            }
            
            completed = 0
            for future in concurrent.futures.as_completed(future_to_video):
                try:
                    row, log_text, p_stat = future.result()
                    with log_container:
                        with st.expander(f"Processed: {future_to_video[future]['title'][:50]}...", expanded=False):
                            st.markdown(log_text)
                    if row:
                        sheet.append_row(row)
                        proc_stats["saved"] += 1
                        time.sleep(1.5)
                except Exception as e:
                    st.error(f"Error: {e}")
                completed += 1
                progress_bar.progress(completed / len(viral_candidates))

        st.success(f"🎉 Analysis Complete! {proc_stats['saved']} new rows saved.")
        
    except Exception as e:
        st.error(f"Critical Error: {e}")