# Project 4: A Serverless Platform

In this project you build a serverless platform in Python. Clients call
functions on it over HTTP. The platform runs each call on one of several backend
servers and sends the result back.

Each backend is a `runner` process (`runner/runner.c`, provided) with a few
built-in functions. The platform checks which backends are up, spreads calls
across them, retries when a backend dies, and starts more backends under heavy
load.

You work inside a Docker container, so everyone has the same versions of Python,
gcc and curl.

- **[SETUP.md](SETUP.md)**: how to set up the container.
- **[SPEC.md](SPEC.md)**: what to implement.

---

## What you implement

You write **one** file, `platform/serverless.py`, and only the `Platform` class
inside it:

| Methods | What they do | Part |
|---|---|---|
| `deploy`, `list_functions`, `lookup` | the function registry, saved to disk | I |
| `_probe`, `backends_status` | check whether each backend is up and which functions it runs, and report it | II |
| `_acquire`, `_release`, `invoke` | pick a backend, limit requests per backend, apply a time limit, retry elsewhere | III |
| `metrics` | report the counters | IV |
| `_autoscale` | start more backends when needed, stop them when idle | V |

---

## How to work

Work through the Parts in [SPEC.md](SPEC.md) in order, inside Project 4's
container (see [SETUP.md](SETUP.md)). Each Part ends with a way to check it. If
something goes wrong, see the troubleshooting list in SETUP.md.
