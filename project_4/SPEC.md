# Project 4: Specification

In this project you build a **serverless platform**. Clients call functions on
it over HTTP, and the platform sends each call to one of several backends to run.

Each backend is a `runner` process (`runner/runner.c`, provided). It listens on a
TCP port and replies to one-line commands:

- `PING` replies `PONG`;
- `FUNCS` lists the functions it can run;
- `RUN <fn> <payload>` runs a function. There are two: `wordcount`
  (`RUN wordcount a b c` replies `OK 3`) and `sum` (`RUN sum 1 2 3` replies
  `OK 6`).

You implement the `Platform` class in `platform/serverless.py`.

| What | Methods | Part |
|---|---|---|
| the function registry | `deploy`, `list_functions`, `lookup` | I |
| health checks, and reporting each backend's status | `_probe`, `backends_status` | II |
| running a request on a backend | `_acquire`, `_release`, `invoke` | III |
| counters | `metrics` | IV |
| autoscaling | `_autoscale` | V |

Everything runs inside Project 4's Docker container (see SETUP.md).
`python3 platform/serverless.py --help` lists the platform's options.

---

## What is provided

Read these before you start; your methods build on them.

- **`runner_call(host, port, line, timeout)`** sends one command to a backend and
  returns the reply. If something goes wrong, it raises one of:
  - `OSError`: the backend could not be reached. Try another backend.
  - `socket.timeout`: the backend accepted the connection but did not reply
    within `timeout` seconds.
- **The state in `Platform.__init__`**:
  - `self.backends`: a list with one dict per backend. Each dict holds `host`,
    `port`, `up`, `funcs`, `fails`, `outstanding` (requests in progress),
    `invocations`, `errors` and `latency_sum`;
  - `self.registry`: the deployed functions, by name;
  - `self.metrics_data`: the counters that `/metrics` reports;
  - `self.lock`: requests run on many threads at once, so hold this lock
    whenever you read or change any of the above;
  - `self.slot_free`: a `threading.Condition` that uses `self.lock`. Part III
    uses it to wait until a backend has room.
- **Helpers you call:** `_save_registry()` writes the registry to disk, and
  `_note_failure(b)` stops requests going to backend `b` until it passes a health
  check again.
- **Loops that call your code:** every `HEALTH_INTERVAL` seconds, a loop calls
  your `_probe` on each backend. Every `SCALE_INTERVAL` seconds, another loop
  calls your `_autoscale`.

The HTTP requests the platform answers:

```text
GET  /healthz                      -> {"status":"ok"}
POST /functions  {name, function?}    deploy a function        (Part I)
GET  /functions                       list functions           (Part I)
GET  /functions/<name>                look one up              (Part I)
POST /invoke     {name, payload?}     run it on a backend      (Part III)
POST /scale      {replicas}           set the number of backends (provided)
GET  /backends                        backend status           (Part II)
GET  /metrics                         counters                 (Part IV)
```

---

## Running the platform

Use two shells inside the container (run `docker compose exec dev bash` from
`project_4/setup` once for each), both in `/work`.

**Shell 1** runs the platform. Its errors (Python tracebacks) appear here.

```bash
python3 platform/serverless.py --port 8000 \
        --backend 127.0.0.1:9001 --backend 127.0.0.1:9002 --backend 127.0.0.1:9003 \
        --registry /tmp/reg.json --invoke-timeout 2
```

Restart it after each edit to `serverless.py`.

**Shell 2** runs the backends and sends requests. The commands below:

- build the backend;
- start backends on ports 9001 and 9002, and save their process IDs in `$B1` and
  `$B2`;
- define `req`, which is `curl` that also prints the HTTP status code;
- define `code`, which prints only the status code.

Port 9003 is left without a backend on purpose, so you can see a backend that is
down.

```bash
gcc -O2 -o /tmp/runner runner/runner.c
/tmp/runner 9001 > /dev/null &  B1=$!
/tmp/runner 9002 > /dev/null &  B2=$!
req()  { curl -s -w ' %{http_code}\n' "$@"; }
code() { curl -s -o /dev/null -w '%{http_code}\n' "$@"; }
```

