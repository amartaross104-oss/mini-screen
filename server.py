"""Mini Screen server: turns a phone into a PC companion display.

Binds to 127.0.0.1 only. The phone reaches it through `adb reverse`
(USB), so nothing is exposed to the network. Every API call needs a
random per-run token.
"""
import asyncio
import ctypes
import hashlib
import json
import os
import queue as pyqueue
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
def sorted_apps(cfg):
    """The app buttons in alphabetical order. The page asks to launch app number N, so the list the page shows and the
    list the launcher uses must be the same; both go through this one function."""
    return sorted(cfg["apps"], key=lambda a: a["name"].casefold())


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
ART_META = {}       # key -> {"hash", "first" (when we first saw the song), "v" (bumped when the image changes)}
ART_SETTLE_S = 20   # how long after a song starts we keep checking that its album art has really arrived
LYRICS_CACHE = {}   # key -> list | None


# ---------------------------------------------------------------- audio spectrum
def _default_out_id():
    """Id of Windows' CURRENT default output device (it changes when headphones / Bluetooth connect)."""
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    return str(getattr(dev, "id", None) or dev.GetId())


def spectrum_thread():
    """Keep the visualizer attached to whatever the default output device is right now.

    A session captures one device; when the default device changes (headphones, Bluetooth, speakers) the
    session ends, PyAudio is torn down (it caches the device list) and a new one starts on the new device.
    """
    try:
        _com_init()
    except Exception:
        pass
    while True:
        try:
            _spectrum_session()
        except Exception as e:  # visualizer is optional; keep retrying (e.g. the device vanished mid-song)
            print(f"[visualizer] {e!r}", file=sys.stderr)
        time.sleep(2)


def _spectrum_session():
    global SPECTRUM
    import pyaudiowpatch as pyaudio
    p = pyaudio.PyAudio()
    try:
        start_id = _default_out_id()
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
        try:
            while stream.is_active():
                time.sleep(2)
                if _default_out_id() != start_id:          # the default output changed: re-attach to the new one
                    print("[visualizer] output device changed, re-attaching", file=sys.stderr)
                    break
        finally:
            stream.stop_stream()
            stream.close()
    finally:
        p.terminate()
        SPECTRUM = SPECTRUM * 0


# ---------------------------------------------------------------- volume (COM thread)
def _com_init():
    import comtypes
    comtypes.CoInitialize()


COM_POOL = ThreadPoolExecutor(1, initializer=_com_init)
def _vol_endpoint():
    # Ask Windows for the CURRENT default output device every time. It used to be cached once, so after switching
    # to headphones the mini screen kept showing/changing the old speakers' volume.
    from pycaw.pycaw import AudioUtilities
    dev = AudioUtilities.GetSpeakers()
    if hasattr(dev, "EndpointVolume"):
        return dev.EndpointVolume
    from comtypes import CLSCTX_ALL
    from pycaw.pycaw import IAudioEndpointVolume
    iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return ctypes.cast(iface, ctypes.POINTER(IAudioEndpointVolume))


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


