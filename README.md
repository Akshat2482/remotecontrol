# Jarvis Remote Control Protocol

Say "Hey Jarvis, activate remote control protocol" and control your PC from
an Android app, from anywhere on Earth. Two halves:

- `pc/remote_control.py` — plugs into your existing Jarvis.py. Streams your
  screen, injects taps/drags as real mouse events, routes chat messages
  into your command pipeline, and opens a Cloudflare Tunnel so the phone
  can reach it over the internet.
- `android/` — a Kotlin app: bottom pane mirrors your screen (tap/drag),
  top pane is a chat box with a text field and a mic button.

## How type mode vs. dictate mode works

There's no explicit mode switch in the app — it's implicit in *how* you
send the message:

- **Type a message and hit send** → "type" mode. Jarvis replies with text
  only, never speaks.
- **Tap the mic and talk** → "dictate" mode. Your speech is transcribed by
  Android's built-in speech recognizer, sent as text, and Jarvis's reply is
  both shown as text *and* spoken out loud via the PC's TTS.

This matches exactly what you described: type mode never talks back,
dictate mode always does.

## Setup — PC side

1. Install the extra dependencies:
   ```bash
   pip install -r pc/requirements-remote.txt
   ```
2. Install `cloudflared` (free, no Cloudflare account needed for this):
   - Windows: `winget install --id Cloudflare.cloudflared`
   - or download directly: https://github.com/cloudflare/cloudflared/releases
   - Make sure it's on your PATH (test with `cloudflared --version` in a
     new terminal).
3. Copy `pc/remote_control.py` next to your `Jarvis.py`.
4. Wire the wake phrase into your existing voice mode — in
   `voice_mode.py`, wherever you currently handle a finished transcript:

   ```python
   from remote_control import maybe_activate_remote_control

   def on_transcript(text: str):
       if maybe_activate_remote_control(text):
           return  # was the activation phrase — don't run it as a normal command
       ...your existing command handling...
   ```

5. **Wire in your real command handling.** `remote_control.py` ships with a
   placeholder `process_command()` that only understands "open chrome" and
   "open notepad" so you can test the pipe end-to-end. Replace it with a
   call into your actual Jarvis command/LLM pipeline — see the comment
   block at the top of the file for exactly where.
6. Same for `speak()` — it tries to import your existing `tts_engine`
   module first, and only falls back to bare `pyttsx3` if that's not
   found. If your TTS module has a different function name/signature,
   adjust the import in `speak()`.

To test without wiring anything into Jarvis.py first, just run:
```bash
python pc/remote_control.py
```
This starts the server immediately (skips the wake-phrase gate) and prints
the tunnel URL + auth token to the terminal.

## Setup — Android app

You don't need Android Studio. GitHub builds it for you:

1. Push this whole `jarvis-remote-control/` folder to a GitHub repo (or a
   folder inside an existing one — the workflow only triggers on changes
   under `android/`).
2. Go to the repo's **Actions** tab → **Build Jarvis Remote APK** → if it
   hasn't run automatically, click **Run workflow**.
3. Once it finishes (a few minutes), open the workflow run and download
   the **jarvis-remote-debug-apk** artifact — that's a zip containing
   `app-debug.apk`.
4. Transfer the APK to your phone and install it. You'll need to allow
   "install from unknown sources" for whichever app you use to open it,
   since it's not from the Play Store.

## Using it

1. Say the activation phrase to Jarvis. It'll print (and speak) something
   like:
   > Remote control protocol active. Server: wss://random-words.trycloudflare.com. Token: aB3x...

2. Open the app, paste the `wss://...` URL and the token into the connect
   screen, tap **Connect**.
3. Bottom pane shows your screen — tap to click, drag to drag.
4. Top pane: type a message and hit send, or tap the mic and talk.

## Security — read this before exposing your PC

Anyone with your tunnel URL *and* token can control your PC. The token is
regenerated randomly every time the server starts and only shown to you —
treat it like a password and don't share it. Cloudflare's free "quick
tunnel" URLs are also random and expire when the tunnel process stops, so
between the token and the URL there are two things an attacker would need
simultaneously, but this is still meaningfully less locked-down than, say,
a VPN. Don't leave the remote control protocol running unattended for long
stretches if that concerns you — it's designed to be started on demand by
voice, not left on permanently.

## Known limitations / where to go from here

- **Screen streaming** is periodic JPEG frames (~2 fps), not real-time
  video — you picked this over WebRTC for simplicity. Good enough for
  clicking through menus and dialogs; not good for anything requiring
  smooth motion.
- **Multi-monitor**: `remote_control.py` only captures the primary
  monitor (`sct.monitors[1]`). Adjust `_capture_frame_jpeg()` if you want
  to pick a different one or stream all of them.
- **One phone at a time**: input handling assumes a single controlling
  connection. Multiple phones can technically connect and will all see
  the screen stream, but their taps/drags will collide.
- **Tunnel URL changes every restart** (Cloudflare's free quick tunnels
  are ephemeral). If you want a stable, unchanging URL, that requires a
  free Cloudflare account and a *named* tunnel instead — a reasonable v2
  upgrade if the copy-paste-a-new-URL-each-time flow gets old.
