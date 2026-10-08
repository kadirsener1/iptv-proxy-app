#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FFmpeg tabanlı HLS re-stream proxy.
- IPTV sağlayıcısına HER ZAMAN tek bağlantı olarak görünür.
- Segmentler RAM diskte veya /tmp dizininde tutulur.
- İzleyici yoksa N saniye sonra FFmpeg kapanır.
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
from urllib.parse import quote
from aiohttp import web

# ==================== AYARLAR ====================
# Render için HOST "0.0.0.0" olmalıdır. Port ise Render tarafından dinamik atanır.
BIND_HOST    = "0.0.0.0"
PROXY_PORT   = int(os.environ.get("PORT", 8080))

# Proje ana dizini
BASE_DIR = Path(__file__).resolve().parent

# Render uyumlu dosya yolları
LOCAL_M3U_PATH  = os.environ.get("LOCAL_M3U_PATH", str(BASE_DIR / "playlist.m3u"))
LOCAL_JSON_PATH = os.environ.get("LOCAL_JSON_PATH", str(BASE_DIR / "channels.json"))
LOG_DIR         = os.environ.get("LOG_DIR", str(BASE_DIR / "logs"))

# Render ortamı için /tmp disk kullanımı daha kararlıdır
HLS_BASE_DIR = "/tmp/iptv_hls"

HLS_TIME       = 4         # segment süresi (sn) — düşük gecikme: 2, kararlı: 4
HLS_LIST_SIZE  = 12        # m3u8'de tutulacak segment sayısı
IDLE_TIMEOUT   = 100       # izleyici yoksa FFmpeg'i kapat (sn)
STARTUP_WAIT   = 60        # yayının hazır olması için beklenecek max sn
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
    "Access-Control-Allow-Methods": "GET, OPTIONS",
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


