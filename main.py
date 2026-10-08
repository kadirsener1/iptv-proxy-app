#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy (Standby Ekranlı).
- Yayın kapatıldığında 'YAYIN KAPALIDIR' video döngüsü döner.
- IPTV sağlayıcısına sıfır istek gider.
- Ultra düşük kota ve sıfır CPU tüketir.
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
ADMIN_KEY    = os.environ.get("ADMIN_KEY", "admin123")  # <-- YÖNETİCİ ŞİFRENİZ

BASE_DIR = Path(__file__).resolve().parent
LOCAL_M3U_PATH  = os.environ.get("LOCAL_M3U_PATH", str(BASE_DIR / "playlist.m3u"))
LOCAL_JSON_PATH = os.environ.get("LOCAL_JSON_PATH", str(BASE_DIR / "channels.json"))
LOG_DIR         = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))

HLS_BASE_DIR = "/tmp/iptv_hls"
STANDBY_TS_PATH = os.path.join(HLS_BASE_DIR, "standby.ts")

HLS_TIME       = 4
HLS_LIST_SIZE  = 12
IDLE_TIMEOUT   = 100
STARTUP_WAIT   = 60
FFMPEG_BIN     = "ffmpeg"

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


# ==================== STANDBY (KAPALI) EKRANI OLUŞTURUCU ====================
def generate_standby_clip():
    """1 kereliğine mikro boyutlu 'Yayın Kapalıdır' video segmenti üretir"""
    if os.path.exists(STANDBY_TS_PATH) and os.path.getsize(STANDBY_TS_PATH) > 0:
        return

    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    log.info("Standby (Yayın Kapalı) ekranı oluşturuluyor...")

    # Siyah ekran üzerine şık yazı ve sessiz ses kanalı
    cmd = [
        FFMPEG_BIN, "-y",
        "-f", "lavfi", "-i", f"color=c=black:s=1280x720:d={HLS_TIME}:r=25",
        "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo",
        "-t", str(HLS_TIME),
        "-vf", "drawtext=text='YAYIN ŞU ANDA KAPALIDIR. Maç Saatinde Açılacaktır':fontcolor=white:fontsize=44:x=(w-text_w)/2:y=(h-text_h)/2",
        "-c:v", "libx264", "-tune", "stillimage", "-pix_fmt", "yuv420p", "-b:v", "35k",
        "-c:a", "aac", "-b:a", "16k",
        "-f", "mpegts", STANDBY_TS_PATH
    ]
    try:
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except Exception as e:
        log.warning(f"Standby klibi oluşturulamadı: {e}")


