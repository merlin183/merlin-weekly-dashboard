"""Create installer-owned local settings. Never overwrite an existing environment."""
from pathlib import Path
import secrets
import os

def create_environment(root: Path) -> Path:
    target=root/'.env'
    password=secrets.token_urlsafe(32)
    template=(root/'.env.example').read_text(encoding='utf-8')
    values={
        'POSTGRES_PASSWORD': password,
        'DATABASE_URL': f'postgresql://weekly:{password}@127.0.0.1:5432/weekly',
        'JWT_SECRET': secrets.token_urlsafe(48),
    }
    lines=[]
    for line in template.splitlines():
        name=line.split('=',1)[0]
        lines.append(f'{name}={values[name]}' if name in values else line)
    descriptor=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
        stream.write('\n'.join(lines)+'\n')
    return target

if __name__=='__main__':
    try:
        create_environment(Path(__file__).resolve().parents[1])
    except FileExistsError:
        raise SystemExit('Existing .env preserved. Edit your installer-owned settings explicitly.')
    print('Created local .env with generated credentials. Values are not printed; do not commit this file.')
