#!/usr/bin/env python3
"""
make_bootable_usb.py
=====================

Interactive, terminal-based tool for writing a bootable ISO image to a USB
drive on Linux, using only native utilities (lsblk, dd, blkdev, findmnt,
udevadm, sync, etc.) - no third-party Python packages required.

Safety features:
  - Lists real disks only (lsblk --nodeps -d, type=disk), with size/model/
    vendor/transport/removable info, numbered for easy selection.
  - Automatically detects and EXCLUDES the drive that hosts the running
    system (root "/", "/boot", "/boot/efi", and any drive containing an
    active swap partition) from the selectable list.
  - Refuses to operate on a device that currently has any mounted
    partitions unless the user explicitly agrees to have them unmounted
    first (each partition is unmounted individually, with errors reported).
  - Warns and blocks writing to non-removable ("fixed") disks unless the
    user passes --allow-fixed, since a wrongly-guessed disk on a headless
    or multi-disk server is a common way to destroy the wrong drive.
  - Verifies the ISO path exists, is a regular file, and looks like a
    real ISO (extension + `file` magic check) before proceeding.
  - Requires the user to type the exact device name (e.g. "sdb") as a
    final confirmation, in addition to a yes/no prompt, immediately after
    a bright red warning that the target drive will be COMPLETELY erased.
  - Streams dd's progress (status=progress) live to the terminal AND
    appends timestamped lines to a log file so the operation can be
    audited or tailed from another terminal.
  - Verifies the tool is run as root (dd/blkdev access requires it) and
    checks required utilities are present before doing anything.

Usage:
    sudo python3 make_bootable_usb.py
    sudo python3 make_bootable_usb.py --allow-fixed   # allow non-removable disks to show up

This script intentionally avoids "cleverness" - every destructive action is
plain, logged, and requires explicit, unambiguous confirmation.
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import time

if os.geteuid() != 0:
    sys.exit("Error : This util must be run as root.")


# --------------------------------------------------------------------------- #
# Terminal colors (plain ANSI, no external deps)
# --------------------------------------------------------------------------- #
class C:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    BOLD = "\033[1m"
    RESET = "\033[0m"


def cprint(msg, color=None, bold=False):
    prefix = (C.BOLD if bold else "") + (color or "")
    suffix = C.RESET if (color or bold) else ""
    print(f"{prefix}{msg}{suffix}")


def die(msg, code=1):
    cprint(f"\nERROR: {msg}", C.RED, bold=True)
    sys.exit(code)


# --------------------------------------------------------------------------- #
# Pre-flight checks
# --------------------------------------------------------------------------- #
REQUIRED_TOOLS = [
    "lsblk",
    "dd",
    "findmnt",
    "blockdev",
    "umount",
    "sync",
    "file",
    "udevadm",
]


def check_requirements():
    missing = [t for t in REQUIRED_TOOLS if shutil.which(t) is None]
    if missing:
        die(
            "Missing required system utilities: "
            + ", ".join(missing)
            + "\nInstall them via your distro's package manager (they are normally "
            "part of util-linux / coreutils) and try again."
        )

    if os.geteuid() != 0:
        die(
            "This script must be run as root (it needs raw access to block "
            "devices).\nTry: sudo python3 make_bootable_usb.py"
        )


# --------------------------------------------------------------------------- #
# Discovering disks
# --------------------------------------------------------------------------- #
def run(cmd, **kwargs):
    """Run a command, returning CompletedProcess. Raises on failure unless check=False."""
    return subprocess.run(cmd, capture_output=True, text=True, **kwargs)


def get_system_disks():
    """
    Return the set of parent disk device names (e.g. {'sda'}) that back the
    running system: root filesystem, /boot, /boot/efi, and active swap.
    These are excluded from the selectable list no matter what.
    """
    system_disks = set()

    critical_mountpoints = ["/", "/boot", "/boot/efi"]
    for mp in critical_mountpoints:
        result = run(["findmnt", "-n", "-o", "SOURCE", mp])
        if result.returncode == 0 and result.stdout.strip():
            src = result.stdout.strip().splitlines()[0]
            parent = source_to_parent_disk(src)
            if parent:
                system_disks.add(parent)

    # Active swap partitions
    try:
        with open("/proc/swaps") as f:
            lines = f.readlines()[1:]
        for line in lines:
            dev = line.split()[0]
            parent = source_to_parent_disk(dev)
            if parent:
                system_disks.add(parent)
    except FileNotFoundError:
        pass

    return system_disks


def source_to_parent_disk(source):
    """
    Given something like /dev/sda1, /dev/nvme0n1p2, or a mapper/LVM path,
    resolve it to the underlying physical parent disk name (e.g. 'sda',
    'nvme0n1'). Returns None if it cannot be resolved (e.g. tmpfs, overlay).
    """
    if not source.startswith("/dev/"):
        return None  # tmpfs, overlay, etc. - not a physical disk

    dev_name = source[len("/dev/") :]

    result = run(["lsblk", "-no", "PKNAME", f"/dev/{dev_name}"])
    if result.returncode == 0 and result.stdout.strip():
        pk = result.stdout.strip().splitlines()[0]
        if pk:
            return pk

    # If PKNAME is empty, the device might already be a whole disk, or it's
    # an LVM/mapper device - walk lsblk's dependency tree to find the disk.
    result = run(["lsblk", "-no", "TYPE", f"/dev/{dev_name}"])
    if result.returncode == 0 and "disk" in result.stdout:
        return dev_name

    # Fall back to lsblk -s (inverse dependency listing) to find ancestor disks
    result = run(["lsblk", "-no", "NAME,TYPE", "-s", f"/dev/{dev_name}"])
    if result.returncode == 0:
        for line in result.stdout.strip().splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == "disk":
                return parts[0].lstrip("`-|- ").strip()
    return None


def list_candidate_disks(allow_fixed=False):
    """
    Return a list of dicts describing selectable disks, using lsblk JSON
    output. Excludes system disks always; excludes non-removable disks
    unless allow_fixed=True.
    """
    result = run(
        [
            "lsblk",
            "--json",
            "--nodeps",  # only whole disks, not partitions
            "-o",
            "NAME,SIZE,MODEL,VENDOR,TRAN,RM,RO,TYPE,ROTA,SERIAL",
        ]
    )
    if result.returncode != 0:
        die(f"Failed to run lsblk: {result.stderr.strip()}")

    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        die("Could not parse lsblk output.")

    system_disks = get_system_disks()

    disks = []
    for entry in data.get("blockdevices", []):
        if entry.get("type") != "disk":
            continue
        name = entry.get("name")
        if name in system_disks:
            continue
        if entry.get("ro") in (True, "1", 1):
            continue  # read-only media, can't dd to it
        removable = entry.get("rm") in (True, "1", 1)
        if not removable and not allow_fixed:
            continue
        disks.append(
            {
                "name": name,
                "path": f"/dev/{name}",
                "size": entry.get("size") or "?",
                "model": (entry.get("model") or "").strip() or "Unknown model",
                "vendor": (entry.get("vendor") or "").strip() or "",
                "transport": entry.get("tran") or "?",
                "removable": removable,
                "serial": entry.get("serial") or "",
            }
        )
    return disks, system_disks


def get_mounted_partitions(disk_name):
    """Return list of (device_path, mountpoint) currently mounted for a disk."""
    result = run(["lsblk", "--json", "-o", "NAME,MOUNTPOINT", f"/dev/{disk_name}"])
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []

    mounted = []

    def walk(nodes):
        for n in nodes:
            mp = n.get("mountpoint")
            if mp:
                mounted.append((f"/dev/{n.get('name')}", mp))
            children = n.get("children") or []
            if children:
                walk(children)

    walk(data.get("blockdevices", []))
    return mounted


# --------------------------------------------------------------------------- #
# User interaction
# --------------------------------------------------------------------------- #
def prompt_select_disk(disks):
    cprint("\nAvailable target drives:\n", C.BLUE, bold=True)
    header = f"{'#':<3} {'DEVICE':<10} {'SIZE':<8} {'TRANSPORT':<10} {'REMOVABLE':<10} {'MODEL / VENDOR'}"
    print(header)
    print("-" * len(header))
    for i, d in enumerate(disks, start=1):
        model_vendor = f"{d['vendor']} {d['model']}".strip()
        removable_str = "yes" if d["removable"] else "NO (fixed)"
        print(
            f"{i:<3} {d['path']:<10} {d['size']:<8} {d['transport']:<10} "
            f"{removable_str:<10} {model_vendor}"
        )
    print()

    while True:
        choice = input(
            f"Select target drive [1-{len(disks)}] (or 'q' to quit): "
        ).strip()
        if choice.lower() in ("q", "quit", "exit"):
            cprint("Aborted by user.", C.YELLOW)
            sys.exit(0)
        if not choice.isdigit():
            cprint("Please enter a valid number.", C.YELLOW)
            continue
        idx = int(choice)
        if 1 <= idx <= len(disks):
            return disks[idx - 1]
        cprint("Number out of range.", C.YELLOW)


def prompt_iso_path():
    while True:
        path = input("\nPath to the OS ISO file: ").strip().strip("'\"")
        path = os.path.expanduser(path)

        if not path:
            cprint("Please enter a path.", C.YELLOW)
            continue
        if not os.path.exists(path):
            cprint(f"File not found: {path}", C.RED)
            continue
        if not os.path.isfile(path):
            cprint(f"Not a regular file: {path}", C.RED)
            continue

        size = os.path.getsize(path)
        if size == 0:
            cprint("File is empty (0 bytes). Choose a valid ISO.", C.RED)
            continue

        # Sanity check it looks like an ISO (extension + `file` magic, best effort)
        looks_like_iso = path.lower().endswith((".iso", ".img"))
        file_result = run(["file", "-b", path])
        magic = file_result.stdout.lower() if file_result.returncode == 0 else ""
        magic_ok = any(
            kw in magic for kw in ("iso 9660", "boot sector", "dos/mbr", "udf")
        )

        if not looks_like_iso and not magic_ok:
            cprint(
                f"Warning: '{os.path.basename(path)}' does not look like a "
                f"typical ISO/IMG file (file type reported: '{file_result.stdout.strip() or 'unknown'}').",
                C.YELLOW,
            )
            confirm = input("Use it anyway? [y/N]: ").strip().lower()
            if confirm != "y":
                continue

        return path, size


def human_size(num_bytes):
    step = 1024.0
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num_bytes < step:
            return f"{num_bytes:.1f} {unit}"
        num_bytes /= step
    return f"{num_bytes:.1f} PB"


def handle_mounted_partitions(disk):
    mounted = get_mounted_partitions(disk["name"])
    if not mounted:
        return

    cprint(f"\n{disk['path']} currently has mounted partition(s):", C.YELLOW, bold=True)
    for dev, mp in mounted:
        print(f"   {dev}  ->  {mp}")

    answer = (
        input(
            "\nThese must be unmounted before writing the image. "
            "Unmount them now? [y/N]: "
        )
        .strip()
        .lower()
    )
    if answer != "y":
        die("Cannot proceed while target partitions are mounted. Aborting.")

    for dev, mp in mounted:
        result = run(["umount", dev])
        if result.returncode != 0:
            die(
                f"Failed to unmount {dev} ({mp}): {result.stderr.strip()}\n"
                "Close any programs/file managers using it and try again."
            )
        cprint(f"Unmounted {dev}", C.GREEN)

    # Re-check nothing is still mounted (e.g. auto-remount by a daemon)
    still_mounted = get_mounted_partitions(disk["name"])
    if still_mounted:
        die(
            "Some partitions are still mounted after unmount attempt "
            "(a daemon may be re-mounting them). Aborting for safety."
        )


def final_confirmation(disk, iso_path, iso_size):
    cprint("\n" + "=" * 64, C.RED, bold=True)
    cprint("  DANGER: DESTRUCTIVE OPERATION AHEAD", C.RED, bold=True)
    cprint("=" * 64, C.RED, bold=True)
    print(
        f"\nTarget drive : {C.BOLD}{disk['path']}{C.RESET} "
        f"({disk['size']}, {disk['vendor']} {disk['model']})"
    )
    print(f"ISO image    : {iso_path} ({human_size(iso_size)})")
    cprint(
        f"\nALL DATA on {disk['path']} (all partitions, all files) will be "
        f"PERMANENTLY ERASED and replaced with the contents of the ISO image.",
        C.RED,
    )
    cprint("This cannot be undone.\n", C.RED, bold=True)

    answer = input("Type 'yes' to continue, anything else to abort: ").strip().lower()
    if answer != "yes":
        cprint("Aborted by user. No changes were made.", C.YELLOW)
        sys.exit(0)

    typed = input(
        f"\nAs a final check, type the device name exactly ('{disk['name']}') to confirm: "
    ).strip()
    if typed != disk["name"]:
        cprint("Device name did not match. Aborting for safety.", C.RED, bold=True)
        sys.exit(1)


# --------------------------------------------------------------------------- #
# Writing the image
# --------------------------------------------------------------------------- #
def setup_logger():
    log_dir = os.path.expanduser("~/usb_writer_logs")
    os.makedirs(log_dir, exist_ok=True)
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(log_dir, f"usb_write_{timestamp}.log")
    return log_path


def log_line(log_path, text):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(log_path, "a") as f:
        f.write(f"[{ts}] {text}\n")


def write_image(disk, iso_path, iso_size, log_path):
    device = disk["path"]

    cprint(f"\nLogging to: {log_path}", C.BLUE)
    log_line(log_path, f"Starting write of '{iso_path}' ({iso_size} bytes) to {device}")

    # Make sure nothing auto-mounts the device mid-write (best effort; not fatal
    # if udevadm settle isn't available or errors out).
    run(["udevadm", "settle"])

    dd_cmd = [
        "dd",
        f"if={iso_path}",
        f"of={device}",
        "bs=4M",
        "conv=fsync,noerror",
        "oflag=direct",
        "status=progress",
    ]
    log_line(log_path, "Command: " + " ".join(dd_cmd))
    cprint(f"\nRunning: {' '.join(dd_cmd)}\n", C.BLUE, bold=True)

    start = time.time()
    try:
        process = subprocess.Popen(
            dd_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except FileNotFoundError:
        die("dd not found on PATH.")

    # dd writes progress lines using \r; stream them live and log periodically.
    last_logged = 0.0
    buf = ""
    try:
        while True:
            char = process.stdout.read(1)
            if char == "" and process.poll() is not None:
                break
            if char in ("\r", "\n"):
                if buf.strip():
                    sys.stdout.write("\r" + buf.strip() + " " * 10)
                    sys.stdout.flush()
                    now = time.time()
                    if now - last_logged > 5:  # throttle log writes to every 5s
                        log_line(log_path, buf.strip())
                        last_logged = now
                buf = ""
            else:
                buf += char
        if buf.strip():
            log_line(log_path, buf.strip())
    except KeyboardInterrupt:
        process.terminate()
        log_line(log_path, "ABORTED by user (KeyboardInterrupt).")
        die("\nWrite aborted by user. The drive may be left in an inconsistent state.")

    returncode = process.wait()
    elapsed = time.time() - start

    if returncode != 0:
        log_line(log_path, f"dd exited with error code {returncode}")
        die(
            f"\ndd reported an error (exit code {returncode}). "
            f"See log for details: {log_path}"
        )

    print()
    cprint(f"dd completed in {elapsed:.1f}s. Flushing write cache (sync)...", C.BLUE)
    log_line(log_path, f"dd completed successfully in {elapsed:.1f}s. Syncing.")
    run(["sync"])

    run(["udevadm", "settle"])
    log_line(log_path, "Sync complete. Operation finished successfully.")
    cprint(
        "\nDone. The bootable USB drive has been created successfully.",
        C.GREEN,
        bold=True,
    )
    cprint(f"Full log available at: {log_path}", C.GREEN)
    print(
        f"\nYou can now safely remove {device} once your desktop environment "
        "reports it is safe to do so (or run 'udisksctl power-off -b "
        f"{device}' if available)."
    )


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
def main():
    parser = argparse.ArgumentParser(
        description="Interactively write a bootable ISO to a USB drive using native Linux tools."
    )
    parser.add_argument(
        "--allow-fixed",
        action="store_true",
        help="Also list non-removable ('fixed') disks as selectable targets. "
        "Use with extreme caution - only for advanced/edge-case setups.",
    )
    args = parser.parse_args()

    cprint("Bootable USB Creator", C.BLUE, bold=True)
    cprint("Native-tools based (lsblk / dd / findmnt / umount)\n", C.BLUE)

    check_requirements()

    disks, system_disks = list_candidate_disks(allow_fixed=args.allow_fixed)

    if system_disks:
        cprint(
            f"(Excluded system/boot drive(s) from the list: "
            f"{', '.join('/dev/' + d for d in sorted(system_disks))})",
            C.YELLOW,
        )

    if not disks:
        die(
            "No eligible target drives found.\n"
            "- Make sure a USB drive is plugged in.\n"
            "- If your target is a non-removable/fixed disk, re-run with --allow-fixed.\n"
            "- Fixed/system disks are always excluded for safety."
        )

    disk = prompt_select_disk(disks)

    if not disk["removable"]:
        cprint(
            f"\nWARNING: {disk['path']} is reported as a NON-REMOVABLE (fixed) disk.",
            C.RED,
            bold=True,
        )
        answer = (
            input("Are you absolutely sure this is correct? [y/N]: ").strip().lower()
        )
        if answer != "y":
            cprint("Aborted by user.", C.YELLOW)
            sys.exit(0)

    handle_mounted_partitions(disk)

    iso_path, iso_size = prompt_iso_path()

    # Warn (don't block) if ISO is larger than the target device where we can tell.
    result = run(["blockdev", "--getsize64", disk["path"]])
    if result.returncode == 0 and result.stdout.strip().isdigit():
        dev_size = int(result.stdout.strip())
        if iso_size > dev_size:
            cprint(
                f"\nWARNING: ISO size ({human_size(iso_size)}) is larger than "
                f"{disk['path']} ({human_size(dev_size)}). The write will fail "
                "partway through.",
                C.RED,
                bold=True,
            )
            answer = input("Continue anyway? [y/N]: ").strip().lower()
            if answer != "y":
                cprint("Aborted by user.", C.YELLOW)
                sys.exit(0)

    final_confirmation(disk, iso_path, iso_size)

    log_path = setup_logger()
    write_image(disk, iso_path, iso_size, log_path)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        cprint("\nAborted by user.", C.YELLOW)
        sys.exit(130)
