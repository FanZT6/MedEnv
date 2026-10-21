# Contributing

Use synthetic cases in tests. Do not commit real patient text, identifiers, images, private import manifests, model weights, API credentials, or clinical trajectories.

Run `python -m unittest discover -s tests -v`, `python scripts/check_release.py`, and `python -m build` before proposing changes. Describe any changes to tool access, terminal scoring, prompts, or cohort selection because they can invalidate comparisons with earlier results.

The default CI suite is offline and uses synthetic data. Model-backed tests require an authorized local environment and explicit service configuration. Do not add hosted CI jobs that fetch or transmit credentialed patient data.
