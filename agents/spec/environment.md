# Development Environment

Captured 2026-09-28 (UTC) via read-only discovery on the owner's machine (Git Bash / MSYS shell on Windows).
No `.env` was opened; no environment variables were printed.

| Item | Value |
|---|---|
| OS / kernel | Windows 11 (NT 10.0 build 26200), shell: MINGW64/MSYS (Git Bash) 3.6.7 |
| CPU (model, cores/threads) | Intel Core Ultra 9 275HX — 24 cores / 24 logical processors |
| RAM | 31.4 GB |
| GPU(s) (model, VRAM, driver, CUDA) | 1× NVIDIA GeForce RTX 5060 (laptop, WDDM), 8 GB VRAM (8151 MiB), driver 577.13, CUDA 12.9 |
| Disk (type, free space) | NVMe SSD (Micron 3500, 953 GB total) — ~228 GB free at capture time |
| Container runtime (Docker/Podman, GPU runtime) | **None available** — `docker` not found; containerization must be optional/degraded locally |
| Toolchains (python, node, rust, gcc/clang, cmake) | Python 3.14.5, Node 24.18.0, Rust 1.98.1, CMake 4.1.3; **g++/gcc not found** on PATH |
| Network constraints | Outbound HTTPS works (web research performed). No other constraints discovered. |

Implications for the project (recorded so architecture respects them):

- Single consumer-grade 8 GB GPU: benchmarks and workloads must fit a small VRAM budget and degrade gracefully to CPU.
- Windows-first development: scripts and tooling must run under Git Bash on Windows (POSIX-ish), avoid Linux-only assumptions
  (no `lscpu`/`free`; no Docker socket). WSL2 was not probed — TODO(phase 04) verify if it exists before choosing containerization.
- No container runtime means local dev runs directly on the host toolchains; Docker is an optional delivery tier only.
- Python 3.14 is very new — dependency wheels must be checked for compatibility when scaffolding tooling.

Deployment tiers (define in phase 03, must respect the table above):
- Local development
- Production-like local deployment
- Optional cloud/distributed deployment

Secrets: supplied only via environment variables (`HF_KEY`, see `.env.example`).
