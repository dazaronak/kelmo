# Offline runtime boundary

Status: this page describes the isolated runtime experiment (`packaging/kelmo-isolated`,
`bootstrap.py`, `runtime.py`, `linux_runtime.py`), not the default launcher. The default
`kelmo` launcher starts `entry.py` with Python `-I -S`; that controls imports and is not a
filesystem or network sandbox (see TECHNICAL.md). Everything below is the boundary the
isolated runtime is built to provide.

In the isolated runtime, the archive and the deterministic wiki compiler run without network
access. This is an OS restriction, established before Python processes site packages or imports
dependencies. `local_files_only=True` avoids accidental downloads; it does not
replace the OS boundary.

The threat model includes a malicious dependency executing at Python startup,
opening sockets, starting a child process, reading credentials, or modifying the
installed application. A kernel compromise or a privileged host administrator
is outside this boundary. A compromised core can still damage data explicitly
assigned to it; retained backups must be outside its writable filesystem.

## Process boundaries

- The isolated launcher (`kelmo-isolated`) starts a standard-library-only bootstrap
  with Python isolated mode and site initialization disabled. The bootstrap
  selects allowed paths, clears credentials from the environment, closes inherited
  file descriptors other than stdio, and starts the runtime inside OS isolation.
  The current macOS build accepts the Homebrew Python 3.12 interpreter outside
  the dependency environment. Replacing the venv interpreter with package code
  must fail before that code executes. The deployment installer must keep the
  trusted launcher/bootstrap outside dependency-owned write paths and reject
  wheel ownership collisions; wheel installation alone does not establish that
  complete supply-chain boundary.
- Runtime code, dependencies and provisioned models are read-only. The selected
  vault and a private scratch directory supply the writable paths. A source file
  explicitly passed to the CLI receives read-only access. Other user files,
  credential stores, host sockets and other application state are excluded.
- macOS uses the existing strict Seatbelt policy as its starting point. Remove
  the old localhost exception. Restrict executable paths and Mach/IPC access.
- Linux requires both filesystem and network namespaces, with no host networking,
  host sockets or credentials mounted. Missing isolation support is a startup
  error, never permission to run unconfined. Network namespaces alone are not a
  complete filesystem sandbox.
- Sync, encrypted backup delivery, model provisioning and optional LLM resolution
  run outside the core. They use fixed configured destinations and operation
  types. Kelmo responses cannot supply arbitrary URLs, shell commands or file
  targets for those processes. Runtime has none of their credentials.

Normal wiki reading, rendering, fact application, link checks, graph indexing and
local Git commits require no network. External wiki links are text, not fetch
instructions. A conflict requiring model judgment remains pending until a
separately authorized resolver supplies a result.

## Verification and installation gates

Inject a synthetic startup dependency and exercise TCP, UDP, localhost, child
execution, forbidden file reads and runtime-file writes. Each attempt must be
denied while an actual local archive/search/wiki operation succeeds. Test the
installed launcher, including Python startup, rather than just a Python wrapper
around socket calls. Do not provide an unsafe fallback or an environment flag
that bypasses the launcher policy.

Direct library imports are for already isolated embedding applications; they
cannot sandbox a caller that has already imported arbitrary dependencies.
Installing a wheel is not the complete deployment installer. Every production
entry path, including MCP, hooks, worker, graph and administration, must use the
same verified boundary before deployment parity can pass.

## Linux test deployment

The Linux adapter supports x86_64 with system Python 3.12 or 3.13 and bubblewrap.
It creates private user, PID, network, IPC and mount namespaces, drops capabilities,
and installs a seccomp filter before importing application dependencies. Socket
creation is denied, including Unix sockets and localhost. Only selected filesystem
roots are mounted; runtime code and model files are read-only. The filter denies
cross-process memory access, namespace changes and io_uring. This is not a syscall
allowlist or a guarantee against kernel vulnerabilities.

The sandbox supplies an artificial machine ID. The tested ONNX Runtime crashed
during import without one, after attempting a shell fallback. No host machine ID
or shell access was granted. Model provisioning mounts the five selected artifacts
into a flat read-only input view, so a Hugging Face cache's symlink layout need not
be exposed inside the runtime.

The wheel includes a JSON stdio gate. Deployment fixes its runtime and vault;
requests cannot select another vault, arbitrary input files, provisioning commands
or a shell command. This dispatcher does not configure SSH authentication, and a deployed
forced-command account is not yet verified.

The synthetic Linux run covered initialization, pinned model import, capture,
worker execution, archive/wiki application, semantic search, replay and doctor.
Reinstallation from the current lock is still required before release. The macOS same-UID environment-read gap remains
open; native macOS is not a verified host-credential boundary. The same-UID process-argument read is open for the
same reason: `macos_policy()` allowlists `sysctl-read` by name and names no `kern.procargs2` rule, so a confined
process can read another process's arguments on macOS, and hiding process arguments is a claim only the Linux adapter makes and only its kernel case measures.

External model resolution remains outside this sandbox. The proposed daemon can
claim a bounded job and submit a proposal through the confined CLI; it must not
receive direct database or registry write access. Durable resolution jobs and the
daemon are not implemented by the Linux adapter.
