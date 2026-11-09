"""Portable installation and release checks without any database or network."""
from pathlib import Path
import hashlib,importlib.util,json,subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
gate=module('release_gate','scripts/check_release.py')
setup=module('environment_setup','scripts/setup_env.py')
container_check=module('container_install_check','scripts/check_container_install.py')


def tiny_synthetic_jpeg(width=1,height=1,extra=b''):
    """One baseline grayscale block; only synthetic pixels and thumbnail-free JFIF."""
    def segment(marker,payload): return b'\xff'+bytes([marker])+(len(payload)+2).to_bytes(2,'big')+payload
    counts=b'\x01'+b'\x00'*15
    return (b'\xff\xd8'+extra
            +segment(0xe0,b'JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00')
            +segment(0xdb,b'\x00'+b'\x01'*64)
            +segment(0xc0,b'\x08'+height.to_bytes(2,'big')+width.to_bytes(2,'big')+b'\x01\x01\x11\x00')
            +segment(0xc4,b'\x00'+counts+b'\x00'+b'\x10'+counts+b'\x00')
            +segment(0xda,b'\x01\x01\x00\x00\x3f\x00')+b'\x3f\xff\xd9')


def write_screenshot_review(root,raw=None,**changes):
    name='docs/screenshots/pm/01-home.jpg'
    raw=tiny_synthetic_jpeg() if raw is None else raw
    image=root/name;image.parent.mkdir(parents=True,exist_ok=True);image.write_bytes(raw)
    entry={'path':name,'role':'pm','title':'Synthetic PM screen','sha256':hashlib.sha256(raw).hexdigest(),
           'width':1,'height':1,'reviewed_synthetic':True,**changes}
    review={'schema_version':1,'source_commit':'a'*40,'images':[entry]}
    (root/gate.SCREENSHOT_MANIFEST).write_text(json.dumps(review),encoding='utf-8')
    return [name,gate.SCREENSHOT_MANIFEST]


