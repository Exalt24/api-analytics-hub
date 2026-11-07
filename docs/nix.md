# Nix dev shell

`flake.nix` and `flake.lock` give one command that opens a shell with the same Python, PostgreSQL, git and Node on any machine that has Nix, and one command that runs the fast tests inside it.

## What is pinned and what is not

| Part | Pinned by | Version in the shell (checked 2026-11-07) |
|---|---|---|
| Python | `flake.lock` (nixpkgs revision `c56a87d`, branch `nixos-26.05`) | 3.13.17 (CI asks for "3.13") |
| PostgreSQL server and client | `flake.lock` | 17.11, with the pgvector extension 0.8.2 |
| git, Node, curl | `flake.lock` | git 2.54.0, Node 22.23.4 (the web CI job uses 22) |
| Python packages | `requirements.txt`, installed with pip into `.venv-nix/` | the exact versions in that file |

The Python packages do not come from Nix. They are installed from `requirements.txt` the first time you enter the shell (and again when that file changes), which needs the network and has no hashes, the same as the CI job. So the lock makes the interpreter and the tools identical; it does not make the pip downloads byte for byte identical.

Two differences from the docker compose stack: its image is `pgvector/pgvector:0.8.7-pg17`, and nixpkgs on this revision has pgvector 0.8.2. The database suites were not run in the Nix shell (see below), so nothing here shows that migration 010 works with 0.8.2.

## Commands

```bash
nix develop                          # the shell; builds .venv-nix on the first entry
nix run .#fast-tests                 # the fast tests, without opening a shell
nix develop -c hub-fast-tests        # the same, from inside the shell's PATH
nix flake check                      # evaluates every output, checks the tool versions, shellchecks the test command
nix flake lock --no-update-lock-file # fails if flake.lock does not match flake.nix
```

## The fast tests

`hub-fast-tests` runs 470 tests in about a second and a half (once the virtualenv exists): the lock check, credential encryption, the connector registry, token refresh, webhook signatures, alert policy, order bucketing, the secret and document link scans, the vendor retry and money rules, and the billing signature and proration maths. They need no Postgres, no Chromium and no stripe-mock. The list is in `flake.nix` (`fastTestArgs`), and a test fails if a file named there does not exist.

The other 6,300 or so tests are not in this command. About thirty files need the Postgres from `docker compose up`, some need Chromium or stripe-mock. CI runs them all with service containers (`.github/workflows/ci.yml`); the full run on 2026-11-07 on the author's machine took 6 minutes 41 seconds.

## When flake.nix changes

If you change an input in `flake.nix`, or want a newer nixpkgs, run `nix flake lock` (or `nix flake update`) and commit `flake.lock` with it. `tests/test_flake_lock.py` fails if the two files disagree: a different branch, a different repository, an added or removed input, or a lock entry with no revision or hash. It reads the files as text, so it also runs in the plain Python CI job and on a machine without Nix. `nix flake lock --no-update-lock-file` says the same thing with Nix's own parser.

## What was run, and where

The commands above were run on 2026-11-07 inside the `nixos/nix` Docker image (Nix 2.35.4) on Windows 11 with Docker Desktop, never on a NixOS machine and never on macOS. The image runs with the Nix build sandbox off. The output is in [test-runs/2026-11-07-nix-flake.txt](test-runs/2026-11-07-nix-flake.txt), with a deliberate breakage: asking `flake.nix` for another branch turned `nix flake lock --no-update-lock-file`, `nix flake check` and `tests/test_flake_lock.py` red, and restoring it turned them green. The flake lists four systems (Linux and macOS, x86_64 and aarch64); only `x86_64-linux` was evaluated and built. CI runs the same commands on a GitHub Ubuntu runner with the sandbox on (job `nix` in `.github/workflows/ci-more.yml`, using `cachix/install-nix-action` pinned by commit).

Not covered: the database suites and the Chromium tests inside the shell, the web app's `npm ci`, and Playwright's browser download (Nix does not provide a browser here).
