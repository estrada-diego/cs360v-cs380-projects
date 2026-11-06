# Project 4: Setup

## Setting up the container

Project 4 runs in a Docker container on your own machine. You installed Docker in
Project 0 (Part 4). If you have not, follow
[Project 0's SETUP.md](../project_0/SETUP.md), "Part 4: Docker", step 1.

Start Project 4's container from `<repo>/project_4/setup` on your machine:

```bash
cd <repo>/project_4/setup
docker compose up -d --build      # the first build takes a few minutes
docker compose exec dev bash      # a shell inside the container
```

The shell starts in `/work`, which is your `project_4` folder. You can edit
files on your machine, and the container will directly see the changes.
Run all the commands in SPEC.md inside the container.

When you are done, type `exit` to leave the shell, then stop the container from
`project_4/setup` on your machine:

```bash
docker compose down
```

## Layout

```text
.
├── SPEC.md                  # the spec, in parts (READ FIRST)
├── README.md                # overview + what you implement
├── SETUP.md                 # this file
├── setup/
│   ├── Dockerfile           # the container image (provided)
│   └── compose.yaml         # the container (run `docker compose` from here)
├── runner/
│   └── runner.c             # the backend server (provided, do not edit)
├── platform/
│   └── serverless.py        # *** YOU IMPLEMENT: the Platform class ***
└── tests/
    ├── run_tests.sh         # run this: the test suite, every check the grader runs
    └── run_tests.py         # the checks themselves (run_tests.sh runs this)
```

## Troubleshooting

- **Restart the platform after you edit `serverless.py`.** A running platform
  keeps the old code. Stop it and start it again.
- **Read the platform's output first.** If a request fails or hangs, the Python
  error (traceback) appears in the terminal where you started the platform.
- **Port already in use.** If starting the platform or a backend fails with
  `Address already in use`, one from an earlier run is still running on that
  port. `pgrep -a runner` and `pgrep -af serverless.py` list them with their
  process IDs; stop one with `kill <id>`.
- **Stop the backends when you are done:** `pkill runner` stops them all.
- **Container not running.** If `docker compose exec` says
  `service "dev" is not running`, start it from `project_4/setup` on your
  machine with `docker compose up -d`.
