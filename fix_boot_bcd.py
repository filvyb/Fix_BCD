#!/usr/bin/env python3
#
# Fixing Windows boot manager entries (BCD registry)
# Uses reged and fdisk.
# You typically need to run this as root.
# Follows https://gist.github.com/lupoDharkael/f0054016e2dbdddc0293871af3eb6189
# (c) Kurt Garloff <kurt@garloff.de>, 6/2025
# SPDX-License-Identifier: CC-BY-SA-4.0
"Fix Windows Boot BCD files"

import os
import sys
import re
import getopt
import shutil
import registry_dict


if not shutil.which("fdisk"):
    raise RuntimeError("You need fdisk installed")
if os.geteuid() != 0:
    print("WARNING: Not running as root, can't see disk information.", file=sys.stderr)


# Globals
PartUUIDs = {}
DiskUUIDs = {}
PartDisks = {}
PartDskNm = {}
PartDescr = {}
DiskOutput = {}
SourcePartUUIDs = {}
TargetPartUUIDs = {}


def multiws_split(stg):
    "Split string fields delimited with multiple whitespace"
    el = []
    ln = len(stg)
    previx = 0
    ix = stg.find(' ', previx)
    if ix == -1:
        ix = stg.find('\t', previx)
    while ix != -1:
        el.append(stg[previx:ix])
        previx = ix+1
        while ln > previx and stg[previx].isspace():
            previx += 1
        ix = stg.find(' ', previx)
        if ix == -1:
            ix = stg.find('\t', previx)
    if previx < ln:
        el.append(stg[previx:])
    return el


def disk_uuid(disk):
    "Use fdisk to get DISK UUID"
    # global DiskOutput
    uuid = None
    for ln in os.popen(f"fdisk -l /dev/{disk}"):
        ln = ln.rstrip('\n')
        if ln[:17] == "Disk identifier: ":
            uuid = ln[17:].lower()
        if not ln.startswith(f"/dev/{disk}"):
            continue
        arr = multiws_split(ln)
        DiskOutput[arr[0][5:]] = f"{arr[4]:6} " + " ".join(arr[5:])
    return uuid


def find_loop(nm):
    "Find whole disk loop device for dm- partitions (kpartx)"
    loops = filter(lambda x: x.startswith("loop"), os.listdir("/dev/mapper"))
    for lp in loops:
        if os.path.basename(os.readlink(f"/dev/mapper/{lp}")) == nm:
            return lp
    return None


def strip_part(nm):
    "Find whole disk name by stripping partition suffix"
    if not nm[-1].isdigit():
        return None
    if nm[:2] == "dm":
        nm = find_loop(nm)
        if not nm:
            return None
    dnm = nm[:-1]
    while dnm[-1].isdigit():
        dnm = dnm[:-1]
    if dnm[-1] == 'p':
        dnm = dnm[:-1]
    return dnm


