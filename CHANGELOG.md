# Changelog

All notable changes to Atlas Agent Platform are recorded in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/), and the project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). This maintained changelog starts at version 1.2.0.

## Unreleased

<!-- Add future changes here under Added, Changed, Deprecated, Removed, Fixed, or Security. -->

## 1.2.0 - 2026-09-28

### Added

- Added a macOS and Linux development launcher alongside the existing Windows PowerShell launcher.
- Added generic OpenAI-compatible embedding settings with an OpenAI default.
- Added English intent recognition and U.S.-oriented starter scenarios to the conversational Agent Builder.
- Added a CI repository policy check that blocks tracked environment files and non-English product text.

### Changed

- Made the primary workspace, Agent Builder, Web IDE, API messages, examples, and generated agent files English-first.
- Repositioned Atlas as a self-hosted, OpenAI-ready agent workspace for U.S. teams and developers.
- Standardized the supported model and connector experience around OpenAI-compatible APIs and GitHub.
- Updated the README with cross-platform setup, environment configuration, and the project's security model.

### Fixed

- Removed a stale generated-app fallback expression that could raise an error after a failed model request.
- Generated English agent requests now receive useful names and capability mappings.
- Removed the legacy prototype and internal planning artifacts so the repository presents one production-oriented architecture.

### Security

- Removed an accidentally tracked local environment file and expanded `.gitignore` coverage for nested environment files.
