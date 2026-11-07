"""Mini Screen server: turns a phone into a PC companion display.

Binds to 127.0.0.1 only. The phone reaches it through `adb reverse`
(USB), so nothing is exposed to the network. Every API call needs a
random per-run token.
"""
import asyncio
import ctypes
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import psutil
from aiohttp import web

BASE = Path(__file__).parent
def load_cfg():
    """Re-read config.json each time so edits apply without restarting."""
    return json.loads((BASE / "config.json").read_text(encoding="utf-8"))


CFG = load_cfg()
PORT = int(CFG.get("port", 8765))
HOST = CFG.get("host", "127.0.0.1")
TOKEN_FILE = BASE / ".token"


def load_token():
    """Persistent token so a bookmark on the phone keeps working across restarts."""
    try:
        t = TOKEN_FILE.read_text().strip()
        if len(t) >= 16:
            return t
    except OSError:
        pass
    t = secrets.token_urlsafe(24)
    TOKEN_FILE.write_text(t)
    return t


TOKEN = load_token()
LOCAL_IPS = {"127.0.0.1", "::1"}

BANDS = 32
SPECTRUM = np.zeros(BANDS)
STATE = {"media": None, "stats": {}, "vol": {"level": 0, "muted": False}}
ART_CACHE = {}      # key -> bytes
LYRICS_CACHE = {}   # key -> list | None


# ---------------------------------------------------------------- audio spectrum
def spectrum_thread():
    global SPECTRUM
    try:
        import pyaudiowpatch as pyaudio
        p = pyaudio.PyAudio()
        wasapi = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        spk = p.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if not spk.get("isLoopbackDevice"):
            for lb in p.get_loopback_device_info_generator():
                if spk["name"] in lb["name"]:
                    spk = lb
                    break
        ch = int(spk["maxInputChannels"])
        rate = int(spk["defaultSampleRate"])
        n = 4096  # finer frequency resolution so low bands are not empty
        win = np.hanning(n)
        # 60 Hz - 14 kHz: below ~60 Hz is rumble/DC, above ~14 kHz most music (and MP3/AAC) has nothing.
        edges = np.geomspace(60, 14000, BANDS + 1)
        freqs = np.fft.rfftfreq(n, 1 / rate)
        idx = []
        for i in range(BANDS):
            ix = np.where((freqs >= edges[i]) & (freqs < edges[i + 1]))[0]
            if len(ix) == 0:  # narrow low band with no FFT bin: use the nearest bin instead of leaving it dead
                ix = np.array([int(np.argmin(np.abs(freqs - np.sqrt(edges[i] * edges[i + 1]))))])
            idx.append(ix)
        centers = np.sqrt(edges[:-1] * edges[1:])
        tilt = 3.0 * np.log2(centers / 1000.0)  # music rolls off ~3 dB/octave; compensate so highs move too
        buf = np.zeros(n)
        ref = -30.0  # slow auto-gain reference (dB) so it looks lively at any volume

        def cb(data, frames, t, status):
            global SPECTRUM
            global_buf = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            mono = global_buf.reshape(-1, ch).mean(axis=1)
            nonlocal buf, ref
            buf = np.concatenate([buf, mono])[-n:]
            mag = np.abs(np.fft.rfft(buf * win)) / (n / 4)
            out = np.array([mag[ix].max() for ix in idx])
            db = 20 * np.log10(out + 1e-6) + tilt
            if db.max() < -85:  # silence: let the bars fall
                SPECTRUM = SPECTRUM * 0.8
                return (None, pyaudio.paContinue)
            ref = max(db.max(), ref - 0.3)  # follows the loudest band, decays ~14 dB/s
            v = np.clip((db - (ref - 45)) / 45, 0, 1)
            SPECTRUM = np.maximum(v, SPECTRUM * 0.82)
            return (None, pyaudio.paContinue)

        stream = p.open(format=pyaudio.paInt16, channels=ch, rate=rate, input=True,
                        frames_per_buffer=1024, input_device_index=spk["index"],
                        stream_callback=cb)
        stream.start_stream()
        while stream.is_active():
            time.sleep(1)
    except Exception as e:  # visualizer is optional
        print(f"[visualizer disabled] {e!r}", file=sys.stderr)


# ---------------------------------------------------------------- volume (COM thread)
def _com_init():
    import comtypes
    comtypes.CoInitialize()


COM_POOL = ThreadPoolExecutor(1, initializer=_com_init)
_endpoint = None


