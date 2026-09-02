"""Draw docs/mesh.svg: how VOX agents find each other.

Excalidraw-style, generated rather than drawn by hand so the picture can be
regenerated when the protocol changes:

    python3 docs/make_mesh_diagram.py

The wobble is deterministic (a fixed seed), so re-running with no changes
produces an identical file and the diff stays empty.
"""

from __future__ import annotations

import math
import random
from pathlib import Path

W, H = 1180, 900
SEED = 20260902

INK = "#2c3038"
MUTED = "#6b7280"
AMBER = "#b07d2b"
SAGE = "#5f7a5f"
CLAY = "#bd6a48"
SLATE = "#4a6fa5"
PAPER = "#fdfcf8"

FONT = "'Segoe UI', 'Helvetica Neue', Inter, system-ui, sans-serif"

rng = random.Random(SEED)
parts: list[str] = []


def wobble(x: float, y: float, amount: float = 1.6) -> tuple[float, float]:
    return x + rng.uniform(-amount, amount), y + rng.uniform(-amount, amount)


def sketch_line(x1, y1, x2, y2, stroke=INK, width=1.7, dash=None, amount=1.4):
    """One line, drawn twice with a slight wobble, the way a pen would."""
    for _ in range(2):
        ax, ay = wobble(x1, y1, amount)
        bx, by = wobble(x2, y2, amount)
        mx, my = wobble((x1 + x2) / 2, (y1 + y2) / 2, amount * 1.6)
        parts.append(
            f'<path d="M{ax:.1f} {ay:.1f} Q{mx:.1f} {my:.1f} {bx:.1f} {by:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width}" '
            f'stroke-linecap="round"'
            + (f' stroke-dasharray="{dash}"' if dash else "")
            + "/>"
        )


def sketch_box(x, y, w, h, stroke=INK, fill="none", width=1.8, radius=10):
    if fill != "none":
        parts.append(
            f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" '
            f'fill="{fill}" stroke="none"/>'
        )
    sketch_line(x + radius, y, x + w - radius, y, stroke, width)
    sketch_line(x + w, y + radius, x + w, y + h - radius, stroke, width)
    sketch_line(x + w - radius, y + h, x + radius, y + h, stroke, width)
    sketch_line(x, y + h - radius, x, y + radius, stroke, width)
    for cx, cy, a0 in (
        (x + radius, y + radius, 180),
        (x + w - radius, y + radius, 270),
        (x + w - radius, y + h - radius, 0),
        (x + radius, y + h - radius, 90),
    ):
        a1 = a0 + 90
        px = cx + radius * math.cos(math.radians(a0))
        py = cy + radius * math.sin(math.radians(a0))
        qx = cx + radius * math.cos(math.radians(a1))
        qy = cy + radius * math.sin(math.radians(a1))
        parts.append(
            f'<path d="M{px:.1f} {py:.1f} A{radius} {radius} 0 0 1 {qx:.1f} {qy:.1f}" '
            f'fill="none" stroke="{stroke}" stroke-width="{width}" stroke-linecap="round"/>'
        )


def arrow(x1, y1, x2, y2, stroke=INK, width=1.8, dash=None):
    sketch_line(x1, y1, x2, y2, stroke, width, dash)
    angle = math.atan2(y2 - y1, x2 - x1)
    for side in (-1, 1):
        a = angle + side * math.radians(26)
        parts.append(
            f'<path d="M{x2:.1f} {y2:.1f} L{x2 - 13 * math.cos(a):.1f} '
            f'{y2 - 13 * math.sin(a):.1f}" fill="none" stroke="{stroke}" '
            f'stroke-width="{width}" stroke-linecap="round"/>'
        )


