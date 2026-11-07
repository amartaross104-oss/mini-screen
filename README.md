# Mini Screen

Turn an old (or spare) Android phone into a small companion display for your Windows PC.

- **Music**: now playing from any Windows media player (Apple Music, YouTube Music in a browser, etc.), album art, synced lyrics (LRCLIB) and a live spectrum visualizer.
- **Home**: big clock, prayer times (JAKIM method via Aladhan) with the next prayer countdown, PC stats (CPU, RAM, disk, network, temperature).
- **Control**: volume and mute, launch-app buttons, lock, sleep, show desktop, and a screenshot tool where you crop the shot on the phone before it is saved.
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
3. The dashboard opens straight away. Tap **FULL** in the bottom bar to hide the browser bars.

The page always looks landscape: if the phone is in portrait (for example after the screen turned off and on again) it rotates itself by 90 degrees. Tilt the phone to the left (camera on the left), or press **FLIP** (shown only in portrait) to reverse the direction. After the screen wakes, the page reconnects to the server by itself, so there is nothing to tap or refresh.

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
| Google Fonts | Pixel fonts (Silkscreen, VT323) | Standard font request |

## Known limits

- The PC temperature is the hottest Windows thermal zone (ARM laptops expose no single CPU sensor without extra drivers). No GPU stats unless `nvidia-smi` exists.
- The spectrum uses WASAPI loopback (`PyAudioWPatch`); if that fails the visualizer stays empty and everything else keeps working.
- "Sleep" uses the Windows suspend call and may hibernate if hibernation is enabled.
