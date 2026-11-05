"""Labels: a free text per Shimmer, e.g. the participant wearing it ("alberto"). Kept on this computer only:
never written to the Shimmer, its Bluetooth name does not change. Shown next to the name, in grey and in
brackets, and copied into every recording made while it is set."""
import html as _html

MAX_LEN = 40
GREY = "#8a8a86"


def get(cfg, mac) -> str:
    return (cfg.device(mac).get("label") or "").strip() if mac else ""


def set_label(cfg, mac, text) -> str:
    text = " ".join((text or "").split())[:MAX_LEN]
    cfg.update_device(mac, label=text)
    return text


def plain(name, label) -> str:
    return f"{name} ({label})" if label else name


def rich(name, label, grey=GREY) -> str:
    """name (label) with the label in grey, for rich-text labels and plot titles."""
    return (f"{_html.escape(name)} <span style='color:{grey}; font-weight:400'>({_html.escape(label)})</span>"
            if label else _html.escape(name))
