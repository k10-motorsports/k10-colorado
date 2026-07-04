#!/usr/bin/env python3
"""Send Python to the running Blender's K10 live bridge (live_server.py) and print the result.

    python3 blender/live_client.py 'print(len(bpy.data.objects))'
    python3 blender/live_client.py -f some_snippet.py
    echo 'bpy.ops.mesh.primitive_cube_add()' | python3 blender/live_client.py -

`bpy` is in scope Blender-side and globals persist between calls. Set `_ = <value>` in your code to return
it. Exits nonzero (and prints the Blender traceback to stderr) if the code raised. Localhost only.
"""
import json
import socket
import sys

HOST, PORT = "127.0.0.1", 9761


def main() -> int:
    args = sys.argv[1:]
    if not args:
        print(__doc__); return 2
    if args[0] == "-f":
        code = open(args[1]).read()
    elif args[0] == "-":
        code = sys.stdin.read()
    else:
        code = args[0]
    try:
        s = socket.create_connection((HOST, PORT), timeout=300)
    except OSError as e:
        sys.stderr.write(f"[k10-live] no bridge on {HOST}:{PORT} ({e}). In Blender, run blender/live_server.py.\n")
        return 3
    s.sendall((json.dumps({"code": code}) + "\n").encode("utf-8"))
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(1 << 16)
        if not chunk:
            break
        buf += chunk
    s.close()
    r = json.loads(buf.decode("utf-8"))
    sys.stdout.write(r.get("stdout", ""))
    if r.get("result") not in (None, "None"):
        print("=>", r["result"])
    if not r.get("ok"):
        sys.stderr.write(r.get("error", "unknown error") + "\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
