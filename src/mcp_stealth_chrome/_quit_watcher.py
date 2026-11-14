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

A browser launched with keep_alive stays when its server exits, and this watcher with it. Two
more settings cover it: `max_orphan_seconds` ends the browser once it has had no server for that
long (MCP_STEALTH_KEEP_ALIVE_MAX_HOURS), and `pages_matter` false (headless) skips the window
logic, since nobody can quit a headless browser. `keep_alive_record` is removed once Chrome is
gone, if it still names this PID.
"""
import base64
import hashlib
import json
import os
import re
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


def process_starts(pids, legacy=False):
    """{pid: start} for the PIDs that run, start as ps prints it (lstart, UTC, C locale).

    With the command line this is a process's identity: a PID handed out again belongs to a
    process that started later. Missing PIDs are not in the result. None when ps cannot answer
    (and always on Windows, whose ps, if any, is Git Bash's)."""
    pids = [int(p) for p in pids if isinstance(p, int) and p > 0]
    if not pids or sys.platform == "win32":
        return None
    if sys.platform.startswith("linux") and not legacy:
        return _linux_starts(pids)
    return _ps_starts(pids)


def _ps_starts(pids):
    """{pid: start} from ps lstart, the format every start time had before Linux read ticks."""
    try:
        result = subprocess.run(["ps", "-o", "pid=,lstart=", "-p", ",".join(str(p) for p in pids)],
                                capture_output=True, text=True, timeout=5,
                                env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
    except Exception:
        return None
    found = {}
    for line in result.stdout.splitlines():
        head, _, start = line.strip().partition(" ")
        if head.isdigit() and start.strip():
            found[int(head)] = " ".join(start.split())
    if not found and result.returncode not in (0, 1):
        return None
    return found


def _linux_starts(pids):
    """Linux: the start time in clock ticks since boot, from /proc/<pid>/stat (field 22). ps
    prints whole seconds, and two processes on one PID within a second read the same."""
    found = {}
    for pid in pids:
        try:
            with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
            # The name in parentheses can hold spaces and parentheses; fields follow the last ")".
            fields = text[text.rindex(")") + 2:].split()
            found[pid] = f"ticks:{int(fields[19])}"
        except (OSError, ValueError, IndexError):
            continue
    return found


def process_start(pid, like=None):
    """When this PID's process started, or None when it does not run or cannot be read.

    `like` is a start time to compare with: it is read in that one's format, so a record
    written with ps lstart text, before Linux read clock ticks, still identifies its process."""
    legacy = bool(like) and not str(like).startswith("ticks:")
    found = process_starts([pid], legacy)
    return None if found is None else found.get(int(pid))


def generation(start):
    """A short tag for one process generation, for file names."""
    return hashlib.sha256(str(start).encode()).hexdigest()[:10] if start else "unknown"


# A flag's value ends where the command line ends or the next argument starts: another flag,
# or the URL Chrome was given. ps drops quoting, so a value may hold spaces ("Application
# Support"), and a bare substring let /tmp/profile claim /tmp/profile-evil.
_VALUE_END = r"(?=$|\s+(?:--|[A-Za-z][A-Za-z0-9+.-]*:))"


def has_flag(command, flag, value):
    """Whether a command line carries `flag=value` with exactly that value."""
    return bool(command) and re.search(re.escape(f"{flag}={value}") + _VALUE_END, command) is not None


def names_profile(command, user_data_dir):
    return has_flag(command, "--user-data-dir", user_data_dir)


def identity(pid, start, matches):
    """'same', 'different', 'gone' or 'unknown' for the process a PID runs now, against the one
    identified by its start time and a predicate on its command line. Unknown is never same."""
    command = process_command(pid)
    if command is None:
        return "gone"
    if command == "":
        return "unknown"
    if not matches(command):
        return "different"
    now = process_start(pid, start)
    if now is None or not start:
        return "unknown"
    return "same" if now == start else "different"


def signal_if_same(pid, sig, start, matches):
    """Signal a PID only while it is still the process identified, checked just before.

    Linux: through a pidfd opened before the check, so the signal reaches exactly the process
    checked even if it ends and its PID is reused meanwhile. Elsewhere the check and the kill
    are a few system calls apart. True when sent."""
    fd = None
    if hasattr(os, "pidfd_open") and hasattr(signal, "pidfd_send_signal"):
        try:
            fd = os.pidfd_open(int(pid))
        except OSError:
            return False
    try:
        if identity(pid, start, matches) != "same":
            return False
        if fd is not None:
            signal.pidfd_send_signal(fd, sig)
        else:
            os.kill(int(pid), sig)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            os.close(fd)


def browser_of(user_data_dir):
    """The predicate for a browser's main process on this profile."""
    return lambda command: "--type=" not in command and names_profile(command, user_data_dir)


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


def _read_exact(sock, count):
    data = b""
    while len(data) < count:
        chunk = sock.recv(count - len(data))
        if not chunk:
            raise OSError("socket closed")
        data += chunk
    return data


def browser_call(host, port, method, timeout=3.0, params=None):
    """One CDP call on the browser websocket, its `result` dict, or None on any failure."""
    body = http_get(host, port, "/json/version")
    try:
        url = json.loads(body.decode("utf-8", "replace"))["webSocketDebuggerUrl"]
    except Exception:
        return None
    path = "/" + url.split("://", 1)[-1].split("/", 1)[-1]
    try:
        sock = socket.create_connection((host, int(port)), timeout=timeout)
    except Exception:
        return None
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
                return None
            reply += data
        if b" 101 " not in reply.split(b"\r\n", 1)[0]:
            return None
        leftover = reply.partition(b"\r\n\r\n")[2]
        message = {"id": 1, "method": method}
        if params:
            message["params"] = params
        payload = json.dumps(message).encode()
        mask = os.urandom(4)
        header = bytes([0x81])
        if len(payload) < 126:
            header += bytes([0x80 | len(payload)])
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        sock.sendall(header + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))

        class _Buffered:
            def __init__(self, first):
                self.first = first

            def recv(self, count):
                if self.first:
                    chunk, self.first = self.first[:count], self.first[count:]
                    return chunk
                return sock.recv(count)

        reader = _Buffered(leftover)
        for _ in range(50):
            head = _read_exact(reader, 2)
            length = head[1] & 0x7F
            if length == 126:
                length = struct.unpack(">H", _read_exact(reader, 2))[0]
            elif length == 127:
                length = struct.unpack(">Q", _read_exact(reader, 8))[0]
            data = _read_exact(reader, length)
            if head[0] & 0x0F != 0x1:
                continue
            message = json.loads(data.decode("utf-8", "replace"))
            if isinstance(message, dict) and message.get("id") == 1:
                result = message.get("result")
                return result if isinstance(result, dict) else None
        return None
    except Exception:
        return None
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


def gone(pid, user_data_dir, start=None):
    """True on positive evidence that the PID no longer runs this browser: no such process,
    one whose command line does not name the profile, or one started at another time (the
    number was handed out again)."""
    command = process_command(pid)
    if command is None:
        return True
    if command == "":
        return False
    if not names_profile(command, user_data_dir):
        return True
    if start:
        now = process_start(pid, start)
        return now is not None and now != start
    return False


def wait_gone(pid, user_data_dir, within, start=None):
    """True once the browser's PID is gone, checked every 50 ms for `within` seconds."""
    deadline = time.monotonic() + within
    while not gone(pid, user_data_dir, start):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.05)
    return True


