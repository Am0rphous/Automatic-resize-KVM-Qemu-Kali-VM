#!/usr/bin/env python3
import os
import time
import subprocess
import sys
import logging
import json
import base64
from Xlib import display, X
import threading

# Run this script or simply run "xrandr --output Virtual-1 --auto" within the VM

# === Configure here ===
VM_NAME = "kali1"             # exact name from `virsh list --all`
WIN_NAME_PART = "kali1"       # or part of the VM window's title/class
DELAY_SECONDS = 2             # debounce: wait after last resize before executing
GUEST_EXEC_POLL_INTERVAL = 2  # seconds between polling guest-exec status
# =======================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")

if "DISPLAY" not in os.environ:
    os.environ["DISPLAY"] = ":0"
if "XAUTHORITY" not in os.environ:
    possible = [os.path.expanduser("~/.Xauthority"), "/run/user/{}/gdm/Xauthority".format(os.getuid())]
    for p in possible:
        if os.path.exists(p):
            os.environ["XAUTHORITY"] = p
            break

try:
    d = display.Display()
except Exception as e:
    logging.error("Could not connect to X display: %s", e)
    sys.exit(1)

root = d.screen().root

def find_window_recursive(win, part):
    part = part.lower()
    try:
        name = (win.get_wm_name() or "").lower()
        cls = [c.lower() for c in (win.get_wm_class() or [])]
        if part in name or any(part in c for c in cls):
            return win
    except Exception:
        pass
    try:
        tree = win.query_tree()
    except Exception:
        return None
    for ch in tree.children:
        res = find_window_recursive(ch, part)
        if res:
            return res
    return None

logging.info("Searching for window containing: %s", WIN_NAME_PART)
w = find_window_recursive(root, WIN_NAME_PART)
if not w:
    logging.info("Window not found immediately. Waiting up to 30s for the window to appear.")
    for _ in range(30):
        w = find_window_recursive(root, WIN_NAME_PART)
        if w:
            break
        time.sleep(1)
    if not w:
        logging.error("Window still not found; exiting.")
        sys.exit(1)

try:
    wid = getattr(w, 'id', None)
    wname = w.get_wm_name()
except Exception:
    wid = None
    wname = None
logging.info("Found window id=%s name=%s", wid, wname)

try:
    w.change_attributes(event_mask = X.StructureNotifyMask)
except Exception as e:
    logging.warning("Could not set event_mask on the window: %s", e)

# Default guest command (adjust path/user as needed)
# Using user "kali"
guest_cmd = (
    '{"execute":"guest-exec","arguments":'
    '{"path":"/usr/bin/env",'
    '"arg":["bash","-lc",'
    '"sudo -u kali XAUTHORITY=/home/kali/.Xauthority DISPLAY=:0 /usr/bin/xrandr --output Virtual-1 --auto || sudo -u kali XAUTHORITY=/home/kali/.Xauthority DISPLAY=:0 /usr/bin/xrandr --auto"],'
    '"capture-output":true}}'
)

# Debounce state
last_geom = None
_timer = None
_lock = threading.Lock()

# Try to get initial size from the window
try:
    g = w.get_geometry()
    last_geom = (g.width, g.height)
    logging.info("Initial size: %sx%s", g.width, g.height)
except Exception:
    last_geom = None

def _virsh_qemu_agent(cmd_json):
    """Run virsh qemu-agent-command and return (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            ["virsh", "-c", "qemu:///system", "qemu-agent-command", VM_NAME, cmd_json, "--timeout", "5000"],
            capture_output=True, text=True
        )
        return proc.returncode, proc.stdout, proc.stderr
    except FileNotFoundError:
        logging.error("virsh not found. Ensure libvirt/virsh is installed.")
        return 127, "", "virsh not found"
    except Exception as ex:
        logging.error("Error running virsh: %s", ex)
        return 1, "", str(ex)

def send_guest_exec():
    # Start guest-exec and get pid from JSON response
    rc, out, err = _virsh_qemu_agent(guest_cmd)
    if rc != 0:
        logging.error("virsh failed starting guest-exec (rc=%s) stderr=%s", rc, err.strip())
        return

    try:
        j = json.loads(out)
    except Exception as ex:
        logging.error("Could not parse guest-exec JSON: %s", ex)
        logging.debug("Raw output: %s", out)
        return

    pid = j.get("return", {}).get("pid")
    if not pid:
        logging.error("Did not get pid from guest-exec response: %s", out.strip())
        return
    logging.info("guest-exec started in guest, pid=%s", pid)

    # Poll guest-exec-status until finished, then decode and log output
    status_cmd = json.dumps({
        "execute": "guest-exec-status",
        "arguments": {"pid": pid}
    })

    while True:
        time.sleep(GUEST_EXEC_POLL_INTERVAL)
        rc2, out2, err2 = _virsh_qemu_agent(status_cmd)
        if rc2 != 0:
            logging.error("virsh failed querying guest-exec-status (rc=%s) stderr=%s", rc2, err2.strip())
            return
        try:
            sj = json.loads(out2)
        except Exception as ex:
            logging.error("Could not parse guest-exec-status JSON: %s", ex)
            logging.debug("Raw status output: %s", out2)
            return

        ret = sj.get("return", {})
        # If the process has exited or was signaled
        if ret.get("exited") or ret.get("signal"):
            out_b64 = ret.get("out-data")
            err_b64 = ret.get("err-data")
            if out_b64:
                try:
                    decoded = base64.b64decode(out_b64).decode(errors="replace")
                    logging.info("guest stdout:\n%s", decoded.strip())
                except Exception as ex:
                    logging.warning("Could not decode stdout: %s", ex)
            if err_b64:
                try:
                    decoded = base64.b64decode(err_b64).decode(errors="replace")
                    logging.warning("guest stderr:\n%s", decoded.strip())
                except Exception as ex:
                    logging.warning("Could not decode stderr: %s", ex)

            logging.info("guest-exec finished: exited=%s signal=%s exitcode=%s",
                         ret.get("exited"), ret.get("signal"), ret.get("exitcode"))
            return
        else:
            logging.debug("guest-exec-status: running (pid=%s)", pid)

def debounce_timer():
    global _timer
    with _lock:
        _timer = None
    send_guest_exec()

def schedule_debounce():
    global _timer
    with _lock:
        if _timer is not None:
            _timer.cancel()
        _timer = threading.Timer(DELAY_SECONDS, debounce_timer)
        _timer.daemon = True
        _timer.start()

logging.info("Monitoring window for changes. VM=%s", VM_NAME)

# Event loop
while True:
    e = d.next_event()
    if e.type == X.ConfigureNotify:
        try:
            ev_win = getattr(e, 'window', None)
            if ev_win is None:
                continue
            if getattr(ev_win, 'id', None) != getattr(w, 'id', None):
                continue
        except Exception:
            continue

        geom = (e.width, e.height)
        if geom != last_geom:
            last_geom = geom
            logging.info("Resize detected for window id=%s: %sx%s", getattr(w, 'id', None), e.width, e.height)
            schedule_debounce()
        else:
            logging.debug("ConfigureNotify without size change: %sx%s", e.width, e.height)
