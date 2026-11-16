"""
Module: Video App (M4-S2)
TARS-AI Built-in Commands Initiative
Description: Pygame app that manages an mpv subprocess for video playback.
             Follows the TARS-AI app framework (init/reset/update/render/cleanup).

Payload store (module-level, one lock): _pending_play / _stop_requested /
_playing / _generation — eyes precedent (module_app_eyes.py).
"""
import os
import sys
import time
import threading
import subprocess
import tempfile

import pygame

from modules.module_messageQue import queue_message

# -----------------------------------------------------------------------
# Payload store — module-level, one lock (eyes precedent)
# -----------------------------------------------------------------------

_payload_lock = threading.Lock()
_pending_play = None       # {"url": str, "title": str} or None
_stop_requested = False
_playing = False
_generation = 0            # bumped on every request_play
_should_exit = False       # set by VideoApp when it wants to exit
_restore_prior = True      # whether to restore prior app on exit


def request_play(url, title=""):
    """Queue a play request from any thread (called from the skill thread)."""
    global _pending_play, _generation
    with _payload_lock:
        _pending_play = {"url": url, "title": title}
        _generation += 1


def request_stop():
    """Signal a stop request from any thread."""
    global _stop_requested
    with _payload_lock:
        _stop_requested = True


def is_playing():
    with _payload_lock:
        return _playing


def drain_exit_request():
    """Called by the UIManager run loop — returns (should_exit, restore_prior)."""
    global _should_exit, _restore_prior
    with _payload_lock:
        exit_now = _should_exit
        restore = _restore_prior
        _should_exit = False
        _restore_prior = True
    return exit_now, restore


# -----------------------------------------------------------------------
# VideoApp
# -----------------------------------------------------------------------


