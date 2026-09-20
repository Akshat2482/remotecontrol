"""
Jarvis Remote Control Protocol — PC-side server.

Wire this into your existing Jarvis.py like this:

    from remote_control import maybe_activate_remote_control

    # inside your voice_mode.py's transcript handler, wherever you get the
    # recognized text from Parakeet:
    def on_transcript(text: str):
        if maybe_activate_remote_control(text):
            return  # was the activation phrase, don't process as a normal command
        ... your existing command handling ...

Saying "Hey Jarvis, activate remote control protocol" (or just "activate
remote control protocol" once Jarvis is already listening) starts the
server and prints/speaks the public URL to give the Android app.

What this module does:
  1. Starts a local WebSocket server (screen stream + touch input + chat)
  2. Auto-starts a Cloudflare Tunnel so the phone can reach it from anywhere
     (requires the free `cloudflared` binary — see README)
  3. Streams the screen as periodic JPEG frames
  4. Turns tap/drag messages from the phone into real mouse events
  5. Routes chat messages into `process_command()` — THIS IS THE INTEGRATION
     POINT for your existing Jarvis command/LLM pipeline. Replace the
     placeholder implementation with a call into your own code.
  6. Only speaks a reply out loud (TTS) when the message came in as
     "dictate" mode; "type" mode always gets a text-only reply.

Standalone testing (without wiring into Jarvis.py at all):
    python remote_control.py
This starts the server immediately, skips the wake-phrase gate, and prints
the connection URL + token to the terminal.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import os
import re
import secrets
import subprocess
import threading
import time

import mss
import pyautogui
import websockets
from PIL import Image

# ---------------- Config ----------------
WS_PORT = 8787
FRAME_INTERVAL = 0.5          # seconds between screen frames (~2 fps, per your "simple JPEG" choice)
JPEG_QUALITY = 55             # lower = smaller/faster, higher = clearer
JPEG_MAX_WIDTH = 900          # downscale before encoding, saves a lot of bandwidth/CPU

pyautogui.FAILSAFE = False    # don't abort on cursor-in-corner (this is remote-driven input)

# ---------------- Auth ----------------
# A fresh random token is generated each time the server starts. It's shown/
# spoken alongside the tunnel URL — enter both in the Android app's connect
# screen. This is the only thing stopping a random person who finds your
# tunnel URL from controlling your PC, so treat it like a password.
AUTH_TOKEN = secrets.token_urlsafe(16)

_connected_clients: set[websockets.WebSocketServerProtocol] = set()
_server_thread: threading.Thread | None = None
_server_loop: asyncio.AbstractEventLoop | None = None
_running = False

# drag state (per-connection would be more correct, but one PC + one phone
# controlling it at a time is the intended use case, so a single shared
# state is fine and much simpler)
_dragging = False


# ============================================================
# 1) INTEGRATION POINT — wire this into your real Jarvis pipeline
# ============================================================

def process_command(text: str) -> str:
    """
    Replace this with a call into your existing Jarvis command/LLM handling
    (the same pipeline your typed terminal commands and voice commands
    already go through) so "open email and draft..." etc. actually works.

    Example of what to do instead of this placeholder:

        from jarvis_core import handle_command  # whatever your real entry point is
        def process_command(text: str) -> str:
            return handle_command(text)

    This placeholder only understands a couple of trivial commands so you
    can test the app end-to-end before wiring in the real thing.
    """
    lower = text.lower()

    if "open chrome" in lower:
        try:
            os.startfile("chrome")  # Windows
            return "Opening Chrome."
        except Exception as e:
            return f"Couldn't open Chrome: {e}"

    if "open notepad" in lower:
        try:
            subprocess.Popen(["notepad.exe"])
            return "Opening Notepad."
        except Exception as e:
            return f"Couldn't open Notepad: {e}"

    return (
        f"(placeholder) I heard: \"{text}\". "
        "Wire process_command() in remote_control.py into your real Jarvis "
        "pipeline to actually act on this."
    )


def speak(text: str) -> None:
    """
    Replace this with your existing TTS engine call, e.g.:
        from tts_engine import speak as tts_speak
        def speak(text: str) -> None:
            tts_speak(text)
    Falls back to pyttsx3 directly if your tts_engine module isn't importable,
    so this still works standalone.
    """
    try:
        from tts_engine import speak as jarvis_speak  # your existing module
        jarvis_speak(text)
        return
    except Exception:
        pass

    try:
        import pyttsx3
        engine = pyttsx3.init()
        engine.say(text)
        engine.runAndWait()
    except Exception as e:
        print(f"[tts] speak failed: {e}")


# ============================================================
# 2) Wake-phrase gate — call this from your voice_mode.py transcript handler
# ============================================================

_ACTIVATE_PATTERN = re.compile(r"\bactivate remote control protocol\b", re.IGNORECASE)


def maybe_activate_remote_control(transcript: str) -> bool:
    """
    Call this with every transcribed line from your existing voice mode.
    Returns True (and starts the server) if the transcript was the
    activation phrase, so your caller can skip normal command handling
    for that utterance. Returns False otherwise.
    """
    if not _ACTIVATE_PATTERN.search(transcript):
        return False

    if _running:
        speak("Remote control protocol is already active.")
        return True

    start_remote_control()
    return True


# ============================================================
# 3) Screen capture + streaming
# ============================================================

def _capture_frame_jpeg() -> bytes:
    with mss.mss() as sct:
        monitor = sct.monitors[1]  # primary monitor
        raw = sct.grab(monitor)
    img = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")

    if img.width > JPEG_MAX_WIDTH:
        ratio = JPEG_MAX_WIDTH / img.width
        img = img.resize((JPEG_MAX_WIDTH, int(img.height * ratio)), Image.BILINEAR)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


async def _stream_screen():
    while _running:
        if _connected_clients:
            try:
                jpeg_bytes = await asyncio.to_thread(_capture_frame_jpeg)
                message = json.dumps({
                    "type": "frame",
                    "data": base64.b64encode(jpeg_bytes).decode("ascii"),
                })
                await asyncio.gather(
                    *(c.send(message) for c in list(_connected_clients)),
                    return_exceptions=True,
                )
            except Exception as e:
                print(f"[stream] frame capture/send error: {e}")
        await asyncio.sleep(FRAME_INTERVAL)


# ============================================================
# 4) Input injection — taps/drags from the phone become real mouse events
# ============================================================

def _apply_input(msg: dict) -> None:
    global _dragging
    screen_w, screen_h = pyautogui.size()

    msg_type = msg.get("type")
    x_norm = msg.get("x")
    y_norm = msg.get("y")

    if x_norm is None or y_norm is None:
        return
    x = max(0, min(screen_w - 1, int(x_norm * screen_w)))
    y = max(0, min(screen_h - 1, int(y_norm * screen_h)))

    if msg_type == "tap":
        pyautogui.click(x, y)
    elif msg_type == "drag_start":
        pyautogui.moveTo(x, y)
        pyautogui.mouseDown()
        _dragging = True
    elif msg_type == "drag_move":
        if _dragging:
            pyautogui.moveTo(x, y)
    elif msg_type == "drag_end":
        if _dragging:
            pyautogui.moveTo(x, y)
            pyautogui.mouseUp()
        _dragging = False


# ============================================================
# 5) WebSocket connection handling
# ============================================================

async def _handle_client(ws: websockets.WebSocketServerProtocol):
    authed = False
    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type")

            if msg_type == "auth":
                if msg.get("token") == AUTH_TOKEN:
                    authed = True
                    _connected_clients.add(ws)
                    await ws.send(json.dumps({"type": "auth_ok"}))
                else:
                    await ws.send(json.dumps({"type": "auth_fail"}))
                    await ws.close()
                continue

            if not authed:
                continue  # ignore everything until authenticated

            if msg_type in ("tap", "drag_start", "drag_move", "drag_end"):
                await asyncio.to_thread(_apply_input, msg)

            elif msg_type == "chat":
                text = msg.get("text", "")
                mode = msg.get("mode", "type")  # "type" or "dictate"
                reply = await asyncio.to_thread(process_command, text)

                await ws.send(json.dumps({
                    "type": "chat_reply",
                    "text": reply,
                    "speak": mode == "dictate",
                }))

                if mode == "dictate":
                    await asyncio.to_thread(speak, reply)
    finally:
        _connected_clients.discard(ws)


# ============================================================
# 6) Tunnel — ngrok (primary) with cloudflared fallback
# ============================================================

def _start_ngrok_tunnel(port: int) -> str | None:
    """
    Launches `ngrok http <port>` and parses the public URL from ngrok's
    local API (http://localhost:4040/api/tunnels).
    Requires ngrok installed and `ngrok authtoken <TOKEN>` run once.
    Returns None if ngrok isn't configured or the URL can't be found.
    """
    import urllib.request

    try:
        proc = subprocess.Popen(
            ["ngrok", "http", str(port), "--log=stdout", "--log-level=error"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("[tunnel] ngrok not found on PATH — falling back to cloudflared")
        return None

    # ngrok's API is at http://localhost:4040 — poll it until a tunnel URL appears
    deadline = time.time() + 15
    while time.time() < deadline:
        try:
            with urllib.request.urlopen("http://localhost:4040/api/tunnels", timeout=2) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            for t in data.get("tunnels", []):
                url = t.get("public_url", "")
                if url.startswith("https://"):
                    return url
        except Exception:
            pass
        time.sleep(0.5)

    proc.terminate()
    return None


def _start_cloudflare_tunnel(port: int) -> str | None:
    """
    Launches `cloudflared tunnel --url http://localhost:<port>` and parses
    the public https://*.trycloudflare.com URL from its output.
    Requires cloudflared installed (see README) — returns None if it's
    missing or the URL can't be found in time.
    """
    try:
        proc = subprocess.Popen(
            ["cloudflared", "tunnel", "--url", f"http://localhost:{port}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        print("[tunnel] cloudflared not found on PATH — install it first (see README)")
        return None

    url_pattern = re.compile(r"https://[a-zA-Z0-9\-]+\.trycloudflare\.com")
    deadline = time.time() + 20

    for line in proc.stdout:
        match = url_pattern.search(line)
        if match:
            return match.group(0)
        if time.time() > deadline:
            break

    return None


def _start_tunnel(port: int) -> str | None:
    """Try ngrok first, fall back to cloudflared if ngrok isn't configured."""
    url = _start_ngrok_tunnel(port)
    if url:
        return url
    return _start_cloudflare_tunnel(port)


# ============================================================
# 7) Start / stop
# ============================================================

# Your phone number in international format (without + or spaces) —
# WhatsApp Web will open in the default browser with a pre-filled message
# to this number. Change this to your own number.
WHATSAPP_PHONE = "919962919450"


def _notify_whatsapp(text: str) -> None:
    """Open WhatsApp Web with a pre-filled message to WHATSAPP_PHONE and
    auto-send it. Tries Ctrl+Enter first (WhatsApp's send shortcut); if the
    page hasn't finished loading yet, waits longer and retries."""
    try:
        import urllib.parse
        import time

        encoded = urllib.parse.quote(text)
        url = f"https://web.whatsapp.com/send?phone={WHATSAPP_PHONE}&text={encoded}"
        os.startfile(url)  # Windows

        # WhatsApp Web needs time to load, focus the chat box, and put the
        # cursor in the message field. Ctrl+Enter is WhatsApp's send shortcut
        # (plain Enter just inserts a newline while drafting).
        time.sleep(4)
        pyautogui.hotkey("ctrl", "enter")
    except Exception as e:
        print(f"[notify] couldn't auto-send WhatsApp: {e}")


def start_remote_control() -> None:
    global _running, _server_thread, _server_loop
    if _running:
        return
    _running = True

    def _run():
        global _server_loop
        loop = asyncio.new_event_loop()
        _server_loop = loop
        asyncio.set_event_loop(loop)

        async def _main():
            async with websockets.serve(_handle_client, "0.0.0.0", WS_PORT):
                asyncio.create_task(_stream_screen())
                while _running:
                    await asyncio.sleep(1)

        loop.run_until_complete(_main())

    _server_thread = threading.Thread(target=_run, daemon=True)
    _server_thread.start()

    tunnel_url = _start_tunnel(WS_PORT)

    if tunnel_url:
        ws_url = tunnel_url.replace("https://", "wss://")
        message = (
            f"JARVIS: Remote control protocol active. "
            f"Server: {ws_url}. Token: {AUTH_TOKEN}. "
            f"Enter both in the app's connect screen."
        )
    else:
        message = (
            f"JARVIS: Remote control server started locally on port {WS_PORT}, "
            f"but the public tunnel didn't come up — check that cloudflared "
            f"is installed. Token: {AUTH_TOKEN}."
        )

    print(f"[remote_control] {message}")
    speak(message)
    _notify_whatsapp(message)


def stop_remote_control() -> None:
    global _running
    _running = False


if __name__ == "__main__":
    # Standalone test mode — skips the wake-phrase gate entirely.
    print(f"[remote_control] auth token: {AUTH_TOKEN}")
    start_remote_control()
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        stop_remote_control()