# ==================== YEREL DOSYALARI GÜNCELLE ====================
def update_local_files(external_url=None):
    proxy_url = external_url or os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PROXY_PORT}")
    os.makedirs(os.path.dirname(LOCAL_JSON_PATH), exist_ok=True)
    os.makedirs(os.path.dirname(LOCAL_M3U_PATH), exist_ok=True)

    # channels.json
    existing_json = []
    if os.path.exists(LOCAL_JSON_PATH):
        try:
            with open(LOCAL_JSON_PATH, "r", encoding="utf-8") as f:
                existing_json = json.load(f)
        except Exception:
            existing_json = []

    deleted_lowers = [d.strip().lower() for d in DELETED_CHANNELS]
    existing_json = [
        item for item in existing_json
        if item.get("id", "").strip().lower() not in deleted_lowers
        and item.get("name", "").strip().lower() not in deleted_lowers
    ]

    proxy_map = {ch["name"].strip().lower(): f"{proxy_url}/live/{ch['id']}.m3u8" for ch in KANALLAR}
    json_matched = set()

    for item in existing_json:
        item_name = item.get("name", "").strip().lower()
        if item_name in proxy_map:
            ch_obj = next(c for c in KANALLAR if c["name"].strip().lower() == item_name)
            item["url"]   = proxy_map[item_name]
            item["group"] = ch_obj["group"]
            item["logo"]  = ch_obj["logo"]
            json_matched.add(item_name)

    for ch in KANALLAR:
        if ch["name"].strip().lower() not in json_matched:
            existing_json.append({
                "id": ch["id"],
                "name": ch["name"],
                "group": ch["group"],
                "logo": ch["logo"],
                "url": f"{proxy_url}/live/{ch['id']}.m3u8"
            })

    try:
        with open(LOCAL_JSON_PATH, "w", encoding="utf-8") as f:
            json.dump(existing_json, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"JSON güncelleme hatası: {e}")


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

    def _prepare_dir(self):
        if os.path.isdir(self.dir):
            shutil.rmtree(self.dir, ignore_errors=True)
        os.makedirs(self.dir, exist_ok=True)

    def _build_cmd(self):
        m3u8_path = os.path.join(self.dir, "index.m3u8")
        seg_pattern = os.path.join(self.dir, "seg_%05d.ts")

        cmd = [
            FFMPEG_BIN,
            "-hide_banner",
            "-loglevel", "warning",
            "-nostdin",
            "-rw_timeout", "15000000",
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
            "-user_agent", "VLC/3.0.18 LibVLC/3.0.18",
            "-i", self.src,
            "-c", "copy",
            "-f", "hls",
            "-hls_time", str(HLS_TIME),
            "-hls_list_size", str(HLS_LIST_SIZE),
            "-hls_flags", "delete_segments+append_list+omit_endlist+independent_segments",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", seg_pattern,
            "-hls_allow_cache", "1",
            m3u8_path
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
            log.info(f"[{self.id}] FFmpeg başlatılıyor...")
            self.proc = subprocess.Popen(
                cmd,
                stdout=ff_log,
                stderr=ff_log,
                stdin=subprocess.DEVNULL,
                start_new_session=True
            )
            self.started_at = time.time()

    async def stop(self):
        async with self.lock:
            if not self.proc:
                return
            if self.proc.poll() is None:
                log.info(f"[{self.id}] FFmpeg durduruluyor.")
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
        self.streams: dict[str, ChannelStream] = {
            ch["id"]: ChannelStream(ch) for ch in KANALLAR
        }

    def get(self, cid: str) -> ChannelStream | None:
        return self.streams.get(cid)

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
                    log.warning(f"[{cid}] FFmpeg ölmüş, yeniden başlatılıyor")
                    await st.start()


manager = StreamManager()


# ==================== HTTP HANDLER'LAR ====================
async def handle_m3u8(request):
    cid = request.match_info.get("channel_id")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Yok", headers=CORS_HEADERS)

    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    dynamic_proxy_url = f"{scheme}://{host}"

    # --- KANAL KAPALIYSA: STANDBY CANLI DÖNGÜ LİSTESİ DÖNDÜR ---
    if not st.enabled:
        seq = int(time.time() // HLS_TIME)
        standby_lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:3",
            f"#EXT-X-TARGETDURATION:{HLS_TIME}",
            f"#EXT-X-MEDIA-SEQUENCE:{seq}",
            f"#EXTINF:{HLS_TIME}.000,",
            f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq}",
            f"#EXTINF:{HLS_TIME}.000,",
            f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 1}",
            f"#EXTINF:{HLS_TIME}.000,",
            f"{dynamic_proxy_url}/hls/standby/seg.ts?seq={seq + 2}",
        ]
        return web.Response(
            text="\n".join(standby_lines),
            content_type="application/vnd.apple.mpegurl",
            headers={**CORS_HEADERS, "Cache-Control": "no-cache"}
        )

    # --- KANAL AÇIKSA: NORMAL CANLI YAYINI DÖNDÜR ---
    st.touch()
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
        if not s:
            out_lines.append("")
        elif s.startswith("#"):
            out_lines.append(s)
        else:
            seg_name = s.split("?")[0].split("/")[-1]
            out_lines.append(f"{dynamic_proxy_url}/hls/{cid}/{seg_name}")

    return web.Response(
        text="\n".join(out_lines),
        content_type="application/vnd.apple.mpegurl",
        headers={**CORS_HEADERS, "Cache-Control": "no-cache"}
    )


async def handle_segment(request):
    cid = request.match_info.get("channel_id")
    name = request.match_info.get("name")

    if not re.fullmatch(r"[A-Za-z0-9_\-\.]+\.(ts|m4s|mp4|aac|key)", name):
        return web.Response(status=400, headers=CORS_HEADERS)

    st = manager.get(cid)
    if not st:
        return web.Response(status=404, headers=CORS_HEADERS)

    st.touch()
    seg_path = os.path.join(st.dir, name)
    if not os.path.exists(seg_path):
        await asyncio.sleep(0.3)
        if not os.path.exists(seg_path):
            return web.Response(status=404, headers=CORS_HEADERS)

    try:
        return web.FileResponse(
            seg_path,
            headers={
                **CORS_HEADERS,
                "Cache-Control": "public, max-age=6",
                "Content-Type": "video/mp2t"
            }
        )
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)


