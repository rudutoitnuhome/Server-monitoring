#!/usr/bin/env bash
# Image the TrueNAS boot pool onto the backup vdev.
#
# What this captures, and why each piece is needed to actually boot again:
#   boot-pool-<stamp>.zfs.zst  the OS itself: a recursive zfs send of a
#                              point-in-time snapshot, so it is internally
#                              consistent even though the pool is live (a dd of
#                              a mounted pool is not).
#   esp-<stamp>.img            the 512M EFI system partition (bootloader).
#                              Static, so dd is safe and correct here.
#   biosboot-<stamp>.img       the 1M BIOS-boot partition, for legacy booting.
#   parttable-<stamp>.gpt      sgdisk backup of the GPT, to recreate the layout.
#   config-<stamp>/            freenas-v1.db + pwenc_secret - the pair TrueNAS
#                              needs to restore settings onto a fresh install.
#                              This is the fastest recovery path by far.
#
# Read-only with respect to the running system apart from creating a snapshot.
set -euo pipefail

# Pass a previous stamp as $1 to resume: existing snapshot and image are reused
# rather than redone.
DEST=/mnt/backup/bootbackup
STAMP="${1:-$(date +%Y%m%d-%H%M)}"
SNAP="boot-pool@bootbak-${STAMP}"
LOG="${DEST}/boot-backup-${STAMP}.log"

mkdir -p "$DEST"
exec > >(tee -a "$LOG") 2>&1
echo "=== boot pool backup ${STAMP}"

# Resolve the live boot device from the pool itself rather than hardcoding a
# letter: /dev/sdX letters on this box have moved every time a disk was touched.
# Take the ONLINE member specifically. A faulted member still prints its last
# known path ("13557137303570482368 UNAVAIL ... was /dev/sdn3"), and matching the
# first /dev/sdX3 in the output picked THAT - a device which no longer exists.
BOOTPART=$(zpool status -P boot-pool | awk '/\/dev\/sd[a-z]+3/ && $2=="ONLINE" {print $1; exit}')
if [ -z "${BOOTPART:-}" ]; then echo "FATAL: could not resolve boot partition"; exit 1; fi
BOOTDEV="${BOOTPART%3}"
echo "boot device: ${BOOTDEV}  (pool member ${BOOTPART})"
echo "serial:      $(lsblk -dno SERIAL "$BOOTDEV")"
zpool status boot-pool | sed 's/^/    /'

echo
echo "=== free space on destination"
df -h /mnt/backup | tail -1

echo
echo "=== 1/5 snapshot ${SNAP}"
if zfs list -t snapshot "$SNAP" >/dev/null 2>&1; then
  echo "    already exists, reusing"
else
  zfs snapshot -r "$SNAP"
fi

echo "=== 2/5 zfs send -> ${DEST}/boot-pool-${STAMP}.zfs.zst"
# Plain -R (not -c): the stream decompresses on the way out and zstd packs it
# here, which keeps the image restorable by any OpenZFS, not just one with the
# same compression features enabled.
IMG="${DEST}/boot-pool-${STAMP}.zfs.zst"
if [ -s "$IMG" ] && zstd -t "$IMG" >/dev/null 2>&1; then
  echo "    image already present and intact ($(du -h "$IMG" | cut -f1)), reusing"
else
  zfs send -R "$SNAP" | zstd -T0 -3 -q -o "$IMG"
fi

echo "=== 3/5 boot partitions"
dd if="${BOOTDEV}2" of="${DEST}/esp-${STAMP}.img" bs=1M status=none
dd if="${BOOTDEV}1" of="${DEST}/biosboot-${STAMP}.img" bs=1M status=none
sgdisk --backup="${DEST}/parttable-${STAMP}.gpt" "$BOOTDEV" >/dev/null

echo "=== 4/5 TrueNAS config (db + pwenc_secret)"
mkdir -p "${DEST}/config-${STAMP}"
cp -p /data/freenas-v1.db /data/pwenc_secret "${DEST}/config-${STAMP}/"

