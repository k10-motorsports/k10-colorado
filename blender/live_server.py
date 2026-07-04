"""K10 live Blender bridge — the "operator" socket.

Run this INSIDE your running Blender so the K10 operator can send Python to it and read scene state while
you design. Two ways to load it:
  • Text Editor  → Open live_server.py → Run Script  (starts immediately; re-run to restart)
  • Preferences  → Add-ons → Install… → pick this file → enable "K10 Live Bridge"  (persists across restarts)

It listens on 127.0.0.1:9761. Requests are newline-delimited JSON: {"code": "<python>"} and it replies
{"ok", "stdout", "result", "error"}. Your code runs on Blender's MAIN thread (safe for bpy) via a timer;
the socket runs on a background thread so the UI never blocks. Globals PERSIST between calls (interactive),
and `bpy` is always in scope. Set the module-level name `_` to return a value to the operator.

Nothing here reaches the internet — it binds to localhost only. Stop it by quitting Blender (or disabling
the add-on).
"""
bl_info = {
    "name": "K10 Live Bridge",
    "author": "k10-colorado",
    "version": (1, 0, 0),
    "blender": (4, 2, 0),
    "location": "Runs a localhost socket (127.0.0.1:9761) for the K10 operator",
    "description": "Execute operator-sent Python on the main thread; read/modify the scene live.",
    "category": "Development",
}

import io
import json
import queue
import socket
import threading
import traceback
from contextlib import redirect_stdout

import bpy

PORT = 9761
_req: "queue.Queue" = queue.Queue()
_G = {"bpy": bpy, "__name__": "k10_live"}   # persistent globals across calls (interactive session state)
_server_started = False


def _pump():
    """Main-thread timer: run one queued code block per tick, capture stdout + `_`, hand back the result."""
    try:
        code, ev, holder = _req.get_nowait()
    except queue.Empty:
        return 0.05
    res = {"ok": True, "stdout": "", "result": None, "error": ""}
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            _G.pop("_", None)
            exec(compile(code, "<k10-live>", "exec"), _G)   # noqa: S102 — trusted localhost operator
            r = _G.get("_", None)
            res["result"] = None if r is None else repr(r)
    except Exception:
        res["ok"] = False
        res["error"] = traceback.format_exc()
    res["stdout"] = buf.getvalue()
    holder.append(res)
    ev.set()
    return 0.01


def _serve():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind(("127.0.0.1", PORT))
    except OSError as e:
        print(f"[k10-live] cannot bind :{PORT} ({e}) — another bridge already running?")
        return
    srv.listen(5)
    print(f"[k10-live] listening on 127.0.0.1:{PORT} — operator can drive this Blender")
    while True:
        try:
            conn, _addr = srv.accept()
        except OSError:
            break
        try:
            data = b""
            while not data.endswith(b"\n"):
                chunk = conn.recv(1 << 16)
                if not chunk:
                    break
                data += chunk
            if not data:
                conn.close(); continue
            req = json.loads(data.decode("utf-8"))
            ev = threading.Event(); holder: list = []
            _req.put((req.get("code", ""), ev, holder))
            ev.wait(timeout=300)
            out = holder[0] if holder else {"ok": False, "error": "timeout waiting for main thread"}
            conn.sendall((json.dumps(out) + "\n").encode("utf-8"))
        except Exception as e:  # noqa: BLE001
            try:
                conn.sendall((json.dumps({"ok": False, "error": repr(e)}) + "\n").encode("utf-8"))
            except Exception:
                pass
        finally:
            conn.close()


def _start():
    global _server_started
    if _server_started:
        return
    _server_started = True
    if not bpy.app.timers.is_registered(_pump):
        bpy.app.timers.register(_pump, persistent=True)
    threading.Thread(target=_serve, daemon=True).start()


def register():   # add-on entry point
    _start()


def unregister():
    if bpy.app.timers.is_registered(_pump):
        bpy.app.timers.unregister(_pump)


# Also start when run directly from the Text Editor (Run Script).
_start()
