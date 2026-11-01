"""AURA README art. Reuses portrait buffers; draws diagrams from documented results.

Palette: water #091e29, pearl #e1efed, mist #97b2b5, teal #94d5c8,
coral #d9ac94. Georgia for the wordmark; Segoe UI for information.
One portrait-led cover, one measured outcome, two distinct execution paths.
No network calls, private log exports or application changes.
"""
from pathlib import Path
import math, random
from PIL import Image, ImageDraw, ImageFilter, ImageChops
import render as art

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'docs/readme'
OUT.mkdir(parents=True,exist_ok=True)

def label(im,xy,s,size=25,color=art.INK,anchor=None,light=False):
    art.text(im,xy,s,size,color,light,anchor)

def hero():
    # A new code-rendered portrait, not a retouched source photograph.
    old_media=art.MEDIA
    art.MEDIA=OUT
    art.hero()
    generated=OUT/'aura.png'
    im=Image.open(generated).convert('RGB')
    # Regenerate the lower-left background region of the code-drawn banner.
    bg=art.background(*im.size)
    im.paste(bg.crop((0,475,780,730)),(0,475))
    label(im,(105,490),'Application recovery',35,art.MUTED)
    label(im,(158,624),'Watch the film',23,art.INK)
    label(im,(353,624),'2 min',23,art.MUTED)
    d=ImageDraw.Draw(im)
    d.ellipse((106,625,139,658),outline=art.MUTED,width=1)
    d.polygon([(119,634),(119,649),(130,642)],fill=art.INK)
    im.save(OUT/'hero.png',optimize=True)
    art.MEDIA=old_media

def recovery():
    im=art.background(1400,570);d=ImageDraw.Draw(im)
    label(im,(66,45),'Linkding',36)
    label(im,(66,100),'Configuration recovery',23,art.MUTED)
    label(im,(63,214),'9091',104,art.WARM,light=True)
    d.line([(355,278),(445,278)],fill=art.MUTED,width=2)
    d.line([(434,268),(445,278),(434,288)],fill=art.MUTED,width=2)
    label(im,(480,214),'9090',104,art.TEAL,light=True)
    label(im,(68,349),'Saved settings restored',28)
    label(im,(68,393),'Health and bookmarks requests return HTTP 200',23,art.MUTED)
    label(im,(920,180),'93.3',125,art.INK,light=True)
    label(im,(1190,265),'s',36,art.MUTED)
    label(im,(927,349),'Fault to verified recovery',23,art.MUTED)
    d.line((68,471,1330,471),fill=(42,72,82),width=1)
    label(im,(68,501),'3 model calls',23,art.TEAL)
    label(im,(470,501),'18 complete observations',23,art.TEAL)
    label(im,(1330,501),'31 Oct 2026 / one recorded run',21,art.MUTED,anchor='ra')
    im.save(OUT/'recovery.png',optimize=True)

def architecture():
    im=art.background(1400,770);d=ImageDraw.Draw(im)
    label(im,(65,50),'Observations and actions',35)
    def curve(a,b,t):
        s=t*t*(3-2*t)
        return a[0]+(b[0]-a[0])*t,a[1]+(b[1]-a[1])*s
    edges=[((255,348),(425,y)) for y in (250,348,446)]
    edges += [((670,y),(805,348)) for y in (250,348,446)]
    edges += [((805,348),(940,y)) for y in (242,450)]
    for a,b in edges:
        d.line([curve(a,b,j/90) for j in range(91)],fill=(48,80,90),width=1)
    for y in (306,337,368):
        d.rounded_rectangle((115,y,255,y+24),radius=4,fill=(19,44,56),outline=(69,102,113))
        d.ellipse((236,y+9,242,y+15),fill=art.TEAL)
    label(im,(185,439),'Your app',27,anchor='mt')
    label(im,(185,482),'Docker',22,art.MUTED,anchor='mt')
    label(im,(547,138),'Meta Open 1026',27,anchor='mt')
    label(im,(547,181),'Relevant specialists',20,art.MUTED,anchor='mt')
    for y,s in [(250,'Resources'),(348,'Logs and settings'),(446,'Source code')]:
        d.rounded_rectangle((425,y-29,670,y+29),radius=10,fill=(20,45,57),outline=(49,79,90))
        label(im,(547,y),s,23,anchor='mm')
    label(im,(547,513),'Baseten',21,art.MUTED,anchor='mt')
    d.ellipse((797,340,813,356),fill=art.TEAL)
    label(im,(803,392),'Diagnosis',21,art.MUTED,anchor='mt')
    for y,h in [(168,154),(382,186)]:
        d.rounded_rectangle((940,y,1335,y+h),radius=18,fill=(18,43,55),outline=(55,87,98))
    label(im,(967,190),'Restore saved settings',28)
    label(im,(969,239),'Within the allowed policy',21,art.MUTED)
    label(im,(969,279),'Verify real requests',22,art.TEAL)
    label(im,(967,405),'Prepare a code change',28)
    label(im,(969,455),'Test, review and approve',22,art.MUTED)
    label(im,(969,511),'Deploy and verify the operation',21,art.TEAL)
    d.line((66,617,1335,617),fill=(39,68,78),width=1)
    for x,title,value in [(66,'Interface','React / Three.js / GLSL'),(545,'Controller','Python / FastAPI'),(1010,'History and updates','SQLite / WebSocket')]:
        label(im,(x,647),title,21,art.MUTED)
        label(im,(x,687),value,24)
    frames=[];rng=random.Random(1062)
    seeds=[(rng.random(),rng.uniform(-2,2),rng.uniform(.7,1.6)) for _ in range(8)]
    for f in range(60):
        frame=im.copy();glow=Image.new('RGB',im.size);gd=ImageDraw.Draw(glow);points=[]
        for a,b in edges:
            for phase,off,r in seeds:
                x,y=curve(a,b,(phase+f/60)%1);y+=off
                points.append((x,y,r));gd.ellipse((x-4*r,y-4*r,x+4*r,y+4*r),fill=(39,75,77))
        frame=ImageChops.screen(frame,glow.filter(ImageFilter.GaussianBlur(3)))
        fd=ImageDraw.Draw(frame)
        for x,y,r in points:fd.ellipse((x-r,y-r,x+r,y+r),fill=art.TEAL)
        frames.append(frame)
    palette=frames[0].quantize(colors=128)
    indexed=[f.quantize(palette=palette,dither=Image.Dither.NONE) for f in frames]
    indexed[0].save(OUT/'architecture.gif',save_all=True,append_images=indexed[1:],duration=80,loop=0,optimize=False,disposal=1)
    im.save(OUT/'architecture.png',optimize=True)

if __name__=='__main__':
    hero();recovery();architecture()
    for p in OUT.iterdir():print(p.name,p.stat().st_size)