def collect_partuuids(target_drive=None, source_drive=None):
    """Create dicts by looking at part_uuid_path:
    * PartUUIDs has all Partition UUIDs with device names
    * PartDisks holds Disk UUID belonging to Partition UUIDs
    * PartDskNm holds Disk DevName belonging to Partition UUIDs
    * PartDescr holds descriptions from fdisk -l
    * DiskOutput is the same table indexed by device names
    * DiskUUIDs is a table we build to avoid repeating fdisk -l all the time
    * SourcePartUUIDs has partitions from source_drive (for auto-mapping)
    * TargetPartUUIDs has partitions from target_drive (for auto-mapping)

    If target_drive is specified (e.g., 'sda' or 'nvme0n1'), only partitions
    on that drive will be collected into PartUUIDs (for manual selection).

    If both source_drive and target_drive are specified (for auto-mapping):
    - SourcePartUUIDs will contain partitions from source_drive
    - TargetPartUUIDs will contain partitions from target_drive
    - PartUUIDs will contain partitions from target_drive (for fallback manual mode)
    """
    part_uuid_path = "/dev/disk/by-partuuid/"
    # global PartUUIDs, DiskUUIDs, PartDisks, PartDskNm, PartDescr, SourcePartUUIDs, TargetPartUUIDs

    with os.scandir(part_uuid_path) as pdir:
        for entry in pdir:
            if entry.name.startswith('.') or not entry.is_symlink():
                continue
            part = os.path.basename(os.readlink(entry))
            disknm = strip_part(part)

            include_in_main = True
            if target_drive and disknm != target_drive:
                include_in_main = False

            if disknm and disknm not in DiskUUIDs:
                DiskUUIDs[disknm] = disk_uuid(disknm)

            if source_drive and disknm == source_drive:
                SourcePartUUIDs[entry.name] = part
            if target_drive and disknm == target_drive:
                TargetPartUUIDs[entry.name] = part

            if include_in_main:
                PartUUIDs[entry.name] = part
                if disknm:
                    PartDisks[entry.name] = DiskUUIDs[disknm]
                    PartDskNm[entry.name] = disknm
                    if part in DiskOutput:
                        PartDescr[entry.name] = DiskOutput[part]
                    else:
                        PartDescr[entry.name] = ""

            if source_drive and disknm == source_drive:
                if entry.name not in PartDisks and disknm:
                    PartDisks[entry.name] = DiskUUIDs[disknm]
                    PartDskNm[entry.name] = disknm
                    if part in DiskOutput:
                        PartDescr[entry.name] = DiskOutput[part]
                    else:
                        PartDescr[entry.name] = ""


def counts(arr):
    "Count zeros and ASCII chars in byte array"
    cntz = 0
    cnta = 0
    for val in arr:
        if val == 0:
            cntz += 1
        if 32 <= val <= 122:
            cnta += 1
    return (cntz, cnta)


def uuidstr(hex16):
    "Create UUID string from byte array"
    return f"{hex16[3]:02x}{hex16[2]:02x}{hex16[1]:02x}{hex16[0]:02x}-{hex16[5]:02x}{hex16[4]:02x}-" \
           f"{hex16[7]:02x}{hex16[6]:02x}-{hex16[8]:02x}{hex16[9]:02x}-" \
           f"{hex16[10]:02x}{hex16[11]:02x}{hex16[12]:02x}{hex16[13]:02x}{hex16[14]:02x}{hex16[15]:02x}"


def find_part_disk(hexstr):
    "Search for Part/Disk UUIDs"
    ids = []
    offs = []
    hexarr = registry_dict.arr_from_hexstr(hexstr)
    ln = len(hexarr)
    idx = 32
    while ln >= idx+16:
        while idx < ln and hexarr[idx] == 0:
            idx += 4
        if ln < idx+16:
            return (ids, offs)
        cntz, cnta = counts(hexarr[idx:idx+16])
        if cntz < 2 and cnta <= 10 and (ln == idx+16 or hexarr[idx+16] == 0):
            ids.append(uuidstr(hexarr[idx:idx+16]))
            offs.append(idx)
            idx += 16
        idx += 4
    return (ids, offs)


def partkey(st):
    "Insert 0 in sort key for partition number"
    if not st[-2].isdigit():
        return st[:-2] + '0' + st[-1]
    return st


def is_uuidfmt(st):
    "Returns true if string is in UUID format"
    uuidfmt = re.compile(r'^[0-9a-fA-F]{8}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{4}\-[0-9a-fA-F]{12}$')
    return uuidfmt.match(st) is not None


def build_auto_mapping(source_drive=None):
    """Build automatic partition mapping when source drive is specified.

    Maps partitions from SourcePartUUIDs to TargetPartUUIDs by order.
    Exits with error if partition counts don't match.

    Returns: dict mapping source partition UUIDs to target partition UUIDs,
             or None if source_drive is not specified (manual mode).
    """
    if not source_drive:
        return None

    if not SourcePartUUIDs:
        print(f"ERROR: No partitions found on source drive {source_drive}", file=sys.stderr)
        sys.exit(1)

    if not TargetPartUUIDs:
        print(f"ERROR: No partitions found on target drive", file=sys.stderr)
        sys.exit(1)

    source_partitions_list = sorted(SourcePartUUIDs.keys(), key=lambda x: partkey(SourcePartUUIDs[x]))
    target_partitions_list = sorted(TargetPartUUIDs.keys(), key=lambda x: partkey(TargetPartUUIDs[x]))

    if len(source_partitions_list) != len(target_partitions_list):
        print(f"ERROR: Partition count mismatch!", file=sys.stderr)
        print(f"  Source drive: {len(source_partitions_list)} partition(s)", file=sys.stderr)
        print(f"  Target drive: {len(target_partitions_list)} partition(s)", file=sys.stderr)
        sys.exit(1)

    mapping = {}
    for i, src_uuid in enumerate(source_partitions_list):
        mapping[src_uuid] = target_partitions_list[i]

    print(f"Automatic mode: Mapping {len(mapping)} partition(s)")
    for src, tgt in mapping.items():
        print(f"  {src} ({SourcePartUUIDs[src]}) -> {tgt} ({TargetPartUUIDs[tgt]})")

    return mapping


