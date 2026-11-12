# Take-into-VR bundles: renders the picture cards a live teacher loads into the VR room.
#
#   python -I -X utf8 _shared/vr-pack/render_vr_pack.py <module folder> [--shots <folder>]
#
# Reads  <module folder>/take-into-vr/packs.json   (which pictures, which words, per lesson and activity)
# Writes <module folder>/take-into-vr/Week N/Lesson N - <title>/<activity>/NN - <label>.pdf   one card per PDF
#        <module folder>/take-into-vr/Week N/Lesson N - <title>/Lesson N - all cards.pdf      every card of the lesson
#        <module folder>/take-into-vr/index.json                                               what the Course Optimizer reads
#
# Pictures come out of the lesson page's own asset block, so a card can never show a picture
# the lesson does not use. Every word on a card must already be in the lesson page (verbatim gate).
# Each card is checked in the browser before it is written: pictures loaded, nothing off the card,
# no text on top of a picture or of other text.
import asyncio, datetime, html, json, pathlib, re, shutil, sys

from playwright.async_api import async_playwright

GOLD, CREAM, INK, MUTED, NAVY = "#C9A04A", "#F7EFD9", "#2B2117", "#6b5a3a", "#18213D"

CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #fff; }
body { font-family: Georgia, 'Times New Roman', serif; color: %(INK)s; }
.card { position: relative; overflow: hidden; background: %(CREAM)s; border-left: 36px solid %(GOLD)s;
  display: flex; flex-direction: column; padding: 56px 80px 40px 64px; page-break-after: always; break-after: page; }
