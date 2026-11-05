"""Update the firmware of ONE Shimmer3 docked in a Shimmer base, from the command line.

    python tools/fw_flash.py <slot> <MAC or its last 4 characters> <image.txt> <image sha256>

The same procedure as the app's "Update firmware…" (shimmer_live/fw_update.py). Close Shimmer
Live first: it holds the base's ports. Backups go to ~/Documents/ShimmerFirmwareBackups/.
"""
import datetime as dt
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from shimmer_live import base, fw_update  # noqa: E402
from shimmer_live.config import Config  # noqa: E402


def log(msg):
    print(f"{dt.datetime.now():%H:%M:%S}  {msg}", flush=True)


def main(argv):
    if len(argv) < 4:
        sys.exit(__doc__)
    slot, who, image, sha = int(argv[0]), argv[1], pathlib.Path(argv[2]), argv[3]
    if fw_update.sha256_of(image) != sha.lower():
        sys.exit(f"ABORT: {image.name} does not match the published SHA-256: not touching anything")
    dev = next((d for d in base.detect(Config()) if d.control_port and len(d.ports) == 4), None)
    if dev is None:
        sys.exit("ABORT: no Shimmer base found (is Shimmer Live closed?)")
    link = base.BaseLink(dev)
    last = [""]

    def progress(text):
        if text != last[0] and not (text.startswith("writing…") and not text.endswith("0%")):
            log(text)
        last[0] = text
    try:
        r = fw_update.update(link, slot, who, image, sha, progress)
    finally:
        link.close()
    for n in r.notes:
        log(n)
    for off, label, want, got in r.changed:
        log(f"the new firmware changed byte {off} ({label}): {want:08b} -> {got:08b}")
    log(f"DONE: {r.before} -> {r.after} in {r.seconds:.0f} s. Backup in {r.backup_dir}")
    log("Switch the Shimmer off and on once: the firmware reads its configuration at power-on.")


if __name__ == "__main__":
    main(sys.argv[1:])
