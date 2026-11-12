"""Quit watcher for one headed Chrome, run as its own process beside the browser.

A headed Chrome starts with --no-startup-window so it never takes the keyboard, and Chrome
started that way does not exit on Quit: the quit Apple Event closes every window and the
process stays, with its Dock icon and the profile lock. The server used to end it from inside,
which only worked while that server was alive and healthy. A stdio server exits on a client
reconnect, and one started before a reinstall keeps running old code, so the Quit never
finished and the person had to click the Dock icon.

So the server starts this file for each such browser, detached (its own session), with the
interpreter's -I flag and the source on stdin: nothing is read from the installed package
after it starts, and a reinstall or the server's exit does not touch it.

Standard library only, every import at the top. It polls the browser's DevTools HTTP endpoint;
after `grace` seconds with no page target it writes the quit verdict file, sends Browser.close
over the browser websocket and waits for the PID to end. Only if it does not, it signals that
one PID, after checking that its command line still names the same --user-data-dir (PID reuse).
It exits once Chrome is gone and never acts on any other process. An unanswered endpoint never
counts as "no windows".

A `tool_closed` file next to the verdict means a tool emptied the browser on purpose (tab_close
of the last tab): not a Quit, while the server that wrote it is still running.
"""
import base64
import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time

NAME = "mcp-stealth-quit-watcher"


def process_command(pid):
    """The command line of a live PID, or None when no such process runs (a zombie is gone)."""
    try:
        out = subprocess.run(["ps", "-ww", "-o", "stat=,command=", "-p", str(int(pid))],
                             capture_output=True, text=True, timeout=5).stdout.strip()
    except Exception:
        return ""  # could not look: not proof of anything, so "running, unknown command"
    if not out:
        return None
    stat, _, command = out.partition(" ")
    if "Z" in stat:
        return None
    return command.strip()


def names_profile(command, user_data_dir):
    return bool(command) and f"--user-data-dir={user_data_dir}" in command


def http_get(host, port, path, timeout=2.0):
    """GET on the DevTools HTTP endpoint. Returns the body, or None on any failure.

    Chrome keeps the connection open whatever Connection header it is sent, so the body is
    read to its Content-Length rather than to the end of the stream."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\n"
                         f"Connection: close\r\n\r\n".encode())
            raw = b""
            while b"\r\n\r\n" not in raw:
                data = sock.recv(65536)
                if not data:
                    return None
                raw += data
            head, _, body = raw.partition(b"\r\n\r\n")
            status = head.split(b"\r\n", 1)[0].split()
            if len(status) < 2 or status[1] != b"200":
                return None
            length = None
            for line in head.split(b"\r\n")[1:]:
                name, _, value = line.partition(b":")
                if name.strip().lower() == b"content-length":
                    length = int(value.strip())
            while length is None or len(body) < length:
                data = sock.recv(65536)
                if not data:
                    break
                body += data
            if length is not None and len(body) < length:
                return None
            return body if length is None else body[:length]
    except Exception:
        return None


def page_count(host, port):
    """Page targets the browser has, or None when the endpoint did not answer."""
    body = http_get(host, port, "/json/list")
    if body is None:
        return None
    try:
        targets = json.loads(body.decode("utf-8", "replace"))
    except ValueError:
        return None
    if not isinstance(targets, list):
        return None
    return sum(1 for target in targets if isinstance(target, dict) and target.get("type") == "page")


def request_browser_close(host, port, timeout=2.0):
    """Send Browser.close over the browser websocket. True when Chrome acknowledged it or
    dropped the socket on its way out; whether it went is decided by watching the PID."""
    body = http_get(host, port, "/json/version")
    try:
        url = json.loads(body.decode("utf-8", "replace"))["webSocketDebuggerUrl"]
    except Exception:
        return False
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except Exception:
        return False
    try:
        sock.settimeout(timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        sock.sendall(f"GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\n"
                     f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                     f"Sec-WebSocket-Version: 13\r\n\r\n".encode())
        reply = b""
        while b"\r\n\r\n" not in reply:
            data = sock.recv(4096)
            if not data:
                return False
            reply += data
        if b" 101 " not in reply.split(b"\r\n", 1)[0]:
            return False
        payload = json.dumps({"id": 1, "method": "Browser.close"}).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        else:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        try:
            return sock.recv(4096) is not None  # an answer, or b"" when Chrome closed the socket
        except socket.timeout:
            return False
        except OSError:
            return True
    except OSError:
        return True  # the socket dropped mid-request: Chrome closes it as it goes
    finally:
        try:
            sock.close()
        except OSError:
            pass


def remove(path):
    try:
        os.unlink(path)
    except OSError:
        pass


def write_private(path, data):
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(data, handle)
    except OSError:
        pass


def gone(pid, user_data_dir):
    """True on positive evidence that the PID no longer runs this browser: no such process,
    or one whose command line does not name the profile (the number was handed out again)."""
    command = process_command(pid)
    return command is None or (command != "" and not names_profile(command, user_data_dir))


def wait_gone(pid, user_data_dir, within):
    """True once the browser's PID is gone, checked every 50 ms for `within` seconds."""
    deadline = time.monotonic() + within
    while not gone(pid, user_data_dir):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def send_signal(pid, sig):
    try:
        os.kill(pid, sig)
    except OSError:
        pass