def pick_session(mgr):
    """The session the user means: the one that is actually playing.

    Windows' "current" session is just the most recently active app, which can be a paused one while a video (YouTube,
    Apple TV, a movie player) plays in another app. Showing and controlling that one made the buttons seem dead."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as PS
    sessions = mgr.get_sessions()
    everyone = [sessions.get_at(i) for i in range(sessions.size)]
    cur = mgr.get_current_session()
    playing = [s for s in everyone if s.get_playback_info().playback_status == PS.PLAYING]
    if cur is not None and any(s.source_app_user_model_id == cur.source_app_user_model_id for s in playing):
        return cur
    if playing:
        return playing[0]
    return cur or (everyone[0] if everyone else None)


async def read_media():
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as PS
    from winrt.windows.storage.streams import DataReader
    mgr = await media_manager()
    ses = pick_session(mgr)
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
    # When a song changes, Windows often updates the title first and the album art a few seconds LATER, so the first
    # thumbnail we read can still be the previous song's. Keep re-reading it for ART_SETTLE_S seconds after we first
    # see this song, and replace the cached image (bumping art_v so the phone fetches it again) if it changes.
    if props.thumbnail is not None:
        meta = ART_META.get(key)
        if meta is None or time.time() - meta["first"] < ART_SETTLE_S:
            try:
                stream = await props.thumbnail.open_read_async()
                reader = DataReader(stream)
                await reader.load_async(stream.size)
                data = bytearray(stream.size)
                reader.read_bytes(data)
                data = bytes(data)
                h = hashlib.md5(data).hexdigest()
                if meta is None:
                    ART_META[key] = {"hash": h, "first": time.time(), "v": 1}
                    ART_CACHE[key] = data
                elif meta["hash"] != h:
                    meta["hash"], meta["v"] = h, meta["v"] + 1
                    ART_CACHE[key] = data
                if len(ART_CACHE) > 20:
                    old = next(iter(ART_CACHE))
                    ART_CACHE.pop(old, None)
                    ART_META.pop(old, None)
            except Exception as e:
                print(f"[art] {e!r}", file=sys.stderr)
    return {"title": props.title, "artist": artist, "album": album,
            "playing": playing, "position": pos, "duration": duration, "key": key,
            "has_art": key in ART_CACHE, "art_v": ART_META.get(key, {}).get("v", 0),
            "app": ses.source_app_user_model_id}


async def media_cmd(cmd):
    """Play/pause/next/previous on the playing app. Returns {"ok", "via"}.

    Not every app supports the single "toggle" command (video apps often only offer separate Play and Pause), so
    pause/play is chosen from the app's real state. If the app still refuses, the Windows media key is sent."""
    from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionPlaybackStatus as PS
    mgr = await media_manager()
    ses = pick_session(mgr)
    if ses is not None:
        pb = ses.get_playback_info()
        c, playing = pb.controls, pb.playback_status == PS.PLAYING
        if cmd == "playpause":
            attempts = [ses.try_pause_async] if (playing and c.is_pause_enabled) else \
                       [ses.try_play_async] if (not playing and c.is_play_enabled) else []
            attempts.append(ses.try_toggle_play_pause_async)
        else:
            attempts = [ses.try_skip_next_async if cmd == "next" else ses.try_skip_previous_async]
        for fn in attempts:
            try:
                if await fn():
                    return {"ok": True, "via": "session"}
            except Exception as e:
                print(f"[media {cmd}] {e!r}", file=sys.stderr)
    # No session, or the app did not accept the command: the media key goes to whatever Windows treats as active.
    key_combo({"playpause": 0xB3, "next": 0xB0, "prev": 0xB1}[cmd])   # VK_MEDIA_PLAY_PAUSE / NEXT_TRACK / PREV_TRACK
    return {"ok": True, "via": "key"}


# ---------------------------------------------------------------- up next (Apple Music's Queue panel via UI Automation)
# Windows' media session has no queue, but the Apple Music app exposes its "Playing Next" list to accessibility tools.
# We read that list; we never change the queue (Apple Music offers no way to do that from outside).
QUEUE = {"state": "init", "items": [], "rev": 0}   # state: ok | closed | no_app | other_app | error