`$B1`, `$B2`, `req` and `code` exist only in this shell. If you open a new one, run
the last two lines again, and use `pgrep -a runner` to find the backends'
process IDs.

Three ways to change a backend:

| Command | Effect |
|---|---|
| `kill $B1` | stop the backend: connections to it are refused |
| `kill -STOP $B1` | freeze it: it accepts connections but never replies, like a hung function |
| `kill -CONT $B1` | unfreeze it |

The registry is saved in `/tmp/reg.json`. To start with an empty registry, stop
the platform, run `rm /tmp/reg.json`, and start it again.

---

## Testing

Run the tests after each Part, from `/work` inside the container:

```bash
./tests/run_tests.sh
```

It starts its own backends and platforms on ports 8077, 8079, 9101 to 9104 and
9111, so it does not clash with the ones you started on 8000 and 9001 to 9003.
It prints PASS or FAIL for each check. These are the same checks the grader
runs; there are no hidden tests. Each Part below says which checks should pass
once it is done.

---

## Part I: The function registry

Before a client can call a function, it has to **deploy** it under a name. Each
name runs one of the runner's two functions, `wordcount` or `sum`. The platform
keeps the deployed functions in `self.registry` and saves them to disk, so a
restarted platform still knows them.

In this part you write three methods in `platform/serverless.py`. Each HTTP
request runs on its own thread, so two requests can use `self.registry` at the
same time. Hold `self.lock` whenever you read or change it; otherwise two
deploys of the same name could both succeed.

1. `deploy`, for `POST /functions`;
2. `list_functions`, for `GET /functions`;
3. `lookup`, for `GET /functions/<name>`.

**1. `deploy(name, function)`**

`function` is the runner function that `name` runs. If it is not given, use
`name`. So deploying `wordcount` runs `wordcount`, and deploying `wc` with
function `wordcount` makes `wc` another name for `wordcount`.

- If `name` is empty, return `400`.
- If `name` is already deployed, return `409`.
- Otherwise set `self.registry[name]` to
  `{"function": ..., "created": time.time()}`, call `self._save_registry()`, and
  return `201`.

`deploy` returns an `(http_status, body_dict)` pair. Its docstring shows each
body.

**2. `list_functions()`**

Return a list of the deployed functions, each a dict with `name`, `function` and
`created`. The registry does not store `name` (it is the key), so add it to each
dict.

**3. `lookup(name)`**

Return one function's dict, with `name` added the same way, or `None` if it is
not deployed.

*Test:* 9 of the 26 checks should pass: the first two (the platform builds and
starts), the deploy, list and lookup checks, and the registry surviving a
restart.

---

## Part II: Health checks and backend status

When you start `serverless.py`, you give it a list of backend addresses with
`--backend`, such as `127.0.0.1:9001`. An address in the list does not mean a runner
is listening there: in the setup above, nothing runs on 9003. A runner can also
crash, or be started later.

So every `HEALTH_INTERVAL` (2) seconds, the platform sends each backend `PING`.
A backend that answers `PONG` is marked up, and the platform asks it for its
functions with `FUNCS`. In Part III, requests only go to backends that are up
and have the function.

In this part you write two methods in `platform/serverless.py`. The health loop
runs on its own thread, so hold `self.lock` whenever you read or change a
backend's dict.

1. `_probe`, which the health loop calls on each backend;
2. `backends_status`, for `GET /backends`.

**1. `_probe(b)`**

Check whether backend `b` is up:

1. Send `PING` with `runner_call`. A `PONG` reply means it is up.
2. If it is up, send `FUNCS` and store the function names it lists in
   `b["funcs"]` (a set). Set `b["up"]` to true and `b["fails"]` to 0.
3. If `runner_call` raises `OSError` or the reply is not `PONG`, add 1 to
   `b["fails"]`. Set `b["up"]` to false once `b["fails"]` reaches
   `FAIL_THRESHOLD` (2).

Waiting for two misses in a row means one slow reply does not take a backend
out.

**2. `backends_status()`**

