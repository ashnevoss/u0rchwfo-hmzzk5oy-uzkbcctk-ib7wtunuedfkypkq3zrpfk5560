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
# 1. DEEP SCRAPE (Cheap & Fast - Ultra Barato)
# Actor ID: nixGoSi2KjxbVYctO (esdrasdw/instagram-reels-scrapy)
IG_REEL_ACTOR = "nixGoSi2KjxbVYctO" 

# 2. TRANSCRIBERS (ORIGINAL RELIABLE VERSION)
# This actor uses your OpenAI Key to listen to the video
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
    
    # Debug stats
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

def get_ig_reels(username, days_ago):
    # Stats container
    scrape_stats = {
        "raw_fetched": 0,
        "date_filtered": 0,
        "valid_date_range": 0,
        "rejected_log": []
    }

    username = username.strip().replace("@", "")
    username = username.strip().replace("https://www.instagram.com/", "").replace("/", "")
    
    # Estimate quantity
    estimated_quantity = max(50, days_ago * 3) 
    if estimated_quantity > 500: estimated_quantity = 500
    
    st.info(f"🕵️ Scraping via Ultra Barato API... (Targeting {estimated_quantity} items for {username})")

    run_input = {
        "usernames": [username],
        "quantity_per_user": estimated_quantity,
        "selected_fields": [
            "thumb", "titulo", "usuario", "link_post", 
            "data_criacao_iso", "visualizacoes", "curtidas", 
            "comentarios", "duracao"
        ],
    }
    
    try:
        cutoff_date_obj = datetime.now(timezone.utc) - timedelta(days=days_ago)
        
        run = apify_client.actor(IG_REEL_ACTOR).call(run_input=run_input)
        if not run: return [], scrape_stats

        dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
        scrape_stats["raw_fetched"] = len(dataset_items)
        
        valid_reels = []
        
        for item in dataset_items:
            date_str = item.get("data_criacao_iso")
            reel_date = None
            
            # --- Date Logic ---
            if date_str:
                try:
                    date_str = date_str.replace("Z", "")
                    reel_date = datetime.fromisoformat(date_str).replace(tzinfo=timezone.utc)
                except Exception:
                    pass
            
            caption = item.get("titulo", "") or "No Caption"
            
            if not reel_date:
                scrape_stats["rejected_log"].append({"title": caption[:30], "reason": "No Date Found"})
                continue

            # FILTER: Date
            if reel_date < cutoff_date_obj:
                scrape_stats["date_filtered"] += 1
                # Optional: log a few old ones just to see
                if scrape_stats["date_filtered"] < 5:
                    scrape_stats["rejected_log"].append({"title": caption[:30], "reason": f"Too Old ({reel_date.strftime('%Y-%m-%d')})"})
                continue

            # --- Mapping ---
            views = item.get("visualizacoes", 0)
            if isinstance(views, str):
                views = int(''.join(filter(str.isdigit, views))) if any(c.isdigit() for c in views) else 0
            
            post_url = item.get("link_post", "")
            short_code = post_url.split("/")[-2] if post_url.endswith("/") else post_url.split("/")[-1]

            valid_reels.append({
                "title": caption[:50] + "...",
                "full_caption": caption,
                "views": int(views),
                "url": post_url,
                "published": reel_date.strftime('%Y-%m-%d'),
                "id": short_code
            })
            
        scrape_stats["valid_date_range"] = len(valid_reels)
        return sorted(valid_reels, key=lambda x: x['views'], reverse=True), scrape_stats
        
    except Exception as e:
        st.error(f"Scraper Error: {e}")
        return [], scrape_stats

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
            # --- ORIGINAL RELIABLE LOGIC ---
            clean_url = url.split("?")[0].replace("/p/", "/reel/")
            
            # This uses your OPENAI key and is more robust
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

