#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy (Canlı İzleyici Sayacı & Kalıcı Aylık Kota Takibi).
Gelişmiş Gün/Saat Zamanlayıcı Destekli (Türkiye Saat Dilimi Uyumlu).
"""

import os
import re
import json
import time
import shutil
import asyncio
import subprocess
import logging
import datetime
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

# Gün Adı Eşleştirme Sözlüğü
DAY_MAP = {
    "pazartesi": 0, "monday": 0,
    "salı": 1, "tuesday": 1,
    "çarşamba": 2, "wednesday": 2,
    "perşembe": 3, "thursday": 3,
    "cuma": 4, "friday": 4,
    "cumartesi": 5, "saturday": 5,
    "pazar": 6, "sunday": 6
}

def normalize_day(day_name: str) -> str:
    """Türkçe karakterleri güvenli bir şekilde küçük harfe dönüştürür."""
    if not day_name:
        return ""
    day_name = day_name.strip()
    mapping = {
        "İ": "i", "I": "ı", "Ş": "ş", "ş": "ş",
        "Ç": "ç", "ç": "ç", "Ö": "ö", "ö": "ö",
        "Ü": "ü", "ü": "ü", "Ğ": "ğ", "ğ": "ğ"
    }
    res = []
    for char in day_name:
        res.append(mapping.get(char, char.lower()))
    return "".join(res)


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
        "schedule": []
    },
     {
        "id": "sportv_yedek",
        "name": "SBOX",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://yubsz.dnster.net/live/kadirsener1/Nf9HUKWhdrEuacCm/3264.m3u8",
        "schedule": []
    },
    {
        "id": "bein_sports_1_6817",
        "name": "BEİN SPORTS 1 (6817)",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://0e770a63.ucomist.net/iptv/3HYPASK67VVUSL/6817/index.m3u8",
        "schedule": []
    }
]

def load_dynamic_channels():
    """Kanalları JSON dosyasından yükler, yoksa varsayılanları oluşturur."""
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
def generate_standby_clip():
    if os.path.exists(STANDBY_TS_PATH) and os.path.getsize(STANDBY_TS_PATH) > 0:
        return
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={HLS_TIME}:r=25",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", "drawtext=text='YAYIN SU ANDA KAPALIDIR\\n\\nMac Saatinde Acilacaktir':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2",
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
        self.enabled = channel.get("enabled", True)
        self.schedule = channel.get("schedule", [])
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
        """Kanal URL adresini dinamik olarak günceller ve kaydeder."""
        st = self.streams.get(cid)
        if not st:
            return False
        
        was_running = st.is_alive()
        if was_running:
            await st.stop()
        
        st.src = new_url
        st.ch["url"] = new_url
        
        await save_channels_to_json()
        log.info(f"Kanal URL güncellendi: {cid} -> {new_url}")

        if was_running and st.enabled:
            await st.start()
        
        return True

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            
            # Sunucu nerede barındırılırsa barındırılsın TÜRKİYE saat dilimine (UTC+3) sabitlendi.
            tz_tr = datetime.timezone(datetime.timedelta(hours=3))
            current_dt = datetime.datetime.now(tz_tr)
            current_weekday = current_dt.weekday()  # 0=Pazartesi, 6=Pazar
            current_minutes = current_dt.hour * 60 + current_dt.minute

            for cid, st in self.streams.items():
                in_schedule_slot = False
                
                # --- OTOMATİK ZAMANLAYICI DENETİMİ ---
                if st.schedule:
                    for item in st.schedule:
                        day_str = normalize_day(str(item.get("day", "")))
                        target_weekday = DAY_MAP.get(day_str)
                        if target_weekday == current_weekday:
                            try:
                                sh, sm = map(int, item.get("start", "00:00").split(":"))
                                eh, em = map(int, item.get("end", "00:00").split(":"))
                                start_min = sh * 60 + sm
                                end_min = eh * 60 + em
                                
                                # Şimdiki zaman bu aralıkta mı?
                                if start_min <= current_minutes <= end_min:
                                    in_schedule_slot = True
                                    break
                            except Exception as e:
                                log.warning(f"Zamanlama format ayrıştırma hatası ({cid}): {e}")
                                continue
                    
                    # Eğer durum değiştiyse tetikle ve kaydet
                    if st.enabled != in_schedule_slot:
                        st.enabled = in_schedule_slot
                        log.info(f"Zamanlayıcı Tetiklendi ({cid}): Yayın otomatik olarak {'AÇILDI' if st.enabled else 'KAPATILDI'}.")
                        await save_channels_to_json()
                        if not st.enabled:
                            await st.stop()
                        else:
                            st.touch()
                            await st.start()
                
                # --- ÇALIŞMA / DURDURMA DENETİMLERİ ---
                if not st.enabled:
                    # Yayın kapalıysa FFmpeg kesinlikle kapalı kalmalı
                    if st.is_alive():
                        await st.stop()
                else:
                    # Yayın açık ise (Enabled)
                    if st.schedule and in_schedule_slot:
                        # Eğer zamanlayıcı dilimindeysek: Kesintisiz çalışmalı (Pre-start & No idle timeout)
                        if not st.is_alive():
                            await st.start()
                    else:
                        # Zamanlama tanımlanmamışsa (Manuel mod) ya da zamanlama dışı normal süreçte ise:
                        # Orijinal izleyici tabanlı otomatik durma / açılma (Idle Timeout) devrededir.
                        if st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                            await st.stop()
                        elif (not st.is_alive()) and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                            await st.start()

manager = StreamManager()


async def save_channels_to_json():
    """Tüm kanalların güncel durumunu JSON dosyasına yazar."""
    try:
        channels_data = []
        for s in manager.streams.values():
            s.ch["url"] = s.src
            s.ch["enabled"] = s.enabled
            s.ch["schedule"] = s.schedule
            channels_data.append(s.ch)
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(channels_data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log.error(f"Kanallar JSON dosyasına kaydedilemedi: {e}")


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
    status = {
        "server": {
            "uptime_seconds": int(time.time() - APP_START_TIME),
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
            "schedule": st.schedule
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
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 650px; margin: auto; }
        .card { background: #1e293b; padding: 15px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .stats-grid { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        .stat-box { background: #334155; padding: 12px; border-radius: 8px; text-align: center; }
        .stat-val { font-size: 20px; font-weight: bold; color: #38bdf8; }
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
        input[type=password] { padding: 12px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: white; width: 100%; box-sizing: border-box; margin-bottom: 12px; font-size: 16px; text-align: center; }
        #loginArea { max-width: 400px; margin: 100px auto; text-align: center; }
        .edit-group { margin-top: 12px; border-top: 1px solid #334155; padding-top: 10px; display: flex; gap: 8px; }
        .edit-input { flex: 1; padding: 8px 10px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 13px; }
        .btn-save { background: #3b82f6; font-size: 12px; padding: 6px 12px; }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #16a34a; color: white; padding: 12px 24px; border-radius: 8px; z-index: 9999; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4); }
        .sched-item { display: flex; justify-content: space-between; align-items: center; background: #0f172a; padding: 5px 10px; border-radius: 6px; font-size: 12px; margin-bottom: 4px; }
        .sched-del { color: #ef4444; cursor: pointer; font-weight: bold; padding: 0 4px; }
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
                <div class="stat-val" id="totalViewers" style="color:#a855f7;">0</div>
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

        <h2>📺 Yayın Kontrolü</h2>
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
                    const activeElement = document.activeElement;
                    const isInputFocused = (activeElement && activeElement.id === inputId);

                    if (!card.querySelector('.edit-input')) {
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
                                <input type="text" id="${inputId}" class="edit-input" value="${info.url}" placeholder="Yayın (.m3u8) Linki">
                                <button class="btn btn-save" onclick="updateChannelUrl('${id}')">Kaydet</button>
                            </div>

                            <!-- ZAMANLAYICI ALANI -->
                            <div class="schedule-section" style="margin-top: 15px; border-top: 1px dashed #475569; padding-top: 10px;">
                                <h4 style="margin: 0 0 8px 0; color: #38bdf8; font-size: 13px;">📅 Otomatik Yayın Zamanlayıcı (Çoklu Giriş Destekler)</h4>
                                <div id="sched_list_${id}" style="margin-bottom: 8px; display: flex; flex-direction: column; gap: 4px;"></div>
                                <div style="display:flex; gap: 5px; align-items:center; flex-wrap: wrap;">
                                    <select id="sched_day_${id}" style="padding: 6px; border-radius: 6px; background:#0f172a; color:white; border:1px solid #475569; font-size: 12px;">
                                        <option value="Pazartesi">Pazartesi</option>
                                        <option value="Salı">Salı</option>
                                        <option value="Çarşamba">Çarşamba</option>
                                        <option value="Perşembe">Perşembe</option>
                                        <option value="Cuma">Cuma</option>
                                        <option value="Cumartesi">Cumartesi</option>
                                        <option value="Pazar">Pazar</option>
                                    </select>
                                    <input type="text" id="sched_start_${id}" placeholder="19:00" style="width:45px; padding: 6px; border-radius: 6px; background:#0f172a; color:white; border:1px solid #475569; font-size: 12px; text-align:center;">
                                    <span style="color:#94a3b8; font-size:11px;">ile</span>
                                    <input type="text" id="sched_end_${id}" placeholder="21:30" style="width:45px; padding: 6px; border-radius: 6px; background:#0f172a; color:white; border:1px solid #475569; font-size: 12px; text-align:center;">
                                    <button class="btn btn-save" style="padding: 6px 10px; background:#10b981;" onclick="addSchedule('${id}')">Zaman Ekle</button>
                                </div>
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
                            const inp = card.querySelector('.edit-input');
                            if (inp.value !== info.url) {
                                inp.value = info.url;
                            }
                        }
                    }

                    // Zamanlayıcı Listesini Çiz
                    const listContainer = card.querySelector(`#sched_list_${id}`);
                    if (listContainer) {
                        let listHtml = "";
                        if (info.schedule && info.schedule.length > 0) {
                            info.schedule.forEach((item, index) => {
                                listHtml += `
                                    <div class="sched-item">
                                        <span>🟢 <b>${item.day}</b>: ${item.start} - ${item.end}</span>
                                        <span class="sched-del" onclick="deleteSchedule('${id}', ${index})">❌ Sil</span>
                                    </div>
                                `;
                            });
                        } else {
                            listHtml = `<div style="color:#94a3b8; font-size:11px; font-style:italic;">Zamanlama ayarlanmamış. Yayın 7/24 veya manuel kontrol edilir.</div>`;
                        }
                        listContainer.innerHTML = listHtml;
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

        async function updateChannelUrl(id) {
            const key = getStoredKey();
            const inputEl = document.getElementById(`url_${id}`);
            const newUrl = inputEl.value.trim();
            if(!newUrl) {
                showToast("Lütfen geçerli bir yayın linki girin!", "#dc2626");
                return;
            }
            
            try {
                const res = await fetch(`/admin/update_url?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&url=${encodeURIComponent(newUrl)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    const data = await res.json();
                    inputEl.value = data.url || newUrl;
                    inputEl.blur();
                    showToast('✅ Yayın linki başarıyla güncellendi!');
                    setTimeout(loadStatus, 800);
                } else {
                    showToast('❌ Hata oluştu veya yetkisiz!', '#dc2626');
                    if (res.status === 401) logout();
                }
            } catch(e) {
                showToast('❌ Bağlantı hatası: ' + e.message, '#dc2626');
            }
        }

        async function addSchedule(id) {
            const key = getStoredKey();
            const day = document.getElementById(`sched_day_${id}`).value;
            const start = document.getElementById(`sched_start_${id}`).value.trim();
            const end = document.getElementById(`sched_end_${id}`).value.trim();

            const timeRegex = /^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$/;
            if (!timeRegex.test(start) || !timeRegex.test(end)) {
                showToast("❌ Saat formatı SS:DD (örn: 19:30) olmalıdır!", "#dc2626");
                return;
            }

            try {
                const res = await fetch(`/admin/add_schedule?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&day=${encodeURIComponent(day)}&start=${encodeURIComponent(start)}&end=${encodeURIComponent(end)}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    showToast("✅ Zamanlama başarıyla eklendi!");
                    document.getElementById(`sched_start_${id}`).value = "";
                    document.getElementById(`sched_end_${id}`).value = "";
                    setTimeout(loadStatus, 500);
                } else {
                    const txt = await res.text();
                    showToast("❌ Hata: " + txt, "#dc2626");
                }
            } catch (e) {
                showToast("❌ Bağlantı hatası: " + e.message, "#dc2626");
            }
        }

        async function deleteSchedule(id, index) {
            if (!confirm("Bu zamanlamayı silmek istediğinize emin misiniz?")) return;
            const key = getStoredKey();
            try {
                const res = await fetch(`/admin/delete_schedule?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&index=${index}&_=${Date.now()}`, { cache: 'no-store' });
                if (res.ok) {
                    showToast("✅ Zamanlama silindi!");
                    setTimeout(loadStatus, 500);
                } else {
                    showToast("❌ Silme işlemi başarısız!", "#dc2626");
                }
            } catch (e) {
                showToast("❌ Bağlantı hatası: " + e.message, "#dc2626");
            }
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
    """Giriş şifresinin doğruluğunu kontrol eden endpoint"""
    key = request.query.get("key")
    if key == ADMIN_KEY:
        return web.json_response({"valid": True}, headers=NO_CACHE_HEADERS)
    return web.json_response({"valid": False}, status=401, headers=NO_CACHE_HEADERS)

async def handle_admin_toggle(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    enable = request.query.get("enable") == "true"

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    st.enabled = enable
    await save_channels_to_json()

    if not enable:
        await st.stop()
    else:
        st.touch()          
        await st.start()    
        await asyncio.sleep(0.5) 

    return web.json_response({"success": True, "id": cid, "enabled": st.enabled}, headers=NO_CACHE_HEADERS)

async def handle_admin_update_url(request):
    """Admin panelinden gelen yeni yayın URL'sini kaydeder"""
    key = request.query.get("key")
    cid = request.query.get("id")
    new_url = request.query.get("url")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    if not new_url:
        return web.Response(status=400, text="Geçersiz URL")

    success = await manager.update_channel_url(cid, new_url)
    if not success:
        return web.Response(status=500, text="Güncelleme başarısız")
    
    return web.json_response({
        "success": True, 
        "id": cid, 
        "url": st.src  
    }, headers=NO_CACHE_HEADERS)