def _vol_endpoint():
    global _endpoint
    if _endpoint is None:
        from pycaw.pycaw import AudioUtilities
        dev = AudioUtilities.GetSpeakers()
        if hasattr(dev, "EndpointVolume"):
            _endpoint = dev.EndpointVolume
        else:
            from comtypes import CLSCTX_ALL
            from pycaw.pycaw import IAudioEndpointVolume
            iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            _endpoint = ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))
    return _endpoint


def vol_op(delta=None, set_to=None, toggle_mute=False):
    try:
        ep = _vol_endpoint()
        cur = ep.GetMasterVolumeLevelScalar()
        if set_to is not None:
            cur = set_to / 100
        if delta is not None:
            cur = cur + delta / 100
        if set_to is not None or delta is not None:
            ep.SetMasterVolumeLevelScalar(max(0.0, min(1.0, cur)), None)
        if toggle_mute:
            ep.SetMute(0 if ep.GetMute() else 1, None)
        return {"level": round(ep.GetMasterVolumeLevelScalar() * 100), "muted": bool(ep.GetMute())}
    except Exception as e:
        print(f"[volume] {e!r}", file=sys.stderr)
        return STATE["vol"]


async def volume(**kw):
    loop = asyncio.get_running_loop()
    STATE["vol"] = await loop.run_in_executor(COM_POOL, lambda: vol_op(**kw))
    return STATE["vol"]


# ---------------------------------------------------------------- media session (SMTC)
async def media_manager():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager as M
    return await M.request_async()


async def read_media():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as PS
    from winrt.windows.storage.streams import DataReader
    mgr = await media_manager()
    ses = mgr.get_current_session()
    if ses is None:
        return None
    props = await ses.try_get_media_properties_async()
    tl = ses.get_timeline_properties()
    pb = ses.get_playback_info()
    playing = pb.playback_status == PS.PLAYING
    duration = max(0.0, (tl.end_time - tl.start_time).total_seconds())
    pos = tl.position.total_seconds()
    if playing:
        try:
            pos += (datetime.now(timezone.utc) - tl.last_updated_time).total_seconds()
        except Exception:
            pass
    pos = min(pos, duration) if duration else pos
    artist, album = props.artist or "", props.album_title or ""
    # Apple Music (Windows) puts "Artist — Album" in the artist field.
    if "—" in artist:
        artist, alb = artist.split("—", 1)
        artist, album = artist.strip(), (album or alb.strip())
    key = f"{artist}|{props.title}|{album}"
    if key not in ART_CACHE and props.thumbnail is not None:
        try:
            stream = await props.thumbnail.open_read_async()
            reader = DataReader(stream)
            await reader.load_async(stream.size)
            data = bytearray(stream.size)
            reader.read_bytes(data)
            ART_CACHE[key] = bytes(data)
            if len(ART_CACHE) > 20:
                ART_CACHE.pop(next(iter(ART_CACHE)))
        except Exception as e:
            print(f"[art] {e!r}", file=sys.stderr)
    return {"title": props.title, "artist": artist, "album": album,
            "playing": playing, "position": pos, "duration": duration, "key": key,
            "has_art": key in ART_CACHE, "app": ses.source_app_user_model_id}


async def media_cmd(cmd):
    mgr = await media_manager()
    ses = mgr.get_current_session()
    if ses is None:
        return False
    fn = {"playpause": ses.try_toggle_play_pause_async,
          "next": ses.try_skip_next_async,
          "prev": ses.try_skip_previous_async}[cmd]
    return bool(await fn())


# ---------------------------------------------------------------- stats
_last_net = None


def gpu_stats():
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2, creationflags=0x08000000).stdout.strip()
        u, t = [int(x) for x in out.splitlines()[0].split(",")]
        return {"gpu": u, "gpu_temp": t}
    except Exception:
        return {}


TEMPS = {"c": None}


def temp_thread():
    """Read Windows thermal zones via typeperf (no admin needed; values are Kelvin).

    Reports the hottest zone, which is the closest thing to "SoC temperature"
    that Windows exposes on this machine.
    """
    # One short-lived sample per loop (no long-running child that could be orphaned).
    cmd = ["typeperf", r"\Thermal Zone Information(*)\Temperature", "-sc", "1"]
    while True:
        value = None
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                                 creationflags=0x08000000).stdout
            for line in out.splitlines():
                parts = line.strip().strip('"').split('","')
                try:
                    vals = [float(x) for x in parts[1:]]
                except ValueError:
                    continue  # header or status line
                vals = [v for v in vals if v > 200]  # drop unused zones reporting 0
                if vals:
                    value = round(max(vals) - 273.15)
        except Exception as e:
            print(f"[temp] {e!r}", file=sys.stderr)
        TEMPS["c"] = value
        time.sleep(3)