class VideoApp:
    """AppManager app that spawns mpv in its own fullscreen window.

    Contract: __init__(render_surface, width, height), reset(), update(),
    render(), cleanup(), optional handle_event(event).
    Cleanup is PYGAME-FREE (proc + socket only) — thread-safe from
    the side-effect thread.
    """

    _MPV_POLICY_FMT = (
        "bv[vcodec^=avc1][ext=mp4][height<=720]+ba[ext=m4a]/"
        "bv[height<=720]+ba/b[height<=720]/bv+ba/b"
    )

    def __init__(self, render_surface, width, height):
        self.surface = render_surface
        self.width = width
        self.height = height
        self._proc = None
        self._sock_path = None
        self._my_generation = 0
        self._title = ""
        self._last_error = ""

        self._font = pygame.font.Font(None, 24)

    def reset(self):
        """Drain payload store atomically and spawn mpv if play is pending."""
        global _pending_play, _stop_requested, _playing, _generation

        with _payload_lock:
            play = _pending_play
            _pending_play = None
            _stop_requested = False
            self._my_generation = _generation

        if play:
            self._title = play.get("title", "")
            _playing = True
            self._spawn_mpv(play["url"])
        else:
            _playing = True

    def update(self):
        """Called every frame on the UI thread."""
        global _stop_requested, _pending_play, _generation

        if self._proc is None:
            return

        rc = self._proc.poll()
        if rc is not None:
            self._handle_mpv_exit(rc)
            return

        self._drain_ipc()

        with _payload_lock:
            stop = _stop_requested
            _stop_requested = False
            replay = _pending_play
            new_gen = _generation

        if stop:
            self._send_ipc_quit()
            return

        if replay and new_gen > self._my_generation:
            self._send_ipc_quit()
            return

    def render(self):
        """Render loading / now-playing / error screen."""
        black = (0, 0, 0)
        white = (255, 255, 255)
        self.surface.fill(black)

        if self._last_error:
            text = self._font.render(self._last_error, True, white)
        elif self._title:
            text = self._font.render(self._title, True, white)
        else:
            text = self._font.render("Loading...", True, white)

        self.surface.blit(text, (20, 20))

    def cleanup(self):
        """Terminate mpv and clean up socket.  PYGAME-FREE."""
        global _playing
        if self._proc is not None:
            self._send_ipc_quit()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait()
            self._proc = None
        if self._sock_path and os.path.exists(self._sock_path):
            try:
                os.unlink(self._sock_path)
            except OSError:
                pass
            self._sock_path = None

        with _payload_lock:
            global _pending_play, _stop_requested
            # D15 generation guard (audit R1-F5): clear the payload ONLY if
            # no newer request arrived since this instance drained — a fresh
            # request during relaunch must survive the old instance's cleanup
            # (AppManager.launch cleans BEFORE constructing the new instance).
            if _generation == self._my_generation:
                _pending_play = None
                _stop_requested = False
            _playing = False

    def handle_event(self, event):
        """Tap-to-stop: any mouse/touch event sends IPC quit."""
        if event.type in (pygame.MOUSEBUTTONDOWN,):
            self._send_ipc_quit()
            return True
        return False

    # -- internals -------------------------------------------------------

    def _spawn_mpv(self, url):
        """Spawn mpv with D14 verbatim policy string."""
        pid_sock = os.path.join(
            tempfile.gettempdir(), f"tars_mpv_{os.getpid()}.sock"
        )
        if os.path.exists(pid_sock):
            try:
                os.unlink(pid_sock)
            except OSError:
                pass

        self._sock_path = pid_sock

        input_conf = os.path.join(
            tempfile.gettempdir(), f"tars_mpv_input_{os.getpid()}.conf"
        )
        with open(input_conf, "w") as f:
            f.write("MBTN_LEFT quit\n")

        argv = [
            "mpv",
            "--fullscreen",
            "--force-window=immediate",
            "--really-quiet",
            f"--input-ipc-server={pid_sock}",
            f"--ytdl-format={self._MPV_POLICY_FMT}",
            f"--input-conf={input_conf}",
            url,
        ]

        popen_kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if sys.platform == "linux":
            try:
                import ctypes
                import ctypes.util

                _libc = ctypes.CDLL(
                    ctypes.util.find_library("c"), use_errno=True
                )
                PR_SET_PDEATHSIG = 1
                SIGTERM = 15

                def _preexec():
                    _libc.prctl(PR_SET_PDEATHSIG, SIGTERM)

                popen_kw["preexec_fn"] = _preexec
            except Exception:
                pass

        self._proc = subprocess.Popen(argv, **popen_kw)

        try:
            os.unlink(input_conf)
        except OSError:
            pass

    def _send_ipc_quit(self):
        """Send quit command to mpv via IPC socket."""
        if not self._sock_path:
            return
        try:
            import socket as _socket

            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(0.5)
            sock.connect(self._sock_path)
            sock.sendall(b'{"command":["quit"]}\n')
            sock.close()
        except Exception:
            pass

    def _drain_ipc(self):
        """Non-blocking drain of mpv IPC socket for end-file events."""
        if not self._sock_path:
            return
        try:
            import socket as _socket
            import json

            sock = _socket.socket(_socket.AF_UNIX, _socket.SOCK_STREAM)
            sock.settimeout(0)
            try:
                sock.connect(self._sock_path)
                sock.setblocking(False)
                data = b""
                while True:
                    try:
                        chunk = sock.recv(4096)
                        if not chunk:
                            break
                        data += chunk
                    except BlockingIOError:
                        break
                if data:
                    for line in data.split(b"\n"):
                        if not line.strip():
                            continue
                        try:
                            msg = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if msg.get("event") == "end-file":
                            self._send_ipc_quit()
                            break
            except Exception:
                pass
            finally:
                sock.close()
        except Exception:
            pass

    def _handle_mpv_exit(self, rc):
        """mpv process exited — set error state and request exit."""
        self._proc = None
        if rc != 0 and rc != -9:  # -9 = SIGKILL from stop
            self._last_error = f"Video playback error (code {rc})"
        self._request_exit(restore=True)

    def _request_exit(self, restore=True):
        """Request the UIManager run loop to exit this app."""
        global _should_exit, _restore_prior
        with _payload_lock:
            _should_exit = True
            _restore_prior = restore