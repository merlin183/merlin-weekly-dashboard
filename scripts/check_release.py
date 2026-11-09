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
SCREENSHOT_MANIFEST='docs/screenshots/manifest.json'
SCREENSHOT_PATH=re.compile(r'docs/screenshots/(pm|admin|cs)/[a-z0-9][a-z0-9-]{0,79}\.jpg')
MAX_FILE_BYTES=2*1024*1024


def regular_file(root,name):
    """A reviewed file must not reach outside the tree through links or junctions."""
    path=root/name
    if not path.is_file() or not path.resolve().is_relative_to(root): return False
    return not any(part.is_symlink() or getattr(part,'is_junction',lambda:False)()
                   for part in [path,*path.parents] if part!=root and root in part.parents)


def screenshot_entries(root,paths):
    """Read an explicitly published review manifest; it is also scanned as ordinary text."""
    if SCREENSHOT_MANIFEST not in paths: return {},[]
    error=[{'file':SCREENSHOT_MANIFEST,'rule':'screenshot-manifest'}]
    try:
        if not regular_file(root,SCREENSHOT_MANIFEST): return {},error
        raw=(root/SCREENSHOT_MANIFEST).read_bytes()
        if len(raw)>MAX_FILE_BYTES: return {},error
        data=json.loads(raw.decode('utf-8-sig'))
        if (not isinstance(data,dict) or type(data.get('schema_version')) is not int
                or data['schema_version']!=1
                or not re.fullmatch(r'[0-9a-f]{40}',data.get('source_commit',''))
                or not isinstance(data.get('images'),list) or not 1<=len(data['images'])<=1000):
            return {},error
        entries={}
        for entry in data['images']:
            if not isinstance(entry,dict): return {},error
            name=entry.get('path','')
            match=SCREENSHOT_PATH.fullmatch(name) if isinstance(name,str) else None
            if (not match or name in entries or name not in paths
                    or entry.get('role')!=match[1] or entry.get('reviewed_synthetic') is not True
                    or not isinstance(entry.get('title'),str) or not 1<=len(entry['title'])<=160
                    or not re.fullmatch(r'[0-9a-f]{64}',entry.get('sha256',''))
                    or any(type(entry.get(key)) is not int or not 1<=entry[key]<=4096
                           for key in ('width','height'))):
                return {},error
            entries[name]=entry
        return entries,[]
    except (OSError,UnicodeDecodeError,ValueError,TypeError):
        return {},error