async def poller():
    global _last_net
    loop = asyncio.get_running_loop()
    psutil.cpu_percent(None)
    tick = 0
    while True:
        try:
            STATE["media"] = await read_media()
        except Exception as e:
            print(f"[media] {e!r}", file=sys.stderr)
            STATE["media"] = None
        net = psutil.net_io_counters()
        now = time.time()
        down = up = 0
        if _last_net:
            dt = max(now - _last_net[0], 0.001)
            down = (net.bytes_recv - _last_net[1].bytes_recv) / dt
            up = (net.bytes_sent - _last_net[1].bytes_sent) / dt
        _last_net = (now, net)
        st = {"cpu": round(psutil.cpu_percent(None)), "ram": round(psutil.virtual_memory().percent),
              "disk": round(psutil.disk_usage("C:\\").percent), "down": down, "up": up,
              "temp": TEMPS["c"]}
        if tick % 3 == 0:
            st.update(await loop.run_in_executor(None, gpu_stats))
        else:
            st.update({k: STATE["stats"].get(k) for k in ("gpu", "gpu_temp") if k in STATE["stats"]})
        STATE["stats"] = st
        if tick % 5 == 0:
            await volume()
        tick += 1
        await asyncio.sleep(1)


# ---------------------------------------------------------------- lyrics (LRCLIB)
def _lrclib(endpoint, params):
    req = urllib.request.Request(f"https://lrclib.net/api/{endpoint}?{urllib.parse.urlencode(params)}",
                                 headers={"User-Agent": "MiniScreen/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def fetch_lyrics(title, artist, duration):
    clean = re.sub(r"\s*[\(\[][^)\]]*(feat|ft|with)\.?[^)\]]*[\)\]]", "", title, flags=re.I).strip()
    first_artist = re.split(r",|&| feat\.? | x ", artist, flags=re.I)[0].strip()
    data = None
    # 1) exact match, 2) without duration, 3) cleaned title, 4) fuzzy search
    for t, a, d in ((title, artist, duration), (title, artist, 0), (clean, first_artist, 0)):
        p = {"track_name": t, "artist_name": a}
        if d:
            p["duration"] = int(d)
        data = _lrclib("get", p)
        if data and data.get("syncedLyrics"):
            break
        data = None
    if not data:
        for q in (f"{clean} {first_artist}", clean):
            res = _lrclib("search", {"q": q}) or []
            res = [r for r in res if r.get("syncedLyrics")]
            if res:
                data = min(res, key=lambda r: abs((r.get("duration") or 0) - duration) if duration else 0)
                break
    if not data:
        return None
    lrc = data.get("syncedLyrics")
    if not lrc:
        return None
    lines = []
    for ln in lrc.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", ln)
        if m:
            lines.append([int(m.group(1)) * 60 + float(m.group(2)), m.group(3)])
    return lines or None


# ---------------------------------------------------------------- system actions
def key_combo(*vks):
    for vk in vks:
        ctypes.windll.user32.keybd_event(vk, 0, 0, 0)
    for vk in reversed(vks):
        ctypes.windll.user32.keybd_event(vk, 0, 2, 0)


PENDING = {"img": None}  # last captured screenshot waiting to be cropped/saved


def shot_preview_bytes():
    import io
    img = PENDING["img"]
    if img is None:
        return None
    prev = img.convert("RGB")
    prev.thumbnail((2000, 2000))
    buf = io.BytesIO()
    prev.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def save_shot(rect):
    """rect: dict of x,y,w,h as fractions (0-1) of the image, or None for the full image."""
    img = PENDING["img"]
    if img is None:
        raise ValueError("tiada screenshot")
    if rect:
        W, H = img.size
        x0, y0 = max(0, int(rect["x"] * W)), max(0, int(rect["y"] * H))
        x1, y1 = min(W, int((rect["x"] + rect["w"]) * W)), min(H, int((rect["y"] + rect["h"]) * H))
        if x1 - x0 < 4 or y1 - y0 < 4:
            raise ValueError("kawasan terlalu kecil")
        img = img.crop((x0, y0, x1, y1))
    folder = Path(load_cfg().get("screenshot_dir") or pictures_dir())
    out = folder / f"MiniScreen_{datetime.now():%Y%m%d_%H%M%S}.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    PENDING["img"] = None
    return str(out)


def pictures_dir():
    """Real Pictures folder (may live under OneDrive, not ~/Pictures)."""
    buf = ctypes.create_unicode_buffer(260)
    if ctypes.windll.shell32.SHGetFolderPathW(None, 0x27, None, 0, buf) == 0 and buf.value:
        return Path(buf.value)
    return Path.home() / "Pictures"


def do_system(action):
    if action == "lock":
        ctypes.windll.user32.LockWorkStation()
    elif action == "sleep":
        subprocess.Popen(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])
    elif action == "desktop":
        key_combo(0x5B, 0x44)  # Win+D
    elif action == "shot":
        # Capture only; nothing is written to disk until the user saves a crop.
        from PIL import ImageGrab
        PENDING["img"] = ImageGrab.grab(all_screens=True)
        return "pending"
    else:
        raise ValueError(action)
    return "ok"


def launch(index):
    app = load_cfg()["apps"][index]
    subprocess.Popen(["cmd", "/c", "start", "", app["target"]], creationflags=0x08000000)


# ---------------------------------------------------------------- location (PC side)
LOC_CACHE = {"t": 0.0, "v": None}


def windows_location():
    ps = ("Add-Type -AssemblyName System.Device;"
          "$w=New-Object System.Device.Location.GeoCoordinateWatcher;$w.Start();"
          "for($i=0;$i -lt 20 -and $w.Position.Location.IsUnknown;$i++){Start-Sleep -Milliseconds 400};"
          "$l=$w.Position.Location;"
          "if(-not $l.IsUnknown){$c=[Globalization.CultureInfo]::InvariantCulture;"
          "$l.Latitude.ToString($c)+','+$l.Longitude.ToString($c)};$w.Stop()")
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         timeout=25, creationflags=0x08000000).stdout.strip()
    lat, lon = (float(x) for x in out.split(","))
    return {"lat": lat, "lon": lon, "source": "windows"}


