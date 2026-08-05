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
Already a Btrfs Subvolume?
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

This allows the utility to be safely rerun as additional paths are enabled within the configuration.

---

## Relationship to BootPrep

**add_subvolumes** is intended for initial system deployment and bulk subvolume adoption.

BootPrep serves a different purpose.

Future BootPrep releases may provide one-off subvolume management while **add_subvolumes** remains focused on configuration-driven bulk setup.