class PublicReleaseTests(unittest.TestCase):
    def test_only_individually_reviewed_synthetic_jpegs_are_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);paths=write_screenshot_review(root)
            result=gate.audit(root,paths)
            self.assertTrue(result['pass']);self.assertEqual(result['files'],2)
            self.assertEqual(gate.jpeg_dimensions((root/paths[0]).read_bytes()),(1,1))
            self.assertFalse(gate.audit(root,[paths[0]])['pass'],'Review manifest must also be published')
            outside=root/'docs/other.jpg';outside.write_bytes(tiny_synthetic_jpeg())
            self.assertFalse(gate.audit(root,['docs/other.jpg'])['pass'])

    def test_screenshot_review_binds_role_hash_dimensions_and_explicit_review(self):
        for changes in ({'reviewed_synthetic':False},{'reviewed_synthetic':1},{'role':'admin'},
                        {'sha256':'0'*64},{'width':2},{'height':4097},{'width':True}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);paths=write_screenshot_review(root,**changes)
                self.assertFalse(gate.audit(root,paths)['pass'])

    def test_screenshot_metadata_and_broken_or_trailing_jpeg_are_rejected(self):
        def segment(marker,payload): return b'\xff'+bytes([marker])+(len(payload)+2).to_bytes(2,'big')+payload
        for raw in (tiny_synthetic_jpeg(extra=segment(0xe1,b'Exif\x00\x00synthetic')),
                    tiny_synthetic_jpeg(extra=segment(0xe1,b'http://ns.adobe.com/xap/1.0/\x00synthetic')),
                    tiny_synthetic_jpeg(extra=segment(0xfe,b'synthetic comment')),
                    tiny_synthetic_jpeg(extra=segment(0xee,b'Adobe')),
                    tiny_synthetic_jpeg()[:-1],tiny_synthetic_jpeg()+b'trailing',
                    b'\xff\xd8\xff\xd9',tiny_synthetic_jpeg().replace(b'\xff\xdb\x00C',b'\xff\xdb\xff\xff',1)):
            with self.subTest(raw_length=len(raw)),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);paths=write_screenshot_review(root,raw)
                self.assertFalse(gate.audit(root,paths)['pass'])

    def test_screenshot_manifest_cannot_allow_missing_or_unsafe_paths(self):
        for changes in ({'path':'docs/screenshots/pm/../other.jpg'},
                        {'path':'docs/screenshots/pm/home.jpg'}, {'path':'data/private.jpg'}):
            with self.subTest(changes=changes),tempfile.TemporaryDirectory() as directory:
                root=Path(directory);paths=write_screenshot_review(root,**changes)
                self.assertFalse(gate.audit(root,paths)['pass'])
        with tempfile.TemporaryDirectory() as directory:
            address='private'+'@'+'organization.example'
            root=Path(directory);paths=write_screenshot_review(root,title='Synthetic contact '+address)
            result=gate.audit(root,paths)
            self.assertFalse(result['pass'])
            self.assertTrue(any(item['rule']=='nonexample-email' for item in result['findings']))

    def test_screenshot_symlinks_are_not_reviewed_files(self):
        with tempfile.TemporaryDirectory() as directory,tempfile.TemporaryDirectory() as outside:
            root=Path(directory);paths=write_screenshot_review(root);image=root/paths[0]
            target=Path(outside)/'screen.jpg';target.write_bytes(image.read_bytes());image.unlink()
            try: image.symlink_to(target)
            except (OSError,NotImplementedError): self.skipTest('Symlink creation unavailable')
            self.assertFalse(gate.audit(root,paths)['pass'])

    def test_container_readiness_retries_transient_startup_resets(self):
        import http.client,io
        from unittest.mock import Mock
        client=Mock()
        client.open.side_effect=[ConnectionResetError(),http.client.RemoteDisconnected(),io.BytesIO(b'{'),io.BytesIO(b'{"ok":true}')]
        pause=Mock()
        container_check.wait_for_health(client,attempts=4,pause=pause)
        self.assertEqual(client.open.call_count,4)
        self.assertEqual(pause.call_count,3)
    def test_container_readiness_never_accepts_an_unhealthy_install(self):
        from unittest.mock import Mock
        client=Mock()
        client.open.side_effect=ConnectionRefusedError()
        with self.assertRaises(SystemExit): container_check.wait_for_health(client,attempts=2,pause=Mock())
        self.assertEqual(client.open.call_count,2)
    def test_environment_generation_is_private_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.env.example').write_bytes((ROOT/'.env.example').read_bytes())
            path=setup.create_environment(root);before=path.read_bytes()
            values=dict(line.split('=',1) for line in path.read_text().splitlines() if '=' in line and not line.startswith('#'))
            self.assertGreaterEqual(len(values['JWT_SECRET']),32)
            self.assertTrue(values['POSTGRES_PASSWORD'])
            self.assertEqual(values['BACKGROUND_TASKS_ENABLED'],'false')
            self.assertEqual(values['STORAGE_AUTO_CREATE_BUCKET'],'false')
            self.assertEqual(values['LEGACY_SHARED_LOGIN_ENABLED'],'false')
            with self.assertRaises(FileExistsError): setup.create_environment(root)
            self.assertEqual(path.read_bytes(),before)
    def test_gate_rejects_private_files_and_does_not_echo_matched_values(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);token='ghp_'+'A'*40
            (root/'app.py').write_text('value='+repr(token))
            result=gate.audit(root,['app.py'])
            self.assertFalse(result['pass']);self.assertNotIn(token,json.dumps(result))
            (root/'.env.local').write_text('secret=synthetic')
            self.assertFalse(gate.audit(root,['.env.local'])['pass'])
            (root/'data').mkdir();(root/'data/records.json').write_text('{}')
            self.assertFalse(gate.audit(root,['data/records.json'])['pass'])
    def test_gate_accepts_reserved_examples_and_rejects_unsafe_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'README.md').write_text('installer@example.test')
            self.assertTrue(gate.audit(root,['README.md'])['pass'])
            self.assertFalse(gate.audit(root,['../README.md'])['pass'])
            self.assertFalse(gate.audit(root,['.git/config'])['pass'])
    def test_public_docs_links_resolve(self):
        import re
        files=[ROOT/'README.md',*list((ROOT/'docs').glob('*.md'))]
        for path in files:
            for target in re.findall(r'\[[^\]]*\]\(([^)]+)\)',path.read_text(encoding='utf-8')):
                if re.match(r'[a-z]+:|#',target): continue
                self.assertTrue((path.parent/target.split('#')[0]).exists(),f'{path.name}: unresolved link')
    def test_generated_cs_is_readable_and_reproducible(self):
        result=subprocess.run([sys.executable,'scripts/build_cs_app.py','--check'],cwd=ROOT,capture_output=True)
        self.assertEqual(result.returncode,0)
        self.assertIn((ROOT/'src/cs_app.src.html').read_text(encoding='utf-8'),(ROOT/'src/cs_app.html').read_text(encoding='utf-8'))
    def test_dependency_pins_and_docker_boundaries(self):
        package=json.loads((ROOT/'package.json').read_text());lock=json.loads((ROOT/'package-lock.json').read_text())
        self.assertEqual(package['name'],lock['name'])
        self.assertEqual(package['devDependencies'],lock['packages']['']['devDependencies'])
        self.assertTrue(all('==' in line for line in (ROOT/'requirements.lock').read_text().splitlines() if line and not line.startswith('#')))
        compose=(ROOT/'docker-compose.yml').read_text()
        self.assertIn('127.0.0.1:8080:8080',compose)
        self.assertNotIn('CLOUDFLARE_API_TOKEN',compose)
        self.assertIn('COPY requirements.txt requirements.lock ./',(ROOT/'Dockerfile').read_text())
        self.assertIn('!scripts/bootstrap_admin.py',(ROOT/'.dockerignore').read_text())

if __name__=='__main__': unittest.main()