# ==================== YEREL DOSYALARI GÜNCELLE ====================
def update_local_files(external_url=None):
    # Eğer Render dış URL'i tanımlıysa onu kullan, yoksa yerel portu kullan
    proxy_url = external_url or os.environ.get("RENDER_EXTERNAL_URL", f"http://localhost:{PROXY_PORT}")
    
    # Dizinlerin varlığından emin ol
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

    proxy_map = {ch["name"].strip().lower(): f"{proxy_url}/live/{ch['id']}.m3u8"
                 for ch in KANALLAR}
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

    # playlist.m3u
    # Eğer dosya yoksa sıfırdan oluştur
    if not os.path.exists(LOCAL_M3U_PATH):
        try:
            with open(LOCAL_M3U_PATH, "w", encoding="utf-8") as f:
                f.write("#EXTM3U\n")
        except Exception:
            pass

    if os.path.exists(LOCAL_M3U_PATH):
        try:
            with open(LOCAL_M3U_PATH, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            cleaned_lines = []
            skip_next = False
            for line in lines:
                stripped = line.strip()
                if any(d.lower() in stripped.lower() for d in DELETED_CHANNELS):
                    skip_next = True
                    continue
                if skip_next and (stripped.startswith("http") or stripped.startswith("#EXTVLCOPT")):
                    skip_next = False
                    continue
                skip_next = False

                if stripped.startswith("#EXTINF"):
                    for ch in KANALLAR:
                        if ch["name"].lower() in stripped.lower():
                            line = (f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{ch["name"]}" '
                                    f'tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}\n')
                            break
                cleaned_lines.append(line)

            # Eğer oynatma listesi boşsa temel kanalları ekle
            if len(cleaned_lines) <= 1:
                cleaned_lines = ["#EXTM3U\n"]
                for ch in KANALLAR:
                    cleaned_lines.append(
                        f'#EXTINF:-1 tvg-id="{ch["id"]}" tvg-name="{ch["name"]}" '
                        f'tvg-logo="{ch["logo"]}" group-title="{ch["group"]}",{ch["name"]}\n'
                    )
                    cleaned_lines.append(f"{proxy_url}/live/{ch['id']}.m3u8\n")

            with open(LOCAL_M3U_PATH, "w", encoding="utf-8") as f:
                f.writelines(cleaned_lines)
        except Exception as e:
            log.warning(f"M3U güncelleme hatası: {e}")


# ==================== FFMPEG YÖNETİCİSİ ====================
class ChannelStream:
    def __init__(self, channel: dict):
        self.ch     = channel
        self.id     = channel["id"]
        self.src    = channel["url"]
        self.dir    = os.path.join(HLS_BASE_DIR, self.id)
        self.proc: subprocess.Popen | None = None
        self.last_request = 0.0
        self.lock = asyncio.Lock()
        self.started_at = 0.0
        self.consecutive_fails = 0

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
        async with self.lock:
            if self.proc and self.proc.poll() is None:
                return

            self._prepare_dir()
            cmd = self._build_cmd()
            ff_log = open(os.path.join(LOG_DIR, f"{self.id}.ffmpeg.log"), "ab")
            log.info(f"[{self.id}] FFmpeg başlatılıyor (HLS_TIME={HLS_TIME}s, LIST_SIZE={HLS_LIST_SIZE})")
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
                log.info(f"[{self.id}] FFmpeg durduruluyor (idle)")
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
        self._monitor_task = None

    def get(self, cid: str) -> ChannelStream | None:
        return self.streams.get(cid)

    async def ensure_running(self, cid: str) -> ChannelStream | None:
        st = self.streams.get(cid)
        if not st:
            return None
        if not st.is_alive():
            await st.start()
        waited = 0.0
        while waited < STARTUP_WAIT:
            if st.playlist_ready():
                return st
            await asyncio.sleep(0.5)
            waited += 0.5
            if not st.is_alive():
                await asyncio.sleep(0.5)
                await st.start()
        return st

    async def monitor(self):
        while True:
            await asyncio.sleep(5)
            now = time.time()
            for cid, st in self.streams.items():
                if st.is_alive() and st.last_request and (now - st.last_request) > IDLE_TIMEOUT:
                    await st.stop()
                elif (not st.is_alive()) and st.last_request and (now - st.last_request) < IDLE_TIMEOUT:
                    log.warning(f"[{cid}] FFmpeg ölmüş, yeniden başlatılıyor")
                    await st.start()


manager = StreamManager()


# ==================== HTTP HANDLER'LAR ====================
async def handle_m3u8(request):
    cid = request.match_info.get("channel_id")
    st = manager.get(cid)
    if not st:
        return web.Response(status=404, text="Kanal Yok", headers=CORS_HEADERS)

    st.touch()
    await manager.ensure_running(cid)

    pl = st.playlist_path()
    if not os.path.exists(pl):
        return web.Response(status=503, text="Yayın hazırlanıyor...", headers=CORS_HEADERS)

    try:
        with open(pl, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return web.Response(status=500, text=str(e), headers=CORS_HEADERS)

    # Render için dinamik dış protokol ve host bilgisini al
    scheme = request.headers.get("X-Forwarded-Proto", request.url.scheme)
    host = request.headers.get("X-Forwarded-Host", request.host)
    dynamic_proxy_url = f"{scheme}://{host}"

    out_lines = []
    for line in content.splitlines():
        s = line.strip()
        if not s:
            out_lines.append("")
        elif s.startswith("#"):
            out_lines.append(s)
        else:
            seg_name = s.split("?")[0].split("/")[-1]
            # Yerel host yerine dinamik olarak dışarıdan erişilebilir URL yazılır
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


async def handle_health(request):
    status = {}
    for cid, st in manager.streams.items():
        status[cid] = {
            "running": st.is_alive(),
            "ready": st.playlist_ready(),
            "last_request": st.last_request,
            "uptime": time.time() - st.started_at if st.started_at else 0,
            "hls_time": HLS_TIME,
            "hls_list_size": HLS_LIST_SIZE,
        }
    return web.json_response(status, headers=CORS_HEADERS)


async def handle_options(request):
    return web.Response(headers=CORS_HEADERS)


# ==================== APP ====================
async def on_startup(app):
    os.makedirs(HLS_BASE_DIR, exist_ok=True)
    app["monitor_task"] = asyncio.create_task(manager.monitor())
    log.info(f"HLS dizini: {HLS_BASE_DIR}")
    log.info(f"Ayarlar: HLS_TIME={HLS_TIME}s, HLS_LIST_SIZE={HLS_LIST_SIZE}")
    log.info("IPTV HLS Re-stream Proxy başlatıldı.")


async def on_cleanup(app):
    for st in manager.streams.values():
        await st.stop()
    t = app.get("monitor_task")
    if t:
        t.cancel()


def make_app():
    app = web.Application()
    app.router.add_get("/live/{channel_id}.m3u8", handle_m3u8)
    app.router.add_get("/hls/{channel_id}/{name}", handle_segment)
    app.router.add_get("/health", handle_health)
    app.router.add_route("OPTIONS", "/{tail:.*}", handle_options)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


if __name__ == "__main__":
    if shutil.which(FFMPEG_BIN) is None:
        raise SystemExit("HATA: ffmpeg bulunamadı. Render ortamında kurulu olduğundan emin olun.")

    update_local_files()
    log.info("[*] IPTV HLS Re-stream Proxy başlatıldı: port %d", PROXY_PORT)
    web.run_app(make_app(), host=BIND_HOST, port=PROXY_PORT, access_log=None)
