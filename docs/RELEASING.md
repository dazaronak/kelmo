# Releasing

Releases go to PyPI from GitHub Actions through
[Trusted Publishing](https://docs.pypi.org/trusted-publishers/): PyPI trusts one workflow in one
repository, so no API token is stored in the repository or in GitHub secrets.

## One-time setup

1. On PyPI, the project owner adds a pending publisher (Account, Publishing, "Add a new pending
   publisher", GitHub): project `kelmo`, owner and repository of this repository, workflow
   `release.yml`, environment `pypi`. The first successful release creates the project.
2. In the GitHub repository, Settings, Environments: create `pypi`. Adding yourself as a required
   reviewer makes every release wait for one approval click before anything is uploaded.

## Each release

1. Set the version in three places and commit them to `main`: `version` in `pyproject.toml` (for
   example `0.2.0`), `__version__` in `src/kelmo/__init__.py`, and `workspace.package.version` in
   `rust/Cargo.toml` (`0.2.0`; a development version is spelled `0.2.0-dev.0` there and
   `0.2.0.dev0` in Python). `tests/test_distribution.py::test_versions_agree` fails when they
   differ. Refresh `uv.lock` with `uv lock`.
2. Run `ci.yml` by hand once (Actions, CI, "Run workflow"): a manual run builds the full matrix.
3. Tag that commit `v` plus the same version and push the tag:

   ```sh
   git tag v0.2.0
   git push origin v0.2.0
   ```

`release.yml` runs the full CI (source tests on Linux and macOS, Rust checks, a wheel for each of
Linux x86_64, Linux aarch64 and macOS arm64, each carrying the `kelmod` binary, the sdist, artifact
inspection, fresh installed-launcher tests), checks that the tag matches the built version, and
publishes the exact three wheels and the sdist CI tested. A failed check publishes nothing. PyPI never
accepts the same version twice, so a broken release is fixed by a new version, not by a re-upload.

There is no Intel Mac wheel: onnxruntime, which the embedding library needs, has published no macOS
x86_64 wheel since version 1.24, so Kelmo cannot be installed there.

CI does not download the embedding model, so a green run does not exercise real model search. After a
release, install the package in a clean environment and run one real search.
