# add_subvolumes

Safely create and manage dedicated Btrfs subvolumes on systems using the standard nested `@` and `@home` layout.

For each configured path, the migration engine automatically determines whether a new subvolume should be created, an existing directory should be safely converted into a subvolume while preserving its contents, or an existing subvolume should be skipped.

The project intentionally separates configuration from the migration engine, allowing the same migration logic to be reused with different subvolume layouts.

---

## Features

- Creates new Btrfs subvolumes where configured paths do not yet exist
- Safely converts existing directories into dedicated Btrfs subvolumes
- Automatically skips existing subvolumes
- Preserves existing files, ownership, permissions, ACLs, and extended attributes
- Automatically updates `/etc/fstab`
- Preserves existing mount options while applying recommended Btrfs optimizations to newly created subvolumes
- Configuration-driven using separate root and home configuration files
- Safe to rerun as additional subvolumes are adopted
- Self-contained with no installation required

---

## Requirements

- Linux system using the standard nested `@` and `@home` Btrfs layout
- Bash
- `btrfs-progs`
- `rsync`
- `sudo`

---

## Configuration

The migration policy is intentionally separated from the migration engine.

### ROOTVOLUMES.conf

Defines configured root filesystem subvolumes.

- `CORE_ROOTVOLUMES`
- `OPTIONAL_ROOTVOLUMES`

### HOMEVOLUMES.conf

Defines configured user home subvolumes.

- `CORE_HOMEVOLUMES`
- `OPTIONAL_HOMEVOLUMES`

The supplied configuration enables only broadly useful subvolumes by default. Optional examples may be uncommented or additional paths added to customize the layout without modifying the migration engine.

---

## Usage

Run the script as your normal user.

```bash
chmod +x add_subvolumes.sh

./add_subvolumes.sh
```

Do **not** run the script with `sudo`.

The script requests elevated privileges only when required.

---

## What Happens

For each configured path the migration engine automatically determines whether it should:

- Create a new Btrfs subvolume
- Convert an existing directory into a Btrfs subvolume while preserving its contents
- Skip the path because a Btrfs subvolume already exists

After processing all configured paths, the utility updates `/etc/fstab`, verifies the new mounts, and removes temporary migration directories.

A reboot is recommended after a successful migration.

---

## Safety

The migration engine is designed to be conservative.

- Existing subvolumes are never recreated.
- Existing data is preserved during migration.
- `/etc/fstab` is backed up before modification.
- Existing Btrfs installations can be expanded incrementally without reinstalling or rebuilding the filesystem.

---

## License

GPL-3.0-or-later
