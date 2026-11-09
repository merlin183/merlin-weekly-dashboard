import {readdirSync} from 'node:fs';
import {join,resolve} from 'node:path';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
const root=fileURLToPath(new URL('../',import.meta.url));
const files=[];
function collect(dir){for(const entry of readdirSync(resolve(root,dir),{withFileTypes:true})){const path=join(dir,entry.name);if(entry.isDirectory())collect(path);else if(/\.js$/.test(entry.name))files.push(path);}}
collect('static');collect('tests');
for(const file of files){const result=spawnSync(process.execPath,['--check',file],{cwd:root,stdio:'inherit',windowsHide:true});if(result.status!==0)process.exit(result.status??1);}
console.log(`JavaScript syntax passed: ${files.length} files.`);
