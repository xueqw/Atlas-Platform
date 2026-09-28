# Changelog

All notable changes to Atlas Agent Platform are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). This maintained changelog starts at version 1.1.0; earlier prototype changes remain available in the Git history.

## Unreleased

<!-- Add future changes here under Added, Changed, Deprecated, Removed, Fixed, or Security. -->

## 1.1.0 - 2026-09-28

### Added

- Added dependency-free Okapi BM25 ranking for keyword retrieval when embeddings are unavailable or incompatible.
- Added mixed Chinese and Latin tokenization with Chinese bigrams to reduce single-character false positives.
- Added ten retrieval regression tests covering ranking quality, Chinese tokenization, vector safety, and fallback behavior.
- Added GitHub Actions checks for Python 3.10, Python 3.12, and the production web build.
- Added a repository-native retrieval improvement graphic to the README.
- Added a canonical `VERSION` file, a release checklist, and an automated version-consistency check.

### Changed

- Pinned React, Vite, TypeScript, and their type packages to the versions in the lockfile instead of using `latest`.
- Updated the retrieval diagnostic script to report generic retrieval scores instead of assuming every score is cosine similarity.
- Aligned the root package, web package, and API at version 1.1.0.

### Fixed

- Invalid, non-finite, and malformed stored embeddings no longer crash retrieval.
- Embeddings with dimensions that differ from the query vector now trigger BM25 fallback instead of silently producing unusable results.
- Cosine similarity now rejects mismatched vector dimensions.