async def handle_standby_segment(request):
    """Yayın kapalıyken döngüye giren tek 15 KB'lık mini segmenti sunar"""
    if not os.path.exists(STANDBY_TS_PATH):
        generate_standby_clip()

    if not os.path.exists(STANDBY_TS_PATH):
        return web.Response(status=404, headers=CORS_HEADERS)

    return web.FileResponse(
        STANDBY_TS_PATH,
        headers={
            **CORS_HEADERS,
            "Cache-Control": "public, max-age=4",
            "Content-Type": "video/mp2t"
        }
    )


async def handle_health(request):
    status = {}
    for cid, st in manager.streams.items():
        status[cid] = {
            "enabled": st.enabled,
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "last_request": st.last_request,
            "uptime": time.time() - st.started_at if st.started_at else 0
        }
    return web.json_response(status, headers=CORS_HEADERS)


# ==================== YÖNETİCİ PANELİ ====================
ADMIN_HTML = """
<!DOCTYPE html>
<html lang="tr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>IPTV Kontrol Paneli</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: #0f172a; color: #f8fafc; padding: 20px; max-width: 600px; margin: auto; }
        .card { background: #1e293b; padding: 15px; border-radius: 12px; margin-bottom: 15px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
        h2 { margin-top: 0; color: #38bdf8; }
        .btn { padding: 10px 18px; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; color: white; transition: 0.2s; }
        .btn-on { background: #22c55e; }
        .btn-off { background: #ef4444; }
        .status-badge { display: inline-block; padding: 4px 8px; border-radius: 6px; font-size: 12px; font-weight: bold; }
        .badge-active { background: #15803d; }
        .badge-disabled { background: #b91c1c; }
        input[type=password] { padding: 8px; border-radius: 6px; border: 1px solid #475569; background: #334155; color: white; width: 100%; box-sizing: border-box; margin-bottom: 15px; }
    </style>
</head>
<body>
    <h2>📺 IPTV Yayın Kontrolü</h2>
    <div class="card">
        <label>Yönetici Şifresi:</label>
        <input type="password" id="adminKey" value="admin123" placeholder="Şifrenizi girin">
    </div>
    <div id="channels"></div>

    <script>
        async function loadStatus() {
            const res = await fetch('/health');
            const data = await res.json();
            const container = document.getElementById('channels');
            container.innerHTML = '';

            for (const [id, info] of Object.entries(data)) {
                const card = document.createElement('div');
                card.className = 'card';
                card.innerHTML = `
                    <div style="display:flex; justify-content:space-between; align-items:center;">
                        <div>
                            <h3 style="margin:0 0 5px 0;">${id}</h3>
                            <span class="status-badge ${info.enabled ? 'badge-active' : 'badge-disabled'}">
                                ${info.enabled ? 'YAYINDA (CANLI)' : 'KAPALI (STANDBY EKRANI)'}
                            </span>
                            <span style="font-size:12px; color:#94a3b8; margin-left:5px;">
                                ${info.running ? '(FFmpeg Aktif)' : '(FFmpeg Kapalı)'}
                            </span>
                        </div>
                        <button class="btn ${info.enabled ? 'btn-off' : 'btn-on'}" onclick="toggleChannel('${id}', ${!info.enabled})">
                            ${info.enabled ? 'YAYINI KAPAT' : 'YAYINI AÇ'}
                        </button>
                    </div>
                `;
                container.appendChild(card);
            }
        }

        async function toggleChannel(id, enable) {
            const key = document.getElementById('adminKey').value;
            const res = await fetch(`/admin/toggle?key=${encodeURIComponent(key)}&id=${encodeURIComponent(id)}&enable=${enable}`);
            if (res.ok) {
                loadStatus();
            } else {
                alert('Hata! Şifre yanlış olabilir.');
            }
        }

        loadStatus();
        setInterval(loadStatus, 5000);
    </script>
</body>
</html>
"""

async def handle_admin_page(request):
    return web.Response(text=ADMIN_HTML, content_type="text/html")

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
        log.info(f"[{cid}] Yayın KAPATILDI (Standby devreye girdi).")
    else:
        log.info(f"[{cid}] Yayın AÇILDI.")

    return web.json_response({"success": True, "id": cid, "enabled": st.enabled})


# ==================== APP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    generate_standby_clip()
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    log.info("IPTV HLS Re-stream Proxy başlatıldı.")

async def on_cleanup(app):
    for st in manager.streams.values():
        await st.stop()
    t = app.get("monitor_task")
    if t:
        t.cancel()

def make_app():
    app = web.Application()
    app.router.add_get("/", handle_health)
    app.router.add_get("/health", handle_health)
    app.router.add_get("/admin", handle_admin_page)
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
    update_local_files()
    web.run_app(make_app(), host=BIND_HOST, port=PROXY_PORT, access_log=None)
