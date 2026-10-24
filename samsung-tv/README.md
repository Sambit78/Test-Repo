# Samsung TV cleanup and control

## What this TV is

The photo shows a **Samsung Smart TV running Tizen**, not Android TV, Google TV or Fire TV. You can tell from the Samsung Smart Hub home bar, Samsung TV Plus, "Samsung Promotion" and the Settings/Source/Ambient icons on the left. The second photo (Settings → General → System Manager) shows the model: **Samsung Q60AA 75"**. That's the 2021 QLED Q60A, Australian version, running Tizen 6. The menu paths below follow the 2021 layout.

That means **ADB can't be used**: the TV has no Developer options, no USB or Wireless debugging, no logcat, no `pm disable`, no animation scale and no Private DNS. Samsung's official local-network control allows only the following, and `tvctl.py` uses nothing else:

| Official interface | What it can do | What it can't do |
|---|---|---|
| REST `http://TV:8001/api/v2/` | Model, firmware, power state, network info. Open, close and check apps on some models. | Change settings |
| Remote-control websocket `wss://TV:8002/…/samsung.remote.control` (the same one the SmartThings app and phone remotes use). You approve it once on the TV. | Press any remote key, list installed apps, launch apps (including YouTube on a chosen video) | Disable or uninstall system apps, take screenshots, read settings |
| Tizen Developer Mode (Apps → type 1-2-3-4-5) with Samsung's `sdb` tool | Install **your own signed** Tizen apps (.wgt) | Disable built-in apps, root |

So Samsung's preinstalled apps (Samsung TV Plus, Samsung Promotion, Gallery and so on) **can't be disabled or removed from the system**. They can only be removed from the home bar. Ads and tracking are cut through Samsung's own privacy settings and DNS. Every step below can be undone.

`tvctl.py` uses Python 3's standard library only, so there's nothing to install. Python 3 comes with macOS and Linux. On Windows, install it from python.org.

## 1. Connect (you press, the computer does the rest)

1. On the remote: **Settings (gear) → All Settings → General → Network → Network Status → IP Settings**. Write down the **IP address** (for example 192.168.1.42). The menu is laid out slightly differently on each model year. If yours differs, look under *Connection* instead of *General*.
2. Make sure remote control over the network is allowed: **Settings → General → External Device Manager → Device Connection Manager → Access Notification = First Time Only**. On some years this is under *Connection*.
3. On the computer, on the same Wi-Fi network:
   ```sh
   python3 tvctl.py info --tv 192.168.1.42     # no pairing needed; shows the model
   python3 tvctl.py pair 192.168.1.42          # TV shows "Allow tvctl?" → press OK on the remote
   ```
   If it says **denied**: Settings → General → External Device Manager → Device Connection Manager → Device List → set *tvctl* to Allow → retry.
   If it says **No route to host** on macOS: System Settings → Privacy & Security → Local Network → allow your terminal app, then retry.
   Pairing doesn't work while the TV is off. Turn it on first.

## 2. Live monitor

In a second terminal window:
```sh
python3 tvctl.py monitor
```
This prints every command with a millisecond timestamp. The TV's own event stream, which takes the place of logcat here, appears only while a command runs. Everything is also saved to `~/.samsung-tv/session.log`.

## 3. Back up before changing anything

```sh
python3 tvctl.py backup
```
This creates `backup-<date>/` with `device.json` (model and firmware), `apps.json` and `apps.txt` (every installed app), and `settings-before.txt`. Fill in that last file, or take a photo of each screen, **before** changing the settings in step 4. The TV doesn't let any tool read these settings, so you record them yourself.

## 4. Proposed cleanup (do it only after you OK it)

Do every step with the remote. The key presses are listed so you can also send them with `tvctl.py key …` and watch them happen.

