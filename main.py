#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy (Canlı İzleyici Sayacı & Kalıcı Aylık Kota Takibi).
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


# ==================== KALICI AYLIK KOTA TAKİPÇİSİ ====================
class BandwidthTracker:
    """Kotayı diske kaydeder ve her ay başında otomatik olarak sıfırlar."""
    def __init__(self, filepath):
        self.filepath = filepath
        self.current_month = time.strftime("%Y-%m")
        self.bytes_used = 0
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
                        self.current_month = now_month
                    else:
                        self.bytes_used = 0
                        self.current_month = now_month
                        self.save()
            except Exception:
                self.bytes_used = 0
        else:
            self.bytes_used = 0
            self.current_month = now_month
            self.save()

    def add_bytes(self, n: int):
        now_month = time.strftime("%Y-%m")
        if now_month != self.current_month:
            self.current_month = now_month
            self.bytes_used = 0

        self.bytes_used += n
        self.dirty = True

    def save(self):
        try:
            data = {
                "month": self.current_month,
                "bytes": self.bytes_used,
                "last_updated": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            self.dirty = False
        except Exception as e:
            log.warning(f"Kota dosyaya kaydedilemedi: {e}")

    async def periodic_save(self):
        """Performans için her 10 saniyede bir diske yazar."""
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
        "standby_message": "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir",
        "schedules": []
    },
     {
        "id": "sportv_yedek",
        "name": "SBOX",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://yubsz.dnster.net/live/kadirsener1/Nf9HUKWhdrEuacCm/3264.m3u8",
        "standby_message": "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir",
        "schedules": []
    },
    {
        "id": "bein_sports_1_6817",
        "name": "BEİN SPORTS 1 (6817)",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://0e770a63.ucomist.net/iptv/3HYPASK67VVUSL/6817/index.m3u8",
        "standby_message": "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir",
        "schedules": []
    }
]

def load_dynamic_channels():
    """Kanalları JSON dosyasından yükler, yoksa varsayılanları oluşturur."""
    if os.path.exists(LOCAL_JSON_PATH):
        try:
            with open(LOCAL_JSON_PATH, "r", encoding="utf-8") as f:
                channels = json.load(f)
                # Eksik alanları varsayılanlarla tamamla
                for ch in channels:
                    if "standby_message" not in ch:
                        ch["standby_message"] = "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir"
                    if "schedules" not in ch:
                        ch["schedules"] = []
                return channels
        except Exception as e:
            log.warning(f"Kanallar JSON dosyasından yüklenemedi: {e}")
    try:
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_KANALLAR, f, indent=2, ensure_ascii=False)
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
    """Kullanıcının gerçek IP adresini tespit eder (Cloudflare/Render Proxy Uyumlu)"""
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
def generate_standby_clip(message="YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir"):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    # Mesaj içindeki satır sonlarını FFmpeg drawtext için düzenle
    formatted_msg = message.replace('\n', '\\n')
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={HLS_TIME}:r=25",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", f"drawtext=text='{formatted_msg}':fontcolor=white:fontsize=40:x=(w-text_w)/2:y=(h-text_h)/2",
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
        self.ch      = channel
        self.id      = channel["id"]
        self.src     = channel["url"]
        self.dir     = os.path.join(HLS_BASE_DIR, self.id)
        self.proc: subprocess.Popen | None = None
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.enabled = True
        self.viewers = {}

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

    async def update_channel_settings(self, cid: str, standby_message: str, schedules: list) -> bool:
        st = self.streams.get(cid)
        if not st:
            return False
        
        st.ch["standby_message"] = standby_message
        st.ch["schedules"] = schedules
        
        try:
            channels_data = [s.ch for s in self.streams.values()]
            with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
                json.dump(channels_data, f, indent=2, ensure_ascii=False)
            log.info(f"Kanal ayarları güncellendi: {cid}")
        except Exception as e:
            log.error(f"Kanallar JSON dosyasına yazılamadı: {e}")
        return True

    def check_schedules(self):
        """Zamanlanmış görevleri kontrol eder ve yayınları açıp kapatır."""
        now = datetime.now()
        current_day = now.weekday() # 0: Pzt, 6: Pazar
        current_time_str = now.strftime("%H:%M")

        for cid, st in self.streams.items():
            schedules = st.ch.get("schedules", [])
            if not schedules:
                continue
            
            should_be_enabled = False
            for sch in schedules:
                days = sch.get("days", [])
                start_t = sch.get("start", "00:00")
                end_t = sch.get("end", "23:59")

                if current_day in days:
                    if start_t <= current_time_str <= end_t:
                        should_be_enabled = True
                        break

            # Eğer zamanlanmış kurala göre durum değişmeli ise
            if st.enabled != should_be_enabled:
                st.enabled = should_be_enabled
                log.info(f"Zamanlayıcı tetiklendi: {cid} durumu otomatik olarak {should_be_enabled} yapıldı.")

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            self.check_schedules()
            now = time.time()
            for cid, st in self.streams.items():
                if not st.enabled and st.is_alive():
                    await st.stop()
                elif st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                    await st.stop()
                elif (not st.is_alive()) and st.enabled and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                    await st.start()