def jpeg_dimensions(raw):
    """Parse a complete baseline/progressive screenshot JPEG without loading image libraries.

    Only a thumbnail-free JFIF APP0 segment is permitted. EXIF, XMP, comments and
    other application metadata are rejected rather than silently redistributed.
    This checks structure, not visual privacy: the synthetic review and exact hash
    in the manifest remain required for every individual screenshot.
    """
    if not raw.startswith(b'\xff\xd8') or not raw.endswith(b'\xff\xd9'):
        raise ValueError('JPEG boundary')
    position=2;frame=None;components={};quantization=set();huffman=set();scans=0
    while position<len(raw):
        if raw[position]!=255: raise ValueError('JPEG marker')
        while position<len(raw) and raw[position]==255: position+=1
        if position>=len(raw): raise ValueError('JPEG marker truncated')
        marker=raw[position];position+=1
        if marker==0xd9:
            if position!=len(raw) or frame is None or not scans: raise ValueError('JPEG end')
            return frame[1],frame[2]
        if marker not in {0xe0,0xdb,0xc0,0xc2,0xc4,0xdd,0xda}:
            raise ValueError('JPEG unreviewed metadata or marker')
        if position+2>len(raw): raise ValueError('JPEG segment truncated')
        length=int.from_bytes(raw[position:position+2],'big')
        if length<2 or position+length>len(raw): raise ValueError('JPEG segment length')
        payload=raw[position+2:position+length];position+=length
        if marker==0xe0:
            if (len(payload)!=14 or payload[:5]!=b'JFIF\x00' or payload[5]!=1
                    or payload[6]>2 or payload[7]>2 or payload[12:]!=b'\x00\x00'):
                raise ValueError('JPEG application metadata')
        elif marker==0xdb:
            offset=0
            while offset<len(payload):
                table=payload[offset];offset+=1
                if table>>4>1 or table&15>3: raise ValueError('JPEG quantization')
                offset+=64*(1+(table>>4));quantization.add(table&15)
            if not payload or offset!=len(payload): raise ValueError('JPEG quantization length')
        elif marker in {0xc0,0xc2}:
            if frame is not None or len(payload)<6 or payload[0]!=8: raise ValueError('JPEG frame')
            height=int.from_bytes(payload[1:3],'big');width=int.from_bytes(payload[3:5],'big');count=payload[5]
            if not 1<=width<=4096 or not 1<=height<=4096 or count not in (1,3) or len(payload)!=6+3*count:
                raise ValueError('JPEG frame dimensions')
            for offset in range(6,len(payload),3):
                identifier,sampling,table=payload[offset:offset+3]
                if identifier in components or not 1<=sampling>>4<=4 or not 1<=sampling&15<=4 or table>3:
                    raise ValueError('JPEG frame component')
                components[identifier]=table
            frame=(marker,width,height)
        elif marker==0xc4:
            offset=0
            while offset<len(payload):
                if offset+17>len(payload): raise ValueError('JPEG Huffman header')
                table=payload[offset];count=sum(payload[offset+1:offset+17]);offset+=17
                if table>>4>1 or table&15>3 or not 1<=count<=256 or offset+count>len(payload):
                    raise ValueError('JPEG Huffman table')
                offset+=count;huffman.add((table>>4,table&15))
            if not payload: raise ValueError('JPEG Huffman empty')
        elif marker==0xdd:
            if len(payload)!=2: raise ValueError('JPEG restart interval')
        elif marker==0xda:
            if frame is None or not payload: raise ValueError('JPEG scan before frame')
            count=payload[0]
            if not 1<=count<=len(components) or len(payload)!=1+2*count+3: raise ValueError('JPEG scan length')
            identifiers=set()
            for offset in range(1,1+2*count,2):
                identifier,tables=payload[offset:offset+2]
                if identifier not in components or identifier in identifiers or components[identifier] not in quantization:
                    raise ValueError('JPEG scan component')
                identifiers.add(identifier)
                if tables>>4>3 or tables&15>3: raise ValueError('JPEG scan Huffman selector')
                if frame[0]==0xc0 and ((0,tables>>4) not in huffman or (1,tables&15) not in huffman):
                    raise ValueError('JPEG scan missing Huffman')
            spectral_start,spectral_end,approximation=payload[-3:]
            if (not 0<=spectral_start<=spectral_end<=63 or approximation>>4>13 or approximation&15>13
                    or frame[0]==0xc0 and payload[-3:]!=b'\x00\x3f\x00'):
                raise ValueError('JPEG scan parameters')
            scans+=1;entropy_bytes=0
            while position<len(raw):
                if raw[position]!=255:
                    position+=1;entropy_bytes+=1;continue
                start=position
                while position<len(raw) and raw[position]==255: position+=1
                if position>=len(raw): raise ValueError('JPEG entropy truncated')
                following=raw[position]
                if following==0 or 0xd0<=following<=0xd7:
                    position+=1;entropy_bytes+=1;continue
                position=start;break
            if not entropy_bytes: raise ValueError('JPEG empty scan')
    raise ValueError('JPEG incomplete')

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
    root=Path(root).resolve();paths=list(paths);screenshots,findings=screenshot_entries(root,paths)
    manifest=[];seen=set()
    for name in sorted(paths):
        relative=Path(name)
        bad=relative.is_absolute() or bool(relative.drive) or '\\' in name or '..' in relative.parts or any(part.lower()=='.git' for part in relative.parts)
        key=name.casefold()
        if bad or key in seen:
            findings.append({'file':'[unsafe path]','rule':'path'});continue
        seen.add(key)
        path=root/relative
        if any(part in PRIVATE or part in RUNTIME for part in relative.parts):
            findings.append({'file':name,'rule':'private-folder'});continue
        if path.name.startswith('.env') and path.name not in ALLOWED_NAMES:
            findings.append({'file':name,'rule':'environment-file'});continue
        if not regular_file(root,name):
            findings.append({'file':name,'rule':'nonregular-file'});continue
        if path.suffix=='.jpg' and SCREENSHOT_PATH.fullmatch(name):
            entry=screenshots.get(name)
            if entry is None:
                findings.append({'file':name,'rule':'unreviewed-screenshot'});continue
            raw=path.read_bytes()
            try:
                if len(raw)>MAX_FILE_BYTES or hashlib.sha256(raw).hexdigest()!=entry['sha256']:
                    raise ValueError('Screenshot hash or size')
                if jpeg_dimensions(raw)!=(entry['width'],entry['height']): raise ValueError('Screenshot dimensions')
            except ValueError:
                findings.append({'file':name,'rule':'screenshot-content'});continue
            manifest.append({'path':name,'sha256':entry['sha256']});continue
        if path.suffix not in ALLOWED_SUFFIXES and path.name not in ALLOWED_NAMES:
            findings.append({'file':name,'rule':'unreviewed-file-type'});continue
        raw=path.read_bytes()
        if len(raw)>MAX_FILE_BYTES:
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
