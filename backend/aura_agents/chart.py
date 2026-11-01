"""Codex-owned measured chart for Vision. No provider calls or synthetic data."""
from io import BytesIO
import math
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont


def render_measured_chart(evidence) -> bytes:
    rows = list(evidence.sampled_telemetry or evidence.recent_measurements)[-120:]
    rows = [r for r in rows if isinstance(r.get('monotonic'), (int, float))
            and math.isfinite(r['monotonic'])]
    if len(rows) < 2:
        raise ValueError('At least two timed measurements required')
    start, end = rows[0]['monotonic'], rows[-1]['monotonic']
    if end <= start:
        raise ValueError('Measurement times must increase')
    image = Image.new('RGB', (1000, 600), '#08131b')
    draw = ImageDraw.Draw(image)
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    def font(size):
        return ImageFont.truetype(str(font_path), size) if font_path.exists() else ImageFont.load_default(size=size)
    text, small, title = font(19), font(15), font(27)
    draw.text((40, 25), 'AURA / Measured memory', fill='#d8e6e5', font=title)
    draw.text((40, 66), 'Synthetic workload · Actual worker measurements · Relative time', fill='#8ba4af', font=small)
    mib = 1024 * 1024
    series = [('rss_bytes', '#8de5d7', 'Process RSS'), ('retained_cache_bytes', '#edb46e', 'Retained cache')]
    limits = [(evidence.rss_threshold_bytes, 'RSS detection threshold', '#78978d'),
              (evidence.ceiling_bytes, 'Cache ceiling', '#b58b68')]
    numbers = [r[k]/mib for r in rows for k,_,_ in series
               if isinstance(r.get(k), (int,float)) and math.isfinite(r[k]) and r[k] >= 0]
    numbers += [v/mib for v,_,_ in limits if v is not None]
    if not numbers:
        raise ValueError('No measured memory values')
    upper = max(32, math.ceil(max(numbers)/32)*32)
    left, top, right, bottom = 90, 145, 950, 475
    def x(t): return left+(t-start)/(end-start)*(right-left)
    def y(value): return bottom-value/mib/upper*(bottom-top)
    for i in range(5):
        value=upper*i/4
        yy=bottom-i/4*(bottom-top)
        draw.line((left,yy,right,yy),fill='#23323c')
        draw.text((32,yy-10),f'{value:g}',fill='#9db0b7',font=small)
    draw.text((34,116),'MiB',fill='#9db0b7',font=small)
    for value,label,color in limits:
        if value is None: continue
        yy=y(value)
        for xx in range(left,right,14): draw.line((xx,yy,min(xx+6,right),yy),fill=color)
        draw.text((left+6,yy-20),f'{label}: {value/mib:g} MiB',fill=color,font=small)
    for key,color,label in series:
        previous=None
        for row in rows:
            value=row.get(key)
            fresh=row.get('rss_fresh',True) if key=='rss_bytes' else row.get('status_fresh',True)
            if not fresh or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
                previous=None
                continue
            point=(x(row['monotonic']),y(value))
            if previous is not None: draw.line((previous,point),fill=color,width=3)
            draw.ellipse((point[0]-2,point[1]-2,point[0]+2,point[1]+2),fill=color)
            previous=point
    for i in range(5):
        seconds=(end-start)*i/4
        draw.text((left+i/4*(right-left)-10,bottom+12),f'{seconds:.1f}s',fill='#9db0b7',font=small)
    for i,(_,color,label) in enumerate(series):
        xx=left+i*240
        draw.line((xx,112,xx+25,112),fill=color,width=3)
        draw.text((xx+35,99),label,fill=color,font=text)
    draw.text((40,540),f'{len(rows)} samples · Missing/stale values are gaps · Window {end-start:.1f}s',fill='#9db0b7',font=small)
    output=BytesIO()
    image.save(output,format='PNG')
    return output.getvalue()