Return a list with one dict per backend, with these keys:

- `backend`: `"host:port"`;
- `up`, `outstanding`, `invocations` and `errors`: copied from the backend's
  dict;
- `funcs`: the backend's functions, as a sorted list;
- `avg_latency_ms`: the average time per request in milliseconds, which is
  `b["latency_sum"]` (in seconds) divided by `b["invocations"]`, times 1000. Use
  0 if the backend has served no requests.

`outstanding`, `invocations` and `errors` stay 0 until you write Part III.

*Test:* 16 of the 26 checks should pass: the ones from Part I, three
autoscaling checks that only need `/backends`, and the checks that backends are
found up with their functions, that a backend with nothing running is reported
down, and that a backend that comes back is used again.

---

## Part III: Running a request on a backend

A client runs a function with `POST /invoke`. For example, for
`{"name": "wc", "payload": "a b c"}`, the platform looks up `wc` in the registry
(it runs `wordcount`), picks a backend, sends it `RUN wordcount a b c`, and
returns `{"result": "3", "backend": "127.0.0.1:9001"}`.

Three things can go wrong with a backend, and the platform handles each:

- **It is busy.** Each backend has `MAX_OUTSTANDING` (4) **slots**, so it runs
  at most 4 requests at once. If every slot is taken, the request waits.
- **It hangs.** Each request has a time limit, `--invoke-timeout` seconds. If the
  backend does not reply in time, the request fails with `504`.
- **It is not running.** The request is sent to another backend instead.

In this part you write three methods in `platform/serverless.py`:

1. `_acquire`, which picks a backend and takes a slot on it;
2. `_release`, which gives the slot back;
3. `invoke`, for `POST /invoke`, which uses the other two.

**1. `_acquire(function, deadline)`**

Write the whole method inside `with self.slot_free:`, which holds `self.lock`.
`self.slot_free.wait(timeout)` releases the lock while it waits for `_release` to
free a slot.

1. A backend can be used if it is up, has `function` in `b["funcs"]`, and has
   fewer than `MAX_OUTSTANDING` requests in progress (`b["outstanding"]`).
2. Of those, pick the one with the fewest requests in progress. If several tie,
   pick the one that has served the fewest requests so far
   (`b["invocations"]`), so requests spread out evenly.
3. Take the slot: add 1 to its `b["outstanding"]` and return it.
4. If no backend that is up has `function` at all, return `None`.
5. Otherwise they are all full. Wait with `self.slot_free.wait()` until a slot
   is given back, then go back to step 1. If `deadline` (a `time.monotonic()`
   value) passes first, return the string `"timeout"`.

Do not call `lookup` or anything else that takes `self.lock` inside the `with`:
you already hold the lock, and taking it a second time hangs forever.

**2. `_release(b)`**

Inside `with self.slot_free:`, subtract 1 from `b["outstanding"]` and wake one
waiting request with `self.slot_free.notify()`.

**3. `invoke(name, payload)`**

1. Look up `name`. If it is not deployed, return `404`. Its `function` field is
   the runner function to run.
2. Add 1 to `self.metrics_data["invocations"]` and to its count in
   `self.metrics_data["by_function"]`.
3. Set `deadline = time.monotonic() + self.invoke_timeout`. Until it passes:
   1. Call `_acquire`. If it returns `None`, add 1 to `no_backend` and return
      `503`. If it returns `"timeout"`, add 1 to `rejected` and return `503`.
   2. Send `RUN <function> <payload>` with `runner_call`. For the timeout, use
      the time left before the deadline, but at least 0.05 seconds (a timeout of
      0 or less does not work). Call `_release(b)` afterwards, however it ends.
   3. Handle the result:
      - **The reply starts with `OK`:** add 1 to `successes`. Add the seconds the
        request took to `b["latency_sum"]` and 1 to `b["invocations"]`. Return
        `200` with `{"result": ..., "backend": "host:port"}`, where `result` is
        the reply without `OK ` (so `OK 5` gives `"5"`).
      - **The reply starts with `ERR`:** the function ran and rejected its input.
        Add 1 to `errors` and to `b["errors"]`, and return `400` with the
        message. Do not try another backend: it would give the same error.
      - **`socket.timeout`:** the backend hung. Add 1 to `timeouts` and to
        `b["errors"]`, call `_note_failure(b)`, and return `504`.
      - **`OSError`:** the backend is not running. Add 1 to `b["errors"]`, call
        `_note_failure(b)`, and go back to step 3.1 to try another backend.
