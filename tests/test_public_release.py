"""Portable installation and release checks without any database or network."""
from pathlib import Path
import importlib.util,json,subprocess,sys,tempfile,unittest
ROOT=Path(__file__).resolve().parents[1]
def module(name,path):
    spec=importlib.util.spec_from_file_location(name,ROOT/path);result=importlib.util.module_from_spec(spec);spec.loader.exec_module(result);return result
gate=module('release_gate','scripts/check_release.py')
setup=module('environment_setup','scripts/setup_env.py')

class PublicReleaseTests(unittest.TestCase):
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