async def handle_admin_add_schedule(request):
    """Kanala yeni bir otomatik zaman dilimi ekler"""
    key = request.query.get("key")
    cid = request.query.get("id")
    day = request.query.get("day")
    start = request.query.get("start")
    end = request.query.get("end")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    if not day or not start or not end:
        return web.Response(status=400, text="Eksik Parametre")

    time_re = re.compile(r"^(0[0-9]|1[0-9]|2[0-3]):[0-5][0-9]$")
    if not time_re.match(start) or not time_re.match(end):
        return web.Response(status=400, text="Geçersiz Saat Formatı (Örn: 19:00)")

    normalized_day_str = normalize_day(day)
    if normalized_day_str not in DAY_MAP:
        return web.Response(status=400, text="Geçersiz Gün")

    new_entry = {
        "day": day.capitalize(),
        "start": start,
        "end": end
    }
    
    st.schedule.append(new_entry)
    await save_channels_to_json()
    return web.json_response({"success": True, "schedule": st.schedule}, headers=NO_CACHE_HEADERS)


async def handle_admin_delete_schedule(request):
    """Kanaldaki kayıtlı bir zaman dilimini siler"""
    key = request.query.get("key")
    cid = request.query.get("id")
    try:
        index = int(request.query.get("index", -1))
    except ValueError:
        return web.Response(status=400, text="Geçersiz İndeks")

    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz Erişim")

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadı")

    if index < 0 or index >= len(st.schedule):
        return web.Response(status=400, text="İndeks Limit Dışı")

    st.schedule.pop(index)
    await save_channels_to_json()
    return web.json_response({"success": True, "schedule": st.schedule}, headers=NO_CACHE_HEADERS)


