# Mini Screen

Turn an old (or spare) Android phone into a small companion display for your Windows PC.

- **Music**: now playing from any Windows media player (Apple Music, YouTube Music in a browser, etc.), album art, animated synced lyrics (smooth scroll, the current line grows and its words fill in as they are sung) and a live spectrum visualizer. Lyric timing can be nudged per song with the `-` / `+` buttons; the adjustment is remembered for that song, and songs you have not adjusted yet start from what you usually need for that player (the median of your adjustments, once there are three). Play/pause targets the app that is actually playing (a YouTube video, Apple TV, a movie player), uses separate Play/Pause calls for apps that do not support a toggle, and falls back to the Windows media key. Volume is on the same screen: tap or drag the volume bar, or use `-` / `+` and mute.
- **Queue** (Apple Music for Windows, and YouTube Music in Edge): the upcoming songs with album pictures, and what is playing now. Tap a song once to mark it and again to jump to it. See "Up next" below.
- **Home**: big clock, prayer times (JAKIM method via Aladhan) with the next prayer countdown, PC stats (CPU, RAM, disk, network, temperature).
- **Control**: launch-app buttons (each shows the app's own logo, see "App logos" below), lock, sleep, show desktop, and a screenshot tool where you crop the shot on the phone before it is saved.
- **Burn-in care**: dim levels (the DIM button), small periodic pixel shift and a dark screen saver when nothing is playing.

A small Python server runs on the PC. The phone just opens a web page in its browser, so there is nothing to install on the phone.

> Built and tested on Windows 11 (ARM, Snapdragon X Plus) with a Xiaomi/POCO phone and Brave. It should work on x64 Windows too, but that is not tested.

## Requirements

- Windows 10/11, Python 3.10+
- An Android phone with a modern browser, on the same Wi-Fi (or connected by USB with `adb`)
- Optional: [`adb`](https://developer.android.com/tools/releases/platform-tools) for the USB mode

## Quick start (USB, most secure)

1. Enable **USB debugging** on the phone and plug it in.
2. Double-click `start.bat`. It creates a virtual environment in `%USERPROFILE%\MiniScreen\venv`, installs the requirements, starts the server, and opens the page on the phone through `adb reverse`.
3. The dashboard opens straight away. Touch the page once to go fullscreen and lock to landscape (browsers only allow fullscreen after a touch, and drop it whenever the screen turns off).

The page always looks landscape: if the phone is in portrait (for example after the screen turned off and on again) it rotates itself by 90 degrees until you touch it. Tilt the phone to the left (camera on the left), or press **FLIP** (shown only in portrait) to reverse the direction. Once fullscreen, Android flips between the two landscape directions by itself.

After the screen wakes, the page replaces its connection to the server by itself, so there is nothing to refresh. The **FULL / REFRESH** button enters fullscreen and refreshes in place (connection, app buttons, lyrics, prayer times) without reloading the page, so fullscreen is kept. The page only does a real reload, once, when a new version of it has been installed on the PC.

By default the server only listens on `127.0.0.1`, so nothing on your network can reach it.

## Wi-Fi mode (no cable)

Edit `config.json` (created from `config.example.json` on first run):

```json
{
  "host": "0.0.0.0",
  "allowed_ips": ["192.168.1.50"]
}
```

- `allowed_ips` is the **phone's IP**. Any other address is rejected with 403. Reserve that IP on your router so it does not change.
- On first start a **persistent token** is generated in `.token`. The server writes the full URL (with the token) to `phone_url.txt` and prints it. Open that URL on the phone and bookmark it.
- Windows Firewall will block incoming connections on a *Public* network. Allow only your phone, from an **Administrator** PowerShell:

  ```powershell
  New-NetFirewallRule -DisplayName "MiniScreen" -Direction Inbound -Protocol TCP -LocalPort 8765 -RemoteAddress 192.168.1.50 -Action Allow -Profile Public,Private
  ```

- Browsers block geolocation and wake lock on plain HTTP. The page therefore asks the PC for a location (coarse, from Windows). For accurate prayer times, tap the place name on the **Home** screen and search for your city. The choice is stored on the phone.

### Security notes

- Anyone who can reach the server **and** knows the token can lock/sleep your PC, launch the configured apps and take screenshots. Do not use Wi-Fi mode on public networks, and do not share `.token`, `phone_url.txt` or your `config.json`.
- Only the apps listed in `config.json` can be launched, by index. Nothing arbitrary is executed.
- Traffic is plain HTTP.

## Keep the screen on

The page cannot keep the phone awake over plain HTTP. On the phone enable **Developer options > Stay awake** (screen stays on while charging).

## Auto-start with Windows

Put a shortcut to `%USERPROFILE%\MiniScreen\venv\Scripts\pythonw.exe server.py` (start in this folder) in `shell:startup`.

## Configuration (`config.json`)

| Key | Meaning |
| --- | --- |
| `port` | Server port (default 8765) |
| `host` | `127.0.0.1` (USB only) or `0.0.0.0` (Wi-Fi) |
| `allowed_ips` | Phone IPs allowed to connect in Wi-Fi mode |
| `screenshot_dir` | Where cropped screenshots go (empty = your Pictures folder) |
| `apps` | Launch buttons: `key`, `name`, `target` (URL, protocol, or executable name) |
| `location` | Optional fixed `{ "lat": ..., "lon": ... }` used instead of the PC's location |
| `prayer.method` | Aladhan calculation method (17 = JAKIM) |

## Third-party services

The page contacts these from the phone, and the server contacts LRCLIB. Only the listed data is sent.

| Service | Used for | Data sent |
| --- | --- | --- |
| [LRCLIB](https://lrclib.net) | Synced lyrics | Track title and artist |
| [Aladhan](https://aladhan.com) | Prayer times | Latitude and longitude |
| [BigDataCloud](https://www.bigdatacloud.com) | Place name from coordinates | Latitude and longitude |
| [Open-Meteo Geocoding](https://open-meteo.com) | City search | The text you type |
| [iTunes Search API](https://performance-partners.apple.com/search-api) | Album pictures in the Queue list | Song title and artist (the server asks; the phone then loads the pictures from Apple's image servers) |
| Google Fonts | Pixel fonts (Silkscreen, VT323) | Standard font request |

## Up next: YouTube Music (in Edge)

The same idea works for the "Up next" list on YouTube Music's Now Playing page, with these differences:

- Edge only exposes page content to accessibility tools when web accessibility is on. Open `edge://accessibility`, switch on **Native accessibility API support** and **Web accessibility**, then reload the YouTube Music tab. Without it the Queue tab says so.
- The YouTube Music tab must be the active tab of its window, on the Now Playing page with the **UP NEXT** tab selected. The server never clicks around to open it for you.
- Jumping presses the chosen song's own Play button, so it is exact (no songs in between are started).
- Only Edge is supported. The list shown is what YouTube Music itself lists as upcoming; the server only reads it.

## Up next (Apple Music queue)

Windows' media session has no queue, but the Apple Music app exposes its "Playing Next" panel to accessibility tools (UI Automation). The server reads that list and never changes it.

- The Queue panel in Apple Music must be open. While the Apple Music window is **minimized** the server opens the panel for you and leaves it open (set `"queue_auto_open": false` in `config.json` to disable that). While the window is visible on screen the server never touches it; open the panel yourself.
- Only the rows Apple Music has rendered are visible (typically the next ~15-20 songs).
- Album pictures for the list are looked up in the iTunes Search API by title and artist (a match is only accepted if both agree), cached, and filled in gradually (about one lookup every 3.5 s to stay under the API's rate limit); a song without a match shows a note symbol. The currently playing song, shown at the top, uses the picture from the media session.
- Tapping a song jumps to it by pressing "next" repeatedly. Reordering or "play next" is not possible from outside the app.
- This reads the app's own UI, so an Apple Music update can break it. If it does, the tab shows a message instead of the list.

## App logos

The launch buttons show each app's own logo. Run `make_icons.ps1` (in this folder, Windows PowerShell) once, and again after you add apps: it reads `config.json` and saves a PNG per app into `icons/`, taken from the apps installed on your PC (Store app logos, `steam.exe`, `msedge.exe`...). The `icons/` folder is not part of the repository (it holds other companies' logos), so each user creates it locally. Apps without a local logo (a website, a folder) fall back to a small drawn icon, then to the key letter. The server only serves plain `*.png` files from `icons/`, and only with the access token.

## Known limits

- The PC temperature is the hottest Windows thermal zone (ARM laptops expose no single CPU sensor without extra drivers). No GPU stats unless `nvidia-smi` exists.
- The spectrum uses WASAPI loopback (`PyAudioWPatch`); if that fails the visualizer stays empty and everything else keeps working.
- "Sleep" uses the Windows suspend call and may hibernate if hibernation is enabled.