def process_single_video(video, platform_type, baseline, enable_transcription):
    result_stats = {"transcribed": False, "caption_fallback": False, "failed": False}
    calc_log = []
    
    calc_log.append(f"🎬 **Processing:** [{video['title']}]({video['url']})")
    multiplier = video['views'] / baseline
    calc_log.append(f"   • 🧮 **Math:** {video['views']:,} / {baseline:,} = **{multiplier:.2f}x**")
    
    transcript = "N/A"
    
    # --- LOGIC: CHECK IF TRANSCRIPTION IS ENABLED ---
    if enable_transcription:
        # Try to transcribe using paid actor
        transcript_data = transcribe_video(video['url'], platform_type)
        transcript = str(transcript_data)
    else:
        calc_log.append("   • 💰 **Economy Mode:** Skipped AI Transcription (Using Caption Only).")

    # Fallback / Economy Mode Logic
    is_caption_fallback = False
    if not transcript or len(transcript) < 10 or "N/A" in transcript:
        caption = video.get("full_caption", "")
        if caption and len(str(caption)) > 5:
            transcript = str(caption)
            is_caption_fallback = True
            result_stats["caption_fallback"] = True
            calc_log.append("   • 📝 **Source:** Using Instagram Caption.")
        else:
            calc_log.append("   • ❌ **Status:** No Text Available. Skipping.")
            result_stats["failed"] = True
            return None, "\n".join(calc_log), result_stats
    else:
        result_stats["transcribed"] = True
        calc_log.append(f"   • 🎙️ **Source:** AI Transcript ({len(transcript)} chars)")
    
    # --- AI ANALYSIS ---
    hook = extract_hook_with_ai(transcript)
    topic = generate_topic_with_ai(transcript)
    
    calc_log.append(f"   • 🧠 **AI Analysis:** Topic: '{topic}'")

    final_transcript = f"[CAPTION ONLY] {transcript}" if is_caption_fallback else transcript
    
    # [UPDATED] Row structure: No Format column
    row = [topic, video['views'], f"{round(multiplier, 1)}x", video['published'], video['url'], hook, final_transcript[:40000]]
    
    return row, "\n".join(calc_log), result_stats

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
    time_options = ["7 Days", "14 Days", "30 Days", "60 Days", "90 Days", "180 Days"]
    selected_time = st.selectbox("Scan Last:", time_options, index=2)
    days_ago = int(selected_time.split(" ")[0])

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

        # --- PHASE 1: SCRAPING & FILTERING ---
        status_box = st.status("🔍 **Phase 1: Scouting & Filtering...**", expanded=True)
        with status_box:
            
            if platform == "YouTube Shorts":
                channel_id = get_yt_channel_id(target_handle, YOUTUBE_API_KEY)
                if not channel_id: st.stop()
                videos, scrape_stats = get_yt_shorts(channel_id, YOUTUBE_API_KEY, days_ago)
            else:
                videos, scrape_stats = get_ig_reels(target_handle, days_ago)

            if not videos:
                status_box.update(label="❌ No videos found.", state="error")
                st.stop()

            # FILTER: View Count
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
        m1.metric("Total Scraped", scrape_stats.get("raw_fetched", 0) if platform == "Instagram Reels" else scrape_stats.get("total_fetched", 0))
        m2.metric("Filtered (Date)", scrape_stats.get("date_filtered", 0))
        m3.metric("Filtered (Low Views)", len(skipped_low_views))
        m4.metric("🔥 Viral Hits", len(targets))

        with st.expander("📉 View Rejected / Skipped Data (Click to Expand)"):
            tab_views, tab_date = st.tabs(["❌ Skipped (Low Views)", "❌ Skipped (Date/Errors)"])
            
            with tab_views:
                if skipped_low_views:
                    st.warning(f"These {len(skipped_low_views)} videos were skipped because they didn't hit {target_views:,} views.")
                    st.dataframe(pd.DataFrame(skipped_low_views))
                else:
                    st.success("No videos skipped due to low views.")

            with tab_date:
                if platform == "Instagram Reels" and scrape_stats.get("rejected_log"):
                    st.dataframe(pd.DataFrame(scrape_stats["rejected_log"]))
                else:
                    st.write("No specific date rejection logs available.")

        if not targets:
            st.error("❌ No videos met your viral criteria. Try lowering the Multiplier or Baseline.")
            st.stop()

        # --- PHASE 2: PROCESSING ---
        st.divider()
        st.subheader(f"📝 Processing {len(targets)} Viral Videos")
        
        # COST CONTROL CHECKBOX
        enable_ai_audio = st.checkbox("🎙️ Enable AI Audio Transcription", value=True, help="Uncheck to save money and use captions only. Check to use OpenAI transcription.")

        log_container = st.container()
        results_to_save = []
        
        # Track Processing Stats
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
                
                # Update Stats
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
            # [UPDATED] Headers: Removed 'Format'
            if not existing:
                sheet.append_row(['Topic', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript'])
            sheet.append_rows(results_to_save)
            st.success(f"🎉 Analysis Complete! Saved {len(results_to_save)} rows to Google Sheets.")
            
            with st.expander("📄 View Final Data"):
                # [UPDATED] Columns: Removed 'Format'
                st.dataframe(pd.DataFrame(results_to_save, columns=['Topic', 'Views', 'Multiplier', 'Date', 'URL', 'Hook', 'Transcript']))

    except Exception as e:
        st.error(f"Critical Error: {e}")