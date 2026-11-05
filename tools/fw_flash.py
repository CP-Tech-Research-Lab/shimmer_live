"""Update the firmware of ONE Shimmer3 docked in a Shimmer base (MSP430 bootloader over the base).

    python tools/fw_flash.py <slot> <last 4 of MAC> <image.txt> <image sha256> [--old-image <old.txt>]

Close Shimmer Live first (it holds the base's ports). Every step is logged and every file kept
in ~/Documents/ShimmerFirmwareBackups/<MAC>_<time>/:
  1. dock: check the slot holds the expected Shimmer; read version + InfoMem (configuration and
     calibration, 512 B) and save it                                      -> infomem_before.bin
  2. optional (--old-image): unlock the bootloader with the OLD official image's vectors and read
     the old firmware out                                                  -> old_firmware.bin
     (if the Shimmer does not run exactly that image, the wrong password mass-erases it: the same
      erase step 3 does anyway)
  3. bootloader: mass erase, unlock (all 0xFF after erase), write the new image, verify every
     segment with the chip's CRC, reset into the new firmware
  4. dock: wait for the new firmware, save its default InfoMem            -> infomem_new_defaults.bin
     write the saved InfoMem back, read it back and compare               -> infomem_after.bin
The slot's bootloader lines are unmasked only during steps 2-3 and always masked again.
If anything fails after the erase, the Shimmer has no firmware: run this again (the bootloader is
never erased), or restore the old version the same way with its image.
"""
import datetime as dt
import hashlib
import pathlib
import struct
import sys
import time

import serial
from pyshimmer.uart.dock_api import ShimmerDock
from pyshimmer.uart.dock_const import UART_COMP_SHIMMER, UART_PROP_INFOMEM, UART_SET

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from shimmer_live import base, bsl  # noqa: E402
from shimmer_live.config import Config  # noqa: E402

INFOMEM_SIZE, INFOMEM_CHUNK = 512, 128
SELF_SET_BITS = [(230, 0x01)]          # the firmware sets this bit itself on every config write


def log(msg):
    print(f"{dt.datetime.now():%H:%M:%S}  {msg}", flush=True)


def ctl(link, cmd):
    s = link._control()
    s.reset_input_buffer()
    s.write(cmd.encode())
    buf, end = b"", time.monotonic() + 3
    while time.monotonic() < end and b"\r\n" not in buf:
        buf += s.read(64)
    reply = buf.decode(errors="replace").strip()
    if not reply or reply.startswith("E"):
        raise RuntimeError(f"base answered {reply!r} to {cmd}")
    return reply


def dock_open(port, attempts=30, delay=0.5):
    """(mac, version, ShimmerDock, serial) once the docked firmware answers."""
    last = None
    for _ in range(attempts):
        try:
            s = serial.Serial(port, 115200, timeout=1.0)
        except serial.SerialException as e:
            last = e
            time.sleep(delay)
            continue
        try:
            d = ShimmerDock(s)
            mac = ":".join(f"{b:02X}" for b in d.get_mac_address())
            _hw, fw, ma, mi, rel = d.get_firmware_version()
            return mac, f"{fw.name} {ma}.{mi}.{rel}", d, s
        except Exception as e:                                # noqa: BLE001 - a booting unit drops commands
            last = e
            s.close()
            time.sleep(delay)
    raise RuntimeError(f"the docked Shimmer did not answer on {port}: {last}")


def read_infomem(d):
    return b"".join(d.get_infomem(off, INFOMEM_CHUNK)[:INFOMEM_CHUNK] for off in range(0, INFOMEM_SIZE, INFOMEM_CHUNK))


def write_infomem(d, data):
    for off in range(0, len(data), INFOMEM_CHUNK):
        chunk = data[off:off + INFOMEM_CHUNK]
        d._write_packet(UART_SET, UART_COMP_SHIMMER, UART_PROP_INFOMEM, struct.pack("<BH", len(chunk), off) + chunk)
        d._read_ack()


