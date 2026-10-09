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
from datetime import datetime, timezone, timedelta
from pathlib import Path
from aiohttp import web

# ==================== AYARLAR ====================
BIND_HOST    = "0.0.0.0"
PROXY_PORT   = int(os.environ.get("PORT", 8080))
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "admin123")

BASE_DIR = Path(__file__).resolve().parent
LOCAL_M3U_PATH  = os.environ.get("LOCAL_M3U_PATH", str(BASE_DIR / "playlist.m3u"))
LOCAL_JSON_PATH = os.environ.get("LOCAL_JSON_PATH", str(BASE_DIR / "channels.json"))
LOG_DIR         = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))
USAGE_FILE      = os.environ.get("USAGE_FILE", str(BASE_DIR / "bandwidth_usage.json"))
SETTINGS_FILE   = os.environ.get("SETTINGS_FILE", str(BASE_DIR / "settings.json"))
SCHEDULE_FILE   = os.environ.get("SCHEDULE_FILE", str(BASE_DIR / "schedule.json"))

HLS_BASE_DIR = "/tmp/iptv_hls"
STANDBY_TS_PATH = os.path.join(HLS_BASE_DIR, "standby.ts")

HLS_TIME       = 4
HLS_LIST_SIZE  = 12
IDLE_TIMEOUT   = 100
STARTUP_WAIT   = 60
FFMPEG_BIN     = "ffmpeg"
APP_START_TIME = time.time()

TR_TZ = timezone(timedelta(hours=3))

DEFAULT_STANDBY_MESSAGE = "YAYIN SU ANDA KAPALIDIR\nMac Saatinde Acilacaktir"


# ==================== KALICI AYLIK KOTA TAKİPÇİSİ ====================
class BandwidthTracker:
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
        while True:
            await asyncio.sleep(10)
            if self.dirty:
                self.save()

tracker = BandwidthTracker(USAGE_FILE)


# ==================== GENEL AYARLAR ====================
class SettingsManager:
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {"standby_message": DEFAULT_STANDBY_MESSAGE}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    loaded = json.load(f)
                    self.data.update(loaded)
            except Exception:
                pass

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"Settings kaydedilemedi: {e}")

    def get_standby_message(self):
        return self.data.get("standby_message", DEFAULT_STANDBY_MESSAGE)

    def set_standby_message(self, msg: str):
        self.data["standby_message"] = msg
        self.save()

settings = SettingsManager(SETTINGS_FILE)


