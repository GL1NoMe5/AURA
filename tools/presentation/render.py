"""Render README graphics from existing particle buffers and public measurements.

No model calls, live targets, network, or application changes. Requires Pillow.
Design v2: water #091e29, pearl #e1efed, mist #97b2b5,
teal #94d5c8, fault #d9ac94. A large serif wordmark with the existing
portrait; factual captions in regular sans. No slogan or staged sales opener.
The chart leads with the file and outcome, not a row of metric cards.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import json
from pathlib import Path
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'docs/presentation'
MEDIA = OUT / 'media'
BG = (9, 30, 41)
INK = (225, 239, 237)
MUTED = (151, 178, 181)
TEAL = (148, 213, 200)
WARM = (217, 172, 148)


def font(size, light=False, brand=False):
    # Fonts are used by the local renderer, never redistributed.
    names = [('C:/Windows/Fonts/georgia.ttf' if brand else
              'C:/Windows/Fonts/segoeuil.ttf' if light else 'C:/Windows/Fonts/segoeui.ttf'),
             '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf']
    for name in names:
        if Path(name).exists():
            return ImageFont.truetype(name, size)
    raise RuntimeError('Install a local Segoe UI or DejaVu Sans font to render.')


def text(im, xy, label, size=24, color=INK, light=False, anchor=None, brand=False):
    ImageDraw.Draw(im).text(xy, label, fill=color, font=font(size, light, brand), anchor=anchor)


def background(w, h):
    # Code-drawn visual assets; the portrait photo is never edited.
    im = Image.new('RGB', (w, h), BG)
    d = ImageDraw.Draw(im)
    for y in range(h):
        t = y / h
        d.line((0, y, w, y), fill=(9 + int(3*t), 30 + int(5*t), 41 + int(5*t)))
    return im


def public_evidence(source):
    """Explicit field allowlist; raw logs, identities and paths never exported."""
    raw = json.loads(source.read_text(encoding='utf-8'))
    f = raw['verification']['functional']
    before = raw['evidence']['operation.measured']
    assert raw['phase'] == 'verified_fixed' and f['verified']
    checks = f['checks']
    assert len(checks) == 2 and all(c['ok'] and c['sha256'] == c['expected_sha256'] for c in checks)
    data = {
        'case': 'Linkding v1.46.2 historical file-buffering defect',
        'date': '2026-10-30',
        'repair': 'AI-proposed, human-reviewed and amended; operator-approved deployment',
        'source_sha256': hashlib.sha256(source.read_bytes()).hexdigest(),
        'container_memory_limit_bytes': f['samples'][0]['memory_limit_bytes'],
        'before': {'complete_downloads': sum(bool(c['complete']) for c in before['downloads']),
                   'attempted_downloads': len(before['downloads']),
                   'oom_kills': before['oom_kill_delta']},
        'after': {'complete_downloads': len(checks), 'sha256_matches': len(checks),
                  'file_bytes_each': checks[0]['bytes_received'],
                  'oom_kills': f['oom_kill_delta'],
                  'sampled_max_memory_bytes': f['sampled_peak_memory_bytes'],
                  'transfer_seconds': [c['duration_seconds'] for c in checks]},
        'samples': [{'elapsed_seconds': s['elapsed_seconds'], 'memory_bytes': s['memory_bytes']}
                    for s in f['samples']],
        'measurement': 'Seven discrete memory samples during post-deployment operation checks. '
                       'Lines connect measured points; no continuous peak or time-to-repair claim.',
    }
    assert max(s['memory_bytes'] for s in data['samples']) == data['after']['sampled_max_memory_bytes']
    (OUT / 'case-data.json').write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def hero():
    w, h = 1600, 730
    im = background(w, h)
    points = array('f')
    points.frombytes((ROOT / 'frontend/public/portrait/particles.bin').read_bytes())
    colors = array('f')
    colors.frombytes((ROOT / 'frontend/public/portrait/colors.bin').read_bytes())
    if sys.byteorder != 'little':
        points.byteswap(); colors.byteswap()
    layer = Image.new('RGB', (w, h))
    draw = ImageDraw.Draw(layer)
    rng = random.Random(2710)
    for i in range(len(colors)//3):
        x, y, z = points[i*10:i*10+3]
        opacity = points[i*10+6]
        if opacity <= .01:
            continue
        px, py = 1135+x*135, 350-y*135
        if px < 780:
            continue
        rgb = colors[i*3:i*3+3]
        edge = max(0., min(1., (680-py)/130))
        edge = edge*edge*(3-2*edge)
        gain = (.68 + .32*rng.random()) * min(1., opacity**.5) * edge
        rgb = tuple(int(min(255, (max(0, c)**(1/2.2))*255*gain)) for c in rgb)
        r = .5 if i % 9 else .9
        draw.ellipse((px-r, py-r, px+r, py+r), fill=rgb)
    # Bloom on rendered particle data, not a retouched photo.
    from PIL import ImageChops, ImageEnhance
    glow = ImageEnhance.Brightness(layer.filter(ImageFilter.GaussianBlur(8))).enhance(.55)
    im = ImageChops.screen(im, glow)
    im = ImageChops.screen(im, layer)
    text(im, (85, 220), 'AURA', 226, brand=True)
    text(im, (104, 492), 'Server application diagnostics', 30, MUTED)
    im.save(MEDIA / 'aura.png', optimize=True)


def chart(data):
    im = background(1400, 760)
    text(im, (64, 44), 'Linkding', 37)
    text(im, (1335, 57), 'Download verification', 23, MUTED, anchor='ra')
    d = ImageDraw.Draw(im)
    # Folded file glyph is data context, not a decorative metrics card.
    d.polygon([(67,159),(116,159),(142,185),(142,249),(67,249)],fill=(22,49,61))
    d.line([(67,249),(67,159),(116,159),(142,185),(142,249),(67,249)], fill=(107,143,149),width=2)
    d.line([(116,159),(116,185),(142,185)],fill=(107,143,149),width=2)
    text(im, (170, 168), f"{data['after']['file_bytes_each']/1048576:g} MiB", 46)
    text(im, (172, 229), 'file size', 22, MUTED)
    text(im, (65, 324), 'Before the repair', 23, MUTED)
    text(im, (65, 362), f"{data['before']['complete_downloads']} of 2 downloads completed", 24, WARM)
    text(im, (65, 404), f"{data['before']['oom_kills']} workers killed by the memory limit", 20, MUTED)
    text(im, (65, 490), 'With the reviewed patch', 23, MUTED)
    text(im, (65, 528), f"{data['after']['complete_downloads']} of 2 downloads completed", 24, TEAL)
    text(im, (65, 570), 'Matching file hashes on both routes', 20, MUTED)
    text(im, (65, 605), f"{data['after']['oom_kills']} new out-of-memory kills", 20, MUTED)
    text(im, (565, 166), 'Memory after deployment', 25)
    left, right, top, bottom = 610, 1307, 267, 595
    ymax = 280
    def y(v): return bottom - v / ymax * (bottom-top)
    for value in [0, 128, 256]:
        yy = y(value)
        for xx in range(left, right, 14):
            d.line((xx, yy, xx+5, yy), fill=(47, 77, 87))
        text(im, (left-15, yy), str(value), 18, MUTED, anchor='rm')
    text(im, (565, 221), 'MiB', 18, MUTED)
    text(im, (right, y(256)-16), '256 MiB limit', 20, WARM, anchor='rb')
    pts = [(left + s['elapsed_seconds']/15*(right-left), y(s['memory_bytes']/1048576))
           for s in data['samples']]
    glow = Image.new('RGB', im.size)
    ImageDraw.Draw(glow).line(pts, fill=(25, 57, 60), width=5)
    from PIL import ImageChops
    im = ImageChops.screen(im, glow.filter(ImageFilter.GaussianBlur(10)))
    d = ImageDraw.Draw(im)
    d.line(pts, fill=TEAL, width=3)
    for px, py in pts:
        d.ellipse((px-5, py-5, px+5, py+5), fill=INK)
    px, py = max(zip(pts, data['samples']), key=lambda p:p[1]['memory_bytes'])[0]
    text(im, (px+15, py-8), f"{data['after']['sampled_max_memory_bytes']/1048576:.1f}", 24, INK, anchor='lb')
    for value in [0, 5, 10, 15]:
        text(im, (left+value/15*(right-left), bottom+16), f'{value}s', 18, MUTED, anchor='mt')
    text(im, (565, 667), '7 samples during the two download checks', 20, MUTED)
    text(im, (65, 695), '30 October 2026', 18, MUTED)
    im.save(MEDIA / 'verified-result.png', optimize=True)


def architecture():
    w, h = 1400, 700
    base = background(w, h)
    text(base, (64, 48), 'Inside AURA', 37)
    d = ImageDraw.Draw(base)
    # Parallel specialist paths feed synthesis; tests precede deployment approval.
    # All movement is conceptual, never a model timing or traffic measurement.
    edges = [((230,320),(510,y)) for y in (225,320,415)]
    edges += [((730,y),(895,320)) for y in (225,320,415)]
    edges += [((895,320),(1060,215)), ((1180,255),(1180,390))]
    def curve(a,b,t):
        ease=t*t*(3-2*t)
        return a[0]+(b[0]-a[0])*t, a[1]+(b[1]-a[1])*ease
    for a,b in edges:
        d.line([curve(a,b,i/80) for i in range(81)], fill=(45, 76, 86), width=1)
    for yy in [279,311,343]:
        d.rounded_rectangle((111,yy,229,yy+26),radius=4,outline=(72,107,119),fill=(20,46,58))
        d.ellipse((210,yy+10,216,yy+16),fill=TEAL)
    text(base,(170,410),'Your application',24,anchor='mt')
    text(base,(170,449),'Docker',20,MUTED,anchor='mt')
    text(base,(620,147),'Meta Open 1026',25,INK,anchor='mt')
    for yy,label in [(225,'Resources'),(320,'Logs and settings'),(415,'Source code')]:
        d.rounded_rectangle((510,yy-27,730,yy+27),radius=8,fill=(19,44,56),outline=(55,86,98))
        text(base,(620,yy),label,22,INK,anchor='mm')
    text(base,(620,467),'Baseten',20,MUTED,anchor='mt')
    d.ellipse((887,312,903,328),fill=TEAL)
    text(base,(895,360),'Diagnosis',20,MUTED,anchor='mt')
    for yy,title,sub in [(215,'Patch tests','Isolated Docker container'),(430,'Deployment','Real operation checks')]:
        d.rounded_rectangle((1060,yy-40,1320,yy+40),radius=8,fill=(19,44,56),outline=(72,107,119))
        text(base,(1190,yy),title,25,INK,anchor='mm')
        text(base,(1190,yy-77 if yy==215 else yy+55),sub,19,MUTED,anchor='mt')
    text(base,(1198,328),'Your approval',19,MUTED,anchor='lm')
    text(base,(65,585),'Interface',20,MUTED)
    text(base,(65,621),'React / Three.js / GLSL',23)
    text(base,(550,585),'Controller',20,MUTED)
    text(base,(550,621),'Python / FastAPI',23)
    text(base,(1040,585),'History and updates',20,MUTED)
    text(base,(1040,621),'SQLite / WebSocket',23)
    frames=[]
    rng=random.Random(2049)
    seeds=[(rng.random(), rng.uniform(-3,3),rng.uniform(.7,1.9)) for _ in range(13)]
    for frame in range(72):
        im=base.copy()
        glow=Image.new('RGB', im.size)
        gd=ImageDraw.Draw(glow)
        points=[]
        for a,b in edges:
            for phase,offset,r in seeds:
                t=(phase+frame/72)%1
                x,y=curve(a,b,t); y+=offset
                points.append((x,y,r))
                gd.ellipse((x-3*r,y-3*r,x+3*r,y+3*r),fill=(42,89,89))
        from PIL import ImageChops
        im=ImageChops.screen(im,glow.filter(ImageFilter.GaussianBlur(4)))
        d=ImageDraw.Draw(im)
        for x,y,r in points:
            d.ellipse((x-r,y-r,x+r,y+r),fill=TEAL)
        frames.append(im)
    # Fixed shared palette keeps static text stable across animation frames.
    palette=frames[0].quantize(colors=128)
    frames=[im.quantize(palette=palette,dither=Image.Dither.NONE) for im in frames]
    frames[0].save(MEDIA/'how-it-works.gif',save_all=True,append_images=frames[1:],duration=80,loop=0,optimize=False,disposal=1)
    base.save(MEDIA/'how-it-works.png',optimize=True)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--evidence',type=Path,help='Optional private verified post-deployment.json; exports only allowlisted fields.')
    args=parser.parse_args()
    MEDIA.mkdir(parents=True,exist_ok=True)
    if args.evidence:
        public_evidence(args.evidence)
    data=json.loads((OUT/'case-data.json').read_text())
    hero(); chart(data); architecture()
    print(json.dumps({p.name:p.stat().st_size for p in MEDIA.iterdir()},indent=2))


if __name__=='__main__':
    main()
