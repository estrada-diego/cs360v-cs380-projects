#!/usr/bin/env python3
"""serverless.py: your serverless platform.  *** YOU IMPLEMENT the Platform class. ***

The platform sits in front of several backends: `runner` processes
(runner/runner.c), each listening on its own port. It answers HTTP requests to
deploy functions and run them on the backends. Behind those requests it:

  - saves the deployed functions to disk;
  - sends each request to a backend that is up and can run it;
  - gives up on a request after a time limit;
  - stops using backends that stop answering;
  - limits how many requests each backend handles at once;
  - keeps counters;
  - starts more backends when needed, and stops them when idle.

PROVIDED, do not change: the HTTP server (the Handler class), runner_call,
loading and saving the registry, the health and scaling loops, and startup. You
fill in the Platform methods marked TODO. See SPEC.md.

    HTTP requests:
        GET  /healthz                       {"status":"ok"}
        POST /functions  {name,function?}   deploy a function
        GET  /functions                     list them
        GET  /functions/<name>              look one up
        POST /invoke     {name,payload?}    run it on a backend
        POST /scale      {replicas}         set the number of backends
        GET  /backends                      backend status
        GET  /metrics                       counters

    Commands runner_call sends to a backend: PING / FUNCS / RUN <fn> <payload>

Run:  serverless.py --port 8000 --backend 127.0.0.1:9001 --backend ... \
                    --registry /tmp/registry.json
"""
import argparse
import json
import os
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


# ======================================================================
#  PROVIDED: runner_call, which sends one command to a backend.
# ======================================================================

def runner_call(host, port, line, timeout, connect_timeout=1.0):
    """Send one command line to a backend and return its one-line reply.

    It fails in two different ways, and you must handle them differently:

      - OSError (including ConnectionError): the backend could not be reached.
        It refused the connection, or did not accept it within connect_timeout.
        Try another backend.
      - socket.timeout: the backend accepted the connection but did not reply
        within `timeout`. The function is hung.

    socket.timeout is itself a kind of OSError, so when you catch both, catch
    socket.timeout first.
    """
    ct = min(connect_timeout, timeout)
    try:
        s = socket.create_connection((host, port), timeout=ct)
    except socket.timeout:
        raise ConnectionError(f"{host}:{port} did not accept a connection in {ct:.1f}s")
    with s:
        s.settimeout(timeout)
        s.sendall(line.encode() + b"\n")
        chunks = []
        while True:
            b = s.recv(4096)
            if not b:
                break
            chunks.append(b)
            if b"\n" in b:            # replies are a single line
                break
        return b"".join(chunks).decode(errors="replace").strip()


# ======================================================================
#  The platform.  *** THIS is what you implement. ***
# ======================================================================

