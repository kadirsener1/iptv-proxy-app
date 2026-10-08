#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import logging
import asyncio
import time
import os
import urllib.parse
from aiohttp import web, ClientSession, ClientTimeout

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

LOCAL_HOST = "0.0.0.0"
# Render'ın atadığı portu otomatik okur, bulamazsa 8088 kullanır
PROXY_PORT = int(os.environ.get("PORT", 8088))

KANALLAR = {
    "futbol_tv": "http://nexttr.xyz:8080/live/AbdLk@16729@/V9qK3nRw52La/8.m3u8",
    "bein_sports_1_6817": "http://0e770a63.ucomist.net/iptv/3HYPASK67VVUSL/6817/index.m3u8"
}

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

FORWARD_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "*/*",
    "Connection": "keep-alive"
}

TS_CACHE = {}

async def cleanup_cache():
    while True:
        await asyncio.sleep(15)
        now = time.time()
        expired_keys = [k for k, v in TS_CACHE.items() if now - v[1] > 30]
        for k in expired_keys:
            del TS_CACHE[k]

async def fetch_ts_segment(session, url):
    if url in TS_CACHE:
        return TS_CACHE[url][0]
    
    try:
        async with session.get(url, headers=FORWARD_HEADERS, timeout=ClientTimeout(total=10)) as resp:
            if resp.status == 200:
                data = await resp.read()
                TS_CACHE[url] = (data, time.time())
                return data
    except Exception as e:
        logging.error(f"Segment İndirme Hatası ({url}): {e}")
    return None

async def handle_m3u8(request):
    channel_id = request.match_info.get("channel_id")
    if channel_id not in KANALLAR:
        return web.Response(status=404, text="Kanal Bulunamadı", headers=CORS_HEADERS)

    target_url = KANALLAR[channel_id]

    try:
        timeout = ClientTimeout(total=8)
        async with ClientSession(timeout=timeout) as session:
            async with session.get(target_url, headers=FORWARD_HEADERS, allow_redirects=True) as resp:
                if resp.status != 200:
                    return web.Response(status=resp.status, text="Kaynak Sunucu Hatası", headers=CORS_HEADERS)

                content = await resp.text(errors="ignore")
                base_url = str(resp.url)

                new_lines = []
                for line in content.splitlines():
                    line_str = line.strip()
                    if line_str and not line_str.startswith("#"):
                        abs_url = urllib.parse.urljoin(base_url, line_str)
                        proxy_ts_url = f"/ts_proxy?url={urllib.parse.quote(abs_url)}"
                        new_lines.append(proxy_ts_url)
                    else:
                        new_lines.append(line)

                modified_m3u8 = "\n".join(new_lines)
                
                response_headers = dict(CORS_HEADERS)
                response_headers["Content-Type"] = "application/vnd.apple.mpegurl"
                response_headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                return web.Response(text=modified_m3u8, headers=response_headers)

    except Exception as e:
        logging.error(f"[{channel_id}] M3U8 Hatası: {e}")
        return web.Response(status=502, text=str(e), headers=CORS_HEADERS)

async def handle_ts_proxy(request):
    raw_url = request.query.get("url")
    if not raw_url:
        return web.Response(status=400, text="URL Eksik", headers=CORS_HEADERS)

    target_url = urllib.parse.unquote(raw_url)

    if target_url in TS_CACHE:
        data = TS_CACHE[target_url][0]
    else:
        async with ClientSession() as session:
            data = await fetch_ts_segment(session, target_url)

    if data:
        response_headers = dict(CORS_HEADERS)
        response_headers["Content-Type"] = "video/mp2t"
        response_headers["Cache-Control"] = "public, max-age=60"
        return web.Response(body=data, headers=response_headers)
    else:
        return web.Response(status=502, text="Segment Çekilemedi", headers=CORS_HEADERS)

def make_app():
    app = web.Application()
    app.router.add_get("/live/{channel_id}.m3u8", handle_m3u8)
    app.router.add_get("/ts_proxy", handle_ts_proxy)
    return app

if __name__ == "__main__":
    app = make_app()
    loop = asyncio.get_event_loop()
    loop.create_task(cleanup_cache())
    logging.info(f"[*] Single-Source Stream Caching Proxy Başlatıldı: Port {PROXY_PORT}")
    web.run_app(app, host=LOCAL_HOST, port=PROXY_PORT)