echo "=== 5/5 checksums"
cd "$DEST"
sha256sum "boot-pool-${STAMP}.zfs.zst" "esp-${STAMP}.img" "biosboot-${STAMP}.img" \
          "parttable-${STAMP}.gpt" "config-${STAMP}"/* > "manifest-${STAMP}.sha256"

echo
echo "=== verify the stream is readable end to end"
# zstreamdump parses the send stream and validates its internal checksums
# without touching any pool. The earlier version of this check piped into
# `zfs receive -nv -F -d backup`, which was wrong twice over: -d needs an
# existing target, and -F against the backup pool root is a force-overwrite that
# only the -n saved. Never put -F in a verification step.
set +e
zstd -dc "boot-pool-${STAMP}.zfs.zst" | zstreamdump 2>&1 | tail -8
VERIFY_RC=${PIPESTATUS[1]}
set -e
if [ "$VERIFY_RC" -eq 0 ]; then
  echo "    stream OK (checksums valid end to end)"
else
  echo "    *** STREAM VERIFY FAILED (rc=$VERIFY_RC) - do not trust this image ***"
fi

echo
echo "=== writing RESTORE.md"
cat > "${DEST}/RESTORE.md" <<RESTORE
# Restoring the TrueNAS boot pool (image set ${STAMP})

Two routes. Try the first one.

## 1. Fresh install + config restore (fast, supported, ~20 min)

1. Install TrueNAS from USB onto the new boot device.
2. In the installer or the UI, restore the config from
   \`config-${STAMP}/freenas-v1.db\`. If prompted for or if encrypted secrets
   fail afterwards, also place \`config-${STAMP}/pwenc_secret\` in /data and
   reboot - without it every stored password and API key stays unreadable.
3. Import the \`backup\` pool. Apps, shares and tasks come back with the config.

This is the path to use unless you specifically need the old OS image.

## 2. Restore this image (when you want the exact boot environment back)

Boot any Linux with ZFS (a TrueNAS installer shell will do).

    # recreate the partition layout on the NEW disk (DESTROYS it)
    sgdisk --load-backup=parttable-${STAMP}.gpt /dev/sdX

    # restore the boot partitions
    dd if=esp-${STAMP}.img      of=/dev/sdX2 bs=1M
    dd if=biosboot-${STAMP}.img of=/dev/sdX1 bs=1M

    # recreate the pool and receive the OS into it
    zpool create -f -o ashift=12 -O compression=lz4 boot-pool /dev/sdX3
    zstd -dc boot-pool-${STAMP}.zfs.zst | zfs receive -d boot-pool

    # point the pool at the right boot environment and make it bootable
    zpool set bootfs=boot-pool/ROOT/<env> boot-pool
    zpool export boot-pool

Caveats, honestly: route 2 has not been rehearsed end to end on this hardware.
The image and partition contents are verified; the sequence above is not. If the
boot pool ever dies, expect route 1 to work and treat route 2 as the fallback
that may need fiddling with bootfs or a grub reinstall.

## Verify an image before trusting it

    sha256sum -c manifest-${STAMP}.sha256
    zstd -t boot-pool-${STAMP}.zfs.zst
    zstd -dc boot-pool-${STAMP}.zfs.zst | zstreamdump

## Next time: incremental

The snapshot boot-pool@bootbak-${STAMP} is kept, so a later run can send only
the delta:

    zfs snapshot -r boot-pool@bootbak-<new>
    zfs send -R -I @bootbak-${STAMP} boot-pool@bootbak-<new> | zstd -T0 -3 -o inc.zfs.zst
RESTORE
echo "    $(wc -l < "${DEST}/RESTORE.md") lines"

echo
echo "=== result"
ls -lh "$DEST" | sed 's/^/    /'
echo
echo "snapshot ${SNAP} kept on disk for future incrementals:"
echo "  zfs send -R -I @bootbak-${STAMP} boot-pool@<next> | zstd ... "
echo "done: $(date)"