# ==================== OTOMATİK ZAMANLAMA ====================
class ScheduleManager:
    """
    Her kanal için birden fazla zamanlama destekler.
    schedule.json formatı:
    {
       "futbol_tv": [
           {"day": "mon", "start": "20:00", "end": "23:00"},
           {"day": "wed", "start": "19:00", "end": "22:00"},
           {"day": "all", "start": "21:00", "end": "23:30"}
       ],
       ...
    }
    """
    def __init__(self, filepath):
        self.filepath = filepath
        self.data = {}
        self.load()

    def load(self):
        if os.path.exists(self.filepath):
            try:
                with open(self.filepath, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
            except Exception:
                self.data = {}

    def save(self):
        try:
            with open(self.filepath, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            log.warning(f"Schedule kaydedilemedi: {e}")

    def get_list(self, cid: str) -> list:
        val = self.data.get(cid, [])
        if isinstance(val, dict):
            return [val] if val.get("start") else []
        return val if isinstance(val, list) else []

    def set_list(self, cid: str, entries: list):
        self.data[cid] = entries
        self.save()

    def add_entry(self, cid: str, day: str, start: str, end: str):
        entries = self.get_list(cid)
        entries.append({"day": day or "all", "start": start, "end": end})
        self.data[cid] = entries
        self.save()

    def remove_entry(self, cid: str, index: int):
        entries = self.get_list(cid)
        if 0 <= index < len(entries):
            entries.pop(index)
        self.data[cid] = entries
        self.save()

    def all(self) -> dict:
        return self.data

scheduler = ScheduleManager(SCHEDULE_FILE)


# ==================== KANALLAR ====================
DEFAULT_KANALLAR = [
    {
        "id": "Sportv1",
        "name": "NEXT",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://nexttr.xyz:8080/live/AbdLk@16729@/V9qK3nRw52La/774257.m3u8"
    },
    {
        "id": "sportv_yedek",
        "name": "SBOX",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "https://vavoo.to/vavoo-iptv/play/1629878879d81db9a9baa0"
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
def generate_standby_clip(force=False):
    if os.path.exists(STANDBY_TS_PATH) and os.path.getsize(STANDBY_TS_PATH) > 0 and not force:
        return
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    if force and os.path.exists(STANDBY_TS_PATH):
        try:
            os.remove(STANDBY_TS_PATH)
        except Exception:
            pass

    raw_msg = settings.get_standby_message()
    # drawtext için güvenli hale getir
    safe_msg = raw_msg.replace("'", "").replace(":", "\\:")
    # \n -> gerçek satır atlama
    if "\\n" not in safe_msg and "\n" in safe_msg:
        safe_msg = safe_msg.replace("\n", "\\n")

    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", "color=c=black:s=1280x720:d={}:r=25".format(HLS_TIME),
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", "drawtext=text='{}':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2".format(safe_msg),
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-b:v", "35k",
        "-c:a", "aac", "-b:a", "16k",
        "-f", "mpegts", STANDBY_TS_PATH
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, timeout=20)
        if result.returncode == 0:
            log.info("Standby klibi basariyla olusturuldu.")
        else:
            log.warning(f"Standby klibi FFmpeg hata kodu: {result.returncode}")
            # Fallback: basit mesajla tekrar dene
            fallback_cmd = [
                FFMPEG_BIN, "-y",
                "-f", "lavfi", "-i", "color=c=black:s=1280x720:d={}:r=25".format(HLS_TIME),
                "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
                "-t", str(HLS_TIME),
                "-vf", "drawtext=text='YAYIN KAPALIDIR':fontcolor=white:fontsize=48:x=(w-text_w)/2:y=(h-text_h)/2",
                "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-b:v", "35k",
                "-c:a", "aac", "-b:a", "16k",
                "-f", "mpegts", STANDBY_TS_PATH
            ]
            subprocess.run(fallback_cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
    except Exception as e:
        log.warning(f"Standby klibi olusturulamadi: {e}")


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
            log.info(f"Kanal URL guncellendi: {cid} -> {new_url}")
        except Exception as e:
            log.error(f"Kanallar JSON dosyasina yazilamadi: {e}")
        if was_running and st.enabled:
            await st.start()
        return True

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for cid, st in self.streams.items():
                if not st.enabled and st.is_alive():
                    await st.stop()
                elif st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                    await st.stop()
                elif (not st.is_alive()) and st.enabled and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                    await st.start()

    async def scheduler_loop(self):
        day_map = {0: "mon", 1: "tue", 2: "wed", 3: "thu", 4: "fri", 5: "sat", 6: "sun"}
        while True:
            try:
                now = datetime.now(TR_TZ)
                current_day = day_map.get(now.weekday(), "mon")
                current_hm = now.strftime("%H:%M")

                for cid, st in self.streams.items():
                    entries = scheduler.get_list(cid)
                    if not entries:
                        continue

                    should_be_on = False
                    for entry in entries:
                        eday = entry.get("day", "all")
                        if eday != "all" and eday != current_day:
                            continue
                        estart = entry.get("start", "")
                        eend = entry.get("end", "")
                        if estart and eend and estart <= current_hm < eend:
                            should_be_on = True
                            break

                    # Sadece zamanlama varsa otomatik aç/kapat
                    has_active_schedule = any(e.get("start") and e.get("end") for e in entries)
                    if not has_active_schedule:
                        continue

                    if should_be_on and not st.enabled:
                        st.enabled = True
                        st.touch()
                        await st.start()
                        log.info(f"Zamanlayici: {cid} otomatik ACILDI ({current_hm})")
                    elif not should_be_on and st.enabled and has_active_schedule:
                        st.enabled = False
                        await st.stop()
                        log.info(f"Zamanlayici: {cid} otomatik KAPANDI ({current_hm})")
            except Exception as e:
                log.warning(f"Scheduler hatasi: {e}")

            await asyncio.sleep(30)

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
        return web.Response(status=503, text="Yayin baslatilamadi.", headers=CORS_HEADERS)

    pl = st.playlist_path()
    if not os.path.exists(pl):
        return web.Response(status=503, text="Yayin hazirlaniyor...", headers=CORS_HEADERS)

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


def format_uptime(seconds: int) -> str:
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    mins = (seconds % 3600) // 60
    parts = []
    if days > 0:
        parts.append(f"{days}g")
    if hours > 0 or days > 0:
        parts.append(f"{hours}sa")
    parts.append(f"{mins}dk")
    return " ".join(parts)


async def handle_health(request):
    uptime_sec = int(time.time() - APP_START_TIME)
    status = {
        "server": {
            "uptime_seconds": uptime_sec,
            "uptime_pretty": format_uptime(uptime_sec),
            "ram_usage_mb": get_memory_usage_mb(),
            "total_served_mb": round(tracker.bytes_used / (1024 * 1024), 2),
            "total_served_gb": round(tracker.bytes_used / (1024 * 1024 * 1024), 3),
            "total_viewers": manager.total_viewers(),
            "server_time_tr": datetime.now(TR_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "standby_message": settings.get_standby_message()
        },
        "channels": {}
    }
    for cid, st in manager.streams.items():
        sched_list = scheduler.get_list(cid)
        status["channels"][cid] = {
            "name": st.ch.get("name", cid),
            "url": st.src,
            "enabled": st.enabled,
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "viewers": st.get_viewer_count(),
            "schedules": sched_list
        }
    return web.json_response(status, headers=NO_CACHE_HEADERS)


# ==================== YÖNETİCİ PANELİ ====================
DAY_LABELS = '{"all":"Her gün","mon":"Pazartesi","tue":"Salı","wed":"Çarşamba","thu":"Perşembe","fri":"Cuma","sat":"Cumartesi","sun":"Pazar"}'

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IPTV Kontrol Paneli</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 720px; margin: auto; }
        .card { background: #1e293b; padding: 15px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        .stats-grid-4 { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 15px; }
        @media(min-width: 600px) { .stats-grid-4 { grid-template-columns: repeat(4, 1fr); } }
        .stat-box { background: #334155; padding: 12px; border-radius: 8px; text-align: center; }
        .stat-val { font-size: 18px; font-weight: bold; color: #38bdf8; }
        .stat-lbl { font-size: 11px; color: #94a3b8; margin-top: 4px; }
        h2 { color: #38bdf8; margin-top: 0; }
        .btn { padding: 10px 18px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; transition: 0.2s; font-size: 13px; }
        .btn-sm { padding: 6px 12px; font-size: 11px; }
        .btn-on { background: #22c55e; }
        .btn-off { background: #ef4444; }
        .btn-save { background: #3b82f6; }
        .btn-danger { background: #dc2626; }
        .btn-purple { background: #7c3aed; }
        .btn-logout { background: #475569; font-size: 11px; padding: 6px 12px; margin-top: 10px; }
        .status-badge { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 11px; font-weight: bold; }
        .badge-active { background: #15803d; }
        .badge-disabled { background: #b91c1c; }
        .badge-viewer { background: #0369a1; color: #e0f2fe; }
        .badge-ffmpeg-on { background: #0284c7; color: white; }
        .badge-ffmpeg-off { background: #64748b; color: #cbd5e1; }
        .badge-sched-on { background: #7c3aed; color: white; }
        .badge-sched-off { background: #475569; color: #e2e8f0; }
        input[type=password] { padding: 12px; border-radius: 8px; border: 1px solid #475569; background: #1e293b; color: white; width: 100%; box-sizing: border-box; margin-bottom: 12px; font-size: 16px; text-align: center; }
        #loginArea { max-width: 400px; margin: 100px auto; text-align: center; }
        .edit-group { margin-top: 12px; border-top: 1px solid #334155; padding-top: 10px; display: flex; gap: 8px; }
        .edit-input { flex: 1; padding: 8px 10px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 13px; }
        .toast { position: fixed; top: 20px; left: 50%; transform: translateX(-50%); background: #16a34a; color: white; padding: 12px 24px; border-radius: 8px; z-index: 9999; display: none; box-shadow: 0 4px 12px rgba(0,0,0,0.4); font-size: 14px; }
        textarea.msg-area { width: 100%; box-sizing: border-box; padding: 10px; border-radius: 8px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 13px; resize: vertical; min-height: 60px; }
        .sched-section { margin-top: 12px; border-top: 1px dashed #334155; padding-top: 10px; }
        .sched-entry { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 6px; align-items: center; margin-bottom: 6px; }
        .sched-entry select, .sched-entry input[type=time] { padding: 6px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 12px; }
        .sched-add-row { display: grid; grid-template-columns: 1fr 1fr 1fr auto; gap: 6px; align-items: center; margin-top: 6px; }
        .sched-add-row select, .sched-add-row input[type=time] { padding: 6px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: #cbd5e1; font-size: 12px; }
        .sched-lbl { font-size: 11px; color: #94a3b8; font-weight: bold; margin-bottom: 6px; display: block; }
    </style>
</head>
<body>
    <div id="toastMsg" class="toast"></div>

    <div id="loginArea" class="card">
        <h2>🔒 Yönetici Girişi</h2>
        <p style="color: #94a3b8; font-size: 13px; margin-bottom: 15px;">Lütfen devam etmek için şifrenizi girin.</p>
        <input type="password" id="adminPassword" placeholder="Şifre" onkeypress="if(event.key==='Enter')attemptLogin()">
        <button class="btn btn-on" style="width: 100%;" onclick="attemptLogin()">Giriş Yap</button>
    </div>

    <div id="panelArea" style="display: none;">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <h2>📊 Sunucu Durumu</h2>
            <button class="btn btn-logout" onclick="logout()">Çıkış Yap</button>
        </div>

        <div class="stats-grid-4">
            <div class="stat-box"><div class="stat-val" id="totalViewers" style="color:#a855f7;">0</div><div class="stat-lbl">Canlı İzleyici</div></div>
            <div class="stat-box"><div class="stat-val" id="serverUptime" style="color:#fbbf24;">0 dk</div><div class="stat-lbl">Sunucu Uptime</div></div>
            <div class="stat-box"><div class="stat-val" id="servedGb">0 GB</div><div class="stat-lbl">Aylık Kota</div></div>
            <div class="stat-box"><div class="stat-val" id="ramMb">0 MB</div><div class="stat-lbl">RAM</div></div>
        </div>

        <div class="card">
            <h2>💬 Yayın Kapalı Ekran Mesajı</h2>
            <p style="color:#94a3b8;font-size:12px;margin-bottom:8px;">
                Satır atlamak için <b>\\n</b> kullanın. Sunucu Saati (TR): <span id="serverTime" style="color:#fbbf24;">--</span>
            </p>
            <textarea id="standbyMsg" class="msg-area"></textarea>
            <div style="margin-top:8px"><button class="btn btn-save" style="width:100%" onclick="saveStandbyMessage()">💾 Mesajı Kaydet</button></div>
        </div>

        <h2>📺 Yayın Kontrolü</h2>
        <div id="channels"></div>
    </div>

<script>
const DAY_LABELS = """ + DAY_LABELS + """;
const DAY_OPTIONS = Object.entries(DAY_LABELS).map(([v,l])=>`<option value="${v}">${l}</option>`).join('');
let updateInterval = null;
let standbyMsgFocused = false;

function showToast(msg, color) {
    const t = document.getElementById('toastMsg');
    t.innerText = msg; t.style.background = color || '#16a34a';
    t.style.display = 'block';
    setTimeout(() => { t.style.display = 'none'; }, 2500);
}
function getKey() { return localStorage.getItem("admin_key") || ""; }

async function verifyKey(key) {
    try { const r = await fetch('/admin/verify?key='+encodeURIComponent(key)+'&_='+Date.now(), {cache:'no-store'}); if(r.ok){const d=await r.json(); return d.valid;} } catch(e){} return false;
}
async function attemptLogin() {
    const key = document.getElementById('adminPassword').value;
    if (await verifyKey(key)) { localStorage.setItem("admin_key", key); showPanel(); } else { alert('Şifre Hatalı!'); }
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
    const ma = document.getElementById('standbyMsg');
    ma.addEventListener('focus', ()=>{standbyMsgFocused=true;});
    ma.addEventListener('blur', ()=>{standbyMsgFocused=false;});
    await loadStatus();
    if (updateInterval) clearInterval(updateInterval);
    updateInterval = setInterval(loadStatus, 3000);
}
function isCardFocused(card) { const a=document.activeElement; return a && card.contains(a); }

async function loadStatus() {
    try {
        const res = await fetch('/health?_='+Date.now(), {cache:'no-store'});
        if(!res.ok) return;
        const data = await res.json();
        document.getElementById('totalViewers').innerText = data.server.total_viewers + " Kişi";
        document.getElementById('servedGb').innerText = data.server.total_served_gb + " GB";
        document.getElementById('ramMb').innerText = data.server.ram_usage_mb + " MB";
        document.getElementById('serverUptime').innerText = data.server.uptime_pretty || "0 dk";
        document.getElementById('serverTime').innerText = data.server.server_time_tr || "--";
        if(!standbyMsgFocused) {
            const ma = document.getElementById('standbyMsg');
            if(ma.value !== (data.server.standby_message||"")) ma.value = data.server.standby_message||"";
        }
        const container = document.getElementById('channels');
        for (const [id, info] of Object.entries(data.channels)) {
            let card = document.getElementById('card_'+id);
            if(!card) { card=document.createElement('div'); card.id='card_'+id; card.className='card'; container.appendChild(card); }
            const focused = isCardFocused(card);
            const scheds = info.schedules || [];
            const hasSched = scheds.length > 0;
            if(!card.querySelector('.edit-input')) {
                let schedHtml = '';
                scheds.forEach((s,i) => {
                    schedHtml += `<div class="sched-entry" data-idx="${i}"><span style="font-size:12px;color:#e2e8f0;">${DAY_LABELS[s.day]||s.day}</span><span style="font-size:12px;color:#e2e8f0;">${s.start||'?'}</span><span style="font-size:12px;color:#e2e8f0;">${s.end||'?'}</span><button class="btn btn-danger btn-sm" onclick="removeSched('${id}',${i})">Sil</button></div>`;
                });
                card.innerHTML = `
                    <div style="display:flex;justify-content:space-between;align-items:center;">
                        <div>
                            <h3 style="margin:0 0 5px 0;color:#f8fafc;" class="ch-title">${info.name||id}</h3>
                            <div style="display:flex;gap:5px;align-items:center;flex-wrap:wrap;margin-bottom:6px;">
                                <span class="status-badge badge-state ${info.enabled?'badge-active':'badge-disabled'}">${info.enabled?'YAYINDA':'KAPALI'}</span>
                                <span class="status-badge badge-viewer badge-vc">👥 ${info.viewers} İzleyici</span>
                                <span class="status-badge badge-ff ${info.running?'badge-ffmpeg-on':'badge-ffmpeg-off'}">${info.running?'● FFmpeg Aktif':'○ FFmpeg Kapalı'}</span>
                                <span class="status-badge badge-sc ${hasSched?'badge-sched-on':'badge-sched-off'}">⏰ ${hasSched?scheds.length+' Zamanlama':'Manuel'}</span>
                            </div>
                        </div>
                        <button class="btn btn-tog ${info.enabled?'btn-off':'btn-on'}" onclick="toggleChannel('${id}',${!info.enabled})">${info.enabled?'KAPAT':'AÇ'}</button>
                    </div>
                    <div class="edit-group">
                        <input type="text" id="url_${id}" class="edit-input" value="${info.url}" placeholder="Yayın Linki">
                        <button class="btn btn-save btn-sm" onclick="updateUrl('${id}')">Kaydet</button>
                    </div>
                    <div class="sched-section">
                        <span class="sched-lbl">📅 Zamanlama (TR Saati):</span>
                        <div class="sched-list">${schedHtml || '<div style="font-size:12px;color:#64748b;">Zamanlama yok</div>'}</div>
                        <div class="sched-add-row">
                            <select id="newDay_${id}">${DAY_OPTIONS}</select>
                            <input type="time" id="newStart_${id}" placeholder="Açılış">
                            <input type="time" id="newEnd_${id}" placeholder="Kapanış">
                            <button class="btn btn-purple btn-sm" onclick="addSched('${id}')">+ Ekle</button>
                        </div>
                    </div>`;
            } else {
                card.querySelector('.ch-title').innerText = info.name||id;
                const bs = card.querySelector('.badge-state');
                bs.className='status-badge badge-state '+(info.enabled?'badge-active':'badge-disabled');
                bs.innerText = info.enabled?'YAYINDA':'KAPALI';
                card.querySelector('.badge-vc').innerText = '👥 '+info.viewers+' İzleyici';
                const bf = card.querySelector('.badge-ff');
                bf.className='status-badge badge-ff '+(info.running?'badge-ffmpeg-on':'badge-ffmpeg-off');
                bf.innerText = info.running?'● FFmpeg Aktif':'○ FFmpeg Kapalı';
                const bsc = card.querySelector('.badge-sc');
                bsc.className='status-badge badge-sc '+(hasSched?'badge-sched-on':'badge-sched-off');
                bsc.innerText = '⏰ '+(hasSched?scheds.length+' Zamanlama':'Manuel');
                const bt = card.querySelector('.btn-tog');
                bt.className='btn btn-tog '+(info.enabled?'btn-off':'btn-on');
                bt.innerText = info.enabled?'KAPAT':'AÇ';
                bt.setAttribute('onclick',"toggleChannel('"+id+"',"+(!info.enabled)+")");
                if(!focused) {
                    const inp=card.querySelector('.edit-input');
                    if(inp.value!==info.url) inp.value=info.url;
                    let schedHtml='';
                    scheds.forEach((s,i)=>{
                        schedHtml+=`<div class="sched-entry" data-idx="${i}"><span style="font-size:12px;color:#e2e8f0;">${DAY_LABELS[s.day]||s.day}</span><span style="font-size:12px;color:#e2e8f0;">${s.start||'?'}</span><span style="font-size:12px;color:#e2e8f0;">${s.end||'?'}</span><button class="btn btn-danger btn-sm" onclick="removeSched('${id}',${i})">Sil</button></div>`;
                    });
                    card.querySelector('.sched-list').innerHTML = schedHtml || '<div style="font-size:12px;color:#64748b;">Zamanlama yok</div>';
                }
            }
        }
    } catch(e){}
}
async function toggleChannel(id, enable) {
    const r = await fetch('/admin/toggle?key='+encodeURIComponent(getKey())+'&id='+encodeURIComponent(id)+'&enable='+enable+'&_='+Date.now(), {cache:'no-store'});
    if(r.ok) setTimeout(loadStatus,500); else { alert('Yetkisiz!'); logout(); }
}
async function updateUrl(id) {
    const el=document.getElementById('url_'+id); const u=el.value.trim();
    if(!u){showToast("Link boş olamaz!","#dc2626");return;}
    try{
        const r=await fetch('/admin/update_url?key='+encodeURIComponent(getKey())+'&id='+encodeURIComponent(id)+'&url='+encodeURIComponent(u)+'&_='+Date.now(),{cache:'no-store'});
        if(r.ok){const d=await r.json();el.value=d.url||u;el.blur();showToast('✅ Link güncellendi!');setTimeout(loadStatus,800);}
        else{showToast('❌ Hata!','#dc2626');if(r.status===401)logout();}
    }catch(e){showToast('❌ '+e.message,'#dc2626');}
}
async function addSched(id) {
    const day=document.getElementById('newDay_'+id).value;
    const start=document.getElementById('newStart_'+id).value;
    const end=document.getElementById('newEnd_'+id).value;
    if(!start||!end){showToast("Saat alanları boş olamaz!","#dc2626");return;}
    const p=new URLSearchParams({key:getKey(),id:id,action:'add',day:day,start:start,end:end,_:Date.now()});
    try{
        const r=await fetch('/admin/schedule?'+p.toString(),{cache:'no-store'});
        if(r.ok){showToast('⏰ Zamanlama eklendi!');document.getElementById('newStart_'+id).value='';document.getElementById('newEnd_'+id).value='';setTimeout(loadStatus,500);}
        else{showToast('❌ Hata!','#dc2626');if(r.status===401)logout();}
    }catch(e){showToast('❌ '+e.message,'#dc2626');}
}
async function removeSched(id, idx) {
    const p=new URLSearchParams({key:getKey(),id:id,action:'remove',index:idx,_:Date.now()});
    try{
        const r=await fetch('/admin/schedule?'+p.toString(),{cache:'no-store'});
        if(r.ok){showToast('🗑️ Zamanlama silindi!');setTimeout(loadStatus,500);}
        else{showToast('❌ Hata!','#dc2626');if(r.status===401)logout();}
    }catch(e){showToast('❌ '+e.message,'#dc2626');}
}
async function saveStandbyMessage() {
    const msg=document.getElementById('standbyMsg').value;
    if(!msg.trim()){showToast("Mesaj boş olamaz!","#dc2626");return;}
    const p=new URLSearchParams({key:getKey(),message:msg,_:Date.now()});
    try{
        const r=await fetch('/admin/standby_message?'+p.toString(),{cache:'no-store'});
        if(r.ok){showToast('💬 Mesaj güncellendi!');document.getElementById('standbyMsg').blur();setTimeout(loadStatus,500);}
        else{showToast('❌ Hata!','#dc2626');if(r.status===401)logout();}
    }catch(e){showToast('❌ '+e.message,'#dc2626');}
}
async function init() {
    const k=getKey();
    if(k && await verifyKey(k)){showPanel();return;}
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
    cid = request.query.get("id")
    enable = request.query.get("enable") == "true"
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadi")
    st.enabled = enable
    if not enable:
        await st.stop()
    else:
        st.touch()
        await st.start()
        await asyncio.sleep(0.5)
    return web.json_response({"success": True, "id": cid, "enabled": st.enabled}, headers=NO_CACHE_HEADERS)

async def handle_admin_update_url(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    new_url = request.query.get("url")
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadi")
    if not new_url:
        return web.Response(status=400, text="Gecersiz URL")
    success = await manager.update_channel_url(cid, new_url)
    if not success:
        return web.Response(status=500, text="Guncelleme basarisiz")
    return web.json_response({"success": True, "id": cid, "url": st.src}, headers=NO_CACHE_HEADERS)

async def handle_admin_schedule(request):
    key = request.query.get("key")
    cid = request.query.get("id")
    action = request.query.get("action", "add")
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Bulunamadi")

    time_re = re.compile(r"^\d{2}:\d{2}$")

    if action == "add":
        day = request.query.get("day", "all")
        start = request.query.get("start", "")
        end = request.query.get("end", "")
        if not start or not end:
            return web.Response(status=400, text="Saat alanlari bos olamaz")
        if not time_re.match(start) or not time_re.match(end):
            return web.Response(status=400, text="Saat HH:MM formatinda olmali")
        scheduler.add_entry(cid, day, start, end)
        log.info(f"Zamanlama eklendi: {cid} | {day} {start}-{end}")
    elif action == "remove":
        index = int(request.query.get("index", "0"))
        scheduler.remove_entry(cid, index)
        log.info(f"Zamanlama silindi: {cid} index={index}")

    return web.json_response({"success": True, "id": cid, "schedules": scheduler.get_list(cid)}, headers=NO_CACHE_HEADERS)

async def handle_admin_standby_message(request):
    key = request.query.get("key")
    message = request.query.get("message", "").strip()
    if key != ADMIN_KEY:
        return web.Response(status=401, text="Yetkisiz")
    if not message:
        return web.Response(status=400, text="Mesaj bos olamaz")
    settings.set_standby_message(message)
    try:
        generate_standby_clip(force=True)
    except Exception as e:
        log.warning(f"Standby yeniden uretilemedi: {e}")
    return web.json_response({"success": True, "message": settings.get_standby_message()}, headers=NO_CACHE_HEADERS)


# ==================== APP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    generate_standby_clip()
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    app["save_task"] = asyncio.create_task(tracker.periodic_save())
    app["scheduler_task"] = asyncio.create_task(manager.scheduler_loop())
    log.info("IPTV HLS Re-stream Proxy baslatildi.")

async def on_cleanup(app):
    tracker.save()
    for st in manager.streams.values():
        await st.stop()
    for task_name in ["monitor_task", "save_task", "scheduler_task"]:
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
    app.router.add_get("/admin/schedule", handle_admin_schedule)
    app.router.add_get("/admin/standby_message", handle_admin_standby_message)
    app.router.add_get("/live/{channel_id}.m3u8", handle_m3u8)
    app.router.add_get("/hls/standby/seg.ts", handle_standby_segment)
    app.router.add_get("/hls/{channel_id}/{name}", handle_segment)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    if shutil.which(FFMPEG_BIN) is None:
        raise SystemExit("HATA: ffmpeg bulunamadi.")
    web.run_app(make_app(), host=BIND_HOST, port=PROXY_PORT, access_log=None)
