#!/usr/bin/env python3
"""Project 4 grader: drive the student's serverless platform against real backends.

The backends are plain `runner` processes (the same binary the VM / container /
unikernel run) that the grader starts, kills, hangs, and restarts to probe the
platform's behaviour. Everything is host-side and staged: a partial platform
passes the layers it got working.

    Run it in place with no arguments (via ./run_tests.sh); paths are resolved
    relative to tests/.
"""
import json, os, re, signal, socket, subprocess, sys, tempfile, threading, time, urllib.request, urllib.error

# Paths are computed from this script's location (tests/), so the autograder can
# run it in place with no arguments: the submission IS the project directory.
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PLATFORM = os.path.join(ROOT, "platform", "serverless.py")   # the student's platform
RUNNER_SRC = os.path.join(ROOT, "runner", "runner.c")        # the shared runner backend

P, F = 0, 0
PORT = 8077                      # platform control-plane port
BE_PORTS = [9101, 9102, 9103]    # backend ports (a runner on each)
DEAD_PORT = 9104                 # configured but never given a runner
AS_PORT = 8079                   # a second platform, for the autoscaling checks
AS_WARM = 9111                   # its one warm/static backend
AS_MAX = 3                       # its autoscaling ceiling


# --- result harness ---------------------------------------------------------
# Human PASS/FAIL (by requirement); machine TSV to CS360V_RESULTS if set. No points.
_RESULTS = os.environ.get("CS360V_RESULTS", "")

REQUIREMENTS = {
    "compile": "Your platform is valid Python and the runner backend builds",
    "boots": "the platform starts and answers /healthz",
    "deploy_ok": "deploying a function succeeds",
    "deploy_dup": "deploying a duplicate name is rejected (409)",
    "deploy_empty": "deploying an empty name is rejected (400)",
    "list_shows": "listing shows the deployed functions",
    "lookup_ok": "looking up a known function returns it",
    "lookup_404": "looking up an unknown function is 404",
    "wordcount": "invoking a function returns the correct result",
    "sum": "invoking an aliased function returns the correct result",
    "err_passthrough": "a function's own error is surfaced as a 400, not a fake success",
    "unknown_404": "invoking an unknown function is 404",
    "backends_up": "the platform discovers its backends as up",
    "funcs_discovered": "the platform discovers which functions each backend runs",
    "dead_backend_down": "a configured-but-absent backend is reported down, not routed to",
    "spreads": "a burst of requests spreads across backends",
    "failover": "a backend killed mid-request is failed over so the request still succeeds",
    "recovery": "a recovered backend is used again",
    "hung_backend": "a hung backend times out (504) rather than hanging the platform",
    "saturation": "a saturating burst is shed with 503/504, not hung or wrongly served",
    "counts": "metrics count the invocations driven",
    "scale_up": "the platform launches more backends when it sheds load",
    "scale_bounds": "the fleet never exceeds the configured maximum",
    "scale_down": "idle launched backends are retired back toward the floor",
    "manual_scale": "POST /scale resizes the fleet and clamps to the maximum",
    "persisted": "the function registry survives a platform restart",
}


# the SPEC Part whose code makes each check pass
PART = {
    "compile": "I", "boots": "I", "deploy_ok": "I", "deploy_dup": "I",
    "deploy_empty": "I", "list_shows": "I", "lookup_ok": "I", "lookup_404": "I",
    "persisted": "I",
    "backends_up": "II", "funcs_discovered": "II", "dead_backend_down": "II",
    "recovery": "II",
    "wordcount": "III", "sum": "III", "err_passthrough": "III", "unknown_404": "III",
    "spreads": "III", "failover": "III", "hung_backend": "III", "saturation": "III",
    "counts": "IV",
    "scale_up": "V", "scale_bounds": "V", "scale_down": "V", "manual_scale": "V",
}
BY_PART = {}       # Part -> [passed, total]