def _queue_snapshot(uia, UIA):
    pids = [p.pid for p in psutil.process_iter(["name"]) if (p.info["name"] or "").lower().startswith("applemusic")]
    root, win = uia.GetRootElement(), None
    for pid in pids:
        win = root.FindFirst(UIA.TreeScope_Children, uia.CreatePropertyCondition(UIA.UIA_ProcessIdPropertyId, pid))
        if win:                                          # comtypes returns a NULL pointer (falsy), not None, when nothing matches
            break
    if not win:
        return {"state": "no_app", "items": []}
    by_type = lambda t: uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, t)
    btn = win.FindFirst(UIA.TreeScope_Descendants, uia.CreateAndCondition(
        uia.CreatePropertyCondition(UIA.UIA_NamePropertyId, "Queue"), by_type(UIA.UIA_ButtonControlTypeId)))
    if not btn:
        return {"state": "error", "items": []}          # the app's layout is not what we expect
    tp = btn.GetCurrentPattern(UIA.UIA_TogglePatternId).QueryInterface(UIA.IUIAutomationTogglePattern)
    if tp.CurrentToggleState == 0:                       # panel is closed
        # Only open it ourselves while the Apple Music window is minimized (so nothing visibly changes on screen).
        if win.CurrentIsOffscreen and load_cfg().get("queue_auto_open", True):
            tp.Toggle()
            time.sleep(0.9)
        else:
            return {"state": "closed", "items": []}
    lists = win.FindAll(UIA.TreeScope_Descendants, by_type(UIA.UIA_ListControlTypeId))
    q, best = None, None
    for i in range(lists.Length):                         # the queue panel is the right-most list
        l = lists.GetElement(i)
        left = l.CurrentBoundingRectangle.left
        if best is None or left > best:
            q, best = l, left
    if q is None:
        return {"state": "ok", "items": []}
    rows = q.FindAll(UIA.TreeScope_Children, by_type(UIA.UIA_ListItemControlTypeId))
    items = []
    for i in range(rows.Length):
        texts = rows.GetElement(i).FindAll(UIA.TreeScope_Descendants, by_type(UIA.UIA_TextControlTypeId))
        names = [texts.GetElement(k).CurrentName or "" for k in range(texts.Length)]
        if not names or not names[0]:
            continue                                      # a placeholder row that is not rendered yet
        sub = names[1] if len(names) > 1 else ""
        artist, _, album = sub.partition(" — ")     # "Artist — Album"
        items.append({"title": names[0], "artist": artist.strip(), "album": album.strip(),
                      "dur": names[-1] if len(names) > 2 else ""})
    return {"state": "ok", "items": items}


ITUNES_ART = {}   # (normalized title, normalized artist) -> (artwork url or "", when it was looked up)


def itunes_art(title, artist):
    """Album art for a song from the iTunes Search API (free, no key). Returns "" if no matching song is found.

    Apple Music's own window gives no pictures for the queue rows, so this looks them up by title + artist and only
    accepts a result whose title and artist match, never "the closest thing"."""
    clean = re.sub(r"\s*[\(\[][^)\]]*[\)\]]", "", title).strip()
    want_t = _norm(clean)
    want_a = _norm(re.split(r",|&| feat\.? | x ", artist, flags=re.I)[0])
    for country in ("MY", None):                        # the Malaysian storefront first, then the default one
        params = {"term": f"{clean} {artist}", "entity": "song", "limit": 8}
        if country:
            params["country"] = country
        req = urllib.request.Request("https://itunes.apple.com/search?" + urllib.parse.urlencode(params),
                                     headers={"User-Agent": "MiniScreen/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=8) as r:
                results = json.loads(r.read().decode("utf-8")).get("results", [])
        except Exception:
            continue
        for r in results:
            t, a = _norm(r.get("trackName")), _norm(r.get("artistName"))
            if (t == want_t or (min(len(t), len(want_t)) >= 4 and (t in want_t or want_t in t))) \
                    and (not want_a or want_a in a or a in want_a):
                return r.get("artworkUrl100") or ""
    return ""


def _attach_art(items):
    """Put art URLs on the queue items from the cache. At most ONE new lookup per call (the API allows ~20/minute).
    Returns True while some songs still wait for their lookup."""
    looked, pending = False, False
    for it in items:
        key = (_norm(it["title"]), _norm(it["artist"]))
        hit = ITUNES_ART.get(key)
        if hit is None or (not hit[0] and time.time() - hit[1] > 600):   # never looked up, or a miss older than 10 min
            if not looked:
                hit = ITUNES_ART[key] = (itunes_art(it["title"], it["artist"]), time.time())
                looked = True
            else:
                pending = True
        it["art"] = hit[0] if hit else ""
    return pending


# ---- YouTube Music (in Edge): the "Up next" list on the Now Playing page. Edge only exposes page content to
# accessibility tools when "Web accessibility" is switched on in edge://accessibility.
QUEUE_CMDS = pyqueue.Queue()   # actions for the UI Automation thread (it owns the COM objects)
_TIME_RE = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?$")


def _ytm_window(uia, UIA):
    wins = uia.GetRootElement().FindAll(UIA.TreeScope_Children, uia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_WindowControlTypeId))
    for i in range(wins.Length):
        w = wins.GetElement(i)
        if "YouTube Music" in (w.CurrentName or ""):
            return w
    return None


def _ytm_doc(win, uia, UIA):
    return win.FindFirst(UIA.TreeScope_Descendants, uia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_DocumentControlTypeId))


