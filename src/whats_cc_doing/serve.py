"""`ccdoing serve`: a tiny no-store static server for the status output.

Exists for the remote-dev case: the box generating status.html is a
headless server / WSL2 / cloud instance where "just open the file" is
not a thing. This serves the output directory over HTTP with
Cache-Control: no-store on EVERY response - a cached status page is a
lying status page - and nothing else. Binds 127.0.0.1 by default;
exposing it wider (--bind 0.0.0.0) is an explicit user choice, and the
page contents (process names, session ids, file paths) should be
treated as internal before doing so.

stdlib only, deliberately: no routes, no API, no auth to get wrong.
"""

from __future__ import annotations

import http.server
import json
import mimetypes
import os
import signal as _signal
import subprocess
import sys
import time
import urllib.parse
from functools import partial
from pathlib import Path

from . import dash, registry


class NoStoreHandler(http.server.SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler that disables caching on every response."""

    def end_headers(self) -> None:  # noqa: D102 - see class docstring
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:
        # One line per request is fine; silence only the noisy favicon 404s.
        first = args[0] if args and isinstance(args[0], str) else ""
        if "favicon.ico" in first:
            return
        super().log_message(fmt, *args)


def make_server(
    directory: Path, port: int = 8377, bind: str = "127.0.0.1"
) -> http.server.ThreadingHTTPServer:
    handler = partial(NoStoreHandler, directory=str(directory))
    return http.server.ThreadingHTTPServer((bind, port), handler)


def _print_urls(url: str, *, daemonized: bool = False) -> None:
    """The link is the whole point - print it where it cannot be missed."""
    print()
    print(f"  ==>  {url}")
    print()
    print("  (WSL2: localhost usually works from the Windows browser, but not"
          " always - if not, open it in a Linux browser, e.g. WSLg Chrome)")
    if daemonized:
        print("  running in the background - `ccdoing serve stop` to stop, "
              "`ccdoing serve status` to check")
    else:
        print("  Cache-Control: no-store on all responses; ctrl-c to stop")


def serve(directory: Path, port: int = 8377, bind: str = "127.0.0.1",
          pidfile: Path | None = None) -> int:
    httpd = make_server(directory, port=port, bind=bind)
    host = bind if bind != "0.0.0.0" else "<this-host>"  # noqa: S104 - display only
    actual_port = httpd.server_address[1]
    url = f"http://{host}:{actual_port}/status.html"
    print(f"serving {directory}")
    _print_urls(url)
    if not (directory / "status.html").is_file():
        print("note: no status.html yet - run `ccdoing tick` first")
    if pidfile is not None:
        write_pidfile(pidfile, port=actual_port, bind=bind, url=url, all_projects=False)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if pidfile is not None:
            pidfile.unlink(missing_ok=True)
    return 0


# --------------------------------------------------------------------------
# `ccdoing serve --all`: registry-wide dashboard.
#
# Routes (everything else 404s, no-store on every response):
#   /                      dashboard - one card per registered project
#   /p/<name>/             wrapper page (top bar + iframe of the status page)
#   /p/<name>/<file>       static file from that project's OWN output dir
#   /multi?p=a&p=b[...]    multi-view: 2-4 status pages side by side
#
# Only registered project names resolve, and files are served strictly from
# each project's configured output_dir (resolved-path containment check), so
# nothing outside the status outputs is ever reachable.


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    server_version = "ccdoing"

    def _send(self, code: int, body: str | bytes,
              ctype: str = "text/html; charset=utf-8") -> None:
        data = body.encode() if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _404(self, why: str = "not found") -> None:
        self._send(404, f"<h1>404</h1><p>{why}</p>")

    def log_message(self, fmt: str, *args) -> None:
        first = args[0] if args and isinstance(args[0], str) else ""
        if "favicon.ico" in first:
            return
        super().log_message(fmt, *args)

    def do_GET(self) -> None:  # noqa: N802 - http.server API
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        if path in ("/", "/index.html"):
            self._send(200, dash.render_dashboard_html(dash.load_cards()))
            return
        if path == "/multi":
            names = urllib.parse.parse_qs(parsed.query).get("p", [])[:4]
            cards = [c for c in (dash.resolve_name(n) for n in names) if c]
            if len(cards) < 2:
                self._404("multi-view needs 2-4 registered project names (?p=a&p=b)")
                return
            self._send(200, dash.render_multiview_html(cards))
            return
        if path.startswith("/p/"):
            parts = path[3:].split("/", 1)
            card = dash.resolve_name(parts[0]) if parts[0] else None
            if card is None or card.output_dir is None:
                self._404("unknown project (only registered names resolve)")
                return
            rel = parts[1] if len(parts) > 1 else ""
            if rel in ("", "index.html"):
                self._send(200, dash.render_wrapper_html(card))
                return
            base = card.output_dir.resolve()
            target = (base / rel).resolve()
            if base not in target.parents and target != base:
                self._404("outside the project's status output")
                return
            if not target.is_file():
                self._404("no such file in the status output")
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype == "application/json":
                ctype += "; charset=utf-8"
            try:
                self._send(200, target.read_bytes(), ctype)
            except OSError:
                self._404("unreadable")
            return
        self._404()


def make_all_server(
    port: int = 8377, bind: str = "127.0.0.1"
) -> http.server.ThreadingHTTPServer:
    return http.server.ThreadingHTTPServer((bind, port), DashboardHandler)


def serve_all(port: int = 8377, bind: str = "127.0.0.1",
              pidfile: Path | None = None) -> int:
    httpd = make_all_server(port=port, bind=bind)
    host = bind if bind != "0.0.0.0" else "<this-host>"  # noqa: S104 - display only
    actual_port = httpd.server_address[1]
    n = len(dash.load_cards())
    url = f"http://{host}:{actual_port}/"
    print(f"serving all-projects dashboard ({n} registered)")
    _print_urls(url)
    if pidfile is not None:
        write_pidfile(pidfile, port=actual_port, bind=bind, url=url, all_projects=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        if pidfile is not None:
            pidfile.unlink(missing_ok=True)
    return 0


# --------------------------------------------------------------------------
# Daemon control: `ccdoing serve --daemon` / `serve stop` / `serve status`.
#
# One pidfile per server: the all-projects dashboard's lives beside the
# registry in XDG state (machine-wide singleton), a per-project server's in
# that project's .ccdoing/. The pidfile is JSON {pid, port, bind, url, all}
# written by the SERVER process itself once its socket is bound, so the
# recorded port is always the real one (works with --port 0). Stale files
# (dead pid) are treated as not-running and cleaned up on sight.


def all_pidfile() -> Path:
    return registry.registry_path().parent / "serve-all.pid"


def project_pidfile(state_dir: Path) -> Path:
    return state_dir / "serve.pid"


def write_pidfile(path: Path, *, port: int, bind: str, url: str,
                  all_projects: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "pid": os.getpid(), "port": port, "bind": bind, "url": url,
        "all": all_projects, "started": time.time(),
    }))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, not ours to signal
    except (OverflowError, ValueError):
        return False
    try:
        # A zombie answers kill(0) but is dead for our purposes - happens
        # when the spawning process is still alive and hasn't reaped yet.
        with open(f"/proc/{pid}/stat") as fh:
            if fh.read().rsplit(")", 1)[1].split()[0] == "Z":
                return False
    except (OSError, IndexError):
        pass  # no /proc (macOS): kill(0) verdict stands
    return True


def read_pidfile(path: Path) -> dict | None:
    """The pidfile's record iff its process is alive; stale files removed."""
    try:
        info = json.loads(path.read_text())
        pid = int(info["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not _pid_alive(pid):
        path.unlink(missing_ok=True)
        return None
    info["pid"] = pid
    return info


def stop_daemon(path: Path) -> str:
    """Stop the server the pidfile names. Returns a human one-liner."""
    info = read_pidfile(path)
    if info is None:
        return "not running"
    try:
        os.kill(info["pid"], _signal.SIGTERM)
    except OSError as exc:
        return f"could not stop pid {info['pid']}: {exc}"
    for _ in range(20):  # up to ~2s for a clean exit
        if not _pid_alive(info["pid"]):
            break
        time.sleep(0.1)
    else:
        try:
            os.kill(info["pid"], _signal.SIGKILL)
        except OSError:
            pass
    path.unlink(missing_ok=True)
    return f"stopped (pid {info['pid']}, was {info.get('url', '?')})"


def start_daemon(args: list[str], pidfile: Path, log_path: Path,
                 timeout: float = 5.0,
                 global_args: list[str] | None = None) -> dict | None:
    """Spawn `ccdoing serve ...` detached; wait for its pidfile.

    args are the serve arguments AFTER `serve` (e.g. ["--all", "--port", "0"]);
    global_args go BEFORE it (e.g. ["--config", "/abs/ccdoing.yaml"] so the
    child resolves the same project regardless of its cwd).
    An already-running server is stopped first (restart semantics). Returns
    the new pidfile record, or None when the child died before binding
    (its last log lines are printed).
    """
    prior = read_pidfile(pidfile)
    if prior is not None:
        print(f"restarting: {stop_daemon(pidfile)}")
    pidfile.unlink(missing_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PYTHONUNBUFFERED="1")  # log lines land promptly
    with open(log_path, "ab") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "whats_cc_doing",
             *(global_args or []), "serve", *args],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            start_new_session=True,  # survives this terminal/session
            env=env,
        )
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_pidfile(pidfile)
        if info is not None and info["pid"] == proc.pid:
            return info
        if proc.poll() is not None:
            break
        time.sleep(0.1)
    try:
        tail = log_path.read_text().splitlines()[-5:]
    except OSError:
        tail = []
    print("serve daemon failed to start" + (":" if tail else ""))
    for line in tail:
        print(f"  {line}")
    return None