def ck(status, cid, reason="", tech=""):
    global P, F
    ok = status == "pass"
    req = REQUIREMENTS.get(cid, cid.replace("_", " "))
    part = PART.get(cid, "?")
    print(f"  {'PASS' if ok else 'FAIL'}  {'Part ' + part:<8}  {req}"
          + (f"  ({reason})" if reason and not ok else ""))
    P += ok; F += (not ok)
    tally = BY_PART.setdefault(part, [0, 0])
    tally[0] += ok; tally[1] += 1
    if _RESULTS:
        with open(_RESULTS, "a") as fh:
            fh.write("\t".join([status, cid, req, reason if not ok else "", tech]) + "\n")


def record(group, name, ok, reason=""):
    # the check bodies call record(group, name, ...); map to the harness by name.
    ck("pass" if ok else "fail", name, reason)


# ---- HTTP client to the platform -----------------------------------------

def http(method, path, body=None, timeout=20, port=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"http://127.0.0.1:{port or PORT}{path}", data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try: b = json.loads(e.read() or b"null")
        except ValueError: b = None
        return e.code, b
    except Exception as e:
        return None, {"error": str(e)}


def invoke(name, payload, timeout=20):
    return http("POST", "/invoke", {"name": name, "payload": payload}, timeout)


# ---- harness: runner backends + the platform -----------------------------

class Harness:
    def __init__(self, runner_bin, ports, platform_ports=None, port=PORT,
                 scale_cmd=None, min_backends=0, max_backends=0):
        self.runner_bin = runner_bin
        self.ports = ports                                  # ports we run a runner on
        self.platform_ports = platform_ports or ports       # ports the platform is told about
        self.port = port                                    # the platform's control-plane port
        self.scale_cmd = scale_cmd
        self.min_backends = min_backends
        self.max_backends = max_backends
        self.backends = {}
        self.platform = None
        self.registry = tempfile.mktemp(prefix="p4reg.", suffix=".json")

    def start_backends(self, ports=None):
        for p in (ports or self.ports):
            self.backends[p] = subprocess.Popen([self.runner_bin, str(p)],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def start_platform(self):
        argv = [sys.executable, PLATFORM, "--port", str(self.port),
                "--registry", self.registry, "--invoke-timeout", "2"]
        for p in self.platform_ports:
            argv += ["--backend", f"127.0.0.1:{p}"]
        if self.scale_cmd:
            argv += ["--scale-cmd", self.scale_cmd,
                     "--min-backends", str(self.min_backends),
                     "--max-backends", str(self.max_backends)]
        # only this script records PASS/FAIL, so the platform does not get the
        # results file's path
        env = {k: v for k, v in os.environ.items() if k != "CS360V_RESULTS"}
        self.platform = subprocess.Popen(argv, env=env, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL)

    def restart_platform(self):
        if self.platform:
            self.platform.terminate(); self.platform.wait()
        time.sleep(0.3)
        self.start_platform()
        self.wait_ready()

    def wait_ready(self, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            st, _ = http("GET", "/healthz", timeout=2, port=self.port)
            if st == 200:
                return True
            time.sleep(0.2)
        return False

    def kill_backend(self, port):
        b = self.backends.pop(port, None)
        if b:
            b.kill(); b.wait()

    def stop_backend(self, port):     # hang it (SIGSTOP): connects, never replies
        os.kill(self.backends[port].pid, signal.SIGSTOP)

    def cont_backend(self, port):
        os.kill(self.backends[port].pid, signal.SIGCONT)

    def cleanup(self):
        # terminate (not kill) the platform first, and give it a moment: its
        # SIGTERM handler reaps any backends it launched, so they do not leak.
        if self.platform:
            self.platform.terminate()
            try:
                self.platform.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.platform.kill(); self.platform.wait()
        for b in self.backends.values():
            try: os.kill(b.pid, signal.SIGCONT)
            except OSError: pass
            b.kill()
        for b in self.backends.values():
            b.wait()
        for f in (self.registry, self.registry + ".tmp"):
            try: os.remove(f)
            except OSError: pass


def backend_served(resp):
    """The 'backend' field of a successful invoke, if any."""
    return resp.get("backend") if isinstance(resp, dict) else None


def autoscale_checks(runner_bin):
    """Part V, on its own small fleet: one warm backend plus a launch command and
    a ceiling of AS_MAX. Hang the warm backend so the platform sheds load and must
    scale OUT to keep serving; then stop the load so it scales back DOWN; then
    exercise the manual /scale endpoint and its clamp to the ceiling."""
    h = Harness(runner_bin, [AS_WARM], platform_ports=[AS_WARM], port=AS_PORT,
                scale_cmd=f"{runner_bin} {{port}}", min_backends=1, max_backends=AS_MAX)
    names = ("scale_up", "scale_bounds", "scale_down", "manual_scale")

    def a(method, path, body=None, timeout=8):
        return http(method, path, body, timeout, port=AS_PORT)

    def fleet():
        st, r = a("GET", "/backends")
        return len(r) if isinstance(r, list) else 0

    try:
        h.start_backends()
        time.sleep(0.5)
        h.start_platform()
        if not h.wait_ready(timeout=10):
            for n in names:
                record("autoscale", n, False, "autoscale platform did not come up")
            return
        time.sleep(0.5)
        a("POST", "/functions", {"name": "wordcount"})
        start_fleet = fleet()

        # Hang the only warm backend: invocations are now shed (no healthy
        # capacity), which is the signal to launch more backends.
        h.stop_backend(AS_WARM)
        stop_load = [False]

        def load():
            while not stop_load[0]:
                a("POST", "/invoke", {"name": "wordcount", "payload": "a b c"}, 4)

        threads = [threading.Thread(target=load, daemon=True) for _ in range(8)]
        for t in threads:
            t.start()
        peak = start_fleet
        for _ in range(9):
            time.sleep(1)
            peak = max(peak, fleet())
        stop_load[0] = True
        for t in threads:
            t.join(timeout=5)

        record("autoscale", "scale_up", peak > start_fleet,
               f"under shed load the fleet stayed at {peak} (from {start_fleet}); "
               f"it must launch more backends")
        # require a real (observable) fleet, so an empty /backends cannot pass the
        # ceiling check vacuously.
        record("autoscale", "scale_bounds", 1 <= peak <= AS_MAX,
               f"the fleet peaked at {peak}; want a real fleet that never exceeds "
               f"the configured max of {AS_MAX}")

        # Un-hang the warm backend and let the fleet go idle: it must retire the
        # backends it launched, back toward the floor.
        h.cont_backend(AS_WARM)
        time.sleep(0.5)
        low = fleet()
        for _ in range(10):
            time.sleep(1)
            low = min(low, fleet())
            if low <= start_fleet:
                break
        record("autoscale", "scale_down", 1 <= low <= start_fleet,
               f"after the load stopped the fleet settled at {low} (from a peak of "
               f"{peak}, floor {start_fleet}); it must retire the backends it launched")

        # Manual /scale, and its clamp to the ceiling. Drive one invocation first
        # so the fleet is not "idle": otherwise the autoscaler, which has been
        # watching an idle fleet, may reclaim a manually-added backend before we
        # can observe it (a real manual scale-up during idle is transient too).
        a("POST", "/invoke", {"name": "wordcount", "payload": "x"})
        st, _ = a("POST", "/scale", {"replicas": 2})
        time.sleep(0.4)
        n2 = fleet()
        a("POST", "/scale", {"replicas": 99})
        time.sleep(0.4)
        n3 = fleet()
        record("autoscale", "manual_scale", st == 200 and n2 == 2 and n3 == AS_MAX,
               f"/scale=2 gave {st} and {n2} backends, /scale=99 gave {n3} "
               f"(must clamp to exactly {AS_MAX})")
    finally:
        h.cleanup()


def main():
    # build the runner backends
    runner_bin = tempfile.mktemp(prefix="p4runner.")
    b = subprocess.run(["gcc", "-O2", "-o", runner_bin, RUNNER_SRC],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    runner_ok = b.returncode == 0

    # compile gate: the student's platform is valid Python and the runner builds.
    plat_ok = subprocess.run(
        [sys.executable, "-c", "import ast,sys; ast.parse(open(sys.argv[1]).read())", PLATFORM],
        capture_output=True).returncode == 0
    ck("pass" if (plat_ok and runner_ok) else "fail", "compile",
       "" if (plat_ok and runner_ok) else
       ("your platform did not parse" if not plat_ok else "the runner backend did not build"))
    if not (plat_ok and runner_ok):
        return finish(runner_bin)

    # the platform is also told about a backend with no runner behind it, so we
    # can check it is reported down rather than routed to.
    h = Harness(runner_bin, BE_PORTS, platform_ports=BE_PORTS + [DEAD_PORT])
    try:
        h.start_backends()
        time.sleep(0.6)
        h.start_platform()
        ready = h.wait_ready()
        record("control", "boots", ready, "platform did not answer /healthz")
        if not ready:
            # nothing else can run
            for g, n in (("control","deploy_ok"),("control","deploy_dup"),("control","deploy_empty"),
                         ("control","list_shows"),("control","lookup_ok"),("control","lookup_404"),
                         ("registry","persisted"),("invoke","wordcount"),("invoke","sum"),
                         ("invoke","err_passthrough"),("invoke","unknown_404"),
                         ("health","backends_up"),("health","funcs_discovered"),("health","dead_backend_down"),
                         ("routing","spreads"),("resilience","failover"),("resilience","recovery"),
                         ("timeout","hung_backend"),("capacity","saturation"),("metrics","counts"),
                         ("autoscale","scale_up"),("autoscale","scale_bounds"),
                         ("autoscale","scale_down"),("autoscale","manual_scale")):
                record(g, n, False, "platform not up")
            return finish(runner_bin)

        # let health settle
        time.sleep(0.5)

        # ---- control / registry ----
        st, r = http("POST", "/functions", {"name": "wordcount"})
        record("control", "deploy_ok", st in (200, 201), f"deploy returned {st} {r}")
        st, r = http("POST", "/functions", {"name": "wordcount"})
        record("control", "deploy_dup", st == 409, f"duplicate deploy returned {st}, want 409")
        st, r = http("POST", "/functions", {"name": ""})
        record("control", "deploy_empty", st == 400, f"empty-name deploy returned {st}, want 400")
        http("POST", "/functions", {"name": "adder", "function": "sum"})
        st, r = http("GET", "/functions")
        names = {f.get("name") for f in r} if isinstance(r, list) else set()
        record("control", "list_shows", {"wordcount", "adder"} <= names, f"list = {names}")
        st, r = http("GET", "/functions/adder")
        record("control", "lookup_ok", st == 200 and isinstance(r, dict) and r.get("function") == "sum",
               f"lookup adder = {st} {r}")
        st, r = http("GET", "/functions/nope")
        record("control", "lookup_404", st == 404, f"lookup of unknown returned {st}, want 404")

        # ---- invocation ----
        st, r = invoke("wordcount", "a b c d e")
        record("invoke", "wordcount", st == 200 and isinstance(r, dict) and r.get("result") == "5",
               f"invoke wordcount = {st} {r}")
        st, r = invoke("adder", "10 20 30")
        record("invoke", "sum", st == 200 and isinstance(r, dict) and r.get("result") == "60",
               f"invoke adder(sum) = {st} {r}")
        st, r = invoke("adder", "1 notanumber")
        record("invoke", "err_passthrough", st == 400,
               f"a function error should be a 4xx with the message, got {st} {r}")
        st, r = invoke("ghost", "x")
        record("invoke", "unknown_404", st == 404, f"invoking an unknown function returned {st}, want 404")

        # ---- health / discovery ----
        st, r = http("GET", "/backends")
        up = [b for b in r if b.get("up")] if isinstance(r, list) else []
        record("health", "backends_up", len(up) == len(BE_PORTS), f"{len(up)}/{len(BE_PORTS)} backends up")
        record("health", "funcs_discovered",
               all("wordcount" in b.get("funcs", []) for b in up) and len(up) > 0,
               "backends' FUNCS were not discovered")
        dead = [b for b in r if b.get("backend", "").endswith(str(DEAD_PORT))] if isinstance(r, list) else []
        record("health", "dead_backend_down",
               len(dead) == 1 and not dead[0].get("up"),
               "a configured backend with nothing listening was not reported down")

        # ---- routing spread ----
        served = set()
        for _ in range(3 * len(BE_PORTS)):
            st, r = invoke("wordcount", "a b")
            s = backend_served(r)
            if s: served.add(s)
        record("routing", "spreads", len(served) >= 2,
               f"invocations only ever hit {served}; a fleet should be spread across")

        # ---- resilience: kill a backend, invocations still succeed ----
        h.kill_backend(BE_PORTS[0])
        time.sleep(0.3)
        ok = 0
        for _ in range(6):
            st, r = invoke("wordcount", "a b c", timeout=6)
            if st == 200: ok += 1
        record("resilience", "failover", ok == 6,
               f"only {ok}/6 invocations succeeded after one backend was killed")

        # ---- resilience: restart it, health brings it back ----
        h.start_backends([BE_PORTS[0]])
        time.sleep(h_health_wait())
        st, r = http("GET", "/backends")
        back = any(b.get("up") and b.get("backend", "").endswith(str(BE_PORTS[0])) for b in r) if isinstance(r, list) else False
        record("resilience", "recovery", back,
               "a recovered backend was not brought back up by the health check")

        # ---- timeout: hang every backend, an invoke must time out (not hang) ----
        for p in list(h.backends):
            h.stop_backend(p)
        time.sleep(0.3)
        t0 = time.time()
        st, r = invoke("wordcount", "a", timeout=10)
        dt = time.time() - t0
        record("timeout", "hung_backend", st in (502, 503, 504) and dt < 6,
               f"a hung backend gave {st} in {dt:.1f}s; want a 5xx well under the client timeout")

        # ---- capacity: many concurrent invokes vs a hung fleet ----
        results, lock = [], threading.Lock()
        def fire():
            st, _ = invoke("wordcount", "a", timeout=12)
            with lock: results.append(st)
        ts = [threading.Thread(target=fire) for _ in range(12)]
        for t in ts: t.start()
        for t in ts: t.join()
        for p in list(h.backends):
            h.cont_backend(p)
        no_hang = all(s is not None for s in results)
        no_bogus_success = all(s != 200 for s in results)       # backends were hung
        sheds = any(s in (503, 504) for s in results)           # with a real overload code
        record("capacity", "saturation", no_hang and no_bogus_success and sheds,
               f"under saturation the platform must shed load with a 503/504, not hang, "
               f"fake success, or error out; got {sorted(set(str(s) for s in results))}")

        # ---- metrics ----
        st, m = http("GET", "/metrics")
        record("metrics", "counts",
               isinstance(m, dict) and m.get("invocations", 0) >= 10 and "successes" in m,
               f"metrics missing or not counting: {m if not isinstance(m, dict) else {k: m.get(k) for k in ('invocations','successes','timeouts')}}")

        # ---- autoscaling (Part V): a separate fleet with a launch command ----
        autoscale_checks(runner_bin)

        # ---- persistence across a restart (LAST: the restart must not disturb
        # the checks above, so a platform that never persists fails only here) ----
        h.restart_platform()
        time.sleep(0.5)
        st, r = http("GET", "/functions")
        names = {f.get("name") for f in r} if isinstance(r, list) else set()
        record("registry", "persisted", {"wordcount", "adder"} <= names,
               "the registry did not survive a platform restart")

        return finish(runner_bin)
    finally:
        h.cleanup()


def h_health_wait():
    return 5.0        # > 2 health intervals so a recovered backend is re-probed


def finish(runner_bin):
    try: os.remove(runner_bin)
    except OSError: pass
    print("=" * 35)
    for part in ("I", "II", "III", "IV", "V"):
        if part in BY_PART:
            passed, total = BY_PART[part]
            print(f"  Part {part:<4} {passed}/{total} passed")
    print(f"{P} passed, {F} failed  (of {P + F})")
    return 0 if F == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