class Platform:
    HEALTH_INTERVAL = 2.0     # seconds between background health checks
    MAX_OUTSTANDING = 4       # max requests in progress per backend
    FAIL_THRESHOLD = 2        # missed health checks in a row before it is down
    SCALE_INTERVAL = 1.0      # seconds between _autoscale calls (ticks)
    SCALE_DOWN_TICKS = 3      # idle ticks in a row before stopping a backend

    def __init__(self, backends, registry_path, invoke_timeout=5.0,
                 scale_cmd=None, min_backends=0, max_backends=0):
        # PROVIDED: state you will use. Do not rename these.
        self.registry_path = registry_path
        self.invoke_timeout = invoke_timeout
        self.lock = threading.Lock()
        self.slot_free = threading.Condition(self.lock)   # wait for a free slot (Part III)

        # The backends given at startup are never stopped. Autoscaling adds and
        # removes extra ones, marked "dynamic".
        self.backends = []
        for host, port in backends:
            self.backends.append(self._new_backend(host, port, dynamic=False))

        self.registry = {}       # name -> {"function": runner function, "created": ts}
        self._load_registry()

        self.metrics_data = {
            "invocations": 0, "successes": 0, "errors": 0,
            "timeouts": 0, "rejected": 0, "no_backend": 0,
            "scaled_up": 0, "scaled_down": 0,
            "by_function": {},
        }

        # autoscaling settings, and the state your _autoscale keeps between ticks
        self.scale_cmd = scale_cmd            # command that starts a backend; has {port}
        self.min_backends = min_backends
        self.max_backends = max_backends or len(self.backends)
        self._last_shed = 0                   # rejected + no_backend at the last tick
        self._last_invocations = 0            # invocation count at the last tick
        self._idle_ticks = 0                  # idle ticks in a row

        self._stop = False
        self._health_thread = threading.Thread(target=self._health_loop, daemon=True)
        self._scale_thread = threading.Thread(target=self._scale_loop, daemon=True)

    @staticmethod
    def _new_backend(host, port, dynamic, proc=None):
        return {
            "host": host, "port": port,
            "up": False, "funcs": set(), "fails": 0,
            "outstanding": 0, "invocations": 0, "errors": 0,
            "latency_sum": 0.0, "dynamic": dynamic, "proc": proc,
        }

    # ---- lifecycle + registry persistence (PROVIDED) ------------------

    def start(self):
        self._probe_all()                 # know backend state before serving
        self._health_thread.start()
        if self.scale_cmd:                # only autoscale if told how to launch
            self._scale_thread.start()

    def stop(self):
        self._stop = True
        # reap any backends we launched, so we never leak runner processes
        with self.lock:
            dyn = [b for b in self.backends if b.get("dynamic") and b.get("proc")]
        for b in dyn:
            self._kill_proc(b["proc"])

    def _load_registry(self):
        try:
            with open(self.registry_path) as f:
                self.registry = json.load(f)
        except (OSError, ValueError):
            self.registry = {}

    def _save_registry(self):
        tmp = self.registry_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.registry, f, indent=2)
        os.replace(tmp, self.registry_path)     # rename, so a crash never leaves half a file

    def _health_loop(self):               # PROVIDED: calls your _probe repeatedly
        while not self._stop:
            self._probe_all()
            time.sleep(self.HEALTH_INTERVAL)

    def _probe_all(self):                 # PROVIDED
        for b in self.backends:
            self._probe(b)

    # ---- registry: what you implement ---------------------------------

    def deploy(self, name, function):
        """TODO(student) Part I: register a function. Return (http_status, body_dict).

          - `function` is the runner function to run. If it is empty, use `name`.
          - If `name` is empty, return (400, {"error": ...}).
          - If `name` is already registered, return (409, {"error": ...}).
          - Otherwise set self.registry[name] = {"function": function,
            "created": time.time()}, call self._save_registry(), and return
            (201, {"name": name, "function": function}).

        Hold self.lock while you use self.registry."""
        return 500, {"error": "deploy not implemented"}

    def list_functions(self):
        """TODO(student) Part I: return a list of the registered functions, each a
        dict with "name", "function" and "created". The registry stores the name
        only as the key, so add "name" to each dict. Hold self.lock."""
        return []

    def lookup(self, name):
        """TODO(student) Part I: return the function's dict, including its "name",
        or None if it is not registered. Hold self.lock."""
        return None

    # ---- health checks and backend status: what you implement ---------

    def _probe(self, b):
        """TODO(student) Part II: check whether backend `b` is up.

          1. Send "PING": runner_call(b["host"], b["port"], "PING", 1.5). A
             "PONG" reply means it is up.
          2. If it is up, send "FUNCS" the same way. The reply looks like
             "OK wordcount sum": store the names after "OK" in b["funcs"] as a
             set. Set b["fails"] = 0 and b["up"] = True.
          3. If runner_call raises OSError, or the reply is not "PONG", add 1 to
             b["fails"]. Set b["up"] = False once b["fails"] reaches
             self.FAIL_THRESHOLD.

        Change b["up"], b["fails"] and b["funcs"] while holding self.lock."""
        pass

    def backends_status(self):
        """TODO(student) Part II: for GET /backends, return a list with one
        dict per backend: "backend" ("host:port"), "up", "funcs" (a sorted list),
        "outstanding", "invocations", "errors", and "avg_latency_ms"
        (latency_sum / invocations, in milliseconds; 0 if there are no
        invocations). Hold self.lock."""
        return []

    def _note_failure(self, b):
        """PROVIDED: call this when a request to `b` fails. No more requests go
        to `b` until it passes a health check again."""
        with self.lock:
            b["fails"] += 1
            b["up"] = False

    # ---- picking a backend: what you implement ------------------------

    def _acquire(self, function, deadline):
        """TODO(student) Part III: pick a backend for `function` and take a slot
        on it. `deadline` is a time.monotonic() value.

        Do everything inside `with self.slot_free:` (it also holds self.lock).

          1. A backend can be used if b["up"] is true, `function` is in
             b["funcs"], and b["outstanding"] < self.MAX_OUTSTANDING.
          2. Pick the one with the lowest b["outstanding"]. If several tie, pick
             the one with the lowest b["invocations"].
          3. Add 1 to its b["outstanding"] and return it.
          4. If no backend that is up has `function` at all, return None.
          5. Otherwise they are all full. Wait for a slot with
             self.slot_free.wait(timeout=deadline - time.monotonic()), then go
             back to step 1. Once time.monotonic() passes `deadline`, return the
             string "timeout".

        Do not call lookup() or anything else that takes self.lock in here: you
        already hold it, and taking it a second time hangs forever."""
        return None

    def _release(self, b):
        """TODO(student) Part III: give back the slot _acquire took. Inside
        `with self.slot_free:`, subtract 1 from b["outstanding"] and wake one
        waiting request with self.slot_free.notify()."""
        pass

    # ---- autoscaling --------------------------------------------------
    #
    # Starting and stopping backends is PROVIDED below. You write only
    # _autoscale, which decides when to do it.

    @staticmethod
    def _free_port():
        """PROVIDED: a currently-free localhost TCP port."""
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return port

    @staticmethod
    def _kill_proc(proc):
        """PROVIDED: stop a launched backend process (SIGTERM, then SIGKILL)."""
        if not proc:
            return
        try:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
        except OSError:
            pass

    def _spawn_backend(self):
        """PROVIDED: start one new backend with `scale_cmd` and add it to
        self.backends. Returns the new backend, or None if there are already
        max_backends or no launch command was given. The next health check marks
        it up."""
        if not self.scale_cmd or len(self.backends) >= self.max_backends:
            return None
        port = self._free_port()
        try:
            proc = subprocess.Popen(shlex.split(self.scale_cmd.format(port=port)),
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True)
        except OSError:
            return None
        b = self._new_backend("127.0.0.1", port, dynamic=True, proc=proc)
        with self.lock:
            self.backends.append(b)
            self.metrics_data["scaled_up"] += 1
        return b

    def _reap_backend(self, b):
        """PROVIDED: stop backend `b` and remove it from self.backends. Only call
        this on a backend autoscaling started (b["dynamic"] is true)."""
        with self.lock:
            if b in self.backends:
                self.backends.remove(b)
                self.metrics_data["scaled_down"] += 1
        self._kill_proc(b.get("proc"))

    def _scale_loop(self):
        """PROVIDED: run your _autoscale() every SCALE_INTERVAL."""
        while not self._stop:
            try:
                self._autoscale()
            except Exception:
                pass
            time.sleep(self.SCALE_INTERVAL)

    def scale(self, n):
        """PROVIDED: for POST /scale. Start or stop backends until there are `n`
        (at most max_backends). Only dynamic backends are stopped. Returns the
        new number of backends."""
        n = max(0, min(n, self.max_backends))
        while len(self.backends) < n and self._spawn_backend():
            pass
        while len(self.backends) > n:
            with self.lock:
                dyn = [b for b in self.backends if b.get("dynamic")]
            if not dyn:
                break
            self._reap_backend(dyn[-1])
        return len(self.backends)

    def _autoscale(self):
        """TODO(student) Part V: decide whether to start or stop a backend. Called
        every SCALE_INTERVAL seconds (one tick).

          1. While holding self.lock, read:
               - shed = rejected + no_backend (from self.metrics_data);
               - invocations = self.metrics_data["invocations"];
               - whether any backend has b["outstanding"] > 0;
               - the backends with b["dynamic"] true.
          2. After releasing the lock:
               - If shed > self._last_shed and there are fewer than
                 self.max_backends backends, call self._spawn_backend().
               - The tick is idle if invocations == self._last_invocations and
                 no backend has requests in progress. Add 1 to self._idle_ticks
                 if it is idle, otherwise set it to 0. Once it reaches
                 self.SCALE_DOWN_TICKS and there are more than self.min_backends
                 backends, call self._reap_backend() on a dynamic backend.
               - Save shed in self._last_shed and invocations in
                 self._last_invocations for the next tick.

        Never start or stop processes yourself."""
        pass

    # ---- invocation: what you implement -------------------------------

    def invoke(self, name, payload):
        """TODO(student) Part III: run function `name` with `payload` on a backend.
        Return (http_status, body).

          1. entry = self.lookup(name). If it is None, return 404.
             entry["function"] is the runner function to run.
          2. Add 1 to self.metrics_data["invocations"] and to
             self.metrics_data["by_function"][name].
          3. deadline = time.monotonic() + self.invoke_timeout. While
             time.monotonic() < deadline:
               a. b = self._acquire(function, deadline). If it is None, add 1 to
                  "no_backend" and return 503. If it is "timeout", add 1 to
                  "rejected" and return 503.
               b. Send f"RUN {function} {payload}" with runner_call, using the
                  time left before the deadline (at least 0.05 s) as the
                  timeout. Call self._release(b) afterwards, however it ends.
               c. Handle the result:
                    - reply starts with "OK": add 1 to "successes", add the time
                      taken to b["latency_sum"] and 1 to b["invocations"], and
                      return (200, {"result": reply[3:].strip(),
                      "backend": f"{b['host']}:{b['port']}"}).
                    - reply starts with "ERR": add 1 to "errors" and
                      b["errors"], and return 400 with the message. Do not try
                      another backend.
                    - socket.timeout: add 1 to "timeouts" and b["errors"], call
                      self._note_failure(b), and return 504.
                    - OSError: add 1 to b["errors"], call self._note_failure(b),
                      and go back to step a.
          4. If the deadline passes, add 1 to "errors" and return 502.

        Catch socket.timeout before OSError: socket.timeout is a kind of
        OSError. Hold self.lock whenever you change a counter."""
        return 500, {"error": "invoke not implemented"}

    # ---- metrics: what you implement ----------------------------------

    def metrics(self):
        """TODO(student) Part IV: for GET /metrics, return a copy of
        self.metrics_data (copy "by_function" too) with a "backends" entry set to
        self.backends_status() (Part II).
        Copy while holding self.lock, but call backends_status() after releasing
        it: it takes the lock itself, and self.lock cannot be taken twice."""
        return {}