4. If the deadline passes while trying backends, add 1 to `errors` and return
   `502`.

Hold `self.lock` whenever you change a counter. The counters are what `/metrics`
reports (Part IV) and what the autoscaler reads (Part V).

*Test:* 24 of the 26 checks should pass: the ones from Parts I and II, the
invoke results and errors, requests spreading across backends, failover from a
killed backend, a `504` from a hung backend, and a burst of requests turned away
with `503` or `504` instead of hanging.

---

## Part IV: Metrics

`invoke` keeps counters in `self.metrics_data`: requests received, succeeded,
failed, timed out and turned away. `GET /metrics` reports them, together with
each backend's status from Part II, for example:

```text
{"invocations": 4, "successes": 3, "errors": 1, "timeouts": 0, "rejected": 0,
 "no_backend": 0, "by_function": {"wordcount": 3, "sum": 1}, "backends": [...], ...}
```

In this part you write one method in `platform/serverless.py`, `metrics`, for
`GET /metrics`.

**`metrics()`**

1. While holding `self.lock`, copy `self.metrics_data`, and copy its
   `by_function` dict too. Other threads keep changing the counters, so return a
   copy, not the dict itself.
2. After releasing the lock, set the copy's `backends` entry to
   `self.backends_status()`. `backends_status` takes the lock itself, and taking
   it a second time hangs forever.
3. Return the copy.

*Test:* 25 of the 26 checks should pass: everything except the autoscaling
check that the platform starts more backends.

---

## Part V: Autoscaling

So far the platform only uses the backends listed with `--backend`. If they are
all busy or hung, requests are turned away with `503`. **Autoscaling** means
starting more backends when that happens, and stopping them again once requests
stop.

To autoscale, the platform needs a **launch command** that starts one backend,
with `{port}` where the port goes, and a minimum and maximum number of backends:

```bash
--scale-cmd '/tmp/runner {port}' --min-backends 1 --max-backends 3
```

Starting and stopping backends is provided: `_spawn_backend()` starts one and
adds it to `self.backends`, and `_reap_backend(b)` stops one and removes it. A
new backend is marked up at the next health check.

In this part you write one method in `platform/serverless.py`, `_autoscale`. A
loop calls it every `SCALE_INTERVAL` (1) seconds; each call is one **tick**.

**`_autoscale()`**

1. While holding `self.lock`, read:
   - `shed`: the requests turned away so far, `rejected + no_backend` from
     `self.metrics_data`;
   - `invocations`: `self.metrics_data["invocations"]`;
   - whether any backend has requests in progress (`b["outstanding"] > 0`);
   - the backends autoscaling started (`b["dynamic"]` is true).
2. After releasing the lock, decide:
   - **Start a backend** if `shed` went up since the last tick
     (`self._last_shed`) and there are fewer than `self.max_backends` backends.
   - **Stop a backend** after `SCALE_DOWN_TICKS` (3) idle ticks in a row. A tick
     is idle if `invocations` has not changed since the last tick
     (`self._last_invocations`) and no request is in progress. Count idle ticks in
     `self._idle_ticks`, and set it back to 0 on a tick that is not idle. Stop
     one backend autoscaling started, and only if there are more than
     `self.min_backends`.
   - Save `shed` and `invocations` in `self._last_shed` and
     `self._last_invocations` for the next tick.

Call `_spawn_backend()` and `_reap_backend()` after releasing the lock, since
they take it themselves. Never start or stop processes yourself.

Why compare the invocation count instead of checking `outstanding`?
`wordcount` finishes so quickly that `outstanding` is almost always 0, even while
requests keep arriving.

*Test:* all 26 checks should pass.