.card.land { width: 1600px; height: 1000px; }
.card.port { width: 1000px; height: 1600px; }
.plate { flex: 1 1 0; min-height: 0; display: flex; align-items: center; justify-content: center; }
.plate img { display: block; max-width: 100%%; max-height: 100%%; }
.plate img.fig { height: 100%%; width: auto; max-width: 100%%; object-fit: contain; }
.kicker { font-size: 28px; letter-spacing: 2px; text-transform: uppercase; color: %(MUTED)s; margin-top: 28px; }
.label { font-size: 68px; line-height: 1.15; margin-top: 8px; }
.label::first-letter { text-transform: uppercase; }
.caption { font-size: 38px; line-height: 1.25; margin-top: 10px; color: %(INK)s; }
.credit { font-size: 22px; line-height: 1.3; margin-top: 14px; color: %(MUTED)s; }
.foot { font-size: 26px; color: %(MUTED)s; margin-top: 18px; }
.moment .row { flex: 1 1 0; min-height: 0; display: flex; gap: 60px; }
.moment .words { flex: 1 1 0; min-width: 0; display: flex; flex-direction: column; }
.moment .num { font-size: 210px; line-height: 1; color: %(GOLD)s; }
.moment .kicker { margin-top: 18px; }
.moment .text { font-size: 62px; line-height: 1.22; margin-top: 18px; }
.moment .figs { flex: 0 0 640px; display: flex; align-items: flex-end; justify-content: flex-end; gap: 24px; }
.moment .figs img { display: block; height: 470px; width: auto; max-width: 308px; object-fit: contain; }
.sign { justify-content: center; }
.sign .title { font-size: 150px; line-height: 1.05; }
.sign .kicker { margin-top: 0; margin-bottom: 26px; }
""" % dict(GOLD=GOLD, CREAM=CREAM, INK=INK, MUTED=MUTED, NAVY=NAVY)

# Runs inside the page: returns a list of problems for every .card on it.
GATE_JS = r"""
() => {
  const out = [];
  const hit = (a, b) => a.left < b.right - 1 && b.left < a.right - 1 && a.top < b.bottom - 1 && b.top < a.bottom - 1;
  document.querySelectorAll('.card').forEach((card, n) => {
    const c = card.getBoundingClientRect(), id = card.dataset.name || ('card ' + n);
    const imgs = [...card.querySelectorAll('img')];
    imgs.forEach(i => { if (!i.complete || !i.naturalWidth) out.push(id + ': a picture did not load'); });
    const texts = [...card.querySelectorAll('.t')].map(el => {
      const r = document.createRange(); r.selectNodeContents(el); return { el, box: r.getBoundingClientRect() };
    }).filter(t => t.box.width > 0);
    const boxes = imgs.map(i => i.getBoundingClientRect());
    if (card.scrollHeight > card.clientHeight + 1 || card.scrollWidth > card.clientWidth + 1) out.push(id + ': content runs off the card');
    texts.forEach((t, i) => {
      if (t.box.left < c.left || t.box.right > c.right + 1 || t.box.top < c.top || t.box.bottom > c.bottom + 1) out.push(id + ': text runs off the card (' + t.el.className + ')');
      boxes.forEach(b => { if (hit(t.box, b)) out.push(id + ': text sits on a picture (' + t.el.className + ')'); });
      texts.slice(i + 1).forEach(u => { if (hit(t.box, u.box)) out.push(id + ': two lines of text overlap (' + t.el.className + ' / ' + u.el.className + ')'); });
    });
    boxes.forEach((b, i) => {
      if (b.width < 60 || b.height < 60) out.push(id + ': a picture is too small to read');
      if (b.left < c.left || b.right > c.right + 1 || b.top < c.top || b.bottom > c.bottom + 1) out.push(id + ': a picture runs off the card');
      boxes.slice(i + 1).forEach(b2 => { if (hit(b, b2)) out.push(id + ': two pictures overlap'); });
    });
  });
  return out;
}
"""


def esc(s):
    return html.escape(str(s), quote=True)


def norm(s):
    s = html.unescape(s)
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"').replace("\\'", "'")
    return re.sub(r"\s+", " ", s).strip()


def safe_name(s):
    s = html.unescape(str(s)).replace("’", "'").replace("‘", "'").replace("“", "").replace("”", "")
    s = re.sub(r'[\\/:*?"<>|]', "", s)
    s = re.sub(r"[^\x20-\x7e]", "", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:70].strip(" .")


class Lesson:
    def __init__(self, path):
        self.path = path
        raw = path.read_text(encoding="utf-8")
        m = re.search(r'<script type="application/json" id="[\w-]*assets">(.*?)</script>', raw, re.S)
        if not m:
            raise SystemExit(f"{path.name}: no asset block, cannot build cards from it")
        self.assets = json.loads(m.group(1))
        self.img = self.assets.get("img", {})
        text = raw[: m.start()] + re.sub(r'data:image/[\w+]+;base64,[A-Za-z0-9+/=]+', "", m.group(1)) + raw[m.end():]
        text = re.sub(r'data:image/[\w+]+;base64,[A-Za-z0-9+/=]+', "", text)
        self.text = norm(re.sub(r"<[^>]+>", " ", text)) + " " + norm(text)

    def has(self, s):
        return norm(s) in self.text

    def src(self, key):
        if key not in self.img:
            raise SystemExit(f"{self.path.name}: no picture named {key}")
        return self.img[key]["src"]

    def size(self, key):
        import base64, io
        from PIL import Image
        b = base64.b64decode(self.src(key).split(",", 1)[1])
        return Image.open(io.BytesIO(b)).size


def card_html(L, card, kicker, foot, name):
    t = card["type"]
    if t == "picture":
        w, h = L.size(card["img"])
        orient = "port" if h > w * 1.1 and w >= 600 else "land"
        fig = " fig" if h <= 700 else ""
        parts = [f'<div class="plate"><img class="pic{fig}" src="{L.src(card["img"])}" alt=""></div>',
                 f'<div class="kicker t">{esc(kicker)}</div>',
                 f'<div class="label t">{esc(card["label"])}</div>']
        if card.get("caption"):
            parts.append(f'<div class="caption t">{esc(card["caption"])}</div>')
        if card.get("credit"):
            parts.append(f'<div class="credit t">{esc(card["credit"])}</div>')
        parts.append(f'<div class="foot t">{esc(foot)}</div>')
        return f'<div class="card picture {orient}" data-name="{esc(name)}">{"".join(parts)}</div>', orient
    if t == "moment":
        figs = "".join(f'<img src="{L.src(k)}" alt="">' for k in card["imgs"])
        return (f'<div class="card moment land" data-name="{esc(name)}"><div class="row"><div class="words">'
                f'<div class="num t">{card["n"]}</div><div class="kicker t">{esc(kicker)}</div>'
                f'<div class="text t">{esc(card["text"])}</div></div><div class="figs">{figs}</div></div>'
                f'<div class="foot t">{esc(foot)}</div></div>'), "land"
    if t == "sign":
        return (f'<div class="card sign land" data-name="{esc(name)}"><div class="kicker t">{esc(kicker)}</div>'
                f'<div class="title t">{esc(card["label"])}</div><div class="foot t" style="position:absolute;left:64px;bottom:40px;">{esc(foot)}</div></div>'), "land"
    raise SystemExit(f"unknown card type {t}")


def page(body):
    return f'<!doctype html><html><head><meta charset="utf-8"><style>{CSS}</style></head><body>{body}</body></html>'


SIZE = {"land": ("1600px", "1000px"), "port": ("1000px", "1600px")}


async def main():
    args = sys.argv[1:]
    shots = None
    if "--shots" in args:
        i = args.index("--shots"); shots = pathlib.Path(args[i + 1]); del args[i:i + 2]; shots.mkdir(parents=True, exist_ok=True)
    mod = pathlib.Path(args[0]).resolve()
    out = mod / "take-into-vr"
    spec = json.loads((out / "packs.json").read_text(encoding="utf-8"))
    problems, plan = [], []

    # 1. plan every card and run the verbatim gate before anything is rendered
    for les in spec["lessons"]:
        L = Lesson(mod / les["page"])
        if not L.has(les["title"]):
            problems.append(f'{les["page"]}: lesson title not on the page: {les["title"]}')
        lfolder = f'Week {les["week"]}/Lesson {les["lesson"]} - {safe_name(les["title"])}'
        foot = f'{spec["module"]} · Week {les["week"]} · Lesson {les["lesson"]}'
        cards = []
        for act in les["activities"]:
            for s in act.get("verify", [act["name"]]):
                if not L.has(s):
                    problems.append(f'{les["page"]}: activity name not on the page: {s}')
            afolder = safe_name(act["name"])
            moments = [c for c in act["cards"] if c["type"] == "moment"]
            for n, c in enumerate(act["cards"], 1):
                for field in ("label", "caption", "text", "credit"):
                    if c.get(field) and not L.has(c[field]):
                        problems.append(f'{les["page"]} / {act["name"]}: words not on the lesson page: {c[field]!r}')
                if c["type"] == "moment":
                    c["n"] = moments.index(c) + 1
                    kicker = f'Moment {c["n"]} of {len(moments)}'
                    stem = f'Moment {c["n"]} - {safe_name(c.get("short") or c["text"])[:48].strip()}'
                elif c["type"] == "sign":
                    kicker, stem = act["name"], f'Sign - {safe_name(c["label"]).title() if c["label"].isupper() else safe_name(c["label"])}'
                else:
                    kicker = act["name"]
                    lab = safe_name(c["label"]); lab = lab[:1].upper() + lab[1:]
                    stem = lab
                fname = f'{n:02d} - {stem}.pdf'
                cards.append(dict(card=c, kicker=kicker, activity=act["name"], rel=f'{lfolder}/{afolder}/{fname}',
                                  label=c.get("label") or c.get("text")))
        plan.append(dict(les=les, L=L, folder=lfolder, foot=foot, cards=cards))
    seen = set()
    for p in plan:
        for c in p["cards"]:
            if c["rel"].lower() in seen:
                problems.append(f'two cards would share a file name: {c["rel"]}')
            seen.add(c["rel"].lower())
    if problems:
        print("GATE FAILED before rendering:"); [print("  -", x) for x in problems]; raise SystemExit(1)

    # 2. render
    for d in out.glob("Week *"):
        shutil.rmtree(d)
    index = dict(version=1, module=spec["module"], made=datetime.date.today().isoformat(), lessons=[])
    async with async_playwright() as pw:
        b = await pw.chromium.launch()
        pg = await b.new_page(viewport={"width": 1700, "height": 1700})
        for p in plan:
            files, bodies, orients = [], [], set()
            for c in p["cards"]:
                body, orient = card_html(p["L"], c["card"], c["kicker"], p["foot"], c["rel"])
                await pg.set_content(page(body)); await pg.wait_for_timeout(60)
                bad = await pg.evaluate(GATE_JS)
                if bad:
                    problems += bad; continue
                dest = out / c["rel"]; dest.parent.mkdir(parents=True, exist_ok=True)
                await pg.pdf(path=str(dest), width=SIZE[orient][0], height=SIZE[orient][1], print_background=True,
                             margin=dict(top="0", bottom="0", left="0", right="0"), page_ranges="1")
                if shots:
                    await pg.locator(".card").screenshot(path=str(shots / (c["rel"].replace("/", " __ ")[:-4] + ".png")))
                files.append(dict(path=c["rel"], activity=c["activity"], label=c["label"], bytes=dest.stat().st_size))
                bodies.append((body, orient)); orients.add(orient)
            # combined: landscape pages; a portrait card is laid on its own portrait page via a named @page
            comb = f'Lesson {p["les"]["lesson"]} - all cards.pdf'
            extra = "<style>@page { size: 1600px 1000px; margin: 0; } @page port { size: 1000px 1600px; margin: 0; } .card.port { page: port; }</style>"
            await pg.set_content(page(extra + "".join(x[0] for x in bodies))); await pg.wait_for_timeout(120)
            cdest = out / p["folder"] / comb
            await pg.pdf(path=str(cdest), prefer_css_page_size=True, print_background=True)
            index["lessons"].append(dict(page=p["les"]["page"], week=p["les"]["week"], lesson=p["les"]["lesson"], title=p["les"]["title"],
                                         folder=p["folder"], combined=dict(path=f'{p["folder"]}/{comb}', bytes=cdest.stat().st_size), files=files))
        await b.close()
    if problems:
        print("GATE FAILED while rendering:"); [print("  -", x) for x in problems]; raise SystemExit(1)

    # 3. count gate, then the index
    for p, row in zip(plan, index["lessons"]):
        if len(row["files"]) != len(p["cards"]):
            raise SystemExit(f'{row["page"]}: {len(row["files"])} cards written, {len(p["cards"])} planned')
        for f in row["files"]:
            if f["bytes"] < 3000:
                raise SystemExit(f'{f["path"]}: the PDF is nearly empty')
    (out / "index.json").write_text(json.dumps(index, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    total = sum(f["bytes"] for r in index["lessons"] for f in r["files"]) + sum(r["combined"]["bytes"] for r in index["lessons"])
    for r in index["lessons"]:
        print(f'{r["folder"]}: {len(r["files"])} cards')
    print(f'GATE PASSED: {sum(len(r["files"]) for r in index["lessons"])} cards, {total/1e6:.1f} MB, index.json written')


asyncio.run(main())
