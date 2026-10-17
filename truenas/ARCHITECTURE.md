# Home infrastructure — architecture

State as of 2026-10-17. Written to be pasted into a fresh session as context.

## One paragraph

A single Minisforum N5 PRO runs **Proxmox**, which hosts everything: a **TrueNAS
VM** that owns the disk controller and serves 29.7 TB, an **apps LXC** running
ten Docker services plus NPU-backed AI, and a **V100 VM** for CUDA work (kept
powered off — there isn't RAM for all three). A retired Huawei server holds the
only independent copy of the data and is normally off.

## Hosts

| Host | Address | What it is |
|---|---|---|
| `tank` | **192.168.4.2** | Proxmox VE 9.2.21, kernel 7.0.14-24-pve, Ryzen AI 9 HX PRO 370, 24 threads, **23 GB usable** RAM |
| `truenas` (VM 100) | **192.168.4.34** | TrueNAS 27.0.0-RC.1, 4 cores / 8 GB |
| `apps` (CT 200) | **192.168.4.111** | Debian 12 LXC, unprivileged, 4 cores / 10 GB |
| `gpu` (VM 101) | **192.168.4.40** | Ubuntu 26.04, 6 cores / 8 GB, **stopped, autostart off** |
| old Huawei NAS | **192.168.4.254** | retired, powered off; BMC always on at 192.168.4.5 |
| Home Assistant | 192.168.4.188 | separate machine, not part of this host |

Access: SSH key auth as `root@192.168.4.2`, `truenas_admin@192.168.4.34`,
`root@192.168.4.111`. `sudo` on the TrueNAS VM needs a password (so root-only
work there needs the user); the Proxmox host and the LXC are root already.

## RAM is the binding constraint

23 GB usable, because **8 GB is reserved as iGPU UMA frame buffer** in BIOS
(a deliberate choice for Plex and NPU work). Budget: TrueNAS 8 + apps 10 +
host/ARC ~3 = 21 GB, leaving ~2 GB. **The V100 VM cannot run at the same time**
as the apps LXC, which is why its autostart is off. A second 32 GB SODIMM is the
planned fix and is waiting on funds — treat the ceiling as fixed, not temporary.

Consequence seen in practice: with both AI models resident the LXC sits at
~9.5 of 10 GB and the kernel OOM-kills `obsidian` occasionally (Docker restarts
it, so nothing looks broken).

## Passthrough — the core of the design

IOMMU is on (`amd_iommu=on iommu=pt` in `/etc/default/grub`, GRUB not
systemd-boot). Two devices are bound to `vfio-pci` at boot via
`/etc/modprobe.d/vfio.conf`: `options vfio-pci ids=197b:0585,10de:1db4`, plus
`softdep ahci pre: vfio-pci` so ahci cannot claim the SATA controller first.

| Device | PCI | IOMMU group | Owner |
|---|---|---|---|
| JMicron JMB58x SATA (4× 28 TB) | `c1:00.0` | 17 | **TrueNAS VM** |
| Tesla V100 16 GB | `c6:00.0` | 22 | **V100 VM** |
| Radeon 890M iGPU | `c7:00.0` | 22* | host → shared into the LXC |
| Ryzen AI NPU (XDNA2) | `c8:00.1` | — | host → shared into the LXC |
| Boot NVMe 128 GB / 960 EVO 500 GB | `c2`/`c3:00.0` | 18 / 19 | host |

The four HDDs **do not exist on the host** — only the VM sees them. The pool is
IMPORTED there, never created. Each device sits alone in its group, which is why
this works cleanly.

## Storage

**Host**: `fast` — 464 GB ZFS on the 960 EVO (`ashift=12`, `lz4`, `autotrim`),
holding VM disks and LXC volumes, plus `fast/iso` as ISO/template storage.
Addressed **by serial** (`/dev/disk/by-id/nvme-Samsung_SSD_960_EVO_...`) because
NVMe numbering has already swapped once.

**TrueNAS VM**: `data` — 4× 28 TB raidz1, 102 TB raw / **29.7 TB used**, ONLINE.
Datasets: `Unraid_Backup` 18.5 T, `tm` 7.7 T, `TimeMachine` 1.65 T, `apps`
1.46 T (including three zvols: `arrr_things` 775 G, `AnneFrank-dgl07i` 253 G,
`AnneFrank-71ro1e` 158 G), `NewTimeMachine` 256 G, `Containers` 44 G, `vms`
43 G. 351 snapshots carried over from the old box.

Shares: SMB `tm` and `unraid_backup`; NFS exports of `proxmox-backup`,
`Unraid_Backup` and `apps` to 192.168.4.2 only.

**The circular dependency worth knowing about**: the host NFS-mounts exports
from a VM it hosts. These were `hard` mounts and froze the entire host (SSH and
web UI dead) when the VM stalled. Now `soft,timeo=50,retrans=3`, with
`x-systemd.automount` on `/mnt/tank/apps`, and the PVE storage set `soft` too.
`/mnt/tank/unraid` is bind-mounted into the LXC as `/mnt/media`.

## Network

One bridge, `vmbr0` (192.168.4.2/24, gw .1), with **both NICs as bridge ports**
(`nic0` at 1 Gb, `nic1` currently down). PVE 9 renamed the interfaces to
`nic0`/`nic1`. Guests attach to `vmbr0` with static addresses. MTU 1500
throughout now that the old box's direct 10 GbE link is retired.

## Services

**apps LXC (CT 200)** — Docker 29.9.1, `/opt/stacks/<name>/compose.yaml` managed
through **dockge at :5001**. iGPU in as `dev0/dev1` (`renderD128`/`card0`), NPU
as `dev3` (`/dev/accel/accel0`), `lxc.prlimit.memlock: unlimited`.

- `plex` behind `plex-vpn` (gluetun/WireGuard; keys in `/opt/stacks/apps/.env`)
- `stash`, `prowlarr`, `qbittorrent`, `flaresolverr`, `syncthing`,
  `resilio-sync`, `obsidian`, `dockge`
- **`fastflowlm`** — LLM + speech-to-text on the **NPU**, see below

**TrueNAS VM** — SMB/NFS, plus `server-monitor` (Docker app) publishing 46 MQTT
keys to Home Assistant. Scrub Sundays 00:00; SMART long monthly / short weekly
via cron (TrueNAS 27 has **no SMART scheduling API** — it is `cronjob.create`
resolving disks by serial).

## AI services

`http://192.168.4.111:52625` — one FastFlowLM process, no auth, LAN only:

| Endpoint | Model | Measured |
|---|---|---|
| `/v1/chat/completions` | `qwen3-it:4b` | prefill ~490 tok/s, decode ~15 tok/s |
| `/v1/audio/transcriptions` | `whisper-v3:turbo` | 30 s audio → 6 s |

Runs as `serve qwen3-it:4b --asr 1 --host 0.0.0.0` — **`--host` matters**, it
binds 127.0.0.1 by default. Context window **32,768 tokens**. There is **no
prefix caching and no option for it**, so time-to-first-token is a pure function
of prompt size (~7 s at 3.5k tokens). Everything runs on the NPU, deliberately,
to keep the iGPU free for Plex transcoding.

Home Assistant uses both: `stt.npu_whisper` (HACS `openai_whisper_cloud`) and
`conversation.npu_qwen3_4b` (home-llm `generic_openai` backend), as a secondary
Assist pipeline. A Mac/Ollama pipeline remains the preferred one.

The V100 VM is built and verified (driver 580.178.04 — **R580 is the last branch
supporting Volta**, CUDA 12.4, Docker with the `nvidia` runtime) but idle.

## Traps that have already bitten

- **`/dev/sdX` and DRM node numbering shift between boots.** Resolve disks by
  serial and check `/dev/dri` after any GPU change — the LXC would not start
  when its config named a node that had moved.
- **Aborting a TrueNAS replication job does not stop the `zfs send`.** The
  process is named `zfs: sending …`, so `pkill -f "zfs send"` misses it.
- **`/tmp` on TrueNAS is tmpfs** — scripts left there vanish on reboot and
  `systemd-run` then fails before the unit starts, silently.
- **Booting the old Huawei re-pollutes Home Assistant**: its September
  server-monitor still uses `node_name: TrueNasty` and re-announces ~71 stale
  entities. Stop its `monitor` app, or rename its node, before anything else.
- **HA 2026.10 ignores MQTT `object_id`** — `default_entity_id` is what pins an
  entity id now.

## Open items

1. Second 32 GB SODIMM — unblocks the V100 VM and the LXC's memory pressure.
2. UPS + NUT, and the nightly Proxmox backup job (storage is configured and
   write-tested; the job is not created).
3. Confirm Plex still reports a hardware transcode with a real playback.
4. Nothing has been read back from `tm`/`TimeMachine` through a share yet, and
   the old box remains the only independent copy — do not wipe it.