**Never touched:** Live TV / Terrestrial, Samsung TV Plus (it's a live-TV app), Prime Video, Disney+, Binge, Kayo, 7plus, Optus Sport, YuppTV, YouTube, the Smart Hub launcher, the keyboard, input sources, Bixby/Alexa voice services, and anything not on this list.

| # | Change | Where | Undo |
|---|---|---|---|
| A | Interest-Based Advertisement **Off** | Settings → General → Terms & Privacy → Privacy Choices (older: Support → Terms & Policy) | Turn it back on |
| B | Viewing Information Services **Off** (stops Samsung's viewing tracking, which drives the recommendations) | Same screen | Turn it back on |
| C | Autorun Smart Hub **Off**, Autorun Last App **Off** (the TV starts on your input, not on the ad screen) | Settings → General → Smart Features | Turn them back on |
| D | DNS: **Enter manually → 94.140.14.14** (AdGuard DNS, the same service as `dns.adguard-dns.com`). Samsung only accepts a plain IP, not a Private-DNS hostname. | Settings → General → Network → Network Status → IP Settings → DNS setting | DNS setting → **Obtain automatically** |
| E | Remove **Samsung Promotion** and the "Recommended Apps" tiles from the home bar | Highlight the tile → press **Down** (or hold OK) → **Remove** | Apps → Settings (gear) → app → Add to Home |
| F | Speed: Instant On **On**, clear app caches | Settings → General → Power and Energy Saving → Instant On; Settings → Support → Device Care → Manage Storage → View Details → Clear Cache | Turn Instant On off. Caches rebuild themselves. |

Not possible on Samsung, so it won't be attempted: disabling or uninstalling built-in apps, an animation scale of 0.5, or the "Private DNS" hostname setting. Not included because it can't be undone: Reset Smart Hub (Device Care → Self Diagnosis), which erases all sign-ins.

If DNS blocking stops an app from playing, undo step D. Some streaming apps refuse to play when their ad servers are blocked.

## 5. Open-source app (your choice)

TV Bro is an Android app and can't run on Tizen. The official route for open-source apps on a Samsung TV is Tizen Developer Mode:
- **Jellyfin** (`github.com/jellyfin/jellyfin-tizen`, a client for a Jellyfin media server). Installing it needs Tizen Studio and a free Samsung author certificate. **You** sign in to Samsung to create the certificate. I won't sign in for you.
- To turn on Developer Mode, open **Apps**, then type **1 2 3 4 5** on the remote's number pad (or the on-screen pad) → Developer mode **On** → *Host PC IP* = this computer's IP → OK → restart the TV.
- Tell me which app you want and I'll add the exact install steps. Install only from the project's official GitHub page.

The built-in Samsung **Internet** browser, the "Browser" tile on your home bar, already covers browsing.

## 6. One command: YouTube + a video you name

```sh
python3 tvctl.py youtube "lofi hip hop radio"                 # a title: plays the first search result
python3 tvctl.py youtube https://www.youtube.com/watch?v=XXXX # or a URL / video ID
```
Each step shows on the TV and in the monitor:
1. Presses **Home**.
2. Opens YouTube straight on the video, using Samsung's deep link.
3. **"Who's watching?"**: the TV API can't take screenshots, so the script **asks you** whether the picker is showing. You confirm the highlighted profile. To move the highlight first, run `python3 tvctl.py key LEFT` or `key RIGHT`. Then it presses OK and sends the video again.
4. Asks whether the video is playing, and presses **Play** if it isn't.

The script never presses OK without asking you first. Add `-y` to skip those questions once you trust it.

## Cheat sheet

```sh
python3 tvctl.py info                        # model / firmware / power
python3 tvctl.py apps                        # installed apps + appIds
python3 tvctl.py key HOME                    # any remote key
python3 tvctl.py key DOWN DOWN RIGHT ENTER   # several in a row (asks before ENTER)
python3 tvctl.py key VOLUP VOLDOWN MUTE
python3 tvctl.py key SOURCE                  # input picker
python3 tvctl.py launch 111299001912         # YouTube (other appIds come from `apps`)
python3 tvctl.py youtube "<title or URL>"
python3 tvctl.py monitor                     # live log, second terminal
```
Common keys: `HOME RETURN EXIT UP DOWN LEFT RIGHT ENTER PLAY PAUSE STOP FF REWIND VOLUP VOLDOWN MUTE CHUP CHDOWN SOURCE MENU POWER 0-9 (KEY_0…)`.

## Undo everything

| What | How |
|---|---|
| A, B | Privacy Choices → turn Interest-Based Advertisement and Viewing Information Services back on |
| C | Smart Features → Autorun Smart Hub and Autorun Last App back on |
| D | IP Settings → DNS setting → Obtain automatically |
| E | Apps → Settings → highlight the app → Add to Home |
| F | Power and Energy Saving → Instant On off |
| Pairing | Settings → General → External Device Manager → Device Connection Manager → Device List → delete *tvctl*; on the computer `rm -r ~/.samsung-tv` |
| Developer Mode | Apps → 1 2 3 4 5 → Off → restart TV |

Compare against `backup-<date>/settings-before.txt` to put back exactly what was there.
