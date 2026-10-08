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
                        # Yeni aya girilmişse sıfırla
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


# ==================== KANALLAR ====================
KANALLAR = [
    {
        "id": "futbol_tv",
        "name": "FUTBOL TV",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://nexttr.xyz:8080/live/AbdLk@16729@/V9qK3nRw52La/774257.m3u8"
    },
    {
        "id": "bein_sports_1_6817",
        "name": "BEİN SPORTS 1 (6817)",
        "group": "Spor",
        "logo": "https://raw.githubusercontent.com/kadirsener1/tvmyeni/refs/heads/main/bg.JPG",
        "url": "http://0e770a63.ucomist.net/iptv/3HYPASK67VVUSL/6817/index.m3u8"
    }
]

DELETED_CHANNELS = ["bein_sports_1_6781", "BEİN SPORTS 1 (6781)", "bein sports 1 (6781)"]
CHANNELS_MAP = {ch["id"]: ch for ch in KANALLAR}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
    "Access-Control-Allow-Headers": "*",
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
        self.enabled = True
        self.viewers = {}  # {ip_adresi: son_istek_zamani}

    def record_viewer(self, ip: str):
        """İzleyicinin IP'sini kaydeder/günceller"""
        if ip and ip != "unknown":
            self.viewers[ip] = time.time()
            self.touch()

    def get_viewer_count(self) -> int:
        """Son 12 saniye içinde istek atan benzersiz kullanıcı sayısı"""
        now = time.time()
        active = [ip for ip, last_seen in self.viewers.items() if (now - last_seen) <= 12]
        # Eski IP'leri temizle
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
            "enabled": st.enabled,
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "viewers": st.get_viewer_count()
        }
    return web.json_response(status, headers=CORS_HEADERS)


# ==================== YÖNETİCİ PANELİ (İZLEYİCİ SAYACLI) ====================
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
    </style>
</head>
<body>

    <!-- GİRİŞ EKRANI -->
    <div id="loginArea" class="card">
        <h2>🔒 Yönetici Girişi</h2>
        <p style="color: #94a3b8; font-size: 13px; margin-bottom: 15px;">Lütfen devam etmek için şifrenizi girin.</p>
        <input type="password" id="adminPassword" placeholder="Şifre" onkeypress="handleKeyPress(event)">
        <button class="btn btn-on" style="width: 100%;" onclick="attemptLogin()">Giriş Yap</button>
    </div>

    <!-- PANEL ALANI (Varsayılan olarak gizli) -->
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
                const res = await fetch(`/admin/verify?key=${encodeURIComponent(key)}`);
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
                const res = await fetch('/health');
                if (!res.ok) return;
                const data = await res.json();
                
                document.getElementById('totalViewers').innerText = data.server.total_viewers + " Kişi";
                document.getElementById('servedGb').innerText = data.server.total_served_gb + " GB";
                document.getElementById('ramMb').innerText = data.server.ram_usage_mb + " MB";

                const container = document.getElementById('channels');
                container.innerHTML = '';

                for (const [id, info] of Object.entries(data.channels)) {
                    const card = document.createElement('div');
                    card.className = 'card';
                    card.innerHTML = `
                        <div style="display:flex; justify-content:space-between; align-items:center;">
                            <div>
                                <h3 style="margin:0 0 5px 0;">${id}</h3>
                                <div style="display:flex; gap: 5px; align-items:center; flex-wrap: wrap; margin-bottom: 6px;">
                                    <span class="status-badge ${info.enabled ? 'badge-active' : 'badge-disabled'}">
                                        ${info.enabled ? 'YAYINDA' : 'KAPALI'}
                                    </span>
                                    <span class="status-badge badge-viewer">
                                        👥 ${info.viewers} İzleyici
                                    </span>
                                    <span class="status-badge ${info.running ? 'badge-ffmpeg-on' : 'badge-ffmpeg-off'}">
                                        ${info.running ? '● FFmpeg Aktif' : '○ FFmpeg Kapalı'}
                                    </span>
                                </div>
                            </div>
                            <button class="btn ${info.enabled ? 'btn-off' : 'btn-on'}" onclick="toggleChannel('${id}', ${!info.enabled})">
                                ${info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ'}
                            </button>
                        </div>
                    `;
                    container.appendChild(card);
                }
            } catch(e) {}
        }

        async function toggleChannel(id, enable) {
            const key = getStoredKey();
            const res = await fetch(`/admin/toggle?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&enable=${enable}`);
            if (res.ok) {
                // Değişikliğin hemen yansıması için küçük bir bekleme ve yenileme
                setTimeout(loadStatus, 500);
            } else {
                alert('Oturum Geçersiz veya Şifre Hatalı!');
                logout();
            }
        }

        // Sayfa yüklendiğinde otomatik giriş kontrolü
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
        return web.json_response({"valid": True}, headers=CORS_HEADERS)
    return web.json_response({"valid": False}, status=401, headers=CORS_HEADERS)

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
    if not enable:
        await st.stop()
    else:
        st.touch()          # Son istek zamanını güncelle
        await st.start()    # FFmpeg sürecini anında başlat
        await asyncio.sleep(0.5) # Durumun tam oturması ve is_alive() değerinin güncellenmesi için yarım saniye gecikme payı

    return web.json_response({"success": True, "id": cid, "enabled": st.enabled})


# ==================== APP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    generate_standby_clip()
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    app["save_task"] = asyncio.create_task(tracker.periodic_save())
    log.info("IPTV HLS Re-stream Proxy başlatıldı.")

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
