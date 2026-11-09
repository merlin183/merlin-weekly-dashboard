import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
for(const file of ['test_profile_visibility.js','test_load_performance.js','test_attendance_coverage.js','test_public_defaults.js','test_attachments.js']){
 const result=spawnSync(process.execPath,[`tests/${file}`],{cwd:root,stdio:'inherit',windowsHide:true});
 if(result.status!==0)process.exit(result.status??1);
}