manager = StreamManager()


# ==================== HTTP HANDLER'LAR ====================
async def handle_m3u8(request):
    cid = request.match_info.get("channel_id")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Yok", headers=CORS_HEADERS)

    client_ip = get_client_ip(request)
    st.record_viewer(client_ip)

    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    dynamic_proxy_url = f"{scheme}://{host}"

    if not st.enabled:
        msg = st.ch.get("standby_message", "YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir")
        generate_standby_clip(msg)
        seq = int(time.time() // HLS_TIME)
        standby_lines = [
            "#EXTM3U", "#EXT-X-VERSION:3", f"#EXT-X-TARGETDURATION:{HLS_TIME}",
            f"#EXT-X-MEDIA-SEQUENCE:{seq}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 1}",
            f"#EXTINF:{HLS_TIME}.000,", f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 2}",
        ]
        resp_text = "\n".join(standby_lines)
        tracker.add_bytes(len(resp_text.encode('utf-8')))
        return web.Response(text=resp_text, content_type="application/vnd.apple.mpegurl", headers={**CORS_HEADERS, "Cache-Control": "no-cache"})

    res = await manager.ensure_running(cid)
    if not res:
        return web.Response(status=503, text="Yayın başlatılamadı.", headers=CORS_HEADERS)

    pl = st.playlist_path()
    if not os.path.exists(pl):
        return web.Response(status=503, text="Yayın hazırlanıyor...", headers=CORS_HEADERS)

    try:
        with open(pl, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)

    out_lines = []
    for line in content.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            out_lines.append(s)
        else:
            seg_name = s.split("?")[0].split("/")[-1]
            out_lines.append(f"{dynamic_proxy_url}/hls/{cid}/{seg_name}")

    resp_text = "\n".join(out_lines)
    tracker.add_bytes(len(resp_text.encode('utf-8')))
    return web.Response(text=resp_text, content_type="application/vnd.apple.mpegurl", headers={**CORS_HEADERS, "Cache-Control": "no-cache"})


async def handle_segment(request):
    cid = request.match_info.get("channel_id")
    name = request.match_info.get("name")

    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+\.(ts|m4s|mp4|aac|key)", name):
        return web.Response(status=400, headers=CORS_HEADERS)

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, headers=CORS_HEADERS)

    client_ip = get_client_ip(request)
    st.record_viewer(client_ip)

    seg_path = os.path.join(st.dir, name)
    if not os.path.exists(seg_path):
        await asyncio.sleep(0.3)
        if not os.path.exists(seg_path):
            return web.Response(status=404, headers=CORS_HEADERS)

    try:
        file_size = os.path.getsize(seg_path)
        tracker.add_bytes(file_size)
        return web.FileResponse(seg_path, headers={**CORS_HEADERS, "Cache-Control": "public, max-age=6", "Content-Type": "video/mp2t"})
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)


