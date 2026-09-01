# Architecture

## Overview

**add_subvolumes** is a configuration-driven utility for establishing dedicated Btrfs subvolumes on systems using the standard nested `@` and `@home` layout.

The project intentionally separates **configuration** from the **migration engine**.

```
ROOTVOLUMES.conf
HOMEVOLUMES.conf
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
Validate Available Space
        │
        ▼
Process Configured Paths
        │
        ▼
Update /etc/fstab
        │
        ▼
Verify Mounts
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
5. Updates `/etc/fstab`.
6. Removes the temporary directory.

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

The implementation is distribution-neutral. Compatibility validation covers Debian, Ubuntu, Kubuntu, and CachyOS, representing Debian/Ubuntu-based and Arch-based systems. Version 1.0.1 was regression-tested on Debian and CachyOS after the independent-mount detection was added.

---

## Relationship to BootPrep

**add_subvolumes** is intended for initial system deployment and bulk subvolume adoption.

BootPrep serves a different purpose.

Future BootPrep releases may provide one-off subvolume management while **add_subvolumes** remains focused on configuration-driven bulk setup.
