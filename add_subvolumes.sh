#!/usr/bin/env bash
#
# add_subvolumes.sh
#
# Safely convert existing directories into dedicated Btrfs subvolumes
# while preserving their contents and automatically updating /etc/fstab.
#
# Version: 1.0.1
# License: GPL-3.0-or-later
#
# Copyright (C) 2026 Scott McClain
# SPDX-License-Identifier: GPL-3.0-or-later

set -euo pipefail

if [[ $EUID -eq 0 ]]; then
    echo "Error: Run this script as your normal user, without sudo." >&2
    echo "The script will request sudo privileges when required." >&2
    exit 1
fi

# Identify the active non-root execution user natively
REAL_USER="$USER"
echo "Preparing Btrfs subvolume migration for user: ${REAL_USER}"

###############################################################################
# PHASE 1: PREPARE FSTAB
###############################################################################
echo "Optimizing target BTRFS fstab mount flags..."
sudo cp -a /etc/fstab /etc/fstab.pre-subvolume

# Read options specifically from the root mount line to preserve your installation layout
CURRENT_OPT=$(grep -E '\s/\s' /etc/fstab | awk '{print $4}')
if [ -z "${CURRENT_OPT}" ]; then
  CURRENT_OPT="defaults"
fi

IFS=',' read -r -a OPT_ARRAY <<< "${CURRENT_OPT}"

has_opt() {
  local target="$1"
  for opt in "${OPT_ARRAY[@]}"; do
    if [[ "$opt" == "$target"* ]]; then
      return 0
    fi
  done
  return 1
}

is_btrfs_mountpoint() {
  findmnt -rn --mountpoint "$1" --types btrfs &>/dev/null
}

# Collect target optimizations to add
NEW_FLAGS=()
if ! has_opt "noatime"; then NEW_FLAGS+=("noatime"); fi
if ! has_opt "autodefrag"; then NEW_FLAGS+=("autodefrag"); fi
if ! has_opt "discard"; then
  NEW_FLAGS+=("discard=async")
  echo "Disabling fstrim timer in favor of discard=async..."
  sudo systemctl disable --now fstrim.timer || true
fi
if ! has_opt "compress"; then NEW_FLAGS+=("compress=zstd:1"); fi

# 1. Safely append performance flags ONLY to / and /home lines (Ignoring /swap entirely)
if [ ${#NEW_FLAGS[@]} -gt 0 ]; then
  APPEND_STR=$(IFS=,; echo "${NEW_FLAGS[*]}")
  # Update the root / mount line safely
  sudo sed -i -E "s|(\s+btrfs\s+)(subvol=/@,defaults[^[:space:]]*\|defaults,subvol=/@[^[:space:]]*\|subvol=/@)(\s+)|\1\2,${APPEND_STR}\3|g" /etc/fstab
  # Update the /home mount line safely
  sudo sed -i -E "s|(\s+btrfs\s+)(subvol=/@home,defaults[^[:space:]]*\|defaults,subvol=/@home[^[:space:]]*\|subvol=/@home)(\s+)|\1\2,${APPEND_STR}\3|g" /etc/fstab
fi

# 2. Build the OPTIONS string for downstream subvolumes ensuring "defaults" remains first
OPTIONS="defaults"
for flag in noatime autodefrag discard=async compress=zstd:1; do
  OPTIONS="${OPTIONS},${flag}"
done

echo "Base optimizations established for new subvolumes: ${OPTIONS}"
sleep 2

###############################################################################
# PHASE 2: INITIALIZE MIGRATION
###############################################################################
ROOT_UUID="$(sudo grub-probe --target=fs_uuid /)"
DEVICE="$(df --output=source / | tail -n 1)"
echo "Target Device: ${DEVICE} | UUID: ${ROOT_UUID}"

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

source "${SCRIPT_DIR}/ROOTVOLUMES.conf"
source "${SCRIPT_DIR}/HOMEVOLUMES.conf"

# Build the effective subvolume migration lists from the configured defaults.
ROOTVOLUMES=("${CORE_ROOTVOLUMES[@]}" "${OPTIONAL_ROOTVOLUMES[@]}")
HOMEVOLUMES=("${CORE_HOMEVOLUMES[@]}" "${OPTIONAL_HOMEVOLUMES[@]}")

ROOT_MAX_LEN="$(printf '/%s
' "${ROOTVOLUMES[@]}" | wc -L)"
HOME_MAX_LEN="$(printf '/%s
' "${HOMEVOLUMES[@]}" | wc -L)"

###############################################################################
# PRE-FLIGHT PREVENTATIVE RISK ANALYSIS (SPACE CHECK & PROMPT)
###############################################################################
echo "Performing preventative disk space calculations..."

TOTAL_REQUIRED_KIB=0
for dir in "${ROOTVOLUMES[@]}"; do
  if [[ -d "/${dir}" && ! -L "/${dir}" ]]; then
    DIR_SIZE_KIB=$(sudo du -sk "/${dir}" | awk '{print $1}')
    TOTAL_REQUIRED_KIB=$((TOTAL_REQUIRED_KIB + DIR_SIZE_KIB))
  fi
done

for dir in "${HOMEVOLUMES[@]}"; do
  if [[ -d "/${dir}" && ! -L "/${dir}" ]]; then
    DIR_SIZE_KIB=$(sudo du -sk "/${dir}" | awk '{print $1}')
    TOTAL_REQUIRED_KIB=$((TOTAL_REQUIRED_KIB + DIR_SIZE_KIB))
  fi
done

CUSHION_KIB=$((TOTAL_REQUIRED_KIB / 10))
TOTAL_REQUIRED_KIB=$((TOTAL_REQUIRED_KIB + CUSHION_KIB))

FREE_SPACE_KIB=$(df -k / | tail -n 1 | awk '{print $4}')
REQ_HUMAN=$(awk "BEGIN {print $TOTAL_REQUIRED_KIB/1024/1024}")
FREE_HUMAN=$(awk "BEGIN {print $FREE_SPACE_KIB/1024/1024}")

if [ "$TOTAL_REQUIRED_KIB" -gt "$FREE_SPACE_KIB" ]; then
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  echo "CRITICAL ERROR: INSUFFICIENT STORAGE SPACE FOR SAFE MIGRATION."
  printf "Required Space (with 10%% buffer): %.2f GB\n" "$REQ_HUMAN"
  printf "Available Drive Space: %.2f GB\n" "$FREE_HUMAN"
  echo "Process halted to prevent data loss or filesystem fragmentation."
  echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
  exit 1
else
  printf "Storage validation successful. (Required: %.2f GB | Available: %.2f GB)\n" "$REQ_HUMAN" "$FREE_HUMAN"
fi

# If the target size is greater than 20 GB, force user confirmation
if (( $(awk 'BEGIN {print ('"$TOTAL_REQUIRED_KIB"' > 20971520)}') )); then
  echo "----------------------------------------------------------------------"
  printf "WARNING: You are about to migrate a large amount of data (%.2f GB).\n" "$REQ_HUMAN"
  echo "This operation may take some time depending on your disk performance."
  echo "----------------------------------------------------------------------"
  read -rp "Are you absolutely sure you want to continue? (y/N): " CONFIRM
  case "$CONFIRM" in
    [yY][eE][sS]|[yY])
      echo "Confirmation received. Proceeding with migration..."
      ;;
    *)
      echo "Migration canceled by user."
      exit 0
      ;;
  esac
