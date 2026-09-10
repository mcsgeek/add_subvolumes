# Changelog

All notable changes to add_subvolumes are documented in this file.

The project follows Semantic Versioning.

---

## [2.0.0] - 2026-09-10

### Added

- Added explicit `--dryrun` and `--execute` modes, with `--accept` for exact paths configured to permit an active-data risk decision.
- Added restricted parsing for root, home, and JSON activity-policy configuration without sourcing configuration as shell code.
- Added exact-path policies for accepted active-data risk, managed services and activators, and ignored runtime sockets.
- Added recoverable transaction state, original-`fstab` preservation, and interrupted-run protection.
- Added support for stable root discovery from a nested snapshot, nonstandard root-subvolume names, and a separate Btrfs home filesystem.
- Added package discovery guidance for missing tools on Debian- and Arch-based systems.
- Added automated coverage for migration, policies, activity handling, mount options, tool discovery, recovery, and runtime files.

### Changed

- Rebuilt the migration engine in embedded Python while retaining a self-contained Bash launcher.
- Preserved exact target-path Btrfs mounts and existing administrator mount-option choices.
- Added atomic `fstab` staging and commit with concurrent-edit detection.
- Strengthened data preservation with checksum comparison, ACL and extended-attribute handling, internal hard-link preservation, and refusal of external hard links.
- Refused unsafe symlink ancestors, nested storage, ambiguous mounts, and incomplete activity inspection.
- Made post-commit backup cleanup best effort per path. A busy or failed cleanup retains only the affected backup and does not turn a committed migration into a recovery-required transaction.
- Moved `/opt` from the default core set to the optional vendor-software examples and added `/var/lib/bootprep` as an optional target.

### Compatibility

- Completed both add_subvolumes regression stages on Debian, Kubuntu, TUXEDO OS, Manjaro, CachyOS, and EndeavourOS: initial configured migration and later `/var/lib/bootprep` adoption.

---

## [1.0.1]

- Preserved exact configured target paths already mounted as independent Btrfs subvolumes.
- Regression-tested the independent-mount compatibility fix on Debian and CachyOS.

## [1.0.0]

- Initial configuration-driven Btrfs subvolume migration release.