async def handle_standby_segment(request):
    if not os.path.exists(STANDBY_TS_PATH):
        generate_standby_clip()
    if not os.path.exists(STANDBY_TS_PATH):
        return web.Response(status=404, headers=CORS_HEADERS)

    file_size = os.path.getsize(STANDBY_TS_PATH)
    tracker.add_bytes(file_size)
    return web.FileResponse(STANDBY_TS_PATH, headers={**CORS_HEADERS, "Cache-Control": "public, max-age=4", "Content-Type": "video/mp2t"})


async def handle_health(request):
    uptime_sec = int(time.time() - APP_START_TIME)
    days = uptime_sec // 86400
    hours = (uptime_sec % 86400) // 3600
    minutes = (uptime_sec % 3600) // 60
    seconds = uptime_sec % 60
    uptime_formatted = f"{days} gün, {hours:02d} saat, {minutes:02d} dakika"

    status = {
        "server": {
            "uptime_seconds": uptime_sec,
            "uptime_formatted": uptime_formatted,
            "ram_usage_mb": get_memory_usage_mb(),
            "total_served_mb": round(tracker.bytes_used / (1024 * 1024), 2),
            "total_served_gb": round(tracker.bytes_used / (1024 * 1024 * 1024), 3),
            "total_viewers": manager.total_viewers()
        },
        "channels": {}
    }
    for cid, st in manager.streams.items():
        status["channels"][cid] = {
            "name": st.ch.get("name", cid), 
            "url": st.src,
            "enabled": st.enabled,
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "viewers": st.get_viewer_count(),
            "standby_message": st.ch.get("standby_message", ""),
            "schedules": st.ch.get("schedules", [])
        }
    return web.json_response(status, headers=NO_CACHE_HEADERS)