def get_location():
    manual = load_cfg().get("location")
    if manual and manual.get("lat") is not None:
        return {"lat": manual["lat"], "lon": manual["lon"], "source": "config"}
    if LOC_CACHE["v"] and time.time() - LOC_CACHE["t"] < 600:
        return LOC_CACHE["v"]
    try:
        LOC_CACHE.update(t=time.time(), v=windows_location())
    except Exception as e:
        print(f"[location] {e!r}", file=sys.stderr)
        LOC_CACHE.update(t=time.time(), v=None)
    return LOC_CACHE["v"]


# ---------------------------------------------------------------- HTTP
@web.middleware
async def ip_guard(request, handler):
    """Only this PC and the phone IPs listed in config.json may connect."""
    allowed = LOCAL_IPS | set(load_cfg().get("allowed_ips", []))
    if request.remote not in allowed:
        return web.Response(status=403, text="forbidden")
    return await handler(request)


def authed(req):
    return req.headers.get("X-Token") == TOKEN or req.query.get("t") == TOKEN


async def index(req):
    return web.FileResponse(BASE / "index.html")


async def config(req):
    if not authed(req):
        return web.Response(status=403)
    c = load_cfg()
    return web.json_response({"apps": c["apps"], "prayer": c["prayer"]})


async def api_location(req):
    if not authed(req):
        return web.Response(status=403)
    loop = asyncio.get_running_loop()
    return web.json_response(await loop.run_in_executor(None, get_location) or {"lat": None})


async def art(req):
    if not authed(req):
        return web.Response(status=403)
    data = ART_CACHE.get(req.query.get("k", ""))
    if not data:
        return web.Response(status=404)
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})


async def lyrics(req):
    if not authed(req):
        return web.Response(status=403)
    title, artist = req.query.get("title", ""), req.query.get("artist", "")
    dur = float(req.query.get("duration") or 0)
    key = f"{artist}|{title}"
    if key not in LYRICS_CACHE:
        loop = asyncio.get_running_loop()
        LYRICS_CACHE[key] = await loop.run_in_executor(None, fetch_lyrics, title, artist, dur)
    return web.json_response({"lines": LYRICS_CACHE[key]})


async def api_media(req):
    if not authed(req):
        return web.Response(status=403)
    return web.json_response({"ok": await media_cmd(req.match_info["cmd"])})


