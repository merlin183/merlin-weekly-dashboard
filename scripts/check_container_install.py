"""Exercise a disposable CI Compose project; never targets an installed workspace."""
from pathlib import Path
import http.client,json,os,secrets,subprocess,time,urllib.request,urllib.error

ROOT=Path(__file__).resolve().parents[1]
def wait_for_health(client,attempts=60,pause=time.sleep):
    for attempt in range(attempts):
        try:
            with client.open('http://127.0.0.1:8080/api/health',timeout=2) as response:
                if json.load(response).get('ok'): return
        except (OSError,http.client.HTTPException,json.JSONDecodeError): pass
        if attempt+1<attempts: pause(1)
    raise SystemExit('Container application did not become healthy.')

def main():
    if os.environ.get('GITHUB_ACTIONS')!='true' or not os.environ.get('COMPOSE_PROJECT_NAME','').startswith('merlin-weekly-ci-'):
        raise SystemExit('This verifier requires an isolated GitHub Actions Compose project.')
    if os.environ.get('DOCKER_HOST') or os.environ.get('DOCKER_CONTEXT'):
        raise SystemExit('Remote Docker contexts are not allowed for installation verification.')
    client=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    wait_for_health(client)
    password=secrets.token_urlsafe(32)
    print('::add-mask::'+password)
    args=['docker','compose','--profile','tools','run','--rm','-T','bootstrap','--name','CI Administrator','--email','admin@example.test','--password-stdin']
    first=subprocess.run(args,cwd=ROOT,input=(password+'\n').encode(),capture_output=True,timeout=90)
    if first.returncode!=0: raise SystemExit('Initial administrator command failed inside the container.')
    again=subprocess.run(args,cwd=ROOT,input=(password+'\n').encode(),capture_output=True,timeout=90)
    if again.returncode==0 or b'Bootstrap refused: this installation already has users' not in again.stderr:
        raise SystemExit('Existing installation did not explicitly refuse repeated bootstrap.')
    body=json.dumps({'email':'admin@example.test','password':password}).encode()
    request=urllib.request.Request('http://127.0.0.1:8080/api/auth/login-user',data=body,headers={'Content-Type':'application/json'})
    with client.open(request,timeout=15) as response: login=json.load(response)
    if not login.get('token'): raise SystemExit('New administrator could not log in.')
    request=urllib.request.Request('http://127.0.0.1:8080/api/auth/me',headers={'Authorization':'Bearer '+login['token']})
    with client.open(request,timeout=15) as response: me=json.load(response)
    if me.get('user',{}).get('role')!='admin': raise SystemExit('Administrator role was not preserved.')
    print('Container startup, explicit administrator creation, existing-owner refusal and login passed.')

if __name__=='__main__': main()
