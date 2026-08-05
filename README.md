# add_subvolumes

Safely convert existing directories into dedicated Btrfs subvolumes while preserving their contents and automatically updating `/etc/fstab`.

Unlike creating subvolumes during installation, **add_subvolumes** migrates an existing Btrfs system in place. Existing data is preserved, new mount points are configured automatically, and the utility can be safely rerun as additional subvolumes are adopted.

---

## Features

- Safely converts existing directories into dedicated Btrfs subvolumes
- Preserves existing files, ownership, permissions, ACLs, and extended attributes
- Automatically updates `/etc/fstab`
- Optimizes Btrfs mount options for newly created subvolumes
- Configuration-driven using separate root and home configuration files
- Detects and skips existing subvolumes
- Safe to rerun as additional subvolumes are adopted
- Self-contained with no installation required

---

## Requirements

- Linux system using Btrfs
- Bash
- `btrfs-progs`
- `rsync`
- `sudo`

---

## Configuration

The migration engine is intentionally separated from configuration.

### ROOTVOLUMES.conf

Defines root filesystem subvolumes.

Two arrays are provided:

- `CORE_ROOTVOLUMES`
- `OPTIONAL_ROOTVOLUMES`

### HOMEVOLUMES.conf

Defines home directory subvolumes.

Two arrays are provided:

- `CORE_HOMEVOLUMES`
- `OPTIONAL_HOMEVOLUMES`

The supplied configuration enables only broadly useful subvolumes by default. Additional examples are included but commented out so each installation can be customized without modifying the migration engine.

---

## Usage

Run the script as your normal user:

```bash
chmod +x add_subvolumes.sh

./add_subvolumes.sh
```

Do **not** run the script with `sudo`.

The script requests elevated privileges only when required.

---

## Migration Process

During execution the utility:

1. Optimizes Btrfs mount options
2. Loads the configured migration lists
3. Validates available disk space
4. Creates missing Btrfs subvolumes
5. Migrates existing directory contents
6. Updates `/etc/fstab`
7. Verifies all mounts
8. Removes temporary migration directories

After a successful migration, reboot to activate the new subvolume layout.

---

## Safety

The migration engine is designed to be conservative.

- Existing subvolumes are detected and skipped.
- Existing data is preserved during migration.
- `/etc/fstab` is backed up before modification.
- Existing Btrfs installations can be expanded incrementally without reinstalling or rebuilding the filesystem.

---

## License

GPL-3.0-or-later
