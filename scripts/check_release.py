"""Public release gate. Reports rule IDs and locations, never matched values."""
from pathlib import Path
import hashlib,json,re,subprocess,sys

ROOT=Path(__file__).resolve().parents[1]
RUNTIME={'.git','.venv','venv','node_modules','__pycache__','.pytest_cache','.wrangler','data','.local','dist','coverage'}
PRIVATE={'.local','preserved','data','uploads','backups','exports','outputs','artifacts','.aws','.codex','.agents','.claude','.verification'}
ALLOWED_SUFFIXES={'.py','.js','.mjs','.cjs','.ts','.html','.css','.json','.jsonc','.md','.txt','.yml','.yaml','.sql','.toml','.lock'}
ALLOWED_NAMES={'LICENSE','Dockerfile','Procfile','.gitignore','.gitattributes','.dockerignore','.env.example','.env.test.example'}
RULES={
 'credential-token':re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{32,}|github_pat_[A-Za-z0-9_]{40,}|sk-(?:proj-|svcacct-|ant-)?[A-Za-z0-9_-]{40,}|sb_secret_[A-Za-z0-9_-]{30,}|xox[baprsce]-[A-Za-z0-9-]{20,}|xapp-[A-Za-z0-9-]{20,}|whsec_[A-Za-z0-9]{24,}|AIza[A-Za-z0-9_-]{35}|(?:AKIA|ASIA)[A-Z0-9]{16})\b'),
 'private-key':re.compile(r'-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{100,}'),
 'signed-token':re.compile(r'\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b'),
 'local-user-path':re.compile(r'[A-Za-z]:[\\/]+Users[\\/]+[^\s\'"<>]+',re.I),
 'private-network':re.compile(r'https?://(?:192\.168\.\d+\.\d+|10\.\d+\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)(?=[:/\s\'"<>]|$)',re.I),
}
EMAIL=re.compile(r'[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})')

def selected_paths(root):
    if (root/'.git').exists():
        result=subprocess.run(['git','-c',f'safe.directory={root.as_posix()}','-C',str(root),'ls-files','-z'],capture_output=True,check=True)
        return [name for name in result.stdout.decode().split('\0') if name]
    names=[]
    def visit(folder):
        for path in folder.iterdir():
            if path.is_symlink(): names.append(path.relative_to(root).as_posix())
            elif path.is_dir():
                if path.name not in RUNTIME: visit(path)
            elif not (path.name.startswith('.env') and path.name not in ALLOWED_NAMES): names.append(path.relative_to(root).as_posix())
    visit(root)
    return names

def audit(root,paths):
    root=Path(root).resolve();findings=[];manifest=[];seen=set()
    for name in sorted(paths):
        relative=Path(name)
        bad=relative.is_absolute() or '\\' in name or '..' in relative.parts or any(part.lower()=='.git' for part in relative.parts)
        key=name.casefold()
        if bad or key in seen:
            findings.append({'file':'[unsafe path]','rule':'path'});continue
        seen.add(key)
        path=root/relative
        if any(part in PRIVATE or part in RUNTIME for part in relative.parts):
            findings.append({'file':name,'rule':'private-folder'});continue
        if path.name.startswith('.env') and path.name not in ALLOWED_NAMES:
            findings.append({'file':name,'rule':'environment-file'});continue
        if path.is_symlink() or not path.is_file():
            findings.append({'file':name,'rule':'nonregular-file'});continue
        if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
            findings.append({'file':name,'rule':'unreviewed-file-type'});continue
        raw=path.read_bytes()
        if len(raw)>2*1024*1024:
            findings.append({'file':name,'rule':'oversize'});continue
        try: text=raw.decode('utf-8-sig')
        except UnicodeDecodeError:
            findings.append({'file':name,'rule':'binary'});continue
        if '\x00' in text:
            findings.append({'file':name,'rule':'binary'});continue
        for rule,pattern in RULES.items():
            for match in pattern.finditer(text): findings.append({'file':name,'line':text[:match.start()].count('\n')+1,'rule':rule})
        for match in EMAIL.finditer(text):
            if not re.search(r'(?:^|\.)example\.(?:com|org|net)$|\.(?:test|invalid)$',match[1],re.I):
                findings.append({'file':name,'line':text[:match.start()].count('\n')+1,'rule':'nonexample-email'})
        manifest.append({'path':name,'sha256':hashlib.sha256(raw).hexdigest()})
    digest=hashlib.sha256(json.dumps(manifest,sort_keys=True).encode()).hexdigest()
    return {'pass':not findings and bool(manifest),'files':len(manifest),'digest':digest,'findings':findings}

if __name__=='__main__':
    result=audit(ROOT,selected_paths(ROOT))
    print(json.dumps(result,ensure_ascii=False,indent=2))
    raise SystemExit(0 if result['pass'] else 1)