# ======================================================================
#  PROVIDED: the HTTP server, and startup.
# ======================================================================

class Handler(BaseHTTPRequestHandler):
    platform = None

    def log_message(self, *a):
        pass

    def _send(self, status, body):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        n = int(self.headers.get("Content-Length", 0) or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode())
        except ValueError:
            return None

    def do_GET(self):
        p = self.platform
        if self.path == "/healthz":
            self._send(200, {"status": "ok"})
        elif self.path == "/functions":
            self._send(200, p.list_functions())
        elif self.path.startswith("/functions/"):
            fn = p.lookup(self.path[len("/functions/"):])
            self._send(200, fn) if fn else self._send(404, {"error": "not found"})
        elif self.path == "/backends":
            self._send(200, p.backends_status())
        elif self.path == "/metrics":
            self._send(200, p.metrics())
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        p = self.platform
        body = self._read_json()
        if body is None:
            self._send(400, {"error": "invalid JSON"}); return
        if self.path == "/functions":
            status, resp = p.deploy(body.get("name", ""), body.get("function", ""))
            self._send(status, resp)
        elif self.path == "/invoke":
            status, resp = p.invoke(body.get("name", ""), str(body.get("payload", "")))
            self._send(status, resp)
        elif self.path == "/scale":
            try:
                n = int(body.get("replicas"))
            except (TypeError, ValueError):
                self._send(400, {"error": "replicas (int) is required"}); return
            self._send(200, {"replicas": p.scale(n)})
        else:
            self._send(404, {"error": "not found"})