fi

MNT_TMP="/mnt/btrfs_root_tmp"
sudo mkdir -p "${MNT_TMP}"
sudo mount -o subvolid=5 "${DEVICE}" "${MNT_TMP}"

# Detect whether the running system is booted into a snapshot.
BOOTED_INTO_SNAPSHOT=$(findmnt -no OPTIONS / | grep -q '@/.snapshots/' && echo true || echo false)

###############################################################################
# PHASE 3: MIGRATE ROOT SUBVOLUMES
###############################################################################
echo "Processing Root System Subvolumes..."

for dir in "${ROOTVOLUMES[@]}"; do

  #
  # Never migrate /boot or anything beneath it.
  # The bootstrap cleanup tool intentionally preserves the bootstrap
  # /boot tree after transitioning into Snapshot 1.
  #
  case "$dir" in
    boot|boot/*)
      echo "Skipping /${dir}: Boot paths are intentionally protected and are never migrated into separate Btrfs subvolumes."
      continue
      ;;
  esac

  # Leave independently mounted Btrfs subvolumes in their existing layout.
  if is_btrfs_mountpoint "/${dir}"; then
      echo "Skipping /${dir}: Already an independently mounted Btrfs subvolume."
      continue
  fi

  #
  # Skip if already a Btrfs subvolume.
  #
  if sudo btrfs subvolume show "${MNT_TMP}/@/${dir}" &>/dev/null; then
      echo "Skipping /${dir}: Subvolume already exists."
      continue
  fi

#
# When booted into a snapshot, skip directories that already exist
# inside the @ subvolume. When booted normally into @, creating the
# subvolume is safe.
#
if [[ "${BOOTED_INTO_SNAPSHOT}" == "true" && -e "${MNT_TMP}/@/${dir}" ]]; then
    echo "Skipping /${dir}: Directory already exists inside @ while booted into a snapshot."
    continue
fi

  sudo mkdir -p "${MNT_TMP}/@/$(dirname "${dir}")"

  if [[ -d "/${dir}" && ! -L "/${dir}" ]]; then
    sudo mv -v "/${dir}" "/${dir}-old"
    sudo btrfs subvolume create "${MNT_TMP}/@/${dir}"
    sudo mkdir -p "/${dir}"
    sudo mount -t btrfs -o "subvol=/@/${dir},${OPTIONS}" "${DEVICE}" "/${dir}"

    # Migrate existing contents into the new subvolume.
    sudo rsync -aHAXx --numeric-ids --info=progress2 "/${dir}-old/" "/${dir}/"
  else
    sudo btrfs subvolume create "${MNT_TMP}/@/${dir}"
    sudo mkdir -p "/${dir}"
  fi

  #
  # Restore special permissions where required.
  #
  if [[ "${dir}" == "var/tmp" || "${dir}" == "var/crash" ]]; then
    sudo chmod 1777 "/${dir}"
  fi

  #
  # Restore SELinux contexts when available.
  #
  if command -v restorecon &>/dev/null; then
    sudo restorecon -RF "/${dir}"
  fi

  #
  # Add mount to fstab if not already present.
  #
  if ! grep -q " /${dir} " /etc/fstab; then
    printf "%-41s %-${ROOT_MAX_LEN}s %-5s %-s %-s\n" \
      "UUID=${ROOT_UUID}" \
      "/${dir}" \
      "btrfs" \
      "subvol=/@/${dir},${OPTIONS}" \
      "0 0" | sudo tee -a /etc/fstab > /dev/null
  fi

done

###############################################################################
# PHASE 4: MIGRATE HOME SUBVOLUMES
###############################################################################
echo "Processing User Home Subvolumes..."
for dir in "${HOMEVOLUMES[@]}" ; do
  BTRFS_HOME_PATH=$(echo "${dir}" | sed 's|^home/||')

  # Leave independently mounted Btrfs subvolumes in their existing layout.
  if is_btrfs_mountpoint "/${dir}"; then
    echo "Skipping /${dir}: Already an independently mounted Btrfs subvolume."
    continue
  fi

  # Correctly verify if it's an actual subvolume, not a standard directory
  if sudo btrfs subvolume show "${MNT_TMP}/@home/${BTRFS_HOME_PATH}" &>/dev/null; then
    echo "Skipping /${dir}: Subvolume already exists inside BTRFS layout."
    continue
  fi

  sudo mkdir -p "${MNT_TMP}/@home/$(dirname "${BTRFS_HOME_PATH}")"

  if [[ -d "/${dir}" && ! -L "/${dir}" ]] ; then
    sudo mv -v "/${dir}" "/${dir}-old"
    sudo btrfs subvolume create "${MNT_TMP}/@home/${BTRFS_HOME_PATH}"
    sudo mkdir -p "/${dir}"
    sudo mount -t btrfs -o "subvol=/@home/${BTRFS_HOME_PATH},${OPTIONS}" "${DEVICE}" "/${dir}"

    # Migrated using safe system rsync flags
    sudo rsync -aHAXx --numeric-ids --info=progress2 "/${dir}-old/" "/${dir}/"
  else
    sudo btrfs subvolume create "${MNT_TMP}/@home/${BTRFS_HOME_PATH}"
    sudo mkdir -p "/${dir}"
  fi

  if command -v restorecon &> /dev/null; then
    sudo restorecon -RF "/${dir}"
  fi

  # Only append to fstab if the mount point destination isn't already documented
  if ! grep -q " /${dir} " /etc/fstab; then
    printf "%-41s %-${HOME_MAX_LEN}s %-5s %-s %-s\n" \
      "UUID=${ROOT_UUID}" "/${dir}" "btrfs" "subvol=/@home/${BTRFS_HOME_PATH},${OPTIONS}" "0 0" | sudo tee -a /etc/fstab > /dev/null
  fi
done

sudo umount "${MNT_TMP}"
sudo rmdir "${MNT_TMP}"

###############################################################################
# PHASE 5: FINALIZE CONFIGURATION
###############################################################################
echo "Finalizing access permissions and directory authorization mappings..."
sleep 1
sudo chown -cR "${REAL_USER}:${REAL_USER}" "/home/${REAL_USER}/"
sleep 1
sudo chmod -v 0700 "/home/${REAL_USER}/.gnupg" "/home/${REAL_USER}/.ssh" || true
sleep 1

echo "Reloading system daemons and performing test mounts..."
sudo systemctl daemon-reload
sleep 1
sudo mount -va
echo "Completed storage subsystem attachment verification."
sleep 1

###############################################################################
# PHASE 6: CLEAN UP
###############################################################################
echo "Cleaning up backup folders..."
for dir in "${HOMEVOLUMES[@]}" ; do
  if [[ -d "/${dir}-old" ]] ; then
    sudo rm -rf "/${dir}-old"
  fi
done

for dir in "${ROOTVOLUMES[@]}" ; do
  if [[ -d "/${dir}-old" ]] ; then
    sudo rm -rf "/${dir}-old"
  fi
done

echo "Btrfs subvolume migration completed successfully."

###############################################################################

printf '\n%s\n' '----------------------------------------'
printf ' Migration complete.\n'
printf ' Please reboot to activate all changes.\n'
printf '%s\n\n' '----------------------------------------'