def _ytm_rows(doc, uia, UIA):
    """Rows of the Up next list in page order. Each row has a "Play <title>" button ("Pause <title>" for the song that
    is playing), a title, an artist line and a duration. Returns dicts with title/artist/dur/playing and the button."""
    by_type = lambda t: uia.CreatePropertyCondition(UIA.UIA_ControlTypePropertyId, t)
    els = doc.FindAll(UIA.TreeScope_Descendants, by_type(UIA.UIA_TextControlTypeId))
    texts = []
    for i in range(els.Length):
        e = els.GetElement(i)
        n = (e.CurrentName or "").strip()
        if n:
            r = e.CurrentBoundingRectangle
            texts.append((r.top, r.left, n))
    rows = []
    btns = doc.FindAll(UIA.TreeScope_Descendants, by_type(UIA.UIA_ButtonControlTypeId))
    for i in range(btns.Length):
        b = btns.GetElement(i)
        name = (b.CurrentName or "").strip()
        for prefix, playing in (("Play ", False), ("Pause ", True)):
            if name.startswith(prefix) and len(name) > len(prefix):
                title, br = name[len(prefix):], b.CurrentBoundingRectangle
                # the matching title text sits right next to the button; this also rejects stray "Play ..." buttons
                head = next((t for t in texts if t[2] == title and abs(t[0] - br.top) <= 15 and t[1] > br.left), None)
                if head is None:
                    continue
                artist = next((t[2] for t in texts if t[1] == head[1] and 8 < t[0] - head[0] < 40), "")
                dur = next((t[2] for t in texts if _TIME_RE.match(t[2]) and abs(t[0] - head[0]) < 30), "")
                rows.append({"top": head[0], "title": title, "artist": artist, "dur": dur, "playing": playing, "btn": b})
                break
    rows.sort(key=lambda r: r["top"])
    return rows


def _ytm_snapshot(uia, UIA, win):
    doc = _ytm_doc(win, uia, UIA)
    if not doc:
        return {"state": "ytm_noaccess", "items": []}          # Edge is not exposing the page (web accessibility off)
    tabs = doc.FindAll(UIA.TreeScope_Descendants, uia.CreatePropertyCondition(
        UIA.UIA_ControlTypePropertyId, UIA.UIA_TabItemControlTypeId))
    selected = False
    for i in range(tabs.Length):
        t = tabs.GetElement(i)
        if (t.CurrentName or "").strip().upper() == "UP NEXT":
            try:
                selected = bool(t.GetCurrentPattern(UIA.UIA_SelectionItemPatternId)
                                .QueryInterface(UIA.IUIAutomationSelectionItemPattern).CurrentIsSelected)
            except Exception:
                selected = False
            break
    if not selected:
        return {"state": "ytm_closed", "items": []}             # the Now Playing page / UP NEXT tab is not showing
    rows = _ytm_rows(doc, uia, UIA)
    cur = next((i for i, r in enumerate(rows) if r["playing"]), None)
    if cur is None:                                             # paused: the current row says "Play ..." too, so match by title
        t = _norm((STATE.get("media") or {}).get("title"))
        cur = next((i for i, r in enumerate(rows) if t and _norm(r["title"]) == t), None)
    upcoming = rows[cur + 1:] if cur is not None else []        # unknown current song: show nothing rather than guess
    return {"state": "ok", "source": "ytm",
            "items": [{"title": r["title"], "artist": r["artist"], "album": "", "dur": r["dur"]} for r in upcoming]}