def parse_backends(specs):
    out = []
    for s in specs or []:
        host, _, port = s.rpartition(":")
        out.append((host or "127.0.0.1", int(port)))
    return out


def main():
    ap = argparse.ArgumentParser(description="serverless platform")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--backend", action="append", default=[],
                    help="a runner backend host:port (repeatable)")
    ap.add_argument("--backends-file", help="JSON file: [{\"host\":..,\"port\":..}]")
    ap.add_argument("--registry", default="/tmp/p4-registry.json")
    ap.add_argument("--invoke-timeout", type=float, default=5.0)
    ap.add_argument("--scale-cmd",
                    help="template to launch a new backend, with a {port} "
                         "placeholder, e.g. '/tmp/runner {port}'. Enables autoscaling.")
    ap.add_argument("--min-backends", type=int, default=0,
                    help="never autoscale below this many backends")
    ap.add_argument("--max-backends", type=int, default=0,
                    help="never autoscale above this many backends (0 = the "
                         "number of configured backends, i.e. no scaling out)")
    args = ap.parse_args()

    backends = parse_backends(args.backend)
    if args.backends_file:
        with open(args.backends_file) as f:
            backends += [(b["host"], int(b["port"])) for b in json.load(f)]

    platform = Platform(backends, args.registry, args.invoke_timeout,
                        scale_cmd=args.scale_cmd, min_backends=args.min_backends,
                        max_backends=args.max_backends)
    platform.start()

    # SIGTERM should shut down cleanly (so launched backends are reaped, not
    # leaked); turn it into the same path as Ctrl-C.
    signal.signal(signal.SIGTERM, lambda *a: sys.exit(0))

    Handler.platform = platform
    httpd = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"serverless platform on :{args.port} with {len(backends)} backend(s)", flush=True)
    try:
        httpd.serve_forever()
    except (KeyboardInterrupt, SystemExit):
        pass
    finally:
        platform.stop()


if __name__ == "__main__":
    main()