def end_browser(cfg):
    """Ask the browser to close, then signal its one PID if it did not end. Never any other:
    each signal goes only to the process with the recorded start time and profile, checked
    right before it, and none at all when that cannot be proved."""
    pid, data_dir, start = cfg["pid"], cfg["user_data_dir"], cfg.get("pid_start")
    if gone(pid, data_dir, start):
        return "closed"
    request_browser_close(cfg["host"], cfg["port"])
    if wait_gone(pid, data_dir, float(cfg.get("close_wait", 5.0)), start):
        return "closed"
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not signal_if_same(pid, sig, start, browser_of(data_dir)):
            if gone(pid, data_dir, start):
                return "closed"
            # Still there but not provably this browser: say which, so the caller can stop.
            unknown = identity(pid, start, browser_of(data_dir)) == "unknown"
            return "unidentified" if unknown else "still running"
        if wait_gone(pid, data_dir, 2.0, start):
            return "signalled"
    return "still running"


def server_alive(cfg):
    """The server that started this watcher is still its parent. No signal is sent to it."""
    return os.getppid() == cfg.get("server_pid")


# Tries to end a browser whose process cannot be identified before the watcher stops and
# leaves the reason in the verdict file. Without a bound it retried after every grace for ever.
UNIDENTIFIED_TRIES = 3
NOT_ENDED = ("its process could not be identified (command line or start time unreadable), so it "
             "was not signalled and may still be running")


