#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy (Canlı İzleyici Sayacı, Kalıcı Aylık Kota Takibi, Render Süresi Ölçer & Gelişmiş Zamanlayıcı).
"""

import os
import re
import json
import time
import shutil
import asyncio
import subprocess
import logging
from datetime import datetime
from pathlib import Path
from aiohttp import web

# ==================== AYARLAR ====================
BIND_HOST    = "0.0.0.0"
PROXY_PORT   = int(os.environ.get("PORT", 8080))
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "admin123")  # ŞİFRENİZ

BASE_DIR = Path(__file__).resolve().parent
LOCAL_M3U_PATH  = os.environ.get("LOCAL_M3U_PATH", str(BASE_DIR / "playlist.m3u"))
LOCAL_JSON_PATH = os.environ.get("LOCAL_JSON_PATH", str(BASE_DIR / "channels.json"))
LOG_DIR         = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))
USAGE_FILE      = os.environ.get("USAGE_FILE", str(BASE_DIR / "bandwidth_usage.json"))

HLS_BASE_DIR = "/tmp/iptv_hls"
STANDBY_TS_PATH = os.path.join(HLS_BASE_DIR, "standby.ts")

HLS_TIME       = 4
HLS_LIST_SIZE  = 12
IDLE_TIMEOUT   = 100
STARTUP_WAIT   = 60
FFMPEG_BIN     = "ffmpeg"
APP_START_TIME = time.time()

TURKISH_DAYS = ["Pazartesi", "Salı", "Çarşamba", "Perşembe", "Cuma", "Cumartesi", "Pazar"]
ENGLISH_DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]

# ==================== KALICI AYLIK KOTA & RENDER VE METİN TAKİPÇİSİ ====================
class BandwidthTracker:
    """Kotayı, toplam render süresini ve kapalı ekran metnini diske kaydeder ve her ay başında otomatik olarak sıfırlar."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.current_month = time.strftime("%Y-%m")
        self.bytes_used = 0
        self.total_render_seconds = 0
        self.standby_text = "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir"
        self.dirty = False
        self.load()

    def load(self):
        now_month = time.strftime("%Y-%m")
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("month") == now_month:
                        self.bytes_used = data.get("bytes", 0)
                    else:
                        self.bytes_used = 0
                    self.total_render_seconds = data.get("total_render_seconds", 0)
                    self.standby_text = data.get("standby_text", "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir")
                    self.current_month = now_month
            except Exception:
                self.bytes_used = 0
        else:
            self.bytes_used = 0
            self.total_render_seconds = 0
            self.current_month = now_month
            self.save()

    def add_bytes(self, n: int):
        now_month = time.strftime("%Y-%m")
        if now_month != self.current_month:
            self.current_month = now_month
            self.bytes_used = 0

        self.bytes_used += n
        self.dirty = True

    def add_render_seconds(self, seconds: int):
        self.total_render_seconds += seconds
        self.dirty = True

    def save(self):
        try:
            data = {
                "month": self.current_month,
                "bytes": self.bytes_used,
                "total_render_seconds": self.total_render_seconds,
                "standby_text": self.standby_text,
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.dirty = False
        except Exception as e:
            log.warning(f"Kota dosyaya kaydedilemedi: {e}")

    async def periodic_save(self):
        while True:
            await asyncio.sleep(10)
            if self.dirty:
                self.save()

tracker = BandwidthTracker(USAGE_FILE)

# ==================== KANALLAR (DİNAMİK JSON DESTEKLİ) ====================
DEFAULT_KANALLAR = [
    {
        "id": "futbol_tv",
        "name": "FUTBOL TV",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://nexttr.xyz:8080/live/AbdLk@16729@/V9qK3nRw52La/774257.m3u8",
        "schedules": []
    },
    {
        "id": "sportv_yedek",
        "name": "SBOX",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://yubsz.dnster.net/live/kadirsener1/Nf9HUKWhdrEuacCm/3264.m3u8",
        "schedules": []
    },
    {
        "id": "bein_sports_1_6817",
        "name": "BEİN SPORTS 1 (6817)",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://0e770a63.ucomist.net/iptv/3HYPASK67VVUSL/6817/index.m3u8",
        "schedules": []
    }
]

def load_dynamic_channels():
    if os.path.exists(LOCAL_JSON_PATH):
        try:
            with open(LOCAL_JSON_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Kanallar JSON dosyasından yüklenemedi: {e}")
    try:
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_KANALLAR, f, indent=2)
    except Exception as e:
        log.warning(f"Varsayılan kanallar yazılamadı: {e}")
    return DEFAULT_KANALLAR

KANALLAR = load_dynamic_channels()
DELETED_CHANNELS = ["bein_sports_1_6781", "BEİN SPORTS 1 (6781)", "bein sports 1 (6781)"]
CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}
NO_CACHE_HEADERS = {
    **CORS_HEADERS,
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0"
}

