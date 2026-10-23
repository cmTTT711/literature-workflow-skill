"""Build distributable source wheels and privacy-safe online-install skill packages."""
from pathlib import Path
import hashlib, json, shutil, subprocess, sys, tomllib, zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION=tomllib.loads((ROOT/'pyproject.toml').read_text('utf-8'))['project']['version']
SKILL=ROOT/'skill/literature-search-download'
ROOT_FILES={'SKILL.md','START_HERE.md','VALIDATION.md','COMPONENTS.md','constraints-windows.txt','THIRD_PARTY_NOTICES.md','THIRD_PARTY.json'}
DIRS={'scripts','references','agents','assets','wheels','licenses'}

def sha(path):
    with path.open('rb') as stream:return hashlib.file_digest(stream,'sha256').hexdigest()

def write_json(path,data):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

def public_files():
    files=[]
    for path in SKILL.rglob('*'):
        if not path.is_file():continue
        rel=path.relative_to(SKILL)
        if rel.name=='FILES.sha256.json':continue
        if (len(rel.parts)==1 and rel.name in ROOT_FILES) or (len(rel.parts)>1 and rel.parts[0] in DIRS):
            if any(part.startswith('.') or part in {'__pycache__','profiles','cookies','jobs','downloads','results'} for part in rel.parts):raise ValueError('Private directory: '+str(rel))
            if path.suffix not in {'.md','.json','.txt','.ps1','.yaml','.whl'}:raise ValueError('Unexpected release file: '+str(rel))
            if rel.parts[0]=='assets' and path.suffix!='.json':raise ValueError('Browser binaries must be obtained by end users from upstream')
            files.append(path)
    return sorted(files)

def archive(path,files,base,prefix):
    with zipfile.ZipFile(path,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=6) as z:
        for file in files:z.write(file,prefix+'/'+file.relative_to(base).as_posix(),compress_type=zipfile.ZIP_STORED if file.suffix=='.whl' else zipfile.ZIP_DEFLATED)
    with zipfile.ZipFile(path) as z:
        if z.testzip():raise ValueError('Broken output archive')
    digest=sha(path)
    path.with_suffix('.zip.sha256').write_text(digest+'  '+path.name+'\n',encoding='ascii')
    return {'name':path.name,'bytes':path.stat().st_size,'sha256':digest}

def main():
    wheels=SKILL/'wheels';wheels.mkdir(parents=True,exist_ok=True)
    for component in [ROOT/'vendor/literature-gain',ROOT/'vendor/scanpdf',ROOT]:
        subprocess.run([sys.executable,'-m','pip','wheel','--no-index','--no-deps','--no-build-isolation','--disable-pip-version-check','--wheel-dir',str(wheels),str(component)],check=True)
    if len(list(wheels.glob('*.whl')))!=3:raise ValueError('Expected exactly three source component wheels')
    files=public_files()
    write_json(SKILL/'FILES.sha256.json',{p.relative_to(SKILL).as_posix():{'size':p.stat().st_size,'sha256':sha(p)} for p in files})
    files.append(SKILL/'FILES.sha256.json')
    plugin=ROOT/'codex-plugin/literature-workflow'
    target=plugin/'skills/literature-search-download'
    for p in files:
        dest=target/p.relative_to(SKILL);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    out=ROOT/'dist';out.mkdir(exist_ok=True)
    reports=[archive(out/f'literature-search-download-{VERSION}-win-x64.zip',files,SKILL,'literature-search-download')]
    plugin_files=[p for p in plugin.rglob('*') if p.is_file() and not any(x in {'.venv','__pycache__'} for x in p.relative_to(plugin).parts)]
    reports.append(archive(out/f'literature-workflow-codex-{VERSION}-win-x64.zip',plugin_files,plugin,'literature-workflow'))
    write_json(ROOT/'RELEASE_MANIFEST.json',{'version':VERSION,'repository':'https://github.com/cmTTT711/literature-workflow-skill','assets':reports,'bundled_browser_binaries':False,'third_party_dependency_binaries_redistributed':False})
    print(json.dumps({'version':VERSION,'assets':reports},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
