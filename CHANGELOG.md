# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Planned

- Coverage threshold raised from 70% to 80% (needs more `daemon.run()` tests).
- `ruff format --check` in CI once the tree is formatted.
- PyPI release workflow (`publish.yml`) wired to tag pushes.

## [0.3.0] - 2026-09-12

Release focused on **making an installed copy actually usable**, plus the
config/CLI gaps that the README already promised but the code did not deliver.

### Fixed

- **Packaging: the shipped wheel could not work at all.** `config.toml` lived
  inside the package but was never declared as package data, so
  `pip install .` produced an install with no config file and every command
  failed with `ConfigNotFoundError`. The template now ships explicitly
  (`config.example.toml`), and CI installs the built wheel and runs
  `ponte init` to keep the regression from coming back.
- **`[retry] stable_after` and `[health] max_check_interval` were silently
  ignored.** Both are documented (and covered by tests at the dataclass level),
  but the TOML parsers never read them, so setting them had no effect.
- **The "force killed" notice was dead code.** `stop()` returned a status whose
  `message` could never contain a kill hint, so a `taskkill /F` escalation was
  reported as a clean stop. The message is now propagated.
- **`_smoke_test.py` had been broken since the health-backoff release** (it
  crashed with `AttributeError: '_RC' object has no attribute 'stable_after'`)
  even though the README advertised it. Its hand-written config stubs are now
  in sync, and it exercises the interval backoff deterministically.
- `ponte stop` / `restart` no longer hide a force kill; `ponte init` no longer
  swallows `[ssh]` as a Rich markup tag.
- `_optional_str` is overloaded, so a caller passing a real default no longer
  has to narrow `str | None` (three latent `arg-type` errors).

### Changed

- **The config file moved out of the package** to the per-user config dir
  (`%APPDATA%\ponte\config.toml`, `~/.config/ponte/config.toml`,
  `~/Library/Application Support/ponte/config.toml`). `pip install -U` no
  longer overwrites your settings. Resolution order: `--config` →
  `$PONTE_CONFIG` → user config dir → the legacy in-package path (still
  honoured, logged as deprecated). The working directory is deliberately not
  searched, since services have arbitrary `cwd`s.
- New `ponte init [--path P] [--force]`: writes the config from the template,
  migrates an existing in-package file, and never overwrites without `--force`.
- New global options `--config/-c PATH` and `--version/-V`.
- Unknown/typo'd config keys are now reported instead of being dropped
  silently: `ponte config` prints them and `load_config()` logs them.
- Service installs now launch the daemon with an explicit `--config <file>`, so
  a SYSTEM Scheduled Task reads your config rather than its own `%APPDATA%`.
- `[windows] run_as` now defaults to `user`. The old `system` default could not
  read `~/.ssh` at all, i.e. the default combination was broken out of the box.
- The daemon's working directory is now the config file's directory instead of
  the package's parent (`site-packages` after a normal install).
- Removed the hardcoded `D:\Git\usr\bin\ssh.exe` fallback in favour of
  environment-derived Git / Windows-OpenSSH locations.
- `ponte config` also shows `stable_after`, `max_check_interval` and
  `windows.run_as`.
- Coverage gate raised from 55% to 70%.

### Added

- `ruff` + `mypy` in CI (and as dev dependencies); both are clean.
- A `build` CI job that builds the sdist/wheel, asserts the example config is
  inside the wheel, installs it and exercises `ponte init`.
- `tests/conftest.py` with an autouse fixture that isolates the config
  override, `$PONTE_CONFIG` and the config cache between tests.
- Tests for the new config resolution, `ponte init`, unknown-key warnings,
  service unit generation (systemd/launchd) and the force-kill message.
- `legacy/` — the pre-Python PowerShell/batch/shell scripts, marked deprecated,
  with a command mapping. `setup.ps1` copied your private key into the project
  directory; that logic has been deleted.
- This changelog, project URLs/metadata, and lower bounds on dependencies.

### Migration

```bash
ponte init       # migrates the in-package config if present
ponte install    # re-register the service so it passes --config
```

## [0.2.1]

Tunnel stability fixes (PR #1): a "zombie" SSH process (alive but ports down)
is force-reconnected after 3 consecutive failed health checks; a session that
stays up ≥ `stable_after` seconds resets the retry budget; health-check
intervals back off exponentially during outages (`max_check_interval`); SSH
stderr is read in real time. New `[retry] stable_after` and
`[health] max_check_interval` options.

## [0.2.0]

- `windows.run_as` — choose the Scheduled Task identity/timing: `user`
  (logon) or `system` (boot).
- The SSH console window is suppressed on Windows.

## [0.1.0]

Initial release of the Python CLI: retry loop with exponential backoff +
jitter, health checks, auto-start services for Windows (Scheduled Task),
Linux (systemd user) and macOS (launchd), cross-platform `ssh` resolution,
and the pytest suite with CI/Codecov.

[Unreleased]: https://github.com/modusensus/ponte/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/modusensus/ponte/releases/tag/v0.3.0
[0.2.1]: https://github.com/modusensus/ponte/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/modusensus/ponte/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/modusensus/ponte/releases/tag/v0.1.0
