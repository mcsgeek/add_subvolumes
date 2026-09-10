# add_subvolumes

Safely create and manage dedicated Btrfs subvolumes within existing root and separate-home Btrfs layouts.

**Version 2.0.0**

For each configured path, the migration engine automatically determines whether a new subvolume should be created, an existing directory should be safely converted into a subvolume while preserving its contents, or an existing subvolume should be skipped.

The project intentionally separates configuration from the migration engine, allowing the same migration logic to be reused with different subvolume layouts.

---

## Features

- Provides explicit `--dryrun` and `--execute` modes
- Creates new Btrfs subvolumes where configured paths do not yet exist
- Safely converts existing directories into dedicated Btrfs subvolumes
- Preserves exact target paths already mounted as independent Btrfs subvolumes
- Supports standard and nonstandard root-subvolume names, including operation from a nested snapshot
- Supports a separate Btrfs `/home`, including a different Btrfs filesystem
- Preserves files, ownership, permissions, ACLs, extended attributes, and internal hard links
- Verifies copied data before committing `/etc/fstab`
- Uses a recoverable transaction record when execution is interrupted
- Preserves existing mount choices while adding preferred options only where appropriate
- Handles active paths through explicit, per-path activity policies
- Safely reruns as additional paths are adopted or mount options are normalized
- Remains self-contained with no installation required

---

## Requirements

- Linux system using Btrfs for the root filesystem
- Bash
- Python 3.9 or newer
- `btrfs-progs`
- `rsync`
- `lsof`
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

### ACTIVITY_POLICIES.conf

Defines exact configured paths that need special handling when processes are using them:

- `accept_risk` allows an explicit active-data decision. In unattended execution, `--accept` records that decision for these paths.
- `manage_blockers` lets the engine stop, mask, and later restore safely identified services and activators.
- `ignore_runtime` permits transient sockets to be omitted while retaining strict checks for regular files and FIFOs.

Paths without a policy remain strict. Policies match exact configured paths and do not apply automatically to descendants.

Configuration files are parsed as data instead of being sourced as shell code, and unsafe, duplicate, overlapping, or protected paths are rejected.

---

## Usage

Run the script as your normal user.

```bash
chmod +x add_subvolumes.sh

./add_subvolumes.sh --dryrun
./add_subvolumes.sh --execute
```

Do **not** run the script with `sudo`.

The script requests elevated privileges only when required. Add `--accept` to an execution only when you intend to accept active-data risk for paths listed under `accept_risk`:

```bash
./add_subvolumes.sh --execute --accept
```

`--accept` does not override strict activity checks, inspection failures, unsafe storage, copy verification, or any other safety refusal.

---

## What Happens

For each configured path the migration engine automatically determines whether it should:

- Create a new Btrfs subvolume
- Convert an existing directory into a Btrfs subvolume while preserving its contents
- Skip the path because the requested nested Btrfs subvolume already exists
- Skip the path because it is already an independently mounted Btrfs subvolume

In dry-run mode, the utility discovers the environment, validates configuration and policies, checks activity, and prints the proposed work without starting a transaction.

In execute mode, each conversion is copied and verified while the original remains available as a recovery directory. After all replacement mounts have been verified and synchronized, the utility atomically commits the prepared `/etc/fstab`. It then removes each verified recovery directory independently. A busy or failed post-commit cleanup is reported and retained without reversing an otherwise completed migration.

A reboot is recommended after a successful migration.

---

## Safety

The migration engine is designed to be conservative.

- Existing subvolumes are never recreated.
- Active target-path Btrfs mounts are preserved even when their underlying subvolume names differ from the requested nested layout.
- Existing data is preserved during migration.
- File content and metadata are verified before the `fstab` commit.
- External hard links, nested mounts or subvolumes, unsafe symlink ancestors, and concurrent `fstab` edits cause a refusal.
- `/etc/fstab` is backed up and replaced atomically only after all migrations pass their pre-commit mount checks.
- Interrupted pre-commit work retains its recovery directories, original `fstab`, and transaction record.
- Post-commit cleanup is best effort per recovery directory and cannot turn a committed migration into a recovery-required transaction.
- Existing Btrfs installations can be expanded incrementally without reinstalling or rebuilding the filesystem.

---

## Compatibility

Version 2.0.0 completed regression testing on Debian, Kubuntu, TUXEDO OS, Manjaro, CachyOS, and EndeavourOS. These represent Debian- and Ubuntu-based systems and Arch-based systems.

Compatibility is based on filesystem state rather than distribution names. An exact configured target that is already a Btrfs mount point is retained in its existing layout. For example, a distribution-provided `/srv` mount remains untouched even when its underlying subvolume is not named `@/srv`.

### Verified test cases

| Distribution | Initial configured migration | Later `/var/lib/bootprep` add-on |
| --- | --- | --- |
| Debian | Passed | Passed |
| Kubuntu | Passed | Passed |
| TUXEDO OS | Passed | Passed |
| Manjaro | Passed | Passed |
| CachyOS | Passed | Passed |
| EndeavourOS | Passed | Passed |

All 12 add_subvolumes stages passed. Compatibility is determined by filesystem and service state rather than the distribution name.

---

## License

GPL-3.0-or-later
