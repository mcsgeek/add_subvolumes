#!/usr/bin/env bash
# Version: 2.0.0
# Copyright (C) 2026 Scott McClain
# SPDX-License-Identifier: GPL-3.0-or-later
# Keep the launcher and engine together so the tool remains a single script.
set -euo pipefail

usage() {
    printf 'Usage:\n  %s --dryrun [--accept]\n  %s --execute [--accept]\n' "$0" "$0" >&2
    exit 1
}
RUN_MODE=""
AUTO_ACCEPT=0
for arg in "$@"; do
    case "$arg" in
        --dryrun|--execute) [[ -z "$RUN_MODE" ]] || usage; RUN_MODE="$arg" ;;
        --accept) [[ "$AUTO_ACCEPT" -eq 0 ]] || usage; AUTO_ACCEPT=1 ;;
        *) usage ;;
    esac
done
[[ -n "$RUN_MODE" ]] || usage
command -v python3 >/dev/null || { echo 'Error: Python 3.9 or newer is required; python3 was not found.' >&2; exit 1; }
# Check before importing the engine or reading configuration. Keep this probe
# compatible with older Python 3 interpreters so they get a useful error.
if ! python3 -c '
import sys
if sys.version_info < (3, 9):
    sys.stderr.write("Error: Python 3.9 or newer is required; found {}.{}.{}.\n".format(*sys.version_info[:3]))
    sys.exit(1)
'; then
    exit 1
fi
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
exec python3 - "$RUN_MODE" "$SCRIPT_DIR" "$AUTO_ACCEPT" <<'PYTHON_ENGINE'
"""Btrfs migration engine. Configuration is data, never sourced shell code."""
import dataclasses
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid as uuid_module


class Refusal(Exception):
    pass


class Busy(Refusal):
    def __init__(self, path, pids):
        self.pids = pids
        super().__init__(f"{path}: used by process IDs {', '.join(map(str, sorted(pids)))}. "
                         "Reboot and retry. If it remains busy, review these blockers; "
                         "applications started at login may reopen it.")


class MissingTools(Refusal):
    pass


def discover_tool_packages(missing):
    # Query existing local file indexes only: no sudo, refresh, downloads,
    # package installation, or new lookup dependency. Ambiguity stays unresolved.
    wanted = set(missing)
    if not wanted or any(not re.fullmatch(r"[A-Za-z0-9_.+-]+", tool) for tool in wanted):
        return {}
    pattern = r"^/?(usr/)?(local/)?s?bin/(" + "|".join(re.escape(t) for t in sorted(wanted)) + r")$"
    candidates = {tool: set() for tool in wanted}
    for backend in ("apt-file", "pacman"):
        executable = shutil.which(backend)
        if not executable:
            continue
        args = ([executable, "search", "--regexp", pattern] if backend == "apt-file"
                else [executable, "-F", "--regex", "--machinereadable", pattern])
        try:
            reply = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, timeout=5,
                                   env={**os.environ, "LC_ALL": "C"})
        except (OSError, subprocess.TimeoutExpired, UnicodeError):
            continue
        if reply.returncode or reply.stderr.strip():
            continue
        found = {tool: set() for tool in wanted}
        valid = True
        for line in reply.stdout.splitlines():
            if not line.strip():
                continue
            if backend == "apt-file":
                if ": " not in line:
                    valid = False
                    break
                package, path = line.split(": ", 1)
            else:
                fields = line.split("\0")
                if len(fields) != 4:
                    valid = False
                    break
                _, package, _, path = fields
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9+_.-]*(?::[A-Za-z0-9_-]+)?", package):
                valid = False
                break
            if re.fullmatch(pattern, path):
                found[path.rsplit("/", 1)[-1]].add(package)
        if valid:
            for tool in wanted:
                candidates[tool].update(found[tool])
    return {tool: next(iter(packages)) for tool, packages in candidates.items() if len(packages) == 1}


def missing_tools_message(missing):
    missing = list(dict.fromkeys(missing))
    discovered = discover_tool_packages(missing)
    packages = list(dict.fromkeys(discovered[tool] for tool in missing if tool in discovered))
    unresolved = [tool for tool in missing if tool not in discovered]
    lines = ["Cannot continue: required tools are unavailable.", "",
             "Missing tools: " + ", ".join(missing)]
    if packages:
        lines.append("Packages to install: " + ", ".join(packages))
    if packages and unresolved:
        lines.append("Package lookup unresolved for: " + ", ".join(unresolved))
    instruction = ("Please install these packages and run this script again." if not unresolved
                   else "Please install the packages providing these tools and run this script again.")
    return "\n".join([*lines, "", instruction, "No migration changes have been made."])


SYSTEM_COMMAND_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin")


def command_search_path():
    # Normal-user PATHs can omit sbin even though required tools such as blkid
    # are installed there. Use the same expanded PATH for discovery and child
    # processes, without changing the invoking shell's environment.
    directories = os.environ.get("PATH", os.defpath).split(os.pathsep)
    return os.pathsep.join(dict.fromkeys(p for p in [*directories, *SYSTEM_COMMAND_DIRS] if p))


def command(args, *, privileged=False, input=None, check=True):
    argv = (["sudo", "--"] if privileged else []) + [str(a) for a in args]
    try:
        result = subprocess.run(argv, input=input, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env={**os.environ, "LC_ALL": "C"},
                                timeout=20 if str(args[0]) == "systemctl" else None)
    except subprocess.TimeoutExpired as exc:
        raise Refusal("Service command timed out; its outcome must be rechecked: " + shlex.join(argv)) from exc
    if check and result.returncode:
        raise Refusal(f"{shlex.join(argv)} failed:\n{result.stderr.strip() or result.stdout.strip()}")
    return result


def root(args, **kwargs):
    return command(args, privileged=True, **kwargs)


def beneath(path, parent):
    return path.startswith(parent.rstrip("/") + "/")


def safe_relative(value):
    # Deliberately reject fstab escaping, shell substitutions and alternate
    # spellings rather than silently normalizing potentially dangerous paths.
    if not re.fullmatch(r"[A-Za-z0-9_@.+/-]+", value or ""):
        raise Refusal(f"Unsupported path spelling: {value!r}")
    if value.startswith("/") or any(p in ("", ".", "..") for p in value.split("/")):
        raise Refusal(f"Use a relative path without empty, '.' or '..' components: {value!r}")
    return "/" + value


def load_config(path, family, user, optional=False):
    if not path.exists():
        if optional:
            return []
        raise Refusal(f"Required configuration is missing: {path}")
    allowed = {f"CORE_{family}VOLUMES", f"OPTIONAL_{family}VOLUMES"}
    lexer = shlex.shlex(path.read_text(), posix=True, punctuation_chars="()")
    lexer.whitespace_split = True
    lexer.commenters = "#"
    try:
        tokens = []
        for token in lexer:
            tokens.extend(list(token) if re.fullmatch(r"[()]+", token) else [token])
    except ValueError as exc:
        raise Refusal(f"{path}: {exc}") from exc
    arrays = {}
    index = 0
    while index < len(tokens):
        name = tokens[index].removesuffix("=")
        if tokens[index] != name + "=" or name not in allowed or name in arrays:
            raise Refusal(f"{path}: expected one of {sorted(allowed)}, got {tokens[index]!r}")
        index += 1
        if index == len(tokens) or tokens[index] != "(":
            raise Refusal(f"{path}: {name} must be an array assignment")
        index += 1
        values = []
        while index < len(tokens) and tokens[index] != ")":
            value = tokens[index].replace("${REAL_USER}", user)
            values.append(safe_relative(value))
            index += 1
        if index == len(tokens):
            raise Refusal(f"{path}: unterminated {name}")
        arrays[name] = values
        index += 1
    if set(arrays) != allowed:
        raise Refusal(f"{path}: both core and optional arrays are required (they may be empty)")
    return arrays[f"CORE_{family}VOLUMES"] + arrays[f"OPTIONAL_{family}VOLUMES"]


def validate_config(roots, homes):
    protected_trees = ("/boot", "/dev", "/proc", "/sys", "/run", "/etc",
                       "/usr", "/bin", "/sbin", "/lib", "/lib64", "/lost+found",
                       "/var/lib/add_subvolumes")
    protected_exact = {"/", "/var", "/var/lib", "/mnt", "/media"}
    seen = []
    for path, family in [(p, "root") for p in roots] + [(p, "home") for p in homes]:
        if family == "root" and (path == "/home" or beneath(path, "/home")):
            raise Refusal(f"{path}: /home and descendants belong only in HOMEVOLUMES.conf")
        if family == "home" and not beneath(path, "/home"):
            raise Refusal(f"{path}: home entries must be beneath /home")
        if path in protected_exact or any(path == p or beneath(path, p) for p in protected_trees):
            raise Refusal(f"{path}: protected system path; conversion is not supported")
        if ".snapshots" in PurePosixPath(path).parts:
            raise Refusal(f"{path}: managed snapshot storage is protected")
        for other in seen:
            if path == other or beneath(path, other) or beneath(other, path):
                raise Refusal(f"Duplicate or overlapping configuration entries: {other}, {path}")
        seen.append(path)

