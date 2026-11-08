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

1. Set `version` in `pyproject.toml` (for example `0.1.0`) and commit it to `main`.
2. Tag that commit `v` plus the same version and push the tag:

   ```sh
   git tag v0.1.0
   git push origin v0.1.0
   ```

`release.yml` runs the full CI (source tests on Linux and macOS, artifact build and inspection,
fresh installed-launcher tests), checks that the tag matches the built version, and publishes the
exact wheel and sdist CI tested. A failed check publishes nothing. PyPI never accepts the same
version twice, so a broken release is fixed by a new version, not by a re-upload.

CI does not download the embedding model, so a green run does not exercise real model search. After a
release, install the package in a clean environment and run one real search.