# ==================== LOG ====================
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(LOG_DIR, "proxy.log"), encoding="utf-8"),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("iptv")

def get_client_ip(request):
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip()
    cf_ip = request.headers.get("CF-Connecting-IP")
    if cf_ip:
        return cf_ip.strip()
    return request.remote or "unknown"

def get_memory_usage_mb():
    try:
        import resource
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return round(usage / 1024, 2)
    except Exception:
        return 0.0

# ==================== STANDBY EKRANI ====================
def generate_standby_clip(text=None):
    if text is None:
        text = tracker.standby_text

    escaped_text = text.replace("\n", "\\n").replace("'", "")

    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={HLS_TIME}:r=25",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", f"drawtext=text='{escaped_text}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-b:v", "35k",
        "-c:a", "aac", "-b:a", "16k",
        "-f", "mpegts", STANDBY_TS_PATH
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception as e:
        log.warning(f"Standby klibi oluşturulamadı: {e}")

# ==================== FFMPEG YÖNETİCİSİ ====================
class ChannelStream:
    def __init__(self, channel: dict):
        self.ch = channel
        self.id = channel["id"]
        self.src = channel["url"]
        self.dir = os.path.join(HLS_BASE_DIR, self.id)
        self.proc: subprocess.Popen | None = None
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.enabled = True
        self.viewers = {}
        self.ch.setdefault("schedules", [])
        self._last_schedule_state = None

    def record_viewer(self, ip: str):
        if ip and ip != "unknown":
            self.viewers[ip] = time.time()
            self.touch()

    def get_viewer_count(self) -> int:
        now = time.time()
        active = [ip for ip, last_seen in self.viewers.items() if (now - last_seen) <= 12]
        self.viewers = {ip: last_seen for ip, last_seen in self.viewers.items() if (now - last_seen) <= 60}
        return len(active)

    def _prepare_dir(self):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def _build_cmd(self):
        m3u8_path = os.path.join(self.dir, "index.m3u8")
        seg_pattern = os.path.join(self.dir, "seg_%05d.ts")

        cmd = [
            FFMPEG_BIN, "-hide_banner", "-loglevel", "warning", "-nostdin",
            "-rw_timeout", "15000000", "-reconnect", "1", "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5", "-user_agent", "VLC/3.0.18 LibVLC/3.0.18",
            "-i", self.src, "-c", "copy", "-f", "hls",
            "-hls_time", str(HLS_TIME), "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_type", "mpegts", "-hls_segment_filename", seg_pattern,
            "-hls_allow_cache", "1", m3u8_path
        ]
        return cmd

    async def start(self):
        if not self.enabled:
            return
        async with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            self._prepare_dir()
            cmd = self._build_cmd()
            ff_log = open(os.path.join(LOG_DIR, f"{self.id}.ffmpeg.log"), "ab")
            self.proc = subprocess.Popen(cmd, stdout=ff_log, stderr=ff_log, stdin=subprocess.DEVNULL, start_new_session=True)
            self.started_at = time.time()

    async def stop(self):
        async with self.lock:
            if not self.proc:
                return
            if self.proc.poll() is None:
                try:
                    self.proc.terminate()
                    try:
                        self.proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.proc.kill()
                except Exception:
                    pass
            self.proc = None

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def playlist_path(self):
        return os.path.join(self.dir, "index.m3u8")

    def playlist_ready(self) -> bool:
        p = self.playlist_path()
        if not os.path.exists(p):
            return False
        try:
            with open(p, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
            return content.count("#EXTINF") >= 2
        except Exception:
            return False

    def touch(self):
        self.last_request = time.time()

# ==================== YÖNETİCİ ====================
class StreamManager:
    def __init__(self):
        self.streams: dict[str, ChannelStream] = {ch["id"]: ChannelStream(ch) for ch in KANALLAR}

    def get(self, cid: str) -> ChannelStream | None:
        return self.streams.get(cid)

    def total_viewers(self) -> int:
        return sum(st.get_viewer_count() for st in self.streams.values())

    async def ensure_running(self, cid: str) -> ChannelStream | None:
        st = self.streams.get(cid)
        if not st or not st.enabled:
            return None
        if not st.is_alive():
            await st.start()
        waited = 0.0
        while waited < STARTUP_WAIT:
            if st.playlist_ready():
                return st
            await asyncio.sleep(0.5)
            waited += 0.5
            if not st.is_alive() and st.enabled:
                await asyncio.sleep(0.5)
                await st.start()
        return st

    async def update_channel_url(self, cid: str, new_url: str) -> bool:
        st = self.streams.get(cid)
        if not st:
            return False

        was_running = st.is_alive()
        if was_running:
            await st.stop()

        st.src = new_url
        st.ch["url"] = new_url

        try:
            channels_data = [s.ch for s in self.streams.values()]
            with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(channels_data, f, indent=2, ensure_ascii=False)
            log.info(f"Kanal URL güncellendi: {cid} -> {new_url}")
        except Exception as e:
            log.error(f"Kanallar JSON dosyasına yazılamadı: {e}")

        if was_running and st.enabled:
            await st.start()

        return True

    async def check_and_apply_schedules(self):
        now = datetime.now()
        current_time_str = now.strftime("%H:%M")
        day_idx = now.weekday()
        day_tr = TURKISH_DAYS[day_idx]
        day_en = ENGLISH_DAYS[day_idx]

        for cid, st in self.streams.items():
            scheds = st.ch.get("schedules", []) or []
            if not scheds:
                st._last_schedule_state = None
                continue

            active_in_sched = False
            for s in scheds:
                s_day = str(s.get("day", ""))
                day_match = (
                    s_day == day_tr or
                    s_day == day_en or
                    s_day == "Her Gün" or
                    s_day == "Everyday" or
                    s_day.lower() == "her gun"
                )
                if day_match:
                    start_str = str(s.get("start", "00:00"))
                    end_str = str(s.get("end", "23:59"))
                    if start_str <= current_time_str <= end_str:
                        active_in_sched = True
                        break

            if st._last_schedule_state == active_in_sched:
                continue

            st._last_schedule_state = active_in_sched

            if active_in_sched and not st.enabled:
                st.enabled = True
                st.touch()
                await st.start()
                log.info(f"[ZAMANLAYICI] {st.id} | Verilen gün/saat aralığına göre YAYIN AÇILDI ({day_tr} {current_time_str})")

            elif (not active_in_sched) and st.enabled:
                st.enabled = False
                await st.stop()
                log.info(f"[ZAMANLAYICI] {st.id} | Zaman aralığı dışı YAYIN KAPATILDI ({day_tr} {current_time_str})")

    async def monitor(self):
        last_time = time.time()
        while True:
            await asyncio.sleep(5)
            now = time.time()
            elapsed_seconds = int(now - last_time)
            last_time = now

            active_render_servers = sum(1 for st in self.streams.values() if st.is_alive())
            if active_render_servers > 0:
                tracker.add_render_seconds(elapsed_seconds)

            await self.check_and_apply_schedules()

            for cid, st in self.streams.items():
                if not st.enabled and st.is_alive():
                    await st.stop()
                elif st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                    await st.stop()
                elif (not st.is_alive()) and st.enabled and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                    await st.start()

manager = StreamManager()
