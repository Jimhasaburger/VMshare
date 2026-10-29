import json
import time
import logging
import threading
import asyncio
from fractions import Fraction
from io import BytesIO

import av
from PIL import Image
from av.video.frame import VideoFrame
from flask import Flask

try:
    import numpy as np
    import sounddevice as sd
    HAS_AUDIO = True
except ImportError:
    HAS_AUDIO = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("websockets.server").setLevel(logging.WARNING)

with open("config.json", "r") as f:
    CONFIG = json.load(f)

app = Flask(__name__)

from vm_session import VMSession
session = VMSession(CONFIG)

AUDIO_RATE = 48000
AUDIO_CHANNELS = 1
AUDIO_CHUNK = 960


def create_opus_encoder():
    codec = av.Codec('opus', 'w')
    ctx = codec.create(kind='audio')
    ctx.sample_rate = AUDIO_RATE
    ctx.format = 's16'
    ctx.layout = 'mono'
    ctx.time_base = Fraction(1, AUDIO_RATE)
    ctx.bit_rate = 32000
    ctx.open()
    return ctx


def find_vbcable():
    if not HAS_AUDIO:
        return None
    try:
        devices = sd.query_devices()
        for i, d in enumerate(devices):
            if d["max_input_channels"] > 0 and "cable output" in d["name"].lower():
                return i
    except Exception:
        pass
    return None


@app.route("/api/info")
def api_info():
    return {
        "name": CONFIG["machine_name"],
        "connected": session._connected,
        "width": session.width,
        "height": session.height,
        "has_audio": HAS_AUDIO and find_vbcable() is not None,
    }


def rebuild_encoder(target_fps, new_w, new_h):
    output = BytesIO()
    container = av.open(output, format="h264", mode="w")
    vstream = container.add_stream("libx264", rate=target_fps)
    vstream.width = new_w
    vstream.height = new_h
    vstream.pix_fmt = "yuv420p"
    vstream.options = {
        "tune": "zerolatency",
        "preset": "ultrafast",
        "x264-params": "repeat-headers=1:keyint={}:profile=baseline".format(target_fps * 2),
    }
    return output, container, vstream


async def handle_stream(websocket):
    import websockets
    log.info("Stream client connected")
    target_fps = CONFIG.get("fps", 15)
    interval = 1.0 / target_fps
    w = (session.width + 15) // 16 * 16
    h = (session.height + 15) // 16 * 16

    output, container, vstream = rebuild_encoder(target_fps, w, h)

    opus_encoder = create_opus_encoder()

    device_idx = find_vbcable()
    audio_stream = None
    if HAS_AUDIO and device_idx is not None:
        try:
            audio_stream = sd.InputStream(
                device=device_idx,
                channels=AUDIO_CHANNELS,
                samplerate=AUDIO_RATE,
                dtype="float32",
            )
            audio_stream.start()
            log.info("Audio capture started")
        except Exception as e:
            log.warning(f"Audio init failed: {e}")
            audio_stream = None

    frame_count = 0
    try:
        while True:
            t0 = time.monotonic()

            if session.refresh_resolution():
                new_w = (session.width + 15) // 16 * 16
                new_h = (session.height + 15) // 16 * 16
                if new_w != w or new_h != h:
                    w, h = new_w, new_h
                    try:
                        container.close()
                    except Exception:
                        pass
                    output, container, vstream = rebuild_encoder(target_fps, w, h)
                    frame_count = 0
                    log.info(f"Encoder reconfigured for {w}x{h}")

            rgb_data = session.capture_frame_rgb()
            if rgb_data is not None:
                try:
                    img = Image.frombytes("RGB", (session.width, session.height), rgb_data, "raw", "BGRX")
                    if img.size != (w, h):
                        img = img.resize((w, h))
                    frame = VideoFrame.from_image(img)

                    output.seek(0)
                    output.truncate()
                    for packet in vstream.encode(frame):
                        container.mux(packet)
                    raw = output.getvalue()

                    if raw:
                        await websocket.send(bytes([0x01]) + raw)
                        if frame_count == 0:
                            log.info(f"First video frame sent: {len(raw)} bytes")
                    frame_count += 1
                except websockets.ConnectionClosed:
                    raise
                except Exception as e:
                    log.debug(f"Video encode error: {e}")

            if audio_stream is not None:
                try:
                    data, overflowed = audio_stream.read(AUDIO_CHUNK)
                    if data is not None and len(data) > 0:
                        pcm_int16 = (data.flatten() * 32767).astype(np.int16)
                        frame = av.AudioFrame.from_ndarray(
                            pcm_int16.reshape(1, -1),
                            format='s16',
                            layout='mono',
                        )
                        frame.sample_rate = AUDIO_RATE
                        packets = opus_encoder.encode(frame)
                        for packet in packets:
                            await websocket.send(bytes([0x02]) + packet.bytes())
                except websockets.ConnectionClosed:
                    raise
                except Exception as e:
                    log.debug(f"Audio encode error: {e}")

            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0, interval - elapsed))
    except websockets.ConnectionClosed:
        log.info("Stream client disconnected")
    except Exception as e:
        log.info(f"Stream ended: {e}")
    finally:
        if audio_stream:
            try:
                audio_stream.stop()
                audio_stream.close()
            except Exception:
                pass
        try:
            container.close()
        except Exception:
            pass


async def handle_mouse(websocket):
    import websockets
    log.info("Mouse WebSocket connected")
    button_state = 0
    try:
        async for message in websocket:
            msg = json.loads(message)
            msg_type = msg.get("type", "move")

            if msg_type == "move":
                x = int(msg.get("x", 0))
                y = int(msg.get("y", 0))
                session.move_mouse_abs(x, y)
            elif msg_type == "button":
                btn = msg.get("button", "left")
                pressed = msg.get("pressed", False)
                session.mouse_button(btn, pressed)
            elif msg_type == "scroll":
                dz = int(msg.get("dz", 0))
                session.scroll_mouse(dz)
    except websockets.ConnectionClosed:
        log.info("Mouse client disconnected")
    except Exception as e:
        log.error(f"WS mouse error: {e}")


def run_ws_server():
    import websockets

    host = CONFIG.get("host", "0.0.0.0")
    ws_port = CONFIG.get("ws_port", 5051)
    ws_mouse_port = CONFIG.get("ws_mouse_port", 5052)

    async def serve():
        stream_server = await websockets.serve(handle_stream, host, ws_port)
        log.info(f"Stream WebSocket on ws://{host}:{ws_port}")
        mouse_server = await websockets.serve(handle_mouse, host, ws_mouse_port)
        log.info(f"Mouse WebSocket on ws://{host}:{ws_mouse_port}")
        await asyncio.Future()

    asyncio.run(serve())


def run_flask():
    host = CONFIG.get("host", "0.0.0.0")
    port = CONFIG.get("port", 5050)
    log.info(f"HTTP API on http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    log.info(f"Connecting to VM '{CONFIG['vm_name']}'...")
    if not session.connect():
        log.warning("Could not connect to VM. It may not be running.")

    vb_idx = find_vbcable()
    if vb_idx is not None:
        dev = sd.query_devices(vb_idx)
        log.info(f"VB-Cable found: {dev['name']}")
    else:
        log.warning("VB-Cable not found - audio disabled")

    log.info(f"VM Agent '{CONFIG['machine_name']}'")

    ws_thread = threading.Thread(target=run_ws_server, daemon=True)
    ws_thread.start()

    run_flask()