def default_policies(user):
    return {
        "/var/log": "ask",
        f"/home/{user}/.cache": "ask",
        "/var/crash": "manage",
        "/var/spool": "manage",
        f"/home/{user}/.ssh": "runtime",
    }

def load_policies(path, user):
    # JSON is data only. Relative paths use the same spelling and user
    # expansion as the volume configs. Omitted files retain shipped defaults;
    # explicit empty arrays select strict behavior everywhere.
    if not path.exists():
        return default_policies(user)
    def unique_object(pairs):
        data = {}
        for key, value in pairs:
            if key in data:
                raise Refusal(f"{path}: duplicate policy key {key}")
            data[key] = value
        return data
    try:
        data = json.loads(path.read_text(), object_pairs_hook=unique_object)
    except ValueError as exc:
        raise Refusal(f"{path}: invalid policy JSON: {exc}") from exc
    if (not isinstance(data, dict)
            or not {"accept_risk", "manage_blockers"} <= set(data)
            or set(data) - {"accept_risk", "manage_blockers", "ignore_runtime"}):
        raise Refusal(f"{path}: requires accept_risk and manage_blockers arrays")
    policies = {}
    for name, mode in (("accept_risk", "ask"), ("manage_blockers", "manage"), ("ignore_runtime", "runtime")):
        if name not in data:
            continue
        if not isinstance(data[name], list):
            raise Refusal(f"{path}: {name} must be an array")
        for value in data[name]:
            if not isinstance(value, str):
                raise Refusal(f"{path}: policy entries must be path strings")
            target = safe_relative(value.replace("${REAL_USER}", user))
            validate_config([] if beneath(target, "/home") else [target],
                            [target] if beneath(target, "/home") else [])
            if target in policies:
                raise Refusal(f"{path}: duplicate or conflicting policy for {target}")
            policies[target] = mode
    return policies


def snapshot_base(fsroot):
    match = re.fullmatch(r"(.*)/\.snapshots/[0-9]+/snapshot", fsroot)
    if match:
        return match.group(1) or "/"
    if ".snapshots" in PurePosixPath(fsroot).parts:
        raise Refusal(f"Cannot determine a stable migration root for {fsroot}")
    return fsroot


ATIME_OPTIONS = {"atime", "relatime", "strictatime", "noatime", "norelatime"}


def preferred_options(options):
    """Add preferred defaults without discarding unrelated installation flags."""
    flags = list(dict.fromkeys(f for f in options.split(",") if f))
    # Explicit choices win; preferences only fill missing option families.
    flags = [f for f in flags if f != "defaults"]
    keys = {f.split("=", 1)[0] for f in flags}
    for _, alternatives, value in (
        ("noatime", ATIME_OPTIONS, "noatime"),
        ("autodefrag", {"autodefrag", "noautodefrag"}, "autodefrag"),
        ("discard", {"discard", "nodiscard"}, "discard=async"),
        ("compress", {"compress", "compress-force", "nocompress", "nodatacow", "nodatasum"}, "compress=zstd:1"),
    ):
        if not keys.intersection(alternatives):
            flags.append(value)
    return ",".join(["defaults", *flags])


def options_for(options, configured=None):
    flags = options.split(",")
    if "ro" in flags:
        raise Refusal("The source filesystem is mounted read-only")
    if configured is not None:
        explicit = configured.split(",")
        # findmnt includes implicit relatime. Use the covering mount's fstab
        # entry to distinguish that default from an explicit atime choice.
        flags = [f for f in flags if f not in ATIME_OPTIONS]
        flags.extend(f for f in explicit if f in ATIME_OPTIONS)
        compression = {"compress", "compress-force", "nocompress", "nodatacow", "nodatasum"}
        selected = [f for f in explicit if f.split("=", 1)[0] in compression]
        if selected:
            flags = [f for f in flags if f.split("=", 1)[0] not in compression]
            flags.extend(selected)
    # Preserve inherited access/security and Btrfs options. The destination
    # selector is always generated from discovery, never inherited.
    return preferred_options(",".join(f for f in flags if f and f not in {"rw", "ro", "bind", "rbind", "seclabel"}
                    and not f.startswith(("subvol=", "subvolid="))))


def join_subvol(base, relative):
    return base.rstrip("/") + "/" + relative.lstrip("/")


@dataclasses.dataclass
class Mount:
    target: str
    source: str
    fstype: str
    fsroot: str
    uuid: str
    options: str


@dataclasses.dataclass
class Item:
    path: str
    action: str
    reason: str = ""
    uuid: str = ""
    subvol: str = ""
    options: str = ""
    backup: str = ""
    existed: bool = False
    source_identity: str = ""
    uid: int = 0
    gid: int = 0
    mode: int = 0o755
    size: int = 0
    status: str = "planned"
    accept_active_risk: bool = False
    activity: str = ""