def select_uuid(auto_mapping=None, source_uuid=None):
    "Display list of partitions and ask user to select one, or use automatic mapping"
    # If automatic mapping exists for this source UUID
    if auto_mapping and source_uuid and source_uuid in auto_mapping:
        target_uuid = auto_mapping[source_uuid]
        print(f"  AUTO: Mapping {source_uuid} -> {target_uuid} ({PartUUIDs.get(target_uuid, 'unknown')})")
        return target_uuid

    # Manual mode
    print("  Disks:")
    for disk in sorted(DiskUUIDs):
        print(f"   {disk:8} : {DiskUUIDs[disk]}")
    print("  Partitions:")
    for part in sorted(PartUUIDs, key = lambda x: partkey(PartUUIDs[x])):
        print(f"   {PartUUIDs[part]:10} : {PartDescr[part]:30} : {part}")
    while True:
        ans = input("  Partition (DevName or PartUUID or PartUUID,DiskUUID): ")
        if not ans:
            return None
        if ans in PartUUIDs:
            return ans
        if ans.find(",") != -1:
            pid, did = ans.split(",")
            if is_uuidfmt(pid) and is_uuidfmt(did):
                PartDisks[pid] = did
                return pid
        for partid, partnm in PartUUIDs.items():
            if ans == partnm:
                return partid


def uuid_bytes(ustr):
    "Return reg dump file from bytes representing a UUID"
    return f"{ustr[6:8]},{ustr[4:6]},{ustr[2:4]},{ustr[0:2]},{ustr[11:13]},{ustr[9:11]}," \
           f"{ustr[16:18]},{ustr[14:16]},{ustr[19:21]},{ustr[21:23]}," \
           f"{ustr[24:26]},{ustr[26:28]},{ustr[28:30]},{ustr[30:32]},{ustr[32:34]},{ustr[34:36]}"


def correct_uuid(uuid, offs, dct):
    "Correct leaf assigment string with uuid at offset"
    dstr = dct['Element']
    # print(dstr)
    ix = dstr.find(':')
    assert ix >= 0
    ix += 3*offs+1
    dct['Element'] = dstr[:ix] + uuid_bytes(uuid) + dstr[ix+47:]
    # print(dct['Element'])


