import subprocess
import threading
import logging
import time

log = logging.getLogger(__name__)

VBOXMANAGE = r"C:\Program Files\Oracle\VirtualBox\VBoxManage.exe"


class VMSession:
    def __init__(self, config):
        self.vm_name = config["vm_name"]
        self.width = config.get("width", 1920)
        self.height = config.get("height", 1080)

        self._lock = threading.RLock()
        self._connected = False
        self._target_fps = 15
        self._frame_times = []
        self._session = None
        self._display = None
        self._mouse = None
        self._button_state = 0

    def _run(self, *args):
        cmd = [VBOXMANAGE] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return result.stdout, result.stderr, result.returncode

    def connect(self):
        with self._lock:
            try:
                import virtualbox
                from virtualbox import library

                log.info(f"Connecting to VM '{self.vm_name}' via VirtualBox SDK...")

                vbox = virtualbox.VirtualBox()
                machine = vbox.find_machine(self.vm_name)
                self._session = virtualbox.Session()
                machine.lock_machine(self._session, library.LockType.shared)

                self._display = self._session.console.display
                self._mouse = self._session.console.mouse

                w, h, _, _, _, _ = self._display.get_screen_resolution(0)
                if w > 0 and h > 0:
                    self.width = w
                    self.height = h

                self._connected = True
                log.info(f"Connected to VM '{self.vm_name}' ({self.width}x{self.height})")
                log.info(f"Absolute mouse supported: {self._mouse.absolute_supported}")
                return True

            except Exception as e:
                log.error(f"Failed to connect: {e}")
                self._connected = False
                return False

    def ensure_connected(self):
        if self._connected:
            return True
        return self.connect()

    def reconnect(self):
        self.disconnect()
        time.sleep(0.5)
        return self.connect()

    def _update_quality(self, elapsed):
        self._frame_times.append(elapsed)
        if len(self._frame_times) > 10:
            self._frame_times.pop(0)

    def get_actual_fps(self):
        if len(self._frame_times) < 2:
            return self._target_fps
        avg = sum(self._frame_times) / len(self._frame_times)
        return 1.0 / avg if avg > 0 else self._target_fps

    def capture_frame_rgb(self):
        with self._lock:
            if not self.ensure_connected():
                return None
            try:
                from virtualbox import library
                raw = self._display.take_screen_shot_to_array(
                    0, self.width, self.height, library.BitmapFormat.bgr0
                )
                if not raw:
                    return None
                return bytes(raw)
            except Exception as e:
                log.error(f"Frame capture failed: {e}")
                self._connected = False
                return None

    def move_mouse_abs(self, x, y):
        with self._lock:
            if not self.ensure_connected():
                return False
            if not self._mouse:
                return False
            try:
                self._mouse.put_mouse_event_absolute(
                    x=int(x), y=int(y), dz=0, dw=0, button_state=self._button_state
                )
                return True
            except Exception as e:
                log.error(f"Mouse move failed: {e}")
                return False

    def mouse_button(self, button, pressed):
        with self._lock:
            if not self.ensure_connected():
                return False
            if not self._mouse:
                return False
            try:
                if button == "left":
                    mask = 1
                elif button == "right":
                    mask = 2
                elif button == "middle":
                    mask = 4
                else:
                    return False
                if pressed:
                    self._button_state |= mask
                else:
                    self._button_state &= ~mask
                self._mouse.put_mouse_event(
                    dx=0, dy=0, dz=0, dw=0, button_state=self._button_state
                )
                return True
            except Exception as e:
                log.error(f"Mouse button failed: {e}")
                return False

    def scroll_mouse(self, dz):
        with self._lock:
            if not self.ensure_connected():
                return False
            if not self._mouse:
                return False
            try:
                self._mouse.put_mouse_event(
                    dx=0, dy=0, dz=dz, dw=0, button_state=self._button_state
                )
                return True
            except Exception as e:
                log.error(f"Scroll failed: {e}")
                return False

    def refresh_resolution(self):
        with self._lock:
            if not self._connected or not self._display:
                return False
            try:
                w, h, _, _, _, _ = self._display.get_screen_resolution(0)
                if w > 0 and h > 0 and (w != self.width or h != self.height):
                    self.width = w
                    self.height = h
                    log.info(f"VM resolution changed to {w}x{h}")
                    return True
            except Exception as e:
                log.error(f"Resolution refresh failed: {e}")
            return False

    def disconnect(self):
        self._connected = False
        if self._session:
            try:
                self._session.unlock_machine()
            except Exception:
                pass
            self._session = None
        self._display = None
        self._mouse = None