def _ytm_play(uia, UIA, title):
    """Play a song from the Up next list by pressing its own Play button (an exact jump, no skipping through songs)."""
    win = _ytm_window(uia, UIA)
    doc = _ytm_doc(win, uia, UIA) if win else None
    if not doc:
        return
    rows = _ytm_rows(doc, uia, UIA)
    cur = next((i for i, r in enumerate(rows) if r["playing"]), -1)
    t = _norm(title)
    target = (next((r for i, r in enumerate(rows) if i > cur and _norm(r["title"]) == t), None)
              or next((r for r in rows if _norm(r["title"]) == t), None))
    if target:
        target["btn"].GetCurrentPattern(UIA.UIA_InvokePatternId).QueryInterface(UIA.IUIAutomationInvokePattern).Invoke()


def queue_thread():
    try:
        import comtypes
        import comtypes.client
        comtypes.CoInitialize()
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient as UIA
        uia = comtypes.client.CreateObject(UIA.CUIAutomation, interface=UIA.IUIAutomation)
    except Exception as e:
        print(f"[queue disabled] {e!r}", file=sys.stderr)
        QUEUE.update(state="error")
        return
    last = None
    while True:
        delay = 3.0
        try:
            app = (STATE.get("media") or {}).get("app") or ""
            win = None
            if not app or "AppleMusic" in app:
                snap = _queue_snapshot(uia, UIA)
            elif "edge" in app.lower() and (win := _ytm_window(uia, UIA)):
                snap = _ytm_snapshot(uia, UIA, win)
            else:                                        # some other player: we have no way to read its queue
                snap, delay = {"state": "other_app", "items": []}, 4.0
            snap.setdefault("source", "apple")
            if snap["state"] == "ok" and _attach_art(snap["items"]):
                delay = 3.5                              # songs still waiting for art: keep under the lookup rate limit
        except Exception as e:
            print(f"[queue] {e!r}", file=sys.stderr)
            snap, delay = {"state": "error", "items": []}, 6.0
        if snap != last:
            QUEUE.update(snap)
            QUEUE["rev"] += 1
            last = snap
        try:                                             # wait for the next poll, but react to a tap on a song at once
            cmd = QUEUE_CMDS.get(timeout=delay)
            try:
                _ytm_play(uia, UIA, cmd["title"])
            except Exception as e:
                print(f"[queue play] {e!r}", file=sys.stderr)
        except pyqueue.Empty:
            pass


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


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def fetch_lyrics(title, artist, duration):
    clean = re.sub(r"\s*[\(\[][^)\]]*(feat|ft|with)\.?[^)\]]*[\)\]]", "", title, flags=re.I).strip()
    first_artist = re.split(r",|&| feat\.? | x ", artist, flags=re.I)[0].strip()
    want_title, want_artist = _norm(clean), _norm(first_artist)

    def same_song(r):
        """Only accept the SAME song. A title-only search used to return other artists' songs with the same title
        (e.g. another "Khayalan"), which showed lyrics that did not belong to the playing song at all."""
        if not r or not r.get("syncedLyrics"):
            return False
        t, a = _norm(r.get("trackName")), _norm(r.get("artistName"))
        if not (t == want_title or (min(len(t), len(want_title)) >= 4 and (t in want_title or want_title in t))):
            return False
        if want_artist and a and not (want_artist in a or a in want_artist):
            return False
        if duration and r.get("duration") and abs(r["duration"] - duration) > 10:   # a very different length = another version
            return False
        return True

    # The same song often exists in several versions (single, album, remaster, live) whose lyric timings differ by
    # seconds. That was why lyrics had to be nudged by hand. So: collect every matching version and use the one whose
    # length is closest to the song that is actually playing.
    def gap(r):
        return abs((r.get("duration") or 0) - duration) if duration else 0

    cands = {}
    # 1) exact lookup, 2) without duration, 3) cleaned title; stop as soon as a version of (almost) the same length is found
    for t, a, d in ((title, artist, duration), (title, artist, 0), (clean, first_artist, 0)):
        p = {"track_name": t, "artist_name": a}
        if d:
            p["duration"] = int(d)
        r = _lrclib("get", p)
        if same_song(r):
            cands[r.get("id") or len(cands)] = r
            if not duration or gap(r) <= 2.5:
                break
    # 4) otherwise look at every version LRCLIB lists for this song
    if not cands or min(gap(r) for r in cands.values()) > 2.5:
        for r in (_lrclib("search", {"q": f"{clean} {first_artist}"}) or []):
            if same_song(r):
                cands[r.get("id") or len(cands)] = r
    data = min(cands.values(), key=gap) if cands else None
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
    app = sorted_apps(load_cfg())[index]
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