# ==================== YÖNETİCİ PANELİ ====================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IPTV Kontrol & Canlı İzleyici Paneli</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 700px; margin: auto; }
        .card { background: #1e293b; padding: 15px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .stat-box { background: #334155; padding: 12px; border-radius: 8px; text-align: center; }
        .stat-val { font-size: 18px; font-weight: bold; color: #38bdf8; }
        .stat-lbl { font-size: 11px; color: #94a3b8; margin-top: 4px; }
        h2 { color: #38bdf8; margin-top: 0; }
        .btn { padding: 10px 18px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; transition: 0.2s; }
        .btn-on { background: #22c55e; }
        .btn-off { background: #ef4444; }
        .btn-logout { background: #475569; font-size: 11px; padding: 6px 12px; margin-top: 10px; }
        .status-badge { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .badge-active { background: #15803d; }
        .badge-disabled { background: #b91c1c; }
        .badge-viewer { background: #0369a1; color: #e0f2fe; margin-left: 4px; }
        .badge-ffmpeg-on { background: #0284c7; color: white; }
        .badge-ffmpeg-off { background: #64748b; color: #cbd5e1; }
        input[type=password], input[type=text] { padding: 10px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: white; width: 100%; box-sizing: border-box; margin-bottom: 10px; font-size: 14px; }
        #loginArea { max-width: 400px; margin: 100px auto; text-align: center; }
        .edit-group { margin-top: 10px; border-top: 1px solid #334155; padding-top: 10px; }
        .btn-save { background: #3b82f6; font-size: 12px; padding: 6px 14px; width: 100%; margin-top: 5px; }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #16a34a; color: white; padding: 12px 24px; border-radius: 8px; z-index: 9999; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
        label { font-size: 12px; color: #94a3b8; display: block; margin-bottom: 4px; }
        .schedule-item { background: #0f172a; padding: 8px; border-radius: 6px; margin-bottom: 6px; display: flex; justify-content: space-between; align-items: center; font-size: 12px; }
    </style>
</head>
<body>

    <div id="toastMsg" class="toast"></div>

    <!-- GİRİŞ EKRANI -->
    <div id="loginArea" class="card">
        <h2>🔒 Yönetici Girişi</h2>
        <p style="color: #94a3b8; font-size: 13px; margin-bottom: 15px;">Lütfen devam etmek için şifrenizi girin.</p>
        <input type="password" id="adminPassword" placeholder="Şifre" onkeypress="handleKeyPress(event)">
        <button class="btn btn-on" style="width: 100%;" onclick="attemptLogin()">Giriş Yap</button>
    </div>

    <!-- PANEL ALANI -->
    <div id="panelArea" style="display: none;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>📊 Sunucu & İzleyici Durumu</h2>
            <button class="btn btn-logout" onclick="logout()">Çıkış Yap</button>
        </div>
        
        <div class="stats-grid">
            <div class="stat-box">
                <div class="stat-val" id="uptimeVal">0 gün</div>
                <div class="stat-lbl">Render Sunucusu Çalışma Süresi</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="totalViewers" style="color:#a855f7;">0 Kişi</div>
                <div class="stat-lbl">Canlı İzleyici</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="servedGb">0.00 GB</div>
                <div class="stat-lbl">Harcanan Kota (Aylık)</div>
            </div>
            <div class="stat-box">
                <div class="stat-val" id="ramMb">0 MB</div>
                <div class="stat-lbl">RAM Kullanımı</div>
            </div>
        </div>

        <h2>📺 Yayın Kontrolü & Zamanlama</h2>
        <div id="channels"></div>
    </div>

    <script>
        let updateInterval = null;

        function showToast(msg, color) {
            const t = document.getElementById('toastMsg');
            t.innerText = msg;
            t.style.background = color || '#16a34a';
            t.style.display = 'block';
            setTimeout(() => { t.style.display = 'none'; }, 2500);
        }

        function getStoredKey() {
            return localStorage.getItem("admin_key") || "";
        }

        function handleKeyPress(e) {
            if (e.key === 'Enter') {
                attemptLogin();
            }
        }

        async function verifyKey(key) {
            try {
                const res = await fetch(`/admin/verify?key=${encodeURIComponent(key)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    const data = await res.json();
                    return data.valid;
                }
            } catch (e) {}
            return false;
        }

        async function attemptLogin() {
            const key = document.getElementById('adminPassword').value;
            const isValid = await verifyKey(key);
            if (isValid) {
                localStorage.setItem("admin_key", key);
                showPanel();
            } else {
                alert('Şifre Hatalı!');
            }
        }

        function logout() {
            localStorage.removeItem("admin_key");
            document.getElementById('adminPassword').value = "";
            document.getElementById('panelArea').style.display = "none";
            document.getElementById('loginArea').style.display = "block";
            if (updateInterval) clearInterval(updateInterval);
        }

        async function showPanel() {
            document.getElementById('loginArea').style.display = "none";
            document.getElementById('panelArea').style.display = "block";
            await loadStatus();
            if (updateInterval) clearInterval(updateInterval);
            updateInterval = setInterval(loadStatus, 3000);
        }

        async function loadStatus() {
            try {
                const res = await fetch(`/health?_=${Date.now()}`, { cache: 'no-store' });
                if (!res.ok) return;
                const data = await res.json();
                
                document.getElementById('uptimeVal').innerText = data.server.uptime_formatted;
                document.getElementById('totalViewers').innerText = data.server.total_viewers + " Kişi";
                document.getElementById('servedGb').innerText = data.server.total_served_gb + " GB";
                document.getElementById('ramMb').innerText = data.server.ram_usage_mb + " MB";

                const container = document.getElementById('channels');

                for (const [id, info] of Object.entries(data.channels)) {
                    let card = document.getElementById(`card_${id}`);
                    if (!card) {
                        card = document.createElement('div');
                        card.id = `card_${id}`;
                        card.className = 'card';
                        container.appendChild(card);
                    }

                    const inputId = `url_${id}`;
                    const msgInputId = `msg_${id}`;
                    const activeElement = document.activeElement;
                    const isInputFocused = (activeElement && (activeElement.id === inputId || activeElement.id === msgInputId));

                    if (!card.querySelector('.edit-group')) {
                        let schedulesHtml = '';
                        (info.schedules || []).forEach((sch, idx) => {
                            schedulesHtml += `<div class="schedule-item"><span>Günler: [${sch.days.join(',')}] | ${sch.start} - ${sch.end}</span> <button class="btn btn-off" style="padding:2px 6px; font-size:10px;" onclick="removeSchedule('${id}', ${idx})">Sil</button></div>`;
                        });

                        card.innerHTML = `
                            <div style="display:flex; justify-content:space-between; align-items:center;">
                                <div>
                                    <h3 style="margin:0 0 5px 0; color: #f8fafc;" class="channel-title">${info.name || id}</h3>
                                    <div style="display:flex; gap: 5px; align-items:center; flex-wrap: wrap; margin-bottom: 6px;">
                                        <span class="status-badge badge-state ${info.enabled ? 'badge-active' : 'badge-disabled'}">
                                            ${info.enabled ? 'YAYINDA' : 'KAPALI'}
                                        </span>
                                        <span class="status-badge badge-viewer badge-viewers-count">
                                            👥 ${info.viewers} İzleyici
                                        </span>
                                        <span class="status-badge badge-ffmpeg ${info.running ? 'badge-ffmpeg-on' : 'badge-ffmpeg-off'}">
                                            ${info.running ? '● FFmpeg Aktif' : '○ FFmpeg Kapalı'}
                                        </span>
                                    </div>
                                </div>
                                <button class="btn btn-toggle-action ${info.enabled ? 'btn-off' : 'btn-on'}" onclick="toggleChannel('${id}', ${!info.enabled})">
                                    ${info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ'}
                                </button>
                            </div>
                            
                            <div class="edit-group">
                                <label>Yayın (.m3u8) Linki:</label>
                                <input type="text" id="${inputId}" value="${info.url}" placeholder="Yayın Linki">
                                
                                <label>Yayın Kapalı İken Ekran Mesajı:</label>
                                <input type="text" id="${msgInputId}" value="${info.standby_message || ''}" placeholder="Örn: YAYIN KAPALIDIR">
                                
                                <label>Otomatik Açma/Kapatma Programı (Zamanlama):</label>
                                <div id="schedules_container_${id}" style="margin-bottom:8px;">${schedulesHtml}</div>
                                <div style="display:flex; gap:4px; margin-bottom:8px;">
                                    <input type="text" id="sch_days_${id}" placeholder="Günler (0:Pzt,1:Sal... örn: 0,1,2,3,4,5,6)" style="margin-bottom:0;">
                                    <input type="text" id="sch_start_${id}" placeholder="HH:MM" style="width:70px; margin-bottom:0;">
                                    <input type="text" id="sch_end_${id}" placeholder="HH:MM" style="width:70px; margin-bottom:0;">
                                    <button class="btn btn-on" style="font-size:11px; padding:6px 10px;" onclick="addSchedule('${id}')">Ekle</button>
                                </div>

                                <button class="btn btn-save" onclick="saveChannelSettings('${id}')">Ayarları Kaydet</button>
                            </div>
                        `;
                    } else {
                        card.querySelector('.channel-title').innerText = info.name || id;
                        
                        const badgeState = card.querySelector('.badge-state');
                        badgeState.className = `status-badge badge-state ${info.enabled ? 'badge-active' : 'badge-disabled'}`;
                        badgeState.innerText = info.enabled ? 'YAYINDA' : 'KAPALI';
                        
                        const badgeViewers = card.querySelector('.badge-viewers-count');
                        badgeViewers.innerText = `👥 ${info.viewers} İzleyici`;
                        
                        const badgeFfmpeg = card.querySelector('.badge-ffmpeg');
                        badgeFfmpeg.className = `status-badge badge-ffmpeg ${info.running ? 'badge-ffmpeg-on' : 'badge-ffmpeg-off'}`;
                        badgeFfmpeg.innerText = info.running ? '● FFmpeg Aktif' : '○ FFmpeg Kapalı';
                        
                        const btnToggle = card.querySelector('.btn-toggle-action');
                        btnToggle.className = `btn btn-toggle-action ${info.enabled ? 'btn-off' : 'btn-on'}`;
                        btnToggle.innerText = info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ';
                        btnToggle.setAttribute('onclick', `toggleChannel('${id}', ${!info.enabled})`);

                        if (!isInputFocused) {
                            const inp = card.querySelector(`#${inputId}`);
                            if (inp && inp.value !== info.url) inp.value = info.url;
                            const msgInp = card.querySelector(`#${msgInputId}`);
                            if (msgInp && msgInp.value !== info.standby_message) msgInp.value = info.standby_message || '';
                        }
                    }
                }
            } catch(e) {}
        }

        async function toggleChannel(id, enable) {
            const key = getStoredKey();
            const res = await fetch(`/admin/toggle?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&enable=${enable}&_=${Date.now()}`, { cache: 'no-store' });
            if (res.ok) {
                setTimeout(loadStatus, 500);
            } else {
                alert('Oturum Geçersiz veya Şifre Hatalı!');
                logout();
            }
        }

        async function saveChannelSettings(id) {
            const key = getStoredKey();
            const urlVal = document.getElementById(`url_${id}`).value.trim();
            const msgVal = document.getElementById(`msg_${id}`).value.trim();
            
            try {
                // Önce URL güncellemesi
                await fetch(`/admin/update_url?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&url=${encodeURIComponent(urlVal)}&_=${Date.now()}`, { cache: 'no-store' });
                
                // Sonra mesaj ve diğer ayarların güncellenmesi
                const res = await fetch(`/admin/update_settings`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ key: key, id: id, standby_message: msgVal })
                });

                if (res.ok) {
                    showToast('✅ Kanal ayarları başarıyla kaydedildi!');
                    setTimeout(loadStatus, 800);
                } else {
                    showToast('❌ Kaydedilemedi!', '#dc2626');
                }
            } catch(e) {
                showToast('❌ Hata: ' + e.message, '#dc2626');
            }
        }

        async function addSchedule(id) {
            const key = getStoredKey();
            const daysStr = document.getElementById(`sch_days_${id}`).value.trim();
            const start = document.getElementById(`sch_start_${id}`).value.trim();
            const end = document.getElementById(`sch_end_${id}`).value.trim();
            
            if(!daysStr || !start || !end) {
                alert('Lütfen günleri ve saat aralığını eksiksiz girin (Örn Günler: 0,1,2,3,4,5,6)');
                return;
            }
            const days = daysStr.split(',').map(Number);

            try {
                const res = await fetch(`/admin/add_schedule`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ key: key, id: id, schedule: { days, start, end } })
                });
                if(res.ok) {
                    showToast('✅ Zamanlama eklendi');
                    setTimeout(loadStatus, 500);
                } else {
                    alert('Yetki hatası veya geçersiz veri');
                }
            } catch(e) {}
        }

        async function removeSchedule(id, index) {
            const key = getStoredKey();
            try {
                const res = await fetch(`/admin/remove_schedule`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({ key: key, id: id, index: index })
                });
                if(res.ok) {
                    showToast('🗑️ Zamanlama silindi');
                    setTimeout(loadStatus, 500);
                }
            } catch(e) {}
        }

        async function init() {
            const storedKey = getStoredKey();
            if (storedKey) {
                const isValid = await verifyKey(storedKey);
                if (isValid) {
                    showPanel();
                    return;
                }
            }
            logout();
        }

        init();
    </script>
</body>
</html>
"""

async def handle_admin_page(request):
    return web.Response(text=ADMIN_HTML, content_type="text/html")

async def handle_admin_verify(request):
    key = request.query.get("key")
    if key == ADMIN_KEY:
        return web.json_response({"valid": True}, headers=NO_CACHE_HEADERS)
    return web.json_response({"valid": False}, status=401, headers=NO_CACHE_HEADERS)

async def handle_admin_toggle(request):
    key = request.query.get("key")
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz", headers=CORS_HEADERS)
    
    cid = request.query.get("id")
    enable_str = request.query.get("enable")
    enable = (enable_str.lower() == "true")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Yok", headers=CORS_HEADERS)

    st.enabled = enable
    if not enable:
        await st.stop()
    else:
        await st.start()

    return web.json_response({"status": "ok", "enabled": st.enabled}, headers=NO_CACHE_HEADERS)

async def handle_admin_update_url(request):
    key = request.query.get("key")
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz", headers=CORS_HEADERS)
    
    cid = request.query.get("id")
    new_url = request.query.get("url")

    if not cid or not new_url:
        return web.Response(status=400, text="Eksik parametre", headers=CORS_HEADERS)

    success = await manager.update_channel_url(cid, new_url)
    if success:
        return web.json_response({"status": "ok", "url": new_url}, headers=NO_CACHE_HEADERS)
    return web.Response(status=404, text="Kanal bulunamadı", headers=CORS_HEADERS)

async def handle_admin_update_settings(request):
    try:
        data = await request.json()
        if data.get("key") != ADMIN_KEY:
            return web.Response(status=401, text="Yetkisiz", headers=CORS_HEADERS)
        
        cid = data.get("id")
        standby_msg = data.get("standby_message", "")
        st = manager.get(cid)
        if st:
            st.ch["standby_message"] = standby_msg
            await manager.update_channel_settings(cid, standby_msg, st.ch.get("schedules", []))
            return web.json_response({"status": "ok"}, headers=NO_CACHE_HEADERS)
        return web.Response(status=404, text="Kanal yok", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(status=400, text=str(e), headers=CORS_HEADERS)

async def handle_admin_add_schedule(request):
    try:
        data = await request.json()
        if data.get("key") != ADMIN_KEY:
            return web.Response(status=401, text="Yetkisiz", headers=CORS_HEADERS)
        
        cid = data.get("id")
        schedule = data.get("schedule")
        st = manager.get(cid)
        if st and schedule:
            if "schedules" not in st.ch:
                st.ch["schedules"] = []
            st.ch["schedules"].append(schedule)
            await manager.update_channel_settings(cid, st.ch.get("standby_message", ""), st.ch["schedules"])
            return web.json_response({"status": "ok"}, headers=NO_CACHE_HEADERS)
        return web.Response(status=404, text="Kanal yok", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(status=400, text=str(e), headers=CORS_HEADERS)

async def handle_admin_remove_schedule(request):
    try:
        data = await request.json()
        if data.get("key") != ADMIN_KEY:
            return web.Response(status=401, text="Yetkisiz", headers=CORS_HEADERS)
        
        cid = data.get("id")
        idx = data.get("index")
        st = manager.get(cid)
        if st and "schedules" in st.ch:
            if 0 <= idx < len(st.ch["schedules"]):
                st.ch["schedules"].pop(idx)
                await manager.update_channel_settings(cid, st.ch.get("standby_message", ""), st.ch["schedules"])
                return web.json_response({"status": "ok"}, headers=NO_CACHE_HEADERS)
        return web.Response(status=404, text="Bulunamadı", headers=CORS_HEADERS)
    except Exception as e:
        return web.Response(status=400, text=str(e), headers=CORS_HEADERS)


# ==================== UYGULAMA BAŞLATMA ====================
async def init_app():
    app = web.Application()
    
    # Rotalar
    app.router.add_get('/hls/{channel_id}/index.m3u8', handle_m3u8)
    app.router.add_get('/hls/{channel_id}/{name}', handle_segment)
    app.router.add_get('/hls/standby/seg.ts', handle_standby_segment)
    app.router.add_get('/health', handle_health)
    
    app.router.add_get('/admin', handle_admin_page)
    app.router.add_get('/admin/verify', handle_admin_verify)
    app.router.add_get('/admin/toggle', handle_admin_toggle)
    app.router.add_get('/admin/update_url', handle_admin_update_url)
    app.router.add_post('/admin/update_settings', handle_admin_update_settings)
    app.router.add_post('/admin/add_schedule', handle_admin_add_schedule)
    app.router.add_post('/admin/remove_schedule', handle_admin_remove_schedule)

    # Arka plan görevleri
    asyncio.create_task(manager.monitor())
    asyncio.create_task(tracker.periodic_save())

    return app

if __name__ == '__main__':
    generate_standby_clip()
    app = asyncio.run(init_app())
    web.run_app(app, host=BIND_HOST, port=PROXY_PORT)