def watch(cfg, sleep=time.sleep, clock=time.monotonic):
    """Poll until the browser is gone. Returns why it stopped."""
    pid, data_dir = cfg["pid"], cfg["user_data_dir"]
    grace, interval = float(cfg.get("grace", 3.0)), float(cfg.get("interval", 0.5))
    tool_closed, quit_file = cfg["tool_closed_file"], cfg["quit_file"]
    max_orphan = cfg.get("max_orphan_seconds")
    zero_since = None
    orphan_since = None
    unidentified = 0

    def give_up():
        write_private(quit_file, {"pid": pid, "port": cfg["port"], "user_data_dir": data_dir,
                                  "pid_start": cfg.get("pid_start"), "at": time.time(),
                                  "not_ended": NOT_ENDED})
        return "gave up: identity unreadable"
    while True:
        command = process_command(pid)
        if command is None:
            return "gone"
        if command and not names_profile(command, data_dir):
            return "pid reused"
        if cfg.get("pid_start"):
            now = process_start(pid, cfg["pid_start"])
            if now is not None and now != cfg["pid_start"]:
                return "pid reused"  # another Chrome on the same PID, profile and port
        if max_orphan:
            # Kept alive with no server: counted from when the server went, not from launch.
            if server_alive(cfg):
                orphan_since = None
            else:
                now = clock()
                if orphan_since is None:
                    orphan_since = now
                if now - orphan_since >= float(max_orphan):
                    outcome = end_browser(cfg)
                    if outcome == "unidentified":
                        unidentified += 1
                        if unidentified >= UNIDENTIFIED_TRIES:
                            return give_up()
                    elif outcome != "still running":
                        return "time cap: " + outcome
                    orphan_since = clock()
        if cfg.get("pages_matter", True) is False:
            sleep(interval)
            continue
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
                                  "pid_start": cfg.get("pid_start"), "at": time.time()})
        outcome = end_browser(cfg)
        if outcome == "unidentified":
            unidentified += 1
            if unidentified >= UNIDENTIFIED_TRIES:
                return give_up()
        if outcome in ("still running", "unidentified"):
            remove(quit_file)
            zero_since = clock()  # try again after another grace
            sleep(interval)
            continue
        return "quit: " + outcome


def main(argv):
    cfg = json.loads(argv[-1])
    for sig in (signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, signal.SIG_IGN)
    # The stdio server ignores SIGTERM and a child inherits that, so the server's terminate()
    # did nothing: a tool close and a reattach must be able to end this watcher.
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    try:
        watch(cfg)
    finally:
        remove(cfg["tool_closed_file"])
        if not server_alive(cfg) and not _not_ended(cfg["quit_file"]):
            remove(cfg["quit_file"])  # nobody is left to read the verdict
        forget_kept(cfg)


def _not_ended(path):
    """A verdict saying the browser was left running is kept for whoever looks next."""
    try:
        with open(path, encoding="utf-8") as handle:
            return bool(json.load(handle).get("not_ended"))
    except (OSError, ValueError, AttributeError):
        return False


def forget_kept(cfg):
    """Remove the keep_alive record of a Chrome that is gone, only if it still names that PID."""
    path = cfg.get("keep_alive_record")
    if not path or not gone(cfg["pid"], cfg["user_data_dir"], cfg.get("pid_start")):
        return
    try:
        if os.path.islink(path):
            return
        with open(path, encoding="utf-8") as handle:
            record = json.load(handle)
    except (OSError, ValueError):
        return
    if not isinstance(record, dict) or record.get("pid") != cfg["pid"]:
        return
    if record.get("pid_start") and cfg.get("pid_start") and record["pid_start"] != cfg["pid_start"]:
        return  # a record of another Chrome generation on the same PID
    remove(path)


if __name__ == "__main__":
    main(sys.argv)