async def api_ping(req):
    """Tiny reply used by the phone to measure its round-trip time to the PC."""
    if not authed(req):
        return web.Response(status=403)
    return web.Response(text="ok", headers={"Cache-Control": "no-store"})


async def config(req):
    if not authed(req):
        return web.Response(status=403)
    c = load_cfg()
    apps = []
    for a in sorted_apps(c):
        slug = re.sub(r"[^a-z0-9]+", "-", a["name"].lower()).strip("-")
        has_png = (BASE / "icons" / f"{slug}.png").is_file()          # made by make_icons.ps1 from the installed apps
        apps.append({**a, "slug": slug, **({"icon_png": f"{slug}.png"} if has_png else {})})
    return web.json_response({"apps": apps, "prayer": c["prayer"]})


_ICON_RE = re.compile(r"^[a-z0-9][a-z0-9-]*\.png$")


async def icon_file(req):
    """App logos from the icons folder. Only plain *.png names are accepted, so nothing else on disk can be reached."""
    if not authed(req):
        return web.Response(status=403)
    name = req.match_info["name"]
    path = BASE / "icons" / name
    if not _ICON_RE.match(name) or not path.is_file():
        return web.Response(status=404)
    return web.FileResponse(path, headers={"Cache-Control": "max-age=86400"})


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


async def _skip_ahead(n):
    for _ in range(n):
        await media_cmd("next")
        await asyncio.sleep(0.4)


async def api_queue_jump(req):
    """Jump to an upcoming song. YouTube Music: press that song's own Play button. Apple Music has no way to play a
    chosen queue entry from outside, so there we press "next" n times."""
    if not authed(req):
        return web.Response(status=403)
    body = await req.json()
    if QUEUE.get("source") == "ytm" and body.get("title"):
        QUEUE_CMDS.put({"title": str(body["title"])[:300]})
        return web.json_response({"ok": True, "via": "ytm"})
    n = max(1, min(40, int(body.get("n", 1))))
    asyncio.get_running_loop().create_task(_skip_ahead(n))   # don't keep the phone waiting while it skips
    return web.json_response({"ok": True, "n": n})


async def api_media(req):
    if not authed(req):
        return web.Response(status=403)
    return web.json_response(await media_cmd(req.match_info["cmd"]))


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
    sent_rev = -1
    try:
        while not ws.closed:
            msg = {"spec": [int(v * 100) for v in SPECTRUM]}
            if tick % 20 == 0:
                msg.update(media=STATE["media"], stats=STATE["stats"], vol=STATE["vol"])
                if QUEUE["rev"] != sent_rev:                 # the list is only sent when it changed
                    msg["queue"] = {"state": QUEUE["state"], "items": QUEUE["items"][:40]}
                    sent_rev = QUEUE["rev"]
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
    threading.Thread(target=queue_thread, daemon=True).start()
    app = web.Application(middlewares=[ip_guard])
    app.on_startup.append(on_start)
    app.add_routes([
        web.get("/", index), web.get("/ws", ws_handler), web.get("/api/config", config),
        web.get("/art", art), web.get("/api/lyrics", lyrics), web.get("/api/location", api_location),
        web.get("/api/ping", api_ping), web.get("/icons/{name}", icon_file),
        web.get("/shot.jpg", shot_preview), web.post("/api/shot/save", api_shot_save),
        web.post("/api/shot/cancel", api_shot_cancel),
        web.post("/api/media/{cmd:playpause|next|prev}", api_media),
        web.post("/api/queue/jump", api_queue_jump),
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
