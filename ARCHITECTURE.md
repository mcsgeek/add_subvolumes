# Architecture

## Overview

**add_subvolumes** is a configuration-driven migration utility for establishing dedicated Btrfs subvolumes within an existing Btrfs root layout and, when present, a separate Btrfs home layout.

The project intentionally separates **configuration** from the **migration engine**.

```
ROOTVOLUMES.conf
HOMEVOLUMES.conf
ACTIVITY_POLICIES.conf
        │
        ▼
Configuration
        │
        ▼
Migration Engine
        │
        ▼
Btrfs Subvolumes
        │
        ▼
/etc/fstab
```

The configuration files define **what** should become subvolumes.

The migration engine determines **how** each configured path should be processed.

---

## Design Goals

- Safe
- Repeatable
- Configuration-driven
- Portable
- Non-destructive

---

## Processing Workflow

```
Discover Environment
        │
        ▼
Load Configuration
        │
        ▼
Validate Storage and Activity
        │
        ▼
Start Recoverable Transaction
        │
        ▼
Migrate and Verify Configured Paths
        │
        ▼
Stage /etc/fstab
        │
        ▼
Verify and Synchronize Mounts
        │
        ▼
Commit /etc/fstab Atomically
        │
        ▼
Cleanup
```

---

## Path Processing

Each configured path follows the same decision process.

```
Configured Path
        │
        ▼
Exact Path Is an Active Btrfs Mount?
        │
   Yes  │  No
        ▼
 Skip   │
         ▼
Requested Nested Subvolume Exists?
        │
   Yes  │  No
        ▼
 Skip   │
         ▼
 Directory Exists?
      │        │
    Yes        No
      │        │
      ▼        ▼
 Convert    Create
```

The exact mount-point check uses `findmnt` and requires both an exact target match and the Btrfs filesystem type. A directory merely residing on the root Btrfs filesystem does not satisfy this check. This preserves distribution-provided independent mounts without encoding distribution names or subvolume naming conventions.

When converting an existing directory, the migration engine:

1. Renames the existing directory.
2. Creates a new Btrfs subvolume.
3. Mounts the new subvolume.
4. Copies the existing contents.
5. Stages the required `/etc/fstab` entry.
6. Verifies the copy and replacement mount.

After every changed path passes its final mount check and is synchronized, the engine atomically commits the prepared `fstab`. Verified recovery directories are then removed independently.

---

## Root and Home Layout Discovery

The engine derives the stable root from the active Btrfs mount. When run from a nested Snapper snapshot, it removes the snapshot suffix and creates new root paths beneath the stable base instead of inside the historical snapshot. The base root may be named `@`, `@rootfs`, `@btrfs`, or another valid subvolume path.

Home paths are processed only when `/home` is a separate Btrfs mount. A separate home filesystem may use another device and another subvolume base. Inline home layouts are reported and skipped without blocking eligible root paths.

Exact target paths already mounted as Btrfs subvolumes remain authoritative and are preserved.

---

## Configuration and Activity Policy

The Bash launcher validates the command line and starts an embedded Python 3.9+ migration engine. Configuration files are parsed as a restricted data format; they are never sourced as shell code.

`ROOTVOLUMES.conf` and `HOMEVOLUMES.conf` define migration targets. `ACTIVITY_POLICIES.conf` assigns exceptional behavior to exact configured paths:

- `accept_risk` permits an explicit decision to migrate active data. `--accept` supplies that decision for noninteractive execution.
- `manage_blockers` permits safely identified services and activators to be stopped, masked, and restored.
- `ignore_runtime` permits transient sockets to be omitted while preserving strict checks for regular files and FIFOs.

Unlisted paths use strict activity checks. Policies do not weaken path, storage, mount, hard-link, copy, or transaction validation.

---

## Data and Mount Verification

The migration copy preserves ownership, permissions, ACLs, extended attributes, and hard links within the migrated tree. Hard links that cross the migration boundary are refused because moving only one name would change their semantics.

The engine also refuses nested mounts, unexpected nested subvolumes, unsafe symlink ancestors, ambiguous stacked mounts, and inaccessible activity inspection. Copy verification combines itemized comparison with checksums and validates the mounted filesystem UUID, subvolume path, and read/write state.

Mount options are derived from the authoritative existing mount and `fstab` entry. Explicit administrator choices are retained, runtime selectors are removed, and preferred Btrfs defaults are added only when no explicit alternative exists. Existing eligible entries can receive option-only updates without a live remount.

---

## Transaction and Recovery

Before the first mutation, the engine records the plan and original `fstab` beneath `/var/lib/add_subvolumes`. A pending transaction prevents another run until the interrupted migration is recovered or reviewed.

Before commit, any failed migration retains every recovery directory, replacement mount, original `fstab`, and pending record. The engine does not delete original data unless all changed paths have passed their required checks.

Once the staged `fstab` has been checked against the original and committed atomically, the migration is complete. Recovery-directory cleanup is best effort per path: a busy directory, changed identity, or deletion error retains that one backup and continues cleaning the others. These post-commit cleanup failures are reported but do not recreate a recovery-required transaction.

---

## Idempotency

Existing subvolumes are detected before any migration occurs.

Existing exact target-path Btrfs mounts are also detected before any rename or conversion attempt. Their current mount and `/etc/fstab` layout remain authoritative.

This allows the utility to be safely rerun as additional paths are enabled within the configuration.

---

## Compatibility Model

The migration engine supports two layouts at the same time:

- Ordinary paths that should be converted into nested `@/...` or `@home/...` subvolumes.
- Exact target paths already backed by independent Btrfs mounts, which are preserved unchanged.

The implementation is distribution-neutral. Version 2.0.0 completed two add_subvolumes regression stages on Debian, Kubuntu, TUXEDO OS, Manjaro, CachyOS, and EndeavourOS, representing Debian- and Ubuntu-based systems and Arch-based systems. Every distribution passed both the initial configured migration and later adoption of `/var/lib/bootprep` as an additional subvolume.

---

## Repository Layout

The operational script and configuration files remain at the repository root. Development-only material is separated under `dev/`:

```text
add_subvolumes/
├── add_subvolumes.sh
├── ROOTVOLUMES.conf
├── HOMEVOLUMES.conf
├── ACTIVITY_POLICIES.conf
├── README.md
├── ARCHITECTURE.md
├── CHANGELOG.md
├── dev/
│   ├── README.md
│   └── tests/
└── LICENSE
```

The `dev/` tree is not required to install, run, or operate add_subvolumes.

---

## Relationship to BootPrep

**add_subvolumes** is intended for initial system deployment and bulk subvolume adoption.

BootPrep serves a different purpose.

Future BootPrep releases may provide one-off subvolume management while **add_subvolumes** remains focused on configuration-driven bulk setup.