async def api_volume(req):
    if not authed(req):
        return web.Response(status=403)
    body = await req.json()
    return web.json_response(await volume(delta=body.get("delta"), set_to=body.get("set"),
                                          toggle_mute=bool(body.get("mute"))))


async def api_launch(req):
    if not authed(req):
        return web.Response(status=403)
    body = await req.json()
    launch(int(body["i"]))
    return web.json_response({"ok": True})


async def api_system(req):
    if not authed(req):
        return web.Response(status=403)
    loop = asyncio.get_running_loop()
    try:
        res = await loop.run_in_executor(None, do_system, req.match_info["action"])
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True, "result": res})


async def shot_preview(req):
    if not authed(req):
        return web.Response(status=403)
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, shot_preview_bytes)
    if data is None:
        return web.Response(status=404)
    return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "no-store"})


async def api_shot_save(req):
    if not authed(req):
        return web.Response(status=403)
    body = await req.json()
    rect = body if all(k in body for k in ("x", "y", "w", "h")) else None
    loop = asyncio.get_running_loop()
    try:
        path = await loop.run_in_executor(None, save_shot, rect)
    except Exception as e:
        return web.json_response({"ok": False, "error": str(e)}, status=400)
    return web.json_response({"ok": True, "result": path})


async def api_shot_cancel(req):
    if not authed(req):
        return web.Response(status=403)
    PENDING["img"] = None
    return web.json_response({"ok": True})


async def ws_handler(req):
    if req.query.get("t") != TOKEN:
        return web.Response(status=403)
    ws = web.WebSocketResponse(heartbeat=20)
    await ws.prepare(req)
    tick = 0
    try:
        while not ws.closed:
            msg = {"spec": [int(v * 100) for v in SPECTRUM]}
            if tick % 20 == 0:
                msg.update(media=STATE["media"], stats=STATE["stats"], vol=STATE["vol"])
            await ws.send_json(msg)
            tick += 1
            await asyncio.sleep(0.05)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return ws


async def on_start(app):
    app["poller"] = asyncio.create_task(poller())


def lan_ip():
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # no packet is sent; just picks the outgoing interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def open_on_phone():
    url = f"http://localhost:{PORT}/?t={TOKEN}"
    try:
        subprocess.run(["adb", "reverse", f"tcp:{PORT}", f"tcp:{PORT}"], check=True, timeout=10)
        subprocess.run(["adb", "shell", "am", "start", "-a", "android.intent.action.VIEW", "-d", url],
                       check=True, timeout=10)
        print("Opened on phone.")
    except Exception as e:
        print(f"Could not open on phone automatically ({e!r}).")
    print(f"Open this on the phone if needed: {url}")


def main():
    import threading
    threading.Thread(target=spectrum_thread, daemon=True).start()
    threading.Thread(target=temp_thread, daemon=True).start()
    app = web.Application(middlewares=[ip_guard])
    app.on_startup.append(on_start)
    app.add_routes([
        web.get("/", index), web.get("/ws", ws_handler), web.get("/api/config", config),
        web.get("/art", art), web.get("/api/lyrics", lyrics), web.get("/api/location", api_location),
        web.get("/shot.jpg", shot_preview), web.post("/api/shot/save", api_shot_save),
        web.post("/api/shot/cancel", api_shot_cancel),
        web.post("/api/media/{cmd:playpause|next|prev}", api_media),
        web.post("/api/volume", api_volume), web.post("/api/launch", api_launch),
        web.post("/api/system/{action:lock|sleep|desktop|shot}", api_system),
    ])
    if "--open" in sys.argv:
        threading.Timer(1.5, open_on_phone).start()
    ip = lan_ip()
    urls = [f"http://localhost:{PORT}/?t={TOKEN}  (USB / adb reverse)"]
    if HOST != "127.0.0.1" and ip:
        urls.append(f"http://{ip}:{PORT}/?t={TOKEN}  (Wi-Fi)")
        urls.append(f"http://{os.environ.get('COMPUTERNAME', 'pc').lower()}.local:{PORT}/?t={TOKEN}  (Wi-Fi, by name)")
    (BASE / "phone_url.txt").write_text("\n".join(urls) + "\n", encoding="utf-8")
    print(f"Mini Screen listening on {HOST}:{PORT}; allowed phones: {load_cfg().get('allowed_ips', [])}")
    print("\n".join(urls))
    web.run_app(app, host=HOST, port=PORT, print=None)


if __name__ == "__main__":
    main()
