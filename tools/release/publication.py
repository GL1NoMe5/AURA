"""Prepare the public source snapshot from explicit files, without publishing it.

Reuses the release scanner and path guards. Extra public Markdown/HTML links are
normalized against the known inventory, including legitimate parent links.
Nothing under .local, raw portrait sources, video projects or keys is exported.
"""
from __future__ import annotations
from pathlib import Path
import hashlib, json, posixpath, re, zipfile
from urllib.parse import unquote, urlsplit
import package as core

ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'.local/publication/20261102'
EXTRA=(
    'README.md','.gitignore','docs/OPS_API.md','docs/index.html','docs/.nojekyll',
    'docs/readme/hero.png','docs/readme/recovery.png','docs/readme/architecture.gif',
    'docs/readme/architecture.png','docs/readme/aura.en.vtt',
    'docs/presentation/CASE_STUDY.md','docs/presentation/case-data.json',
    'docs/presentation/CREDITS.md','docs/presentation/media/aura.png',
    'docs/presentation/media/verified-result.png',
    'docs/presentation/media/how-it-works.gif','docs/presentation/media/how-it-works.png',
    'tools/presentation/render.py','tools/presentation/readme.py',
    'tools/release/publication.py',
)

def main():
    OUT.mkdir(parents=True,exist_ok=True)
    members=core.collect_members(ROOT,allow_files=core.ALLOW_FILES+EXTRA,mapping={})
    scan=core.scan_members(members)
    broken=[]
    for name,path in members.items():
        if path.suffix.lower() not in ('.md','.html'):continue
        text=path.read_text(encoding='utf-8')
        refs=re.findall(r'\]\(([^)]+)\)',text)
        refs += re.findall(r'(?:src|href|poster)=[\"\x27]([^\"\x27]+)',text)
        for raw in refs:
            raw=raw.strip().strip('<>')
            u=urlsplit(raw)
            if u.scheme or raw.startswith(('#','//')) or not u.path:continue
            if u.path.startswith('/'):
                # Vite's source HTML resolves /src against the frontend root.
                base='frontend' if name.startswith('frontend/') else ''
                resolved=posixpath.normpath(posixpath.join(base,unquote(u.path).lstrip('/')))
            else:
                resolved=posixpath.normpath(posixpath.join(posixpath.dirname(name),unquote(u.path)))
            if resolved not in members:broken.append({'file':name,'link':raw,'resolved':resolved})
    report={'files':len(members),'scanned_text_files':scan['scanned_text_files'],
            'secret_findings':scan['findings'],'reviewed_exceptions':scan['suppressed'],
            'binary_assets':scan['unscanned_files'],'broken_links':broken}
    (OUT/'checks.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    if scan['findings'] or broken:
        print(json.dumps({'secret_findings':scan['findings'],'broken_links':broken},indent=2))
        raise SystemExit(1)
    manifest=[]
    for name,p in sorted(members.items()):
        manifest.append({'path':name,'size':p.stat().st_size,'sha256':hashlib.sha256(p.read_bytes()).hexdigest()})
    (OUT/'manifest.json').write_text(json.dumps(manifest,indent=2),encoding='utf-8')
    (OUT/'stage-files.txt').write_text('\n'.join(sorted(members))+'\n',encoding='utf-8')
    with zipfile.ZipFile(OUT/'aura-source.zip','w',zipfile.ZIP_DEFLATED) as z:
        for name,p in sorted(members.items()):z.write(p,name)
    print(json.dumps({'files':len(members),'text_scanned':scan['scanned_text_files'],
                      'secret_findings':0,'broken_links':0,
                      'archive_bytes':(OUT/'aura-source.zip').stat().st_size},indent=2))

if __name__=='__main__':main()