def text(x, y, s, size=15, fill=INK, anchor="start", weight="400", style="normal",
         family=FONT, spacing="0"):
    body = (
        s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    parts.append(
        f'<text x="{x}" y="{y}" font-family="{family}" font-size="{size}" '
        f'fill="{fill}" text-anchor="{anchor}" font-weight="{weight}" '
        f'font-style="{style}" letter-spacing="{spacing}">{body}</text>'
    )


def mono(x, y, s, size=13, fill=MUTED, anchor="start"):
    text(x, y, s, size, fill, anchor,
         family="'JetBrains Mono', 'SF Mono', Consolas, monospace")


def step(x, y, n, label, colour=INK):
    parts.append(f'<circle cx="{x}" cy="{y}" r="13" fill="{colour}"/>')
    text(x, y + 5, str(n), 14, PAPER, "middle", "600")
    text(x + 24, y + 6, label, 16, colour, "start", "600", spacing="0.4")


# ---------------------------------------------------------------- the drawing

parts.append(f'<rect width="{W}" height="{H}" fill="{PAPER}"/>')

text(60, 56, "How VOX agents find each other", 25, INK, "start", "600")
text(60, 82, "Nothing leaves the local network segment, and nothing is announced until you press F3.",
     15, MUTED)

# --- the two agents -----------------------------------------------------
sketch_box(60, 112, 330, 92, INK, "#f3f1ea")
text(84, 145, "VOX", 18, INK, "start", "600")
text(134, 145, "PROCESSOR", 13, AMBER, "start", "600", spacing="0.6")
mono(84, 172, "vox-b6ffa342e0d3")
mono(84, 191, "verbs: infer")

sketch_box(790, 112, 330, 92, INK, "#f3f1ea")
text(814, 145, "ingestor-01", 18, INK, "start", "600")
text(922, 145, "SOURCE", 13, SAGE, "start", "600", spacing="0.6")
mono(814, 172, "issued by the same CA")
mono(814, 191, "verbs: ingest")

# --- 1. announce --------------------------------------------------------
arrow(225, 208, 225, 268, SLATE)
arrow(955, 208, 955, 268, SLATE)

sketch_box(60, 272, 1060, 96, SLATE, "#eef2f8")
step(96, 300, 1, "ANNOUNCE", SLATE)
mono(258, 305, "UDP multicast  239.17.42.1:45177  ·  TTL 1  ·  every 60s", 13, SLATE)
mono(96, 336, "{ agent_id, incarnation, whois_port, caps_digest, ts, nonce }  +  Ed25519 signature  +  the sender's certificate")
text(96, 358, "Each agent signs with its own key. There is no shared secret; TTL 1 keeps the packet on this segment.",
     13, MUTED)

# --- 2. verify ----------------------------------------------------------
arrow(300, 372, 300, 424, INK)
sketch_box(60, 428, 500, 176, INK, "#f7f6f1")
step(96, 456, 2, "VERIFY  ·  every packet, in order")
for i, (line, colour) in enumerate([
    ("certificate signed by our CA", CLAY),
    ("announced id is a SAN of that certificate", CLAY),
    ("signature matches the body", CLAY),
    ("timestamp in window, nonce unseen", CLAY),
]):
    y = 492 + i * 26
    parts.append(f'<circle cx="102" cy="{y - 5}" r="3.5" fill="{colour}"/>')
    text(118, y, line, 14, INK)
text(96, 594, "A stranger's packet stops here, before the registry.", 13, MUTED, style="italic")

# --- 3. whois -----------------------------------------------------------
arrow(560, 500, 618, 500, INK)
sketch_box(620, 428, 500, 176, INK, "#f7f6f1")
step(656, 456, 3, "WHOIS  ·  unicast mTLS")
text(656, 492, "Only for a peer that is new or has restarted.", 14, INK)
mono(656, 520, "client → is the server's SAN the id it announced?")
mono(656, 543, "server → is this certificate from our CA?")
mono(656, 566, "server → does the authorizer allow this caller?")
text(656, 594, "Answer: { name, capabilities: { verbs: [...] } }", 13, MUTED, style="italic")

# --- 4. classify --------------------------------------------------------
arrow(870, 608, 870, 656, INK)
sketch_box(620, 660, 500, 196, AMBER, "#faf6ec")
step(656, 688, 4, "CLASSIFY  ·  from the declared verbs", AMBER)
rows = [
    ("ingest, publish", "SOURCE", SAGE),
    ("transform, enrich, infer", "PROCESSOR", AMBER),
    ("store, index, notify", "SINK", SLATE),
    ("schedule, dispatch", "ORCHESTRATOR", CLAY),
    ("observe, audit", "OBSERVER", MUTED),
]
for i, (verbs, category, colour) in enumerate(rows):
    y = 722 + i * 26
    mono(656, y, verbs, 13, INK)
    text(880, y, "→", 13, MUTED)
    text(906, y, category, 13, colour, "start", "600")
text(1010, 826, "never routed work", 12, MUTED, style="italic")

# --- 5. the heartbeat ---------------------------------------------------
arrow(300, 608, 300, 656, INK)
# The two lower boxes read right to left, so the order is drawn in.
arrow(618, 700, 564, 700, MUTED, 1.5, dash="6 5")
sketch_box(60, 660, 500, 196, SAGE, "#f1f5f0")
step(96, 688, 5, "HEARTBEAT  ·  the announcement keeps coming", SAGE)
text(96, 722, "The WHOIS is what is skipped for a peer already known.", 13, MUTED)

states = [("PROBATION", AMBER, 96), ("ACTIVE", SAGE, 244), ("SUSPECT", CLAY, 376)]
for label, colour, x in states:
    sketch_box(x, 748, 120, 34, colour, "#ffffff", 1.6, 8)
    text(x + 60, 770, label, 13, colour, "middle", "600")
arrow(220, 765, 240, 765, INK, 1.5)
arrow(368, 765, 372, 765, INK, 1.5)
mono(172, 742, "whois ok", 11)
mono(316, 742, "3 intervals", 11)

sketch_box(376, 806, 120, 32, MUTED, "#ffffff", 1.6, 8)
text(436, 827, "DEAD", 13, MUTED, "middle", "600")
arrow(436, 786, 436, 804, MUTED, 1.5)
mono(446, 800, "5 intervals", 11)
arrow(376, 800, 300, 790, SAGE, 1.5, dash="5 5")
mono(96, 806, "an announcement brings it back", 11, SAGE)

svg = (
    f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" '
    f'width="{W}" height="{H}" role="img" '
    f'aria-label="VOX mesh: announce, verify, whois, classify, heartbeat">'
    + "".join(parts)
    + "</svg>"
)

out = Path(__file__).resolve().parent / "mesh.svg"
out.write_text(svg, encoding="utf-8", newline="\n")
print(f"wrote {out} ({len(svg)} bytes)")
