"""Render the codebook and the illustrated tool guide to PDF, with screenshots of the practice packet.

Usage:
  python -B -X utf8 tools/annotation/render_docs.py            initial build (after make_packet.py)
  python -B -X utf8 tools/annotation/render_docs.py --update   codebook revision and practice explanation only

Screenshots come from a demo copy of the practice packet with a seeded answer state, opened in headless Microsoft
Edge. The demo copy and the raw screenshots live under .tmp/. The two PDFs go to reports/human_validation_v2/build/, and
one folder per annotator (send_A1/, send_A2/) collects exactly the files that annotator receives. An update writes
the current codebook and the practice explanation to build/ and to send_update/, the same files for both annotators,
and leaves the frozen initial folders untouched.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import shutil
import subprocess
from pathlib import Path

import markdown
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
OUT = ROOT / "reports/human_validation_v2"
BUILD = OUT / "build"
ANNOTATORS = ("A1", "A2")
GUIDE_PDF, EXPLANATION_PDF = "PinTrace_标注工具使用说明_v1.0.pdf", "PinTrace_练习讲解_v1.0.pdf"
WORK = ROOT / ".tmp/annotation-docs"
EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")
WIDTH = 1366
PRACTICE = BUILD / "PinTrace_practice.html"
CALLOUT = (214, 48, 49)

# Each shot: practice item number, window height, seeded answers by position, crop and callouts. The crop is a box or
# a CSS selector (cropped with a margin). A callout is (number, CSS selector, side) and sits beside that element.
SHOTS = {
    "overview": dict(item=6, height=900, crop=(0, 0, WIDTH, 900),
                     callouts=[(1, "#progress-text", "below"), (2, "nav", "top"), (3, ".question", "left"),
                               (4, "#item .muted a", "left"), (5, ".sides .side h3", "left"), (6, ".diffs h3", "left"),
                               (7, "#export", "below")]),
    "answer": dict(item=6, height=1800, crop="#answer", answers={6: ("pin", "high", "")},
                   callouts=[(1, "#answer .labels", "left"), (2, "#answer .row", "left"), (3, "#answer textarea", "left"),
                             (4, "#answer .nav-btns", "left")]),
    "nav": dict(item=6, height=520, crop=(0, 0, 330, 420),
                answers={1: ("remove", "high", ""), 2: ("sha_change", "high", ""), 3: ("mutable", "medium", ""),
                         4: ("unknown", "low", "看不出对应关系"), 5: ("pinned", "high", "")},
                callouts=[]),
    "reference": dict(item=3, height=760, crop=(230, 120, WIDTH, 760), callouts=[]),
    "header": dict(item=6, height=200, crop=(1040, 0, WIDTH, 60), callouts=[]),
}
MARGIN = 28

DEMO_SCRIPT = """
<script>
(function () {
  var seed = %(seed)s;
  try { localStorage.setItem("pintrace:" + seed.packet, JSON.stringify(seed.state)); } catch (e) {}
})();
</script>
"""
AFTER_SCRIPT = """
<script>
(function () {
  var selectors = %(selectors)s;
  var rects = {};
  selectors.forEach(function (s) {
    var e = document.querySelector(s);
    if (e) { var r = e.getBoundingClientRect(); rects[s] = [r.left, r.top, r.right, r.bottom]; }
  });
  var pre = document.createElement("pre"); pre.id = "__rects"; pre.style.display = "none";
  pre.textContent = JSON.stringify(rects); document.body.appendChild(pre);
})();
</script>
"""


def packet_data(page: str) -> dict:
    return json.loads(re.search(r'<script id="packet" type="application/json">(.*?)</script>', page, re.S).group(1))


def demo_page(name: str, shot: dict) -> Path:
    page = PRACTICE.read_text(encoding="utf-8")
    data = packet_data(page)
    answers = {data["items"][pos - 1]["uid"]: dict(answer=a, confidence=c, note=n, updated_at="2026-11-08T00:00:00Z")
               for pos, (a, c, n) in shot.get("answers", {}).items()}
    seed = dict(packet=data["packet_id"], state=dict(answers=answers, position=shot["item"] - 1))
    selectors = [selector for _, selector, _ in shot["callouts"]] + ([shot["crop"]] if isinstance(shot["crop"], str) else [])
    page = page.replace('<script id="packet"', DEMO_SCRIPT % dict(seed=json.dumps(seed, ensure_ascii=False)) + '<script id="packet"', 1)
    page = page.replace("</body>", AFTER_SCRIPT % dict(selectors=json.dumps(selectors)) + "</body>")
    path = WORK / f"demo_{name}.html"
    path.write_text(page, encoding="utf-8")
    return path


def edge(*args: str) -> str:
    base = [str(EDGE), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--no-first-run",
            f"--user-data-dir={WORK / 'profile'}", "--virtual-time-budget=3000"]
    return subprocess.run(base + list(args), capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120).stdout


def font(size: int):
    for name in ("segoeuib.ttf", "arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def anchor(rect, side):
    left, top, right, bottom = rect
    return {"left": (left - 20, (top + bottom) / 2), "top": (left + 16, top - 2),
            "below": ((left + right) / 2, bottom + 14)}[side]


def screenshot(name: str, shot: dict) -> Path:
    demo = demo_page(name, shot)
    url = demo.as_uri() + f"#{shot['item']}"
    raw = WORK / f"raw_{name}.png"
    edge(f"--window-size={WIDTH},{shot['height']}", f"--screenshot={raw}", url)
    dom = edge(f"--window-size={WIDTH},{shot['height']}", "--dump-dom", url)
    match = re.search(r'<pre id="__rects"[^>]*>(.*?)</pre>', dom, re.S)
    rects = json.loads(match.group(1).replace("&quot;", '"')) if match else {}
    image = Image.open(raw).convert("RGB")
    draw, label = ImageDraw.Draw(image), font(15)
    for number, selector, corner in shot["callouts"]:
        if selector not in rects:
            raise ValueError(f"{name}: callout target {selector} not found")
        x, y = anchor(rects[selector], corner)
        x, y = max(13, x), max(13, y)
        draw.ellipse((x - 12, y - 12, x + 12, y + 12), fill=CALLOUT, outline=(255, 255, 255), width=2)
        draw.text((x, y), str(number), fill=(255, 255, 255), font=label, anchor="mm")
    crop = shot["crop"]
    if isinstance(crop, str):
        left, top, right, bottom = rects[crop]
        crop = (max(0, int(left) - MARGIN), max(0, int(top) - 8), min(image.width, int(right) + 8), min(image.height, int(bottom) + 8))
    image = image.crop(crop)
    path = WORK / f"{name}.png"
    image.save(path)
    return path


STYLE = """
@page { size: A4; margin: 16mm 15mm; }
body { font: 10.5pt/1.6 "Microsoft YaHei", "PingFang SC", "Segoe UI", sans-serif; color: #1d2330; }
h1 { font-size: 20pt; margin: 0 0 6pt; } h2 { font-size: 14pt; margin: 18pt 0 6pt; border-bottom: 1px solid #dde1e8; padding-bottom: 3pt; }
h3 { font-size: 12pt; margin: 12pt 0 4pt; }
code { font-family: Consolas, "Cascadia Mono", monospace; font-size: 9.5pt; background: #f1f3f6; padding: 0 3px; border-radius: 3px; }
pre { background: #f1f3f6; padding: 8pt; border-radius: 4px; font-size: 9pt; overflow-wrap: anywhere; white-space: pre-wrap; }
pre code { background: none; padding: 0; }
table { border-collapse: collapse; width: 100%; margin: 6pt 0; font-size: 9.5pt; page-break-inside: auto; }
th, td { border: 1px solid #cfd5df; padding: 4pt 6pt; vertical-align: top; text-align: left; }
th { background: #eef1f6; }
tr { page-break-inside: avoid; }
blockquote { margin: 6pt 0; padding: 4pt 10pt; border-left: 3px solid #2f5bd3; background: #f3f6fd; }
img { max-width: 100%; border: 1px solid #cfd5df; border-radius: 4px; margin: 4pt 0; }
hr { border: none; border-top: 1px solid #dde1e8; margin: 14pt 0; }
"""


def to_pdf(source: Path, target: Path, images: dict[str, Path]) -> None:
    text = source.read_text(encoding="utf-8")
    for name, path in images.items():
        data = base64.b64encode(path.read_bytes()).decode("ascii")
        text = text.replace(f"(images/{name}.png)", f"(data:image/png;base64,{data})")
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    html_path = WORK / (source.stem + ".html")
    html_path.write_text(f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><style>{STYLE}</style></head>"
                         f"<body>{body}</body></html>", encoding="utf-8")
    edge("--no-pdf-header-footer", f"--print-to-pdf={target}", html_path.as_uri())
    if not target.exists():
        raise RuntimeError(f"PDF was not written: {target}")


def codebook_pdf() -> str:
    """File name carrying the version stated on the codebook's own version line."""
    version = re.search(r"版本 v(\d+\.\d+)", (HERE / "codebook.md").read_text(encoding="utf-8")).group(1)
    return f"PinTrace_编码手册_v{version}.pdf"


def update() -> None:
    """Codebook revision plus practice explanation, identical for both annotators."""
    WORK.mkdir(parents=True, exist_ok=True)
    names = [codebook_pdf(), EXPLANATION_PDF]
    if (BUILD / names[0]).exists() and names[0].endswith("_v1.0.pdf"):
        raise ValueError("The codebook version line still says v1.0; an update needs a new version")
    to_pdf(HERE / "codebook.md", BUILD / names[0], {})
    to_pdf(HERE / "practice_explanation.md", BUILD / EXPLANATION_PDF, {})
    folder = OUT / "send_update"
    folder.mkdir(exist_ok=True)
    for name in names:
        shutil.copy2(BUILD / name, folder / name)
    print(json.dumps(dict(send={folder.name: names}), ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--update", action="store_true")
    if parser.parse_args().update:
        update()
        return
    CODEBOOK_PDF = codebook_pdf()
    WORK.mkdir(parents=True, exist_ok=True)
    images = {name: screenshot(name, shot) for name, shot in SHOTS.items()}
    to_pdf(HERE / "codebook.md", BUILD / CODEBOOK_PDF, {})
    to_pdf(HERE / "guide.md", BUILD / GUIDE_PDF, images)
    sent = {}
    for annotator in ANNOTATORS:
        folder = OUT / f"send_{annotator}"
        folder.mkdir(exist_ok=True)
        names = [f"PinTrace_annotation_{annotator}.html", PRACTICE.name, CODEBOOK_PDF, GUIDE_PDF]
        for name in names:
            shutil.copy2(BUILD / name, folder / name)
        sent[folder.name] = names
    print(json.dumps(dict(images=[p.name for p in images.values()], send=sent), ensure_ascii=False))


if __name__ == "__main__":
    main()
