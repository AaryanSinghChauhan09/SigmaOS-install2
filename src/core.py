"""Disk installer for SigmaOS-installer: plan installs from a JSON manifest.

Principles: split into a PURE/TESTED "plan & validate" layer and a thin
side-effecting "execute" layer that self_test never runs against a real
disk (execute() refuses to run without explicit apply=True + root).
Fail-fast manifest validation, deterministic plan ordering.
"""
import argparse, json, os, re, sys

ENGINE = {
    "name": "sigma-installer",
    "summary": "Validate manifests and plan full-disk installs",
    "language": "python",
    "verbs": ["validate-manifest", "plan", "install-plan", "summary"],
    "principles": ["Tests First (Plan & Validate)", "Pure Plan / Thin Execute",
                   "Fail-Fast", "Defense in Depth", "Deterministic Testing"],
}

cli_doc = """\
$ sigma-installer validate-manifest install.json
$ sigma-installer plan install.json
$ sigma-installer install-plan install.json
$ sigma-installer summary install.json
"""


class InstallError(Exception):
    pass


_SIZE_RE = re.compile(r"^(\d+)([MG])$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_FS_TYPES = ("ext4", "btrfs", "xfs", "vfat")
_BOOTLOADERS = ("systemd-boot", "grub", "m1n1")
_RESERVE_GB = 1.0


def parse_size(value):
    """Pure: '512M' / '8G' / int(GB) -> GB float; '100%' -> None."""
    if value == "100%":
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return float(value)
    m = _SIZE_RE.match(str(value))
    if not m:
        raise InstallError(f"bad size: {value!r} (use 512M, 8G, 100% or GB int)")
    amount = int(m.group(1))
    if amount <= 0:
        raise InstallError(f"size must be positive: {value!r}")
    return amount / 1024.0 if m.group(2) == "M" else float(amount)


def validate_manifest(manifest):
    """Pure: full manifest validation; returns a normalized copy or raises."""
    if not isinstance(manifest, dict):
        raise InstallError("manifest must be a JSON object")
    for key in ("name", "disk", "partitions", "users", "packages", "bootloader"):
        if key not in manifest:
            raise InstallError(f"manifest missing required key: {key}")
    if not isinstance(manifest["name"], str) or not manifest["name"].strip():
        raise InstallError("bad product name")

    disk = manifest["disk"]
    if not isinstance(disk, dict):
        raise InstallError("disk must be an object")
    device = disk.get("device")
    if not isinstance(device, str) or not device.startswith("/dev/"):
        raise InstallError(f"disk device must start with /dev/: {device!r}")
    disk_gb = disk.get("size_gb")
    if not isinstance(disk_gb, (int, float)) or isinstance(disk_gb, bool) \
            or disk_gb <= 0:
        raise InstallError(f"disk size_gb must be > 0, got {disk_gb!r}")
    table = disk.get("table", "gpt")
    if table not in ("gpt", "mbp"):
        raise InstallError(f"bad partition table: {table!r}")
    uefi = bool(disk.get("uefi", table == "gpt"))

    parts = manifest["partitions"]
    if not isinstance(parts, list) or not parts:
        raise InstallError("partitions must be a non-empty list")
    names, mounts, total = set(), set(), 0.0
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            raise InstallError(f"partition #{i} must be an object")
        pname = part.get("name")
        if not isinstance(pname, str) or not re.fullmatch(r"[a-z0-9-]{1,16}", pname):
            raise InstallError(f"bad partition name: {pname!r}")
        if pname in names:
            raise InstallError(f"duplicate partition name: {pname}")
        names.add(pname)
        if part.get("fs") not in _FS_TYPES:
            raise InstallError(f"{pname}: unsupported fs {part.get('fs')!r}")
        mp = part.get("mount")
        if not isinstance(mp, str) or not mp.startswith("/"):
            raise InstallError(f"{pname}: mount must be absolute: {mp!r}")
        if mp in mounts:
            raise InstallError(f"duplicate mountpoint: {mp}")
        mounts.add(mp)
        size = parse_size(part.get("size"))
        last = i == len(parts) - 1
        if size is None:
            if not last:
                raise InstallError(f"{pname}: only the last partition may be 100%")
        else:
            total += size
        if part.get("type") == "efi" and not uefi:
            raise InstallError(f"{pname}: EFI partition on a non-UEFI disk")
    if total + _RESERVE_GB > disk_gb:
        raise InstallError(f"partitions ({total:.2f}G) + reserve exceed "
                           f"disk ({disk_gb}G)")
    if "/" not in mounts:
        raise InstallError("manifest must provide a root (/) mount")
    if uefi and not any(p.get("type") == "efi" for p in parts):
        raise InstallError("UEFI disk requires an EFI system partition")

    users = manifest["users"]
    if not isinstance(users, list) or not users:
        raise InstallError("users must be a non-empty list")
    seen_users = set()
    for u in users:
        if not isinstance(u, dict) or not _USER_RE.match(str(u.get("name", ""))):
            raise InstallError(f"bad user: {u!r}")
        if u["name"] in seen_users:
            raise InstallError(f"duplicate user: {u['name']}")
        seen_users.add(u["name"])
        shell = u.get("shell", "/bin/bash")
        if not isinstance(shell, str) or not shell.startswith("/"):
            raise InstallError(f"{u['name']}: bad shell: {shell!r}")

    packages = manifest["packages"]
    if not isinstance(packages, list) or not packages \
            or not all(isinstance(p, str) and p for p in packages):
        raise InstallError("packages must be a non-empty list of names")

    bl = manifest["bootloader"]
    if not isinstance(bl, dict) or bl.get("type") not in _BOOTLOADERS:
        raise InstallError(f"bootloader must be one of {_BOOTLOADERS}")

    return {"name": manifest["name"], "disk": dict(disk, table=table, uefi=uefi),
            "partitions": [dict(p) for p in parts], "users": [dict(u) for u in users],
            "packages": list(packages), "bootloader": dict(bl)}


def plan(manifest):
    """Pure: ordered, deterministic install plan (steps, no commands)."""
    m = validate_manifest(manifest)
    steps = [{"step": "wipe", "target": m["disk"]["device"]},
             {"step": "partition", "target": m["disk"]["device"],
              "table": m["disk"]["table"]}]
    # root ("/" -> ["",""]) sorts before "/boot" -> ["","boot"], etc.
    ordered = sorted(m["partitions"], key=lambda p: p["mount"].split("/"))
    for p in ordered:
        steps.append({"step": "format", "target": p["name"],
                      "fs": p["fs"], "mount": p["mount"]})
    for p in ordered:
        steps.append({"step": "mount", "target": p["name"], "mount": p["mount"]})
    steps.append({"step": "copy-system", "target": "/"})
    steps.append({"step": "packages", "count": len(m["packages"])})
    steps.append({"step": "users", "target": ", ".join(
        u["name"] for u in m["users"])})
    steps.append({"step": "fstab", "target": "/"})
    steps.append({"step": "bootloader", "target": m["bootloader"]["type"]})
    steps.append({"step": "finalize", "target": m["name"]})
    return steps


def install_plan(manifest):
    """Pure: plan() rendered as concrete (but never executed here) commands."""
    m = validate_manifest(manifest)
    disk = m["disk"]["device"]
    cmds = [f"wipefs -q {disk}"]
    if m["disk"]["table"] == "gpt":
        cmds.append(f"sgdisk --zap-all {disk}")
    else:
        cmds.append(f"parted -s {disk} mklabel msdos")
    for i, p in enumerate(m["partitions"], 1):
        size = p["size"]
        cmds.append(f"parted -s {disk} mkpart primary {p['fs']} "
                    f"{size if size != '100%' else str(i) + '%'}")
    n = len(m["partitions"])
    for i, p in enumerate(m["partitions"], 1):
        partdev = f"{disk}{'p' if disk[-1].isdigit() else ''}{i}"
        mkfs = {"vfat": "mkfs.vfat -F32", "ext4": "mkfs.ext4 -F",
                "btrfs": "mkfs.btrfs -f", "xfs": "mkfs.xfs -f"}[p["fs"]]
        cmds.append(f"{mkfs} {partdev}   # -> {p['mount']}")
    root = next(p for p in m["partitions"] if p["mount"] == "/")
    cmds.append(f"mount {root['mount']} /mnt   # (root: {root['name']})")
    cmds.append(f"sigma-copy-root /mnt --packages {','.join(m['packages'])}")
    cmds.append(f"sigma-write-users /mnt {m['users'][0]['name']}")
    cmds.append("sigma-write-fstab /mnt")
    cmds.append(f"sigma-install-bootloader /mnt --type {m['bootloader']['type']}")
    cmds.append("sync")
    return cmds


def summary(manifest):
    """Pure: human-readable summary lines."""
    m = validate_manifest(manifest)
    disk = m["disk"]
    lines = [
        f"product: {m['name']}",
        f"disk: {disk['device']} ({disk['size_gb']}G, {disk['table']}, "
        f"{'uefi' if disk['uefi'] else 'bios'})",
        f"partitions: {len(m['partitions'])} "
        f"({', '.join(p['mount'] for p in m['partitions'])})",
        f"packages: {len(m['packages'])}",
        f"users: {', '.join(u['name'] for u in m['users'])}",
        f"bootloader: {m['bootloader']['type']}",
    ]
    return lines


def execute(manifest, apply=False):
    """THIN EXECUTE LAYER -- never run by self_test.

    Refuses to touch anything unless explicitly applied by root.
    """
    if not apply:
        raise InstallError("execute: dry-run refusal (pass apply=True)")
    if os.geteuid() != 0:
        raise InstallError("execute: root required")
    raise InstallError("execute: not available in plan-only build")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=ENGINE["name"], description=ENGINE["summary"])
    p.add_argument("manifest", help="JSON install manifest")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("validate-manifest", help="validate the manifest")
    sub.add_parser("plan", help="ordered install steps")
    sub.add_parser("install-plan", help="concrete command plan")
    sub.add_parser("summary", help="human-readable summary")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        with open(args.manifest, "r", encoding="utf-8") as fh:
            manifest = json.load(fh)
        if args.cmd == "validate-manifest":
            validate_manifest(manifest)
            print("manifest ok")
        elif args.cmd == "plan":
            for i, step in enumerate(plan(manifest), 1):
                detail = " ".join(f"{k}={v}" for k, v in sorted(step.items())
                                  if k != "step")
                print(f"{i:02}. {step['step']}" + (f" {detail}" if detail else ""))
        elif args.cmd == "install-plan":
            for cmd in install_plan(manifest):
                print(cmd)
        elif args.cmd == "summary":
            for line in summary(manifest):
                print(line)
        return 0
    except (InstallError, OSError, ValueError, json.JSONDecodeError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


GOOD = {
    "name": "SigmaOS",
    "disk": {"device": "/dev/nvme0n1", "size_gb": 512, "table": "gpt",
             "uefi": True},
    "partitions": [
        {"name": "efi", "size": "512M", "type": "efi", "fs": "vfat",
         "mount": "/boot"},
        {"name": "root", "size": "100%", "type": "linux", "fs": "ext4",
         "mount": "/"},
    ],
    "users": [{"name": "alice", "shell": "/bin/bash"}],
    "packages": ["base", "kernel", "desktop"],
    "bootloader": {"type": "systemd-boot"},
}


def _mutate(**changes):
    m = json.loads(json.dumps(GOOD))
    for key, value in changes.items():
        m[key] = value
    return m


def self_test() -> None:
    # good manifest validates and round-trips
    validated = validate_manifest(GOOD)
    assert validated["disk"]["uefi"] is True
    assert validated["disk"]["table"] == "gpt"
    assert validate_manifest(GOOD) == validated      # deterministic
    # parse_size
    assert parse_size("512M") == 512 / 1024
    assert parse_size("8G") == 8.0
    assert parse_size(32) == 32.0
    assert parse_size("100%") is None
    # plan ordering: wipe -> partition -> format -> mount -> ... -> finalize
    steps = [s["step"] for s in plan(GOOD)]
    assert steps == ["wipe", "partition", "format", "format", "mount", "mount",
                     "copy-system", "packages", "users", "fstab", "bootloader",
                     "finalize"], steps
    # format/mount steps cover both partitions, named explicitly
    fmt_targets = [s["target"] for s in plan(GOOD) if s["step"] == "format"]
    assert fmt_targets == ["root", "efi"]   # ordered by mount depth (root first)
    # root mounts first (shallower path)
    mounts = [s["mount"] for s in plan(GOOD) if s["step"] == "mount"]
    assert mounts == ["/", "/boot"]
    # install-plan commands reference the disk and root user
    cmds = install_plan(GOOD)
    assert cmds[0].startswith("wipefs -q /dev/nvme0n1")
    assert any("mkfs.vfat" in c for c in cmds)
    assert any("--type systemd-boot" in c for c in cmds)
    assert cmds[-1] == "sync"
    # summary
    text = "\n".join(summary(GOOD))
    assert "product: SigmaOS" in text and "bootloader: systemd-boot" in text
    # bad manifests are rejected (fail-fast)
    bad_cases = [
        _mutate(partitions=[p for p in GOOD["partitions"] if p["mount"] != "/"]),
        _mutate(partitions=[dict(GOOD["partitions"][1], name="root", fs="zfs"),
                            dict(GOOD["partitions"][1], mount="/")]),
        _mutate(disk={"device": "nvme0n1", "size_gb": 512}),
        _mutate(disk={"device": "/dev/sda", "size_gb": 1}),   # too small
        _mutate(partitions=[dict(GOOD["partitions"][0], size="8G"),   # oversize
                            dict(GOOD["partitions"][1], size="512G")]),
        _mutate(partitions=[dict(GOOD["partitions"][1], size="100%"),
                            dict(GOOD["partitions"][0])]),   # 100% not last
        _mutate(users=[]),
        _mutate(users=[{"name": "Bad User"}]),
        _mutate(users=[{"name": "alice"}, {"name": "alice"}]),
        _mutate(packages=[]),
        _mutate(bootloader={"type": "lilo"}),
        _mutate(disk={"device": "/dev/sda", "size_gb": 512, "table": "gpt",
                      "uefi": True},
                partitions=[dict(GOOD["partitions"][1])]),   # uefi w/o ESP
        _mutate(bootloader=None),
    ]
    for i, bad in enumerate(bad_cases):
        try:
            validate_manifest(bad)
        except InstallError:
            pass
        else:
            raise AssertionError(f"bad manifest #{i} not rejected")
    # execute layer refuses without explicit apply (and self_test never applies)
    try:
        execute(GOOD)
    except InstallError as e:
        assert "dry-run refusal" in str(e)
    else:
        raise AssertionError("execute must refuse dry-run")
    # CLI smoke through main()
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "manifest.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(GOOD, fh)
        bad_path = os.path.join(d, "bad.json")
        with open(bad_path, "w", encoding="utf-8") as fh:
            json.dump(_mutate(users=[]), fh)
        assert main([path, "validate-manifest"]) == 0
        assert main([path, "plan"]) == 0
        assert main([path, "install-plan"]) == 0
        assert main([path, "summary"]) == 0
        assert main([bad_path, "validate-manifest"]) == 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