def end_browser(cfg):
    """Ask the browser to close, then signal its one PID if it did not end. Never any other."""
    pid, data_dir = cfg["pid"], cfg["user_data_dir"]
    request_browser_close(cfg["host"], cfg["port"])
    if wait_gone(pid, data_dir, float(cfg.get("close_wait", 5.0))):
        return "closed"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        # Signalled only while its command line still names this profile.
        if not names_profile(process_command(pid), data_dir):
            return "closed" if gone(pid, data_dir) else "still running"
        send_signal(pid, sig)
        if wait_gone(pid, data_dir, 2.0):
            return "signalled"
    return "still running"


def server_alive(cfg):
    """The server that started this watcher is still its parent. No signal is sent to it."""
    return os.getppid() == cfg.get("server_pid")


def watch(cfg, sleep=time.sleep, clock=time.monotonic):
    """Poll until the browser is gone. Returns why it stopped."""
    pid, data_dir = cfg["pid"], cfg["user_data_dir"]
    grace, interval = float(cfg.get("grace", 3.0)), float(cfg.get("interval", 0.5))
    tool_closed, quit_file = cfg["tool_closed_file"], cfg["quit_file"]
    zero_since = None
    while True:
        command = process_command(pid)
        if command is None:
            return "gone"
        if command and not names_profile(command, data_dir):
            return "pid reused"
        pages = page_count(cfg["host"], cfg["port"])
        if pages is None or pages > 0:
            zero_since = None  # unanswered is not "no windows"
            if pages:
                remove(tool_closed)  # the browser has pages again: that tab_close is over
            sleep(interval)
            continue
        if os.path.exists(tool_closed) and server_alive(cfg):
            zero_since = None
            sleep(interval)
            continue
        now = clock()
        if zero_since is None:
            zero_since = now
        if now - zero_since < grace:
            sleep(interval)
            continue
        # Written before closing starts, so a call arriving meanwhile reads a Quit, not a crash.
        write_private(quit_file, {"pid": pid, "port": cfg["port"], "user_data_dir": data_dir,
                                  "at": time.time()})
        outcome = end_browser(cfg)
        if outcome == "still running":
            remove(quit_file)
            zero_since = clock()  # try again after another grace
            sleep(interval)
            continue
        return "quit: " + outcome


def main(argv):
    cfg = json.loads(argv[-1])
    for sig in (signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    try:
        watch(cfg)
    finally:
        remove(cfg["tool_closed_file"])
        if not server_alive(cfg):
            remove(cfg["quit_file"])  # nobody is left to read the verdict


if __name__ == "__main__":
    main(sys.argv)