def masked(im):
    b = bytearray(im)
    for off, m in SELF_SET_BITS:
        b[off] &= ~m & 0xFF
    return bytes(b)


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    slot, want, image, sha = int(argv[0]), argv[1].upper(), pathlib.Path(argv[2]), argv[3].lower()
    old_image = pathlib.Path(argv[argv.index("--old-image") + 1]) if "--old-image" in argv else None
    if hashlib.sha256(image.read_bytes()).hexdigest() != sha:
        sys.exit(f"ABORT: {image.name} does not match the published SHA-256: not touching anything")
    segs = bsl.read_ti_txt(image)
    if any(a < 0x5C00 for a, _ in segs):
        sys.exit("ABORT: the image writes below main flash (InfoMem/BSL area): refusing")
    dev = next((d for d in base.detect(Config()) if d.control_port and len(d.ports) == 4), None)
    if dev is None:
        sys.exit("ABORT: no Shimmer base found (is Shimmer Live closed?)")
    bsl_port, uart_port = dev.ports[2], dev.ports[3]
    link = base.BaseLink(dev)
    with link.lock:
        log(f"base {dev.serial}: control {dev.control_port}, bootloader {bsl_port}, Shimmer UART {uart_port}")
        ctl(link, "SDS,1$")                                          # lines masked before anything else
        link.select(slot)

        # ---- 1. who is it, and save its configuration ----
        mac, before, d, s = dock_open(uart_port)
        try:
            if not mac.replace(":", "").endswith(want):
                sys.exit(f"ABORT: slot {slot} holds {mac}, not …{want}: nothing done")
            infomem = read_infomem(d)
        finally:
            s.close()
        out = pathlib.Path.home() / "Documents" / "ShimmerFirmwareBackups" / f"{mac.replace(':', '')}_{dt.datetime.now():%Y%m%d-%H%M%S}"
        out.mkdir(parents=True, exist_ok=True)
        (out / "infomem_before.bin").write_bytes(infomem)
        log(f"slot {slot}: {mac}, {before}; configuration saved to {out}/infomem_before.bin")

        # ---- 2 + 3. bootloader ----
        b = bsl.Bsl(bsl_port)                                         # opened while masked: no effect
        erased = False
        try:
            log("unmask bootloader lines: " + ctl(link, "SDS,0$"))
            b.invoke()
            if old_image is not None:
                try:
                    b.unlock(bsl.password_of(bsl.read_ti_txt(old_image)))
                    b.change_baud(115200)
                    old = b"".join(b.read(a, len(dd)) for a, dd in bsl.read_ti_txt(old_image))
                    (out / "old_firmware.bin").write_bytes(old)
                    expect = b"".join(dd for _, dd in bsl.read_ti_txt(old_image))
                    log(f"old firmware read out ({len(old)} bytes): "
                        + ("identical to the official image" if old == expect else "DIFFERS from the official image"))
                except bsl.BslError as e:
                    log(f"old firmware not read: {e}")
                    erased = e.status == 0x05                          # wrong password: chip erased itself
                b.invoke()                                             # fresh session at 9600 baud
            log("mass erase (old firmware removed; the bootloader stays)")
            b.mass_erase()
            erased = True
            b.change_baud(115200)
            b.unlock(b"\xff" * 32)
            log(f"bootloader {b.version().hex()}: writing {sum(len(x) for _, x in segs)} bytes")
            last = [-1]

            def progress(done, total):
                pct = done * 100 // total
                if pct // 10 != last[0]:
                    last[0] = pct // 10
                    log(f"  written {pct}%")
            b.program(segs, progress)
            log("every segment verified by the chip's CRC: identical to the image")
            b.run_application()
        except Exception as e:
            log(f"FAILED: {e}")
            if erased:
                log("The Shimmer has NO firmware now. Leave it docked and run this command again; "
                    f"its configuration is saved in {out}.")
            raise
        finally:
            log("mask bootloader lines: " + ctl(link, "SDS,1$"))
            b.close()

        # ---- 4. new firmware up? put the configuration back ----
        time.sleep(2.5)
        link.select(slot)
        mac2, after, d, s = dock_open(uart_port)
        try:
            log(f"new firmware answers: {mac2}, {after}")
            defaults = read_infomem(d)
            (out / "infomem_new_defaults.bin").write_bytes(defaults)
            diff = [i for i in range(INFOMEM_SIZE) if defaults[i] != infomem[i]]
            log(f"new firmware's defaults differ from the saved configuration at {len(diff)} byte(s)")
            write_infomem(d, infomem)
            back = read_infomem(d)
            (out / "infomem_after.bin").write_bytes(back)
        finally:
            s.close()
        bad = [i for i in range(INFOMEM_SIZE) if masked(back)[i] != masked(infomem)[i]]
        link.disconnect()
        if bad:
            sys.exit(f"WARNING: configuration restored with differences at offsets {bad[:20]}; "
                     f"original in {out}/infomem_before.bin")
        log(f"DONE: {before} -> {after}; configuration restored identical. Files in {out}")


if __name__ == "__main__":
    main(sys.argv[1:])