# ==================== APP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    generate_standby_clip()
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    app["save_task"] = asyncio.create_task(tracker.periodic_save())
    log.info("IPTV HLS Re-stream Proxy ve Otomatik Zamanlayıcı başarıyla başlatıldı.")

async def on_cleanup(app):
    tracker.save()
    for st in manager.streams.values():
        await st.stop()
    for task_name in ["monitor_task", "save_task"]:
        t = app.get(task_name)
        if t:
            t.cancel()

def make_app():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/admin", handle_admin_page)
    app.router.add_get("/admin/verify", handle_admin_verify)
    app.router.add_get("/admin/toggle", handle_admin_toggle)
    app.router.add_get("/admin/update_url", handle_admin_update_url)
    app.router.add_get("/admin/add_schedule", handle_admin_add_schedule)
    app.router.add_get("/admin/delete_schedule", handle_admin_delete_schedule)
    app.router.add_get("/live/{channel_id}.m3u8", handle_m3u8)
    app.router.add_get("/hls/standby/seg.ts", handle_standby_segment)
    app.router.add_get("/hls/{channel_id}/{name}", handle_segment)
    
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    if shutil.which(FFMPEG_BIN) is None:
        raise SystemExit("HATA: ffmpeg bulunamadı.")
    web.run_app(make_app(), host=BIND_HOST, port=PROXY_PORT, access_log=None)
