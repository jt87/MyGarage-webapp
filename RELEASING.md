# Releasing MyGarage

A release is a version tag on `main`. GitHub Actions then tests it, builds
`ghcr.io/jt87/mygarage:<version>` (plus `<major>.<minor>`, `<major>` and `latest`), and publishes a
GitHub release with that version's notes from CHANGELOG.md.

1. In a pull request, set the version in `app/__init__.py`, and in CHANGELOG.md change
   `## <version> (unreleased)` to `## <version> (<date>)`. Update `app_version` in
   `deploy/truenas-catalog/mygarage/app.yaml` and the tag in its `ix_values.yaml`
   (the tests check that these match).
2. Merge it.
3. Tag the merge commit and push the tag:
   ```
   git checkout main && git pull
   git tag v1.1.0 && git push origin v1.1.0
   ```
   The workflow refuses a tag that doesn't match `app/__init__.py` or whose
   changelog section still says "unreleased".

Version numbers: 1.1.1 only fixes things, 1.2.0 adds features, 2.0.0 needs something
from people when they update. Say what under `### Upgrading from <old>` in the changelog:
the app shows that section in its new-version notice, and overnight upgrades never
cross a major version.

Builds of `main` between releases are tagged `edge`, for trying the next release.