def list_and_correct_entries(regd, ovwr_list, source_drive=None):
    "List boot menu entries and correct wrong disk UUIDs"
    file_key = "12000002"
    fil2_key = "22000002"
    desc_key = "12000004"
    disk_key = "11000001"
    osdk_key = "21000001"

    auto_mapping = build_auto_mapping(source_drive)

    unfixed = 0
    fixes = 0
    objs = regd["Objects"]
    for obk, ob in objs.items():
        elms = ob["Elements"]
        if desc_key not in elms or 'Element' not in elms[desc_key]:
            continue
        desc = elms[desc_key]["Element"]
        # TODO: We could actually search partitions for the files referenced here
        if file_key in elms and 'Element' in elms[file_key]:
            desc += " (" + elms[file_key]['Element'].replace("\\\\", "\\") + ")"
        if fil2_key in elms and 'Element' in elms[fil2_key]:
            desc += " (" + elms[fil2_key]['Element'].replace("\\\\", "\\") + ")"
        print(f"Entry {obk}: {desc}")
        resp = None
        for key, txt in (disk_key, "Disk "), (osdk_key, "OSDsk"):
            if key in elms:
                ids, offs = find_part_disk(elms[key]['Element'])
                print(f" {txt} IDs: {ids}")
                if len(ids) != 2:
                    print("  ERROR")
                    unfixed += 1
                    continue
                obk2 = obk
                if obk2[0] == '{' and obk2[-1] == '}':
                    obk2 = obk[1:-1]
                if ids[0] not in PartUUIDs or obk in ovwr_list or obk2 in ovwr_list:
                    print("  Partition UUID needs fixing!")
                    if not resp:
                        resp = select_uuid(auto_mapping, ids[0])
                    if not resp:
                        unfixed += 1
                        continue
                    correct_uuid(resp, offs[0], elms[key])
                    correct_uuid(PartDisks[resp], offs[1], elms[key])
                    fixes += 2
                    # Validate
                    ids, offs = find_part_disk(elms[key]['Element'])
                    assert ids[0] == resp
                    assert ids[1] == PartDisks[resp]
                else:
                    if ids[0] not in PartDisks or not PartDisks[ids[0]]:
                        print(f"  Partition {ids[0]} without known disk")
                        unfixed += 1
                        continue
                    if ids[1] != PartDisks[ids[0]]:
                        print(f"  Partition {PartUUIDs[ids[0]]} should be on {PartDisks[ids[0]]} not {ids[1]}, correct")
                        correct_uuid(PartDisks[ids[0]], offs[1], elms[key])
                        fixes += 1
                    else:
                        print(f"  Partition {PartUUIDs[ids[0]]} on {PartDskNm[ids[0]]} OK")
    return fixes, unfixed


def usage(rc=1):
    "help"
    print("Usage: fix_boot_bcd.py [-n] [-s source_drive] [-d target_drive] [-o entry[,entry]] /PATH/TO/BCD")
    print(" You typically need to run this as root.")
    print(" The BCD file will be changed (but a backup file is created) if any")
    print("  entries need changes. Disk UUIDs for existing partition UUIDs will by")
    print("  automatically fixed. User will be asked about non-existing partitions.")
    print(" -n prevents changes to be written to the BCD registry.")
    print(" -s source_drive specifies the source drive for automatic mapping (e.g., sda)")
    print(" -d target_drive restricts scanning to the target drive (e.g., nvme0n1)")
    print("    When both -s and -d are specified, partitions are automatically mapped:")
    print("    source[i] -> target[i] (exits with error if partition counts don't match)")
    print(" -o entry[,entry[,...]] allows to interactively adjust valied boot entries")
    sys.exit(rc)


def main(argv):
    "Main entry point"
    nochange = False
    ovwr_list = []
    target_drive = None
    source_drive = None
    try:
        opts, args = getopt.gnu_getopt(argv[1:], "hns:d:o:", ('help',))
    except getopt.GetoptError as exc:
        print(exc, file=sys.stderr)
        usage()
    for (opt, arg) in opts:
        if opt in ("-h", "--help"):
            usage(0)
        elif opt == "-n":
            nochange = True
        elif opt == "-s":
            source_drive = arg
        elif opt == "-d":
            target_drive = arg
        elif opt == "-o":
            ovwr_list.extend(arg.split(","))
    if not args:
        usage()

    # Validate flags
    if source_drive and not target_drive:
        print("ERROR: -s (source drive) requires -d (target drive) to be specified", file=sys.stderr)
        sys.exit(1)

    collect_partuuids(target_drive, source_drive)
    # print(f"Partitions: {PartUUIDs}")
    # print(f"Disks: {DiskUUIDs}")
    # print(f"PartDisks: {PartDisks}")
    for arg in args:
        bcd = registry_dict.RegDict(arg)
        # print(bcd)
        fixes, unfixed = list_and_correct_entries(bcd, ovwr_list, source_drive)
        if fixes:
            if not nochange:
                print(f"Commiting {fixes} changes to {arg}")
                bcd.write(True)
                # Note: We ignore the return code here, as reged at times
                # returns non-zero despite succeeding.
            else:
                unfixed += fixes
                print(f"NOT writing {fixes} changes to {arg}")
    return unfixed


if __name__ == "__main__":
    sys.exit(main(sys.argv))