class Migration:
    STATE = "/var/lib/add_subvolumes"
    LOCK = "/run/lock/add_subvolumes.lock"

    def __init__(self, execute, script_dir, auto_accept=False):
        self.execute = execute
        self.auto_accept = auto_accept
        self.script_dir = Path(script_dir)
        self.items = []
        self.tops = {}
        self.temp_dirs = []
        self.staged_fstab = ""
        self.state_dir = ""
        self.pending = False
        self.lock_fd = None
        self.run_id = uuid_module.uuid4().hex
        self.fstab = ""
        self.fstab_entries = {}
        self.option_updates = {}
        self.committed = False
        self.fstab_commit_state = "not-started"
        self.retained_backups = []
        self.mounts = []
        self.service_changes = []
        self.policies = default_policies(pwd.getpwuid(os.getuid()).pw_name)

    def exists(self, path):
        return self.test("-e", path) or self.test("-L", path)

    def test(self, flag, path):
        result = root(["test", flag, path], check=False)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise Refusal(f"Unable to inspect {path}: {result.stderr.strip()}")
        return result.returncode == 0

    def check_ancestors(self, path):
        current = Path("/")
        for part in PurePosixPath(path).parts[1:]:
            current /= part
            if self.test("-L", current):
                raise Refusal(f"{current}: symbolic links are not migration paths")
            if self.exists(current) and not self.test("-d", current):
                raise Refusal(f"{current}: expected a directory")

    def nearest(self, path):
        current = Path(path)
        while not self.exists(current):
            current = current.parent
        return str(current)

    def refresh_mounts(self):
        result = command(["findmnt", "--json", "--list", "--output",
                          "TARGET,SOURCE,FSTYPE,FSROOT,UUID,OPTIONS"])
        self.mounts = []
        def collect(rows):
            for row in rows:
                self.mounts.append(Mount(**{k: row.get(k) or "" for k in
                                           ("target", "source", "fstype", "fsroot", "uuid", "options")}))
                collect(row.get("children", []))
        collect(json.loads(result.stdout)["filesystems"])

    def covering(self, path):
        candidates = [m for m in self.mounts if path == m.target or beneath(path, m.target)]
        if not candidates:
            raise Refusal(f"Cannot discover the filesystem for {path}")
        longest = max(len(m.target) for m in candidates)
        candidates = [m for m in candidates if len(m.target) == longest]
        if len(candidates) != 1:
            raise Refusal(f"Ambiguous stacked mounts covering {path}")
        return candidates[0]

    def validate_mount(self, mount):
        if mount.fstype != "btrfs":
            raise Refusal(f"{mount.target}: expected Btrfs, found {mount.fstype}")
        if not mount.uuid:
            source = mount.source.split("[", 1)[0]
            mount.uuid = root(["blkid", "-s", "UUID", "-o", "value", source]).stdout.strip()
        if not re.fullmatch(r"[a-fA-F0-9-]+", mount.uuid):
            raise Refusal(f"{mount.target}: unable to establish filesystem UUID")
        if mount.fsroot != "/":
            safe_relative(mount.fsroot.removeprefix("/"))
        if not mount.fsroot.startswith("/"):
            raise Refusal(f"{mount.target}: missing filesystem-root path")

    def top(self, uuid):
        if uuid not in self.tops:
            temp = root(["mktemp", "-d", "/run/add-subvolumes.XXXXXXXX"]).stdout.strip()
            self.temp_dirs.append(temp)
            root(["mount", "-t", "btrfs", "-o", "ro,subvolid=5", "UUID=" + uuid, temp])
            self.tops[uuid] = temp
            info = json.loads(command(["findmnt", "--json", "--mountpoint", temp,
                                       "--output", "UUID,FSROOT,OPTIONS"]).stdout)["filesystems"][0]
            if info.get("uuid") != uuid or info.get("fsroot") != "/" or "ro" not in info["options"].split(","):
                raise Refusal("Temporary top-level mount verification failed")
        return self.tops[uuid]

    def is_subvolume(self, path):
        if not self.exists(path):
            return False
        # Btrfs subvolume roots have inode 256; once identified, show must also
        # succeed so an inspection failure never becomes permission to replace.
        inode = root(["stat", "-c", "%i", "--", path]).stdout.strip()
        if inode != "256":
            return False
        root(["btrfs", "subvolume", "show", path])
        return True

    def read_fstab(self):
        self.check_ancestors("/etc")
        if self.test("-L", "/etc/fstab"):
            raise Refusal("/etc/fstab is a symlink; atomic replacement requires a regular file")
        root(["test", "-f", "/etc/fstab"])
        self.fstab = root(["cat", "/etc/fstab"]).stdout
        self.fstab_entries = {}
        for number, line in enumerate(self.fstab.splitlines(), 1):
            fields = line.split()
            if not fields or fields[0].startswith("#"):
                continue
            if len(fields) < 4:
                raise Refusal(f"Malformed /etc/fstab line {number}")
            target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1])
            if target.startswith("/"):
                target = "/" + os.path.normpath(target).lstrip("/")
            self.fstab_entries.setdefault(target, []).append(fields)
        for path, entries in self.fstab_entries.items():
            if path.startswith("/") and len(entries) > 1:
                raise Refusal(f"Conflicting duplicate fstab entries for {path}")
        root(["findmnt", "--verify", "--tab-file", "/etc/fstab"])

    def plan_option_updates(self, roots, homes):
        self.option_updates = {}
        targets = {item.path for item in self.items if item.action == "preserve"}
        if roots:
            targets.add("/")
        for path in sorted(targets):
            entries = self.fstab_entries.get(path, [])
            if not entries:
                continue  # Never invent a persistent mount for an existing layout.
            if len(entries) != 1 or entries[0][2] != "btrfs":
                raise Refusal(f"{path}: cannot safely update its fstab options")
            before = entries[0][3]
            if {"bind", "rbind"}.intersection(before.split(",")):
                continue
            after = preferred_options(before)
            if after != before:
                self.option_updates[path] = {"before": before, "after": after}
        if self.option_updates:
            print(f"Fstab options: {len(self.option_updates)} mounts to update after reboot.")
            groups = {}
            for path, update in self.option_updates.items():
                before = update["before"].split(",")
                additions = tuple(flag for flag in update["after"].split(",") if flag not in before)
                groups.setdefault(additions, []).append(path)
            for additions, paths in groups.items():
                label = ", ".join(paths) if len(paths) <= 3 else f"{len(paths)} configured mounts"
                detail = "add " + ", ".join(additions) if additions else "normalize option formatting"
                print(f"  {label}: {detail}")
            print("Existing option choices will be preserved.")
            if self.changes():
                print()

    def updated_fstab(self):
        lines = []
        for line in self.fstab.splitlines(keepends=True):
            fields = line.split()
            if fields and not fields[0].startswith("#") and len(fields) >= 4:
                target = re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[1])
                if target.startswith("/"):
                    target = "/" + os.path.normpath(target).lstrip("/")
                if target in self.option_updates:
                    update = self.option_updates[target]
                    if fields[3] != update["before"]:
                        raise Refusal(f"{target}: fstab options changed after planning")
                    line = re.sub(r"^(\s*\S+\s+\S+\s+\S+\s+)\S+",
                                  lambda m: m[1] + update["after"], line, count=1)
            lines.append(line)
        return "".join(lines)

    def no_open_users(self, path, ignore_runtime=False):
        if not self.exists(path):
            return
        args = ["lsof", "-nP", "-F", "pft" if ignore_runtime else "p"]
        # Desktop FUSE mounts (e.g. the document portal) can deny even root
        # stat access. lsof scans mount metadata outside +D and warns globally.
        # Exempt only unrelated FUSE mounts from those stat calls; keep all
        # diagnostics and PID checks for the directory being migrated.
        # lsof's exemptions use textual prefixes, so use conservative prefix
        # overlap checks here rather than directory-component comparisons.
        excluded = set()
        for mount in self.mounts:
            target = mount.target
            if not (mount.fstype == "fuse" or mount.fstype.startswith("fuse.")):
                continue
            if not target.startswith("/") or target == "/" or os.path.normpath(target) != target:
                continue
            if path.startswith(target) or target.startswith(path) or target in excluded:
                continue
            args += ["-e", target]
            excluded.add(target)
        result = root([*args, "+D", path], check=False)
        if result.returncode not in (0, 1) or result.stderr.strip():
            raise Refusal(f"Unable to establish open-file safety for {path}: {result.stderr.strip()}")
        pids = {int(line[1:]) for line in result.stdout.splitlines() if re.fullmatch(r"p[0-9]+", line)}
        if ignore_runtime:
            # Evaluate each descriptor, not the process name: a process may
            # hold both a socket and a regular file in this directory.
            pids = set()
            pid, descriptor, kind = None, False, None
            def finish():
                if descriptor and kind != "unix" and pid is not None:
                    pids.add(pid)
            for line in result.stdout.splitlines():
                if line.startswith("p") and line[1:].isdigit():
                    finish()
                    pid, descriptor, kind = int(line[1:]), False, None
                elif line.startswith("f"):
                    finish()
                    descriptor, kind = True, None
                elif line.startswith("t"):
                    kind = line[1:]
            finish()
        pids.discard(os.getpid())
        if pids:
            raise Busy(path, pids)

    def activity_users(self, path, policy_path=None):
        if self.policy_for(policy_path or path) == "runtime":
            self.no_open_users(path, ignore_runtime=True)
        else:
            self.no_open_users(path)

    def runtime_copy_options(self, item):
        options = []

        if item.path == "/root":
            options.append("--exclude=/.xauth*")

        if self.policy_for(item.path) != "runtime":
            return options
        # rsync groups sockets and FIFOs as specials. Never silently omit a
        # FIFO under this socket-only policy; check source and destination.
        for path in (item.backup, item.path):
            if path and self.exists(path):
                if root(["find", path, "-xdev", "-type", "p", "-print", "-quit"]).stdout:
                    raise Refusal(f"{path}: FIFO requires separate handling; runtime policy ignores sockets only")
        options += ["--no-specials", "--info=NONREG0"]
        return options

    def policy_for(self, path):
        return self.policies.get(path, "strict")

    def known_activators(self, path):
        if path == "/var/crash":
            return ("whoopsie.path", "apport-autoreport.path")
        return ()

    def known_user_activators(self, path):
        flatpak_paths = (
            "/var/lib/flatpak",
            f"/home/{pwd.getpwuid(os.getuid()).pw_name}/.local/share/flatpak",
        )
        if path in flatpak_paths:
            return ("app-org.kde.discover.notifier@autostart.service",)
        return ()

    def allow_active_data(self, item):
        return self.policy_for(item.path) == "ask" and item.accept_active_risk

    def check_item_users(self, item, path):
        if not self.allow_active_data(item):
            try:
                self.activity_users(path, item.path)
            except Busy as exc:
                if item.status != "planned":
                    raise Refusal(f"{path}: became busy after migration started "
                                  f"(PIDs {', '.join(map(str, sorted(exc.pids)))}). "
                                  "Recovery is required; do not reboot or restart affected "
                                  "services until the migration is recovered.") from exc
                raise

    def accept_data_risk(self, path):
        if path == "/var/log":
            detail = "Log history, including security/audit records, may be lost or inconsistent."
        elif path.startswith("/home/") and path.endswith("/.cache"):
            detail = ("Active-cache conversion is experimental. Cache updates may be lost or "
                      "inconsistent, and applications may misbehave until restarted.")
        else:
            detail = "Active data may be lost or inconsistent, and applications may malfunction."
        print(f"\n{path} is busy. {detail} Content equality cannot be verified. "
              "Original files will be deleted after migration; applications may continue "
              "using those deleted files until restarted. Reboot after successful completion.", flush=True)
        if self.auto_accept and self.execute and self.policy_for(path) == "ask":
            print(f"Accepted active-data risk for {path} (--accept).")
            return True
        try:
            # stdin contains the embedded engine; consent requires a terminal.
            with open("/dev/tty", "r", encoding="utf-8") as reader, \
                    open("/dev/tty", "w", encoding="utf-8") as writer:
                writer.write(f"Type ACCEPT to convert, or press Enter to skip {path}: ")
                writer.flush()
                return reader.readline().strip() == "ACCEPT"
        except OSError:
            print(f"No interactive terminal; skipping busy {path}.")
            return False

    def unit_info(self, unit):
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.(service|socket|timer|path)", unit):
            raise Refusal(f"Unsupported service unit name: {unit!r}")
        properties = ("Id", "LoadState", "ActiveState", "UnitFileState", "ControlGroup",
                      "CanStop", "CanStart", "RefuseManualStop", "RefuseManualStart", "TriggeredBy", "Triggers",
                      "ConsistsOf", "BoundBy", "PropagatesStopTo", "RequiredBy", "RequisiteOf",
                      "Names", "OnFailure", "OnSuccess", "FailureAction", "SuccessAction", "JobTimeoutAction")
        reply = root(["systemctl", "show", "--no-pager",
                      "--property=" + ",".join(properties), "--", unit])
        info = dict(line.split("=", 1) for line in reply.stdout.splitlines() if "=" in line)
        if info.get("Id") != unit or info.get("LoadState") not in ("loaded", "masked"):
            raise Refusal(f"Cannot safely inspect unit {unit}")
        return info

    def user_unit_info(self, unit):
        if not re.fullmatch(r"[A-Za-z0-9_.@:-]+\.(service|socket|timer|path)", unit):
            raise Refusal(f"Unsupported user service unit name: {unit!r}")

        properties = ("Id", "LoadState", "ActiveState", "UnitFileState")
        reply = command([
            "systemctl", "--user", "show", "--no-pager",
            "--property=" + ",".join(properties), "--", unit
        ], check=False)

        info = dict(
            line.split("=", 1)
            for line in reply.stdout.splitlines()
            if "=" in line
        )

        if info.get("LoadState") == "not-found":
            return None

        if reply.returncode or info.get("Id") != unit or info.get("LoadState") != "loaded":
            raise Refusal(f"Cannot safely inspect user unit {unit}")

        return info

    def check_manageable_unit(self, unit, info):
        names = {unit, *info.get("Names", "").split()}
        protected = {"dbus", "dbus-broker", "dbus-daemon", "polkit", "polkitd",
                     "ssh", "sshd", "sudo", "NetworkManager", "networking",
                     "network", "connman", "wicked", "wpa_supplicant", "iwd",
                     "display-manager", "sddm", "gdm", "gdm3", "lightdm", "xdm",
                     "auditd", "udev", "udisks2", "multipathd", "lvm2-monitor"}
        for name in names:
            stem = name.rsplit(".", 1)[0].split("@", 1)[0]
            if stem in protected or stem.startswith(("systemd-", "user", "getty", "serial-getty",
                                                     "container-getty", "dbus-")):
                raise Refusal(f"{unit}: essential system or session service; left untouched")
        # Stop/start side effects must not launch unrelated handlers or trigger
        # a reboot, shutdown, or other manager-level action.
        if info.get("OnFailure", "").strip() or info.get("OnSuccess", "").strip():
            raise Refusal(f"{unit}: lifecycle handlers could affect unrelated units")
        for key in ("FailureAction", "SuccessAction", "JobTimeoutAction"):
            if info.get(key, "none") not in ("", "none"):
                raise Refusal(f"{unit}: {key} prevents automatic management")

    def blocker_units(self, pids, extra_units=()):
        if not shutil.which("systemctl") or not self.exists("/run/systemd/system"):
            raise Refusal("automatic blocker management requires systemd")
        # Discover ownership rather than requiring a known daemon name.
        # Essential infrastructure and session services remain excluded.
        units = {}
        for unit in extra_units:
            reply = root(
                ["systemctl", "show", "--no-pager", "--property=LoadState", "--", unit],
                check=False,
            )
            state = dict(
                line.split("=", 1)
                for line in reply.stdout.splitlines()
                if "=" in line
            ).get("LoadState", "")

            if state == "not-found":
                continue

            info = self.unit_info(unit)
            self.check_manageable_unit(unit, info)
            units[unit] = info
        for pid in sorted(pids):
            groups = root(["cat", f"/proc/{pid}/cgroup"]).stdout.splitlines()
            paths = []
            for line in groups:
                fields = line.split(":", 2)
                if len(fields) == 3 and (fields[0] == "0" or "name=systemd" in fields[1].split(",")):
                    paths.append(fields[2])
            candidates = {part for path in paths for part in path.split("/") if part.endswith(".service")}
            if len(candidates) != 1:
                raise Refusal(f"PID {pid}: no unambiguous system service; left untouched")
            unit = next(iter(candidates))
            info = self.unit_info(unit)
            cgroup = info.get("ControlGroup", "")
            if not cgroup.startswith("/system.slice/") or not any(
                    path == cgroup or beneath(path, cgroup) for path in paths):
                raise Refusal(f"PID {pid}: service ownership could not be verified")
            self.check_manageable_unit(unit, info)
            units[unit] = info
        # Close the group over stop-propagation dependencies and activators.
        # Every discovered unit receives the same eligibility checks before
        # any service state changes. Cycles are visited only once.
        stop_links = ("ConsistsOf", "BoundBy", "PropagatesStopTo", "RequiredBy", "RequisiteOf")
        queue = list(units)
        cursor = 0
        while cursor < len(queue):
            unit = queue[cursor]
            cursor += 1
            info = units[unit]
            self.check_manageable_unit(unit, info)
            relatives = set()
            for key in stop_links:
                relatives.update(info.get(key, "").split())
            triggers = set(info.get("TriggeredBy", "").split())
            for relative in sorted(relatives | triggers):
                if not relative.endswith((".service", ".socket", ".timer", ".path")):
                    raise Refusal(f"{unit}: dependent {relative} cannot be managed automatically")
                if relative in triggers and not relative.endswith((".socket", ".timer", ".path")):
                    raise Refusal(f"{unit}: unsupported activator {relative}")
                if relative not in units:
                    if len(units) >= 64:
                        raise Refusal("Service dependency group exceeds 64 units; left untouched")
                    try:
                        relative_info = self.unit_info(relative)
                        self.check_manageable_unit(relative, relative_info)
                    except Refusal as exc:
                        raise Refusal(f"{unit}: dependent {relative}: {exc}") from exc
                    units[relative] = relative_info
                    queue.append(relative)
                if relative in triggers and set(units[relative].get("Triggers", "").split()) != {unit}:
                    raise Refusal(f"{relative}: shared or unverified activator")
        for unit, info in units.items():
            self.check_manageable_unit(unit, info)
            if info.get("LoadState") != "loaded":
                raise Refusal(f"{unit}: not a loaded service")
            if info.get("ActiveState") not in ("active", "inactive"):
                raise Refusal(f"{unit}: service is changing state")
            if any(info.get(key) != value for key, value in (
                    ("CanStop", "yes"), ("CanStart", "yes"),
                    ("RefuseManualStop", "no"), ("RefuseManualStart", "no"))):
                raise Refusal(f"{unit}: cannot safely request a stop")
            if info.get("UnitFileState", "").startswith("masked") or self.exists("/run/systemd/system/" + unit):
                raise Refusal(f"{unit}: existing mask or runtime unit must be left untouched")
            for key in stop_links:
                outside = set(info.get(key, "").split()) - units.keys()
                if outside:
                    raise Refusal(f"{unit}: unmanaged dependents: {', '.join(sorted(outside))}")
        return units

    def wait_units(self, units, active):
        deadline = time.monotonic() + 15
        while True:
            states = {unit: self.unit_info(unit).get("ActiveState", "unknown") for unit in units}
            if all(state == ("active" if active else "inactive") for state in states.values()):
                return
            if time.monotonic() >= deadline or "failed" in states.values():
                raise Refusal("Service state did not settle: " + ", ".join(
                    f"{unit} ({state})" for unit, state in states.items()
                    if state != ("active" if active else "inactive")))
            time.sleep(0.2)

    def wait_user_units(self, units, active):
        deadline = time.monotonic() + 15
        while True:
            states = {}
            for unit in units:
                info = self.user_unit_info(unit)
                states[unit] = info.get("ActiveState", "unknown") if info else "inactive"

            expected = "active" if active else "inactive"
            if all(state == expected for state in states.values()):
                return

            if time.monotonic() >= deadline or "failed" in states.values():
                raise Refusal("User service state did not settle: " + ", ".join(
                    f"{unit} ({state})"
                    for unit, state in states.items()
                    if state != expected
                ))

            time.sleep(0.2)

    def restore_services(self, only_path=None):
        entries = [
            entry for entry in self.service_changes
            if not entry["restored"]
            and (only_path is None or entry["path"] == only_path)
        ]
        if not entries:
            return

        # A service may use several configured directories. Keep every held
        # service stopped if any managed migration is only partially complete.
        affected = {entry["path"] for entry in self.service_changes}
        affected.update(
            item.path
            for item in self.items
            if self.policy_for(item.path) == "manage"
        )

        if any(
            item.path in affected
            and item.status not in ("planned", "complete", "complete-backup-retained")
            for item in self.items
        ):
            if only_path is not None:
                return

            raise Refusal(
                "Managed directory needs recovery; its services remain stopped/masked. "
                "Do not restart them or reboot until it is recovered."
            )

        system_entries = [
            entry for entry in entries
            if entry.get("scope", "system") == "system"
        ]
        user_entries = [
            entry for entry in entries
            if entry.get("scope", "system") == "user"
        ]

        # System units are runtime-masked while migration is active.
        system_units = list(dict.fromkeys(
            entry["unit"] for entry in system_entries
        ))

        for unit in system_units:
            root(["systemctl", "unmask", "--runtime", "--", unit])

        active_system = list(dict.fromkeys(
            entry["unit"]
            for entry in system_entries
            if entry["was_active"]
        ))

        if active_system:
            root([
                "systemctl", "start", "--no-block", "--",
                *active_system
            ])
            self.wait_units(active_system, active=True)

        # User units are stopped but deliberately not masked.
        active_user = list(dict.fromkeys(
            entry["unit"]
            for entry in user_entries
            if entry["was_active"]
        ))

        if active_user:
            command([
                "systemctl", "--user", "start", "--no-block", "--",
                *active_user
            ])
            self.wait_user_units(active_user, active=True)

        for entry in entries:
            entry["restored"] = True

        self.record()

        if system_units:
            print("Restored services: " + ", ".join(system_units))

        user_units = list(dict.fromkeys(
            entry["unit"] for entry in user_entries
        ))
        if user_units:
            print("Restored user services: " + ", ".join(user_units))

    def prepare_blockers(self, item, pids, extra_units=()):
        try:
            units = self.blocker_units(pids, extra_units)
        except Refusal as exc:
            item.action, item.reason = "skip", str(exc)
            return False

        if not units:
            try:
                self.activity_users(item.path)
                return True
            except Refusal as exc:
                item.action, item.reason = "skip", str(exc)
                return False

        try:
            # Record intent before *any* service mutation. Runtime masks block
            # activation during copying and disappear on reboot, not disk.
            for unit, info in units.items():
                self.service_changes.append({"unit": unit, "path": item.path, "was_active": info["ActiveState"] == "active",
                                             "restored": False})
            self.record()
            print("Temporarily stopping services/activators: " + ", ".join(units))
            # Stop triggers while their service definitions are still loaded.
            # Masking a live CUPS service first can make cups.path fail while
            # it tries to activate that service during daemon-reload.
            activators = [unit for unit in units if not unit.endswith(".service")]
            if activators:
                root(["systemctl", "stop", "--no-block", "--", *activators])
                self.wait_units(activators, active=False)
            # Load all masks together, after activators are inactive. Avoid
            # intermediate reloads with a partially masked dependency group.
            root(["systemctl", "mask", "--runtime", "--no-reload", "--", *units])
            root(["systemctl", "daemon-reload"])
            for unit in units:
                if self.unit_info(unit).get("UnitFileState") != "masked-runtime":
                    raise Refusal(f"{unit}: temporary mask did not take effect")
            root(["systemctl", "stop", "--no-block", "--", *units])
            self.wait_units(list(units), active=False)
            self.activity_users(item.path)
            return True
        except Refusal as exc:
            # This is still before the first directory mutation. Restore the
            # old service state; if restoration fails, stop with the ledger.
            item.action, item.reason = "skip", str(exc)
            self.restore_services(only_path=item.path)
            return False

    def prepare_user_activators(self, item, units):
        loaded = {}

        try:
            for unit in units:
                info = self.user_unit_info(unit)
                if info is not None:
                    loaded[unit] = info
        except Refusal as exc:
            item.action, item.reason = "skip", str(exc)
            return False

        # The known user activator may not exist on this desktop.
        # In that case, fall back to normal activity handling.
        if not loaded:
            return None

        already_managed = {
            entry["unit"]
            for entry in self.service_changes
            if entry.get("scope", "system") == "user"
            and not entry["restored"]
        }

        loaded = {
            unit: info
            for unit, info in loaded.items()
            if unit not in already_managed
        }

        if not loaded:
            # Another managed path already stopped this same user activator.
            # Keep it stopped and only verify that this path is now quiet.
            try:
                self.activity_users(item.path)
                return True
            except Refusal as exc:
                item.action, item.reason = "skip", str(exc)
                return False

        try:
            # Record intent before stopping anything so recovery knows what
            # changed if execution is interrupted.
            for unit, info in loaded.items():
                self.service_changes.append({
                    "scope": "user",
                    "unit": unit,
                    "path": item.path,
                    "was_active": info.get("ActiveState") == "active",
                    "restored": False,
                })

            self.record()

            print(
                "Temporarily stopping user services/activators: "
                + ", ".join(loaded)
            )

            command([
                "systemctl", "--user", "stop", "--no-block", "--",
                *loaded
            ])

            self.wait_user_units(list(loaded), active=False)

            # Stopping the known activator does not grant an exception to the
            # activity rule. The directory must actually be quiet afterward.
            self.activity_users(item.path)

            return True

        except Refusal as exc:
            item.action, item.reason = "skip", str(exc)
            self.restore_services(only_path=item.path)
            return False

    def prepare_managed_activity(self):
        # Shutdown can write to other selected directories (e.g. CUPS writes
        # /var/cache while releasing /var/spool). Do this before any copying.
        for item in list(self.changes()):
            if self.policy_for(item.path) == "manage":
                if not self.prepare_activity(item):
                    print(f"SKIP {item.path}: {item.reason}")
                self.record()

    def prepare_activity(self, item):
        self.runtime_copy_options(item)
        if self.allow_active_data(item):
            return True

        if self.policy_for(item.path) == "manage":
            already_prepared = any(
                entry["path"] == item.path and not entry["restored"]
                for entry in self.service_changes
            )

            if already_prepared:
                self.activity_users(item.path)
                return True

            user_activators = self.known_user_activators(item.path)
            if user_activators:
                prepared = self.prepare_user_activators(item, user_activators)
                if prepared is not None:
                    return prepared

            activators = self.known_activators(item.path)
            if activators:
                return self.prepare_blockers(item, set(), activators)

        try:
            self.activity_users(item.path)
            return True
        except Busy as exc:
            policy = self.policy_for(item.path)
            if policy == "ask":
                if self.accept_data_risk(item.path):
                    item.accept_active_risk = True
                    self.record()
                    return True
                item.action, item.reason = "skip", "active-data risk was not accepted"
                return False
            if policy == "manage":
                prepared = self.prepare_blockers(item, exc.pids)
                if not prepared:
                    item.reason += " Reboot and retry; review persistent blockers before changing policy."
                return prepared
            item.action, item.reason = "skip", str(exc)
            return False

    def report_skips(self):
        if any(item.action == "skip" for item in self.items):
            print()
        for item in self.items:
            if item.action == "skip":
                reason = ("no separate Btrfs subvolume mounted; home entries deferred"
                          if item.path == "/home" else item.reason)
                print(f"SKIPPED {item.path}: {reason}")

    def report_success(self):
        completed = self.changes()
        if completed:
            noun = "path" if len(completed) == 1 else "paths"
            if any(item.action == "skip" for item in self.items):
                print(f"\nCOMPLETED WITH SKIPPED PATHS: {len(completed)} {noun} migrated.")
            else:
                print(f"\nSUCCESS: Migration completed for {len(completed)} {noun}.")
        elif self.option_updates:
            label = "COMPLETED WITH SKIPPED PATHS" if any(i.action == "skip" for i in self.items) else "SUCCESS"
            print(f"\n{label}: Fstab options updated for {len(self.option_updates)} mounts. "
                  "Existing subvolumes and data were preserved.")
        else:
            print("\nRun complete. No paths were converted; see skipped entries below.")
        self.report_skips()
        print(f"\nFstab backup and run record: {self.state_dir}")
        if self.option_updates:
            print("REBOOT to apply the updated mount options. No live remount was performed.")
        if any(self.policy_for(item.path) == "runtime" for item in completed):
            print("REBOOT NOW to restore runtime sockets; affected authentication or applications may be unavailable until then.")
        if any(self.allow_active_data(item) for item in completed):
            accepted = ", ".join(item.path for item in completed if self.allow_active_data(item))
            print(f"\nConverted with accepted active-data risk: {accepted}")
            print("Recent data may be missing or inconsistent; applications may need restarting.")
            print("REBOOT NOW to make running applications reopen files in the new subvolumes.")
        if self.retained_backups:
            print("\nMigration completed; these old backups were retained for manual review:")
            for retained in self.retained_backups:
                print(f"  {retained['backup']}: {retained['reason']}")

    def no_nested_storage(self, path, allowed=()):
        for mount in self.mounts:
            if beneath(mount.target, path) and mount.target not in allowed:
                raise Refusal(f"{path}: contains mounted path {mount.target}; migrate separately")
        if self.exists(path):
            mount = self.covering(path)
            self.validate_mount(mount)
            relative = path[len(mount.target):].lstrip("/")
            physical = join_subvol(mount.fsroot, relative).rstrip("/") or "/"
            # Filter full filesystem paths ourselves. `list -o ordinary-dir`
            # can identify the containing subvolume rather than this directory.
            listing = root(["btrfs", "subvolume", "list", self.top(mount.uuid)]).stdout
            for line in listing.splitlines():
                _, separator, nested = line.partition(" path ")
                if not separator:
                    raise Refusal("Unable to parse Btrfs subvolume inventory safely")
                nested = "/" + nested.removeprefix("<FS_TREE>/").lstrip("/")
                if beneath(nested, physical):
                    raise Refusal(f"{path}: contains nested subvolume {nested}; automatic conversion is refused")

    def no_external_hardlinks(self, path):
        result = root(["find", path, "-xdev", "-type", "f", "-links", "+1",
                       "-printf", "%D:%i %n\n"])
        counts = {}
        for line in result.stdout.splitlines():
            inode, links = line.split()
            observed, _ = counts.get(inode, (0, int(links)))
            counts[inode] = (observed + 1, int(links))
        if any(observed != links for observed, links in counts.values()):
            raise Refusal(f"{path}: hard links cross the proposed subvolume boundary")

    def metadata(self, path):
        result = root(["stat", "-c", "%d:%i %u %g %a", "--", path]).stdout.split()
        return result[0], int(result[1]), int(result[2]), int(result[3], 8)

    def plan_item(self, path, *, destination=None):
        self.check_ancestors(path)
        if self.exists(path + "-old"):
            raise Refusal(f"{path}-old already exists; recover or review the previous migration first")
        source_mount = self.covering(self.nearest(path))
        self.validate_mount(source_mount)
        if source_mount.target == path:
            return Item(path, "preserve", "already an independent Btrfs mount")
        if self.is_subvolume(path):
            return Item(path, "preserve", "already a Btrfs subvolume")
        if path in self.fstab_entries:
            raise Refusal(f"{path}: has an fstab entry but is not the expected active subvolume; resolve it first")
        source_entries = self.fstab_entries.get(source_mount.target, [])
        configured = source_entries[0][3] if len(source_entries) == 1 else None
        options = options_for(source_mount.options, configured=configured)
        if destination is None:
            base = source_mount.fsroot
            if source_mount.target == "/":
                base = snapshot_base(base)
            relative = path[len(source_mount.target):].lstrip("/")
            subvol = join_subvol(base, relative)
            uuid = source_mount.uuid
        else:
            uuid, subvol, options = destination
        top = self.top(uuid)
        physical = top + subvol
        self.check_ancestors(physical)
        if self.exists(physical):
            # The ordinary source directory can occupy its own destination;
            # the execution rename will vacate it. Anything else is a conflict.
            same = self.exists(path) and self.metadata(path)[0] == self.metadata(physical)[0]
            if not same:
                if (destination is None and source_mount.target == "/"
                        and snapshot_base(source_mount.fsroot) != source_mount.fsroot):
                    return Item(path, "skip", f"destination {subvol} already exists separately in the original root; left unchanged")
                raise Refusal(f"{path}: destination {subvol} already exists separately; no overwrite or merge is allowed")
        base = snapshot_base(source_mount.fsroot) if source_mount.target == "/" else source_mount.fsroot
        if destination is None and source_mount.target == "/" and base != source_mount.fsroot and not self.is_subvolume(top + base):
            raise Refusal(f"{path}: stable root {base} is not a verified subvolume")
        self.no_nested_storage(path)
        activity = ("Runtime sockets will be omitted; reboot after migration"
                    if self.policy_for(path) == "runtime" else "")

        if self.policy_for(path) == "manage":
            user_activators = self.known_user_activators(path)

            if user_activators:
                try:
                    loaded_user = [
                        unit
                        for unit in user_activators
                        if self.user_unit_info(unit) is not None
                    ]

                    if loaded_user:
                        activity = (
                            "Execution will attempt to stop and later restore user service: "
                            + ", ".join(sorted(loaded_user))
                        )

                except Refusal as reason:
                    activity = (
                        "Execution will recheck, then skip if unresolved: "
                        + str(reason)
                    )

            if not activity:
                activators = self.known_activators(path)

                if activators:
                    try:
                        units = self.blocker_units(set(), activators)

                        if units:
                            activity = (
                                "Execution will attempt to stop/mask and later restore: "
                                + ", ".join(sorted(units))
                            )

                    except Refusal as reason:
                        activity = (
                            "Execution will recheck, then skip if unresolved: "
                            + str(reason)
                        )

        busy_reason = ""

        try:
            self.activity_users(path)

        except Busy as exc:
            if self.policy_for(path) == "ask":
                activity = (
                    "Busy: active-data risk will be accepted during execution (--accept)"
                    if self.auto_accept
                    else "Busy: execution will ask to accept active-data risk or skip"
                )

            elif self.policy_for(path) == "manage":
                user_activators = self.known_user_activators(path)

                try:
                    loaded_user = [
                        unit
                        for unit in user_activators
                        if self.user_unit_info(unit) is not None
                    ]

                    if loaded_user:
                        activity = (
                            "Execution will attempt to stop and later restore user service: "
                            + ", ".join(sorted(loaded_user))
                        )
                    else:
                        units = self.blocker_units(exc.pids)
                        activity = (
                            "Execution will attempt to stop/mask and later restore: "
                            + ", ".join(sorted(units))
                        )

                except Refusal as reason:
                    activity = (
                        "Execution will recheck, then skip if unresolved: "
                        + str(reason)
                    )

            else:
                busy_reason = str(exc)

        existed = self.exists(path)
        ancestor = path if existed else self.nearest(path)
        identity, uid, gid, mode = self.metadata(ancestor)
        if not existed:
            mode = 0o755
            if beneath(path, "/home"):
                mode = 0o700
                if ancestor in ("/", "/home"):
                    # Do not create an account's missing home tree owned by
                    # root merely because /home is its nearest ancestor.
                    account_home = "/" + "/".join(PurePosixPath(path).parts[1:3])
                    accounts = [entry for entry in pwd.getpwall() if entry.pw_dir == account_home]
                    if len(accounts) != 1:
                        raise Refusal(f"{path}: cannot determine ownership of the missing home tree")
                    uid, gid = accounts[0].pw_uid, accounts[0].pw_gid
            elif path in ("/var/tmp", "/var/crash"):
                mode = 0o1777
        size = 0
        if existed:
            self.no_external_hardlinks(path)
            # Apparent size is conservative for sparse/compressed files. Never
            # count on compression or reflinks when deciding copy capacity.
            size = int(root(["du", "-sx", "--apparent-size", "--block-size=1", path]).stdout.split()[0])
        backup = path + ".add-subvolumes-" + self.run_id + ".old"
        if self.exists(backup):
            raise Refusal(f"Backup destination already exists: {backup}")
        return Item(path, "skip" if busy_reason else ("convert" if existed else "create"),
                    reason=busy_reason, uuid=uuid, subvol=subvol,
                    options=options, backup=backup, existed=existed,
                    source_identity=identity if existed else "", uid=uid, gid=gid, mode=mode, size=size, activity=activity)

    def build_plan(self, roots, homes):
        errors = []
        for path in roots:
            try:
                self.items.append(self.plan_item(path))
            except Refusal as exc:
                errors.append(str(exc))
                self.items.append(Item(path, "reject", str(exc)))
        if homes:
            try:
                mount = self.covering("/home")
                root_mount = self.covering("/")
                if mount.target != "/home" or mount.fstype != "btrfs":
                    eligible = False
                else:
                    self.check_ancestors("/home")
                    self.validate_mount(mount)
                    eligible = self.is_subvolume("/home") and (
                        mount.uuid, mount.fsroot) != (root_mount.uuid, root_mount.fsroot)
                if not eligible:
                    print("Home processing: skipped")
                    self.items.append(Item("/home", "skip",
                        "Skipping HOMEVOLUMES.conf: /home is not mounted as a separate "
                        "Btrfs subvolume. Root processing will continue. Rerun after "
                        "establishing a separate /home to process these entries."))
                else:
                    options_for(mount.options)
                    print("Home processing: enabled")
                    self.items.append(Item("/home", "preserve", "existing separate home subvolume"))
                    for path in homes:
                        try:
                            self.items.append(self.plan_item(path))
                        except Refusal as exc:
                            errors.append(str(exc))
                            self.items.append(Item(path, "reject", str(exc)))
            except Refusal as exc:
                errors.append(str(exc))
                self.items.append(Item("/home", "reject", str(exc)))
        else:
            print("Home processing: disabled (no configured home entries)")
        print("\nMigration plan:")
        for item in self.items:
            detail = item.reason or f"UUID={item.uuid} subvol={item.subvol}"
            print(f"  {item.action.upper():8} {item.path}: {detail}")
            if item.activity:
                print("           " + item.activity)
        if errors or self.changes() or any(i.action == "preserve" for i in self.items):
            print(flush=True)
        if errors:
            raise Refusal("Preflight rejected one or more paths. No migrations have started.")
        self.plan_option_updates(roots, homes)
        destinations = set()
        for item in self.changes():
            key = (item.uuid, item.subvol)
            if key in destinations:
                raise Refusal(f"Multiple paths would use the same destination: {key}")
            destinations.add(key)
        required = {}
        for item in self.changes():
            required[item.uuid] = required.get(item.uuid, 0) + item.size
        for uuid, size in required.items():
            # Retain all backups until the complete run is verified; allow
            # 10% and 256 MiB metadata margin in addition to all copies.
            budget = size + size // 10 + 256 * 1024 * 1024
            free = int(root(["df", "--block-size=1", "--output=avail", self.top(uuid)]).stdout.splitlines()[-1])
            print(f"  SPACE UUID={uuid}: need {budget / 2**30:.2f} GiB; available {free / 2**30:.2f} GiB")
            if budget > free:
                raise Refusal(f"Insufficient copy space on UUID={uuid}")
        if self.changes():
            print("Preferred mount defaults will be added; unrelated options will be retained.")
            if any(self.policy_for(item.path) == "manage" for item in self.changes()):
                print("Any service changes for blocker management will be temporary.")
            print("Keep affected applications and services stopped throughout execution.")

    def changes(self):
        return [item for item in self.items if item.action in ("convert", "create")]

    def acquire_lock(self):
        root(["test", "-d", "/run/lock"])
        if root(["test", "-L", self.LOCK], check=False).returncode == 0:
            raise Refusal("Lock path is a symlink")
        root(["touch", "--", self.LOCK])
        root(["chmod", "0644", self.LOCK])
        self.lock_fd = os.open(self.LOCK, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refusal("Another add_subvolumes run is active") from exc

    def check_pending(self):
        if self.exists(self.STATE + "/pending.json"):
            record = root(["cat", self.STATE + "/pending.json"]).stdout
            raise Refusal("An interrupted migration requires recovery before another run.\n"
                          f"Recovery record: {self.STATE}/pending.json\n{record}")

    def write_root_file(self, path, content):
        root(["tee", "--", path], input=content)
        root(["chmod", "0600", "--", path])

    def record(self):
        data = {"run": self.run_id, "state_directory": self.state_dir,
                "fstab_committed": self.committed,
                "fstab_commit_state": self.fstab_commit_state,
                "items": [dataclasses.asdict(item) for item in self.items],
                "retained_backups": self.retained_backups,
                "service_changes": self.service_changes, "activity_policies": self.policies,
                "fstab_option_updates": self.option_updates}
        content = json.dumps(data, indent=2) + "\n"
        temp = root(["mktemp", self.STATE + "/.pending.XXXXXXXX"]).stdout.strip()
        self.write_root_file(temp, content)
        root(["sync", "-f", temp])
        root(["mv", "-T", "--", temp, self.STATE + "/pending.json"])
        root(["sync", "-f", self.STATE])
        self.write_root_file(self.state_dir + "/manifest.json", content)

    def start_transaction(self):
        self.check_ancestors(self.STATE)
        if self.exists(self.STATE):
            _, uid, _, mode = self.metadata(self.STATE)
            if uid != 0 or mode & 0o022:
                raise Refusal(f"{self.STATE} must be root-owned and not writable by other users")
        root(["install", "-d", "-m", "0700", self.STATE])
        self.state_dir = self.STATE + "/run-" + self.run_id
        root(["mkdir", "-m", "0700", self.state_dir])
        root(["cp", "-a", "--", "/etc/fstab", self.state_dir + "/fstab.before"])
        self.pending = True
        self.record()
        for uuid, top in self.tops.items():
            root(["umount", top])
            root(["mount", "-t", "btrfs", "-o", "rw,subvolid=5", "UUID=" + uuid, top])
        print(f"Recovery record: {self.STATE}/pending.json")

    def verify_mount(self, item):
        self.refresh_mounts()
        mount = self.covering(item.path)
        self.validate_mount(mount)
        if mount.target != item.path or mount.uuid != item.uuid or mount.fsroot != item.subvol:
            raise Refusal(f"{item.path}: mounted filesystem/subvolume does not match the plan")
        if "ro" in mount.options.split(","):
            raise Refusal(f"{item.path}: replacement mount is unexpectedly read-only")

    def ensure_parents(self, path, item):
        missing = []
        parent = Path(path).parent
        while not self.exists(parent):
            missing.append(parent)
            parent = parent.parent
        self.check_ancestors(str(parent))
        for directory in reversed(missing):
            root(["mkdir", "-m", "0700", "--", directory])
            root(["chown", f"{item.uid}:{item.gid}", "--", directory])
            root(["chmod", "0755", "--", directory])

    def compare_copy(self, item):
        # Active-data consent explicitly waives point-in-time content equality;
        # rsync I/O errors, mount checks and backup identity remain mandatory.
        if not item.existed or self.allow_active_data(item):
            return
        args = ["rsync", "-aHAX", "--numeric-ids", "--checksum", "--dry-run",
                "--itemize-changes", "--delete", "--omit-dir-times"]
        args += self.runtime_copy_options(item)
        args += [item.backup + "/", item.path + "/"]
        result = root(args)
        if result.stdout.strip():
            raise Refusal(f"{item.path}: copy verification found differences; backups retained:\n{result.stdout[:3000]}")

    def migrate(self, item):
        print(f"\n{item.action.capitalize()}: {item.path}", flush=True)
        self.refresh_mounts()
        self.check_ancestors(item.path)
        if self.covering(self.nearest(item.path)).target == item.path:
            raise Refusal(f"{item.path}: a mount appeared after planning")
        self.no_nested_storage(item.path)
        if not self.prepare_activity(item):
            print(f"SKIP {item.path}: {item.reason}")
            self.record()
            return
        try:
            self.check_item_users(item, item.path)
        except Busy as exc:
            item.action, item.reason = "skip", "activity returned before migration: " + str(exc)
            self.restore_services(only_path=item.path)
            self.record()
            print(f"SKIP {item.path}: {item.reason}")
            return
        if item.existed:
            if self.metadata(item.path)[0] != item.source_identity:
                raise Refusal(f"{item.path}: source identity changed after planning")
            self.no_external_hardlinks(item.path)
        elif self.exists(item.path):
            raise Refusal(f"{item.path}: appeared after planning")
        if self.exists(item.backup):
            raise Refusal(f"Backup collision: {item.backup}")
        item.status = "starting"
        self.record()  # Record both names before the first irreversible step.
        if item.existed:
            root(["mv", "-T", "--", item.path, item.backup])
            item.status = "source-renamed"
            self.record()
        physical = self.tops[item.uuid] + item.subvol
        self.check_ancestors(physical)
        if self.exists(physical):
            raise Refusal(f"Destination was not vacated: {item.subvol}")
        self.ensure_parents(physical, item)
        root(["btrfs", "subvolume", "create", physical])
        self.ensure_parents(item.path, item)
        # With an inline destination, creation through the top-level mount
        # already made this subvolume visible at its live path.
        if not self.exists(item.path):
            root(["mkdir", "-m", "0700", "--", item.path])
        root(["mount", "-t", "btrfs", "-o", f"subvol={item.subvol},{item.options}",
              "UUID=" + item.uuid, item.path])
        self.verify_mount(item)
        item.status = "mounted"
        self.record()
        if item.existed:
            args = ["rsync", "-aHAXx", "--numeric-ids", *self.runtime_copy_options(item), item.backup + "/", item.path + "/"]
            if self.allow_active_data(item):
                copied = root(args, check=False)
                if copied.returncode not in (0, 24):
                    raise Refusal(f"{item.path}: copy failed; backup retained: {copied.stderr.strip()}")
                if copied.returncode == 24:
                    print(f"{item.path}: files vanished during copying; covered by accepted active-data risk.")
            else:
                root(args)
            self.check_item_users(item, item.backup)
            self.check_item_users(item, item.path)
            self.compare_copy(item)
        else:
            root(["chown", f"{item.uid}:{item.gid}", "--", item.path])
            root(["chmod", f"{item.mode:o}", "--", item.path])
            if shutil.which("restorecon"):
                root(["restorecon", "-RF", item.path])
        item.status = "verified"
        self.record()

    def commit_fstab(self):
        if root(["cat", "/etc/fstab"]).stdout != self.fstab:
            raise Refusal("/etc/fstab changed during migration; refusing to overwrite it")
        content = self.updated_fstab().rstrip("\n") + "\n"
        for item in self.changes():
            options = preferred_options(f"subvol={item.subvol},{item.options}")
            content += f"UUID={item.uuid}\t{item.path}\tbtrfs\t{options}\t0 0\n"
        self.staged_fstab = root(["mktemp", "/etc/.fstab.add-subvolumes.XXXXXXXX"]).stdout.strip()
        root(["cp", "-a", "--", "/etc/fstab", self.staged_fstab])
        root(["tee", "--", self.staged_fstab], input=content)
        root(["findmnt", "--verify", "--tab-file", self.staged_fstab])
        root(["sync", "-f", self.staged_fstab])
        # Recheck immediately before the atomic replacement as well.
        if root(["cat", "/etc/fstab"]).stdout != self.fstab:
            raise Refusal("/etc/fstab changed while validating the staged update")
        self.fstab_commit_state = "committing"
        self.record()
        root(["mv", "-T", "--", self.staged_fstab, "/etc/fstab"])
        self.staged_fstab = ""
        self.committed = True
        self.fstab_commit_state = "committed"
        root(["sync", "-f", "/etc/fstab"])
        self.record()
        if self.exists("/run/systemd/system"):
            root(["systemctl", "daemon-reload"])

    def delete_verified_backups(self):
        # Before the fstab commit, retain the original all-or-recovery behavior.
        # This path deliberately checks both the backup and the prospective
        # authoritative path for activity.
        if not self.committed:
            for item in reversed(self.changes()):
                self.verify_mount(item)
                if item.existed:
                    if item.status != "verified":
                        raise Refusal(f"Refusing cleanup of unverified migration: {item.path}")
                    self.check_ancestors(item.backup)
                    if self.metadata(item.backup)[0] != item.source_identity:
                        raise Refusal(f"{item.backup}: recovery directory identity changed")
                    self.no_nested_storage(item.backup)
                    self.check_item_users(item, item.backup)
                    self.check_item_users(item, item.path)
                    self.compare_copy(item)
                    root(["sync", "-f", item.path])
                    item.status = "removing-verified-backup"
                    self.record()
                    root(["rm", "-rf", "--one-file-system", "--", item.backup])
                item.status = "complete"
                self.record()
            self.restore_services()
            root(["mv", "-T", "--", self.STATE + "/pending.json", self.state_dir + "/complete.json"])
            root(["sync", "-f", self.state_dir])
            self.pending = False
            return

        # Once fstab is committed, the replacement path is authoritative and
        # was already verified strictly. Cleanup is independent per old backup:
        # never let activity on the new path, or trouble with one old backup,
        # turn a committed migration into a recovery-required transaction.
        for item in reversed(self.changes()):
            if item.existed:
                try:
                    if item.status not in ("verified", "complete"):
                        raise Refusal(f"Refusing cleanup of unverified migration: {item.path}")
                    self.check_ancestors(item.backup)
                    if self.metadata(item.backup)[0] != item.source_identity:
                        raise Refusal(f"{item.backup}: recovery directory identity changed")
                    self.no_nested_storage(item.backup)
                    self.check_item_users(item, item.backup)
                    item.status = "removing-verified-backup"
                    self.record()
                    root(["rm", "-rf", "--one-file-system", "--", item.backup])
                except (Refusal, OSError, ValueError) as exc:
                    item.status = "complete-backup-retained"
                    item.reason = f"Post-commit backup retained: {exc}"
                    self.retained_backups.append({
                        "path": item.path,
                        "backup": item.backup,
                        "reason": str(exc),
                    })
                    print(
                        f"WARNING: Migration is committed; retained old backup {item.backup}. "
                        f"Cleanup was unsafe or failed: {exc}",
                        file=sys.stderr,
                    )
                    self.record()
                    continue
            item.status = "complete"
            self.record()
        self.restore_services()
        root(["mv", "-T", "--", self.STATE + "/pending.json", self.state_dir + "/complete.json"])
        root(["sync", "-f", self.state_dir])
        self.pending = False

    def run(self):
        if os.geteuid() == 0:
            raise Refusal("Run this script as your normal user, without sudo; it requests privileges as needed")
        user = pwd.getpwuid(os.getuid()).pw_name
        safe_relative(user)
        roots = load_config(self.script_dir / "ROOTVOLUMES.conf", "ROOT", user)
        homes = load_config(self.script_dir / "HOMEVOLUMES.conf", "HOME", user, optional=True)
        validate_config(roots, homes)
        self.policies = load_policies(self.script_dir / "ACTIVITY_POLICIES.conf", user)
        if not roots and not homes:
            print("No configured migrations. Nothing to do.")
            return
        os.environ["PATH"] = command_search_path()
        required = ("sudo", "findmnt", "btrfs", "blkid", "mount", "umount", "rsync", "lsof",
                    "stat", "find", "du", "df", "mktemp", "test", "cat", "tee", "touch", "chmod",
                    "chown", "install", "mkdir", "rmdir", "cp", "mv", "rm", "sync")
        missing = [name for name in required if not shutil.which(name)]
        if missing:
            raise MissingTools(missing_tools_message(missing))
        command(["sudo", "-v"])
        os.chdir("/")
        self.acquire_lock()
        self.check_pending()
        self.read_fstab()
        self.refresh_mounts()
        root_mount = self.covering("/")
        self.validate_mount(root_mount)
        options_for(root_mount.options)
        print(f"Active root: UUID={root_mount.uuid} subvol={root_mount.fsroot}")
        base = snapshot_base(root_mount.fsroot)
        print(f"Root migration base: {base}" + (" (snapshot operation)" if base != root_mount.fsroot else ""))
        self.build_plan(roots, homes)
        if not self.execute:
            self.report_skips()
            print("\nDry run complete. No data, fstab, or service settings were changed.")
            return
        if not self.changes() and not self.option_updates:
            self.report_skips()
            print("\nNo eligible paths need migration. Nothing to change.")
            return
        self.start_transaction()
        self.prepare_managed_activity()
        for item in self.changes():
            self.migrate(item)
        for item in self.changes():
            self.verify_mount(item)
            root(["sync", "-f", item.path])
        if self.changes() or self.option_updates:
            self.commit_fstab()
        self.delete_verified_backups()
        self.report_success()

    def cleanup(self):
        failed = False
        try:
            self.restore_services()
        except (Refusal, OSError, ValueError) as exc:
            print(f"Service recovery required: {exc}\nSee {self.STATE}/pending.json", file=sys.stderr)
            failed = True
        if self.staged_fstab:
            result = root(["rm", "-f", "--", self.staged_fstab], check=False)
            failed |= bool(result.returncode)
        for temp in reversed(self.temp_dirs):
            mounted = command(["findmnt", "--mountpoint", temp], check=False).returncode == 0
            if mounted:
                result = root(["umount", temp], check=False)
                if result.returncode:
                    print(f"Unable to unmount temporary mount {temp}: {result.stderr.strip()}", file=sys.stderr)
                    failed = True
                    continue
            result = root(["rmdir", "--", temp], check=False)
            failed |= bool(result.returncode)
        if self.lock_fd is not None:
            os.close(self.lock_fd)
        if self.pending:
            print(f"Migration incomplete. Backups and replacement mounts have been retained.\n"
                  f"Review {self.STATE}/pending.json and {self.state_dir}/fstab.before before recovery.\n"
                  "Do not delete the recovery record or backups merely to bypass this stop.", file=sys.stderr)
        return not failed


def main():
    migration = Migration(sys.argv[1] == "--execute", sys.argv[2], auto_accept=len(sys.argv) > 3 and sys.argv[3] == "1")
    def interrupted(signum, frame):
        raise Refusal(f"Interrupted by signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    status = 0
    try:
        migration.run()
    except (Refusal, OSError, ValueError, KeyboardInterrupt) as exc:
        prefix = "" if isinstance(exc, MissingTools) else "Error: "
        print(f"{prefix}{exc or 'Interrupted'}", file=sys.stderr)
        status = 1
    finally:
        if not migration.cleanup():
            status = 1
    return status


if __name__ == "__main__":
    sys.exit(main())
PYTHON_ENGINE
