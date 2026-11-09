'use strict';

// Public-release behavior checks. All users, reports, responses and storage are synthetic.
const assert = require('node:assert/strict');
const { createHash, randomBytes } = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const plain = value => JSON.parse(JSON.stringify(value));

function runtime(initial = {}, respond = () => ({ ok: true, status: 200, json: async () => ({}) })) {
  const storage = new Map(Object.entries(initial));
  const requests = [];
  const toasts = [];
  let reloads = 0;
  const context = vm.createContext({
    console, TextEncoder,
    location: { origin: 'http://offline.test', reload: () => { reloads += 1; } },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    document: { hidden: false },
    setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
    fetch: async (url, options = {}) => {
      assert.equal(new URL(url).origin, 'http://offline.test');
      requests.push({ url: String(url), options, token: storage.get('_dashboard_auth_token') });
      return respond(url, options);
    },
  });
  context.window = context;
  for (const file of ['static/js/core.js', 'static/app.js']) vm.runInContext(read(file), context, { filename: file });
  const app = vm.runInContext('dashboard()', context);
  app.showToast = (...args) => toasts.push(args);
  return { app, context, storage, requests, toasts, reloads: () => reloads };
}

async function main() {
  let count = 0;
  async function check(label, run) { await run(); count += 1; console.log('PASS ' + label); }

  await check('fresh installs have no preinstalled people, business metrics, answers, approvers or exceptions', () => {
    const { app, context, requests } = runtime();
    for (const key of ['members', 'sections', 'categoryOrder', 'executiveNames', 'excludedLeaveBalanceNames', 'dailyOwnerExcludeNames']) {
      assert.deepEqual(plain(app.teamConfig[key]), [], key);
    }
    assert.deepEqual(plain(app.teamConfig.profileVisibility), { hiddenNames: [] });
    assert.deepEqual(plain(app.teamConfig.approvalDefaults), { primary: '', reference: '' });
    assert.deepEqual(plain(app.teamConfig.ownerColors), {});
    assert.equal(app.teamConfig.recorderUrl, '');
    assert.ok(Object.values(app.teamConfig.company).every(value => value === ''));
    assert.ok(app.data.metricSections.every(section => section.metrics.length === 0));
    assert.deepEqual(plain(app.data.activeProjects.projects), []);
    assert.deepEqual(plain(app.data.meetingAgenda.items), []);
    assert.deepEqual(plain(app.data.aiSummaries), {});
    assert.deepEqual(plain(app.memberReportTemplates), {});
    assert.deepEqual(plain(app.execList), []);
    assert.deepEqual(plain(app.leaveApproverPair()), { pm: '', admin: '' });
    assert.equal(app.settingsAddUserModal.password, '');
    app.openAddUserModal();
    assert.equal(app.settingsAddUserModal.password, '');
    assert.doesNotMatch(read('static/app.js'), /\bpassword\s*:\s*(['"])[^'"]+\1/, 'UI models never prefill a fixed password');
    const people = Object.freeze(['Demo Reviewer A', 'Demo Reviewer B']);
    assert.deepEqual(plain(context.dashboardProfileVisibility.visibleProfiles(people)), [...people]);
    assert.deepEqual(plain(app.visibleProfiles(people)), [...people]);
    assert.equal(requests.length, 0);
  });

  await check('loading installer reports does not inject sample metrics, previous values or business answers', () => {
    const metrics = [{ id: 'synthetic-metric', label: '주간 매출', value: '₩17' }];
    const report = { metricSections: [{ id: 'biz', metrics }] };
    const team = { _version: 3, members: ['Demo Author'], sections: [{ section: 'Installer work', roles: [] }] };
    const local = runtime({ weeklyReportData: JSON.stringify(report), weeklyReportTeamConfig: JSON.stringify(team) });
    assert.deepEqual(plain(local.app.data.metricSections[0].metrics), metrics);
    assert.deepEqual(plain(local.app.teamConfig.members), team.members);
    assert.deepEqual(plain(local.app.teamConfig.sections), team.sections);
    assert.ok(Object.values(local.app.teamConfig.company).every(value => value === ''));
    assert.equal(local.requests.length, 0);
  });

  await check('installer visibility is dynamic, exact and independent between dashboard instances', () => {
    const local = runtime();
    const other = runtime();
    const names = Object.freeze(['Demo Author', 'Demo Reviewer', 'Demo Author Team']);
    const records = Object.freeze(names.map(name => Object.freeze({ user_name: name, status: '승인' })));
    local.app.hrLeaveRequests = records;
    local.app.teamConfig.profileVisibility = { hiddenNames: ['Demo Author'] };
    assert.deepEqual(plain(local.app.visibleProfiles(names)), names.slice(1));
    assert.deepEqual(plain(other.app.visibleProfiles(names)), [...names]);
    assert.deepEqual(plain(local.app.memberOptions(names, 'Demo Author')), [...names.slice(1), 'Demo Author']);
    local.app.teamConfig.profileVisibility.hiddenNames = ['Demo Reviewer'];
    assert.deepEqual(plain(local.app.visibleProfiles(records)).map(row => row.user_name), ['Demo Author', 'Demo Author Team']);
    assert.strictEqual(local.app.hrLeaveRequests, records, 'historical approvals must never be rewritten');
    local.app.teamConfig.profileVisibility = { hiddenNames: [] };
    assert.deepEqual(plain(local.app.visibleProfiles(names)), [...names]);
  });

  await check('configured leave defaults and per-type routing work without built-in approver names', () => {
    const { app } = runtime();
    app.teamConfig.approvalDefaults = { primary: 'Demo PM', reference: 'Demo Admin' };
    assert.deepEqual(plain(app.leaveApproverPair()), { pm: 'Demo PM', admin: 'Demo Admin' });
    app.approvalTypeApprovers.leave = { pm: 'Demo Delegate', admin: 'Demo Reviewer' };
    assert.deepEqual(plain(app.leaveApproverPair()), { pm: 'Demo Delegate', admin: 'Demo Reviewer' });
  });

  await check('new installer members receive editable blank report templates and configured templates stay intact', () => {
    const { app } = runtime();
    app.teamConfig.members = ['Demo Author'];
    const templates = plain(app.memberReportTemplates);
    assert.deepEqual(Object.keys(templates), ['Demo Author']);
    assert.ok(templates['Demo Author'].sections.length > 0);
    assert.ok(app.memberReportSections('Demo Author').every(section => section.content === ''));
    app.teamConfig.memberReportTemplates = { 'Demo Author': { role: 'Installer role', color: '#6b7280', sections: ['Installer section'] } };
    app.saveTeam = () => {};
    app.newMemberName = 'Demo New Member';
    app.addMember();
    assert.deepEqual(plain(app.teamConfig.memberReportTemplates['Demo Author']), { role: 'Installer role', color: '#6b7280', sections: ['Installer section'] });
    assert.ok(app.memberReportTemplates['Demo New Member'].sections.length > 0);
    assert.ok(app.memberReportSections('Demo New Member').every(section => section.content === ''));
  });

  await check('configured executive cards and historical names reconcile without fixed employees', async () => {
    const local = runtime({ _dashboard_auth_token: 'synthetic-token' }, () => ({
      ok: true, status: 200, json: async () => ({ items: [{ name: 'Demo Former Lead', week_label: 'demo-week', current_tasks: 'Synthetic history' }] }),
    }));
    local.app.execSelectedWeek = 'demo-week';
    local.app.teamConfig.executiveNames = ['Demo Current Lead'];
    await local.app.loadExecStatus();
    assert.deepEqual(plain(local.app.execList).map(item => item.name), ['Demo Current Lead', 'Demo Former Lead']);
    assert.equal(local.app.execList[1].current_tasks, 'Synthetic history');
    const historical = local.app.execList[1];
    local.app.teamConfig.executiveNames.push('Demo Additional Lead');
    local.app.syncExecProfiles();
    assert.strictEqual(local.app.execList.find(item => item.name === historical.name), historical);
    assert.equal(local.requests.length, 1, 'configuration reconciliation does not repeat the API read');
  });

  await check('logout revokes the authenticated file session before removing the browser token', async () => {
    let finish;
    const local = runtime({ _dashboard_auth_token: 'synthetic-token', _dashboard_user: '{}' }, () => new Promise(resolve => { finish = resolve; }));
    const pending = local.app.logoutUser();
    await new Promise(resolve => setImmediate(resolve));
    assert.equal(local.storage.get('_dashboard_auth_token'), 'synthetic-token');
    assert.equal(local.reloads(), 0);
    assert.equal(local.requests[0].url, 'http://offline.test/api/auth/logout');
    assert.equal(local.requests[0].options.method, 'POST');
    assert.equal(local.requests[0].options.headers.Authorization, 'Bearer synthetic-token');
    finish({ ok: true, status: 200 });
    await pending;
    assert.equal(local.storage.has('_dashboard_auth_token'), false);
    assert.equal(local.storage.has('_dashboard_user'), false);
    assert.equal(local.reloads(), 1);
    const failed = runtime({ _dashboard_auth_token: 'synthetic-token' }, () => ({ ok: false, status: 503 }));
    await failed.app.logoutUser();
    assert.equal(failed.storage.get('_dashboard_auth_token'), 'synthetic-token');
    assert.equal(failed.reloads(), 0);
    assert.equal(failed.toasts.length, 1);
  });

  await check('password resets require a bounded entered value, matching confirmation and explicit target approval', async () => {
    const entered = randomBytes(24).toString('base64url');
    const target = { id: 'demo-user', name: 'Demo Account' };
    for (const [values, approved] of [
      [[null], true],
      [[''], true],
      [['short'], true],
      [['😀'.repeat(6)], true],
      [['한'.repeat(342)], true],
      [[entered, null], true],
      [[entered, entered + 'different'], true],
      [[entered, entered], false],
    ]) {
      const local = runtime({ _dashboard_auth_token: 'synthetic-token' });
      const pending = [...values];
      local.context.prompt = () => pending.shift();
      local.context.confirm = () => approved;
      await local.app.resetUserPassword(target);
      assert.equal(local.requests.length, 0, 'cancelled, mismatched or invalid reset must not call the server');
    }
    const local = runtime({ _dashboard_auth_token: 'synthetic-token' }, () => ({
      ok: true, status: 200, json: async () => ({ ok: true, name: target.name }),
    }));
    const prompts = [];
    const confirmations = [];
    local.context.prompt = (...args) => { prompts.push(args); return entered; };
    local.context.confirm = message => { confirmations.push(message); return true; };
    await local.app.resetUserPassword(target);
    assert.equal(prompts.length, 2);
    assert.ok(prompts.every(args => args.length === 1), 'reset prompts never suggest a default credential');
    assert.equal(confirmations.length, 1);
    assert.ok(confirmations[0].includes(target.name));
    assert.equal(local.requests.length, 1);
    assert.equal(local.requests[0].url, 'http://offline.test/api/auth/reset-password');
    assert.equal(local.requests[0].options.method, 'POST');
    assert.deepEqual(JSON.parse(local.requests[0].options.body), { user_id: target.id, new_password: entered });
    assert.ok(!JSON.stringify({ prompts, confirmations, toasts: local.toasts }).includes(entered), 'passwords are absent from notices and suggested values');
  });

  await check('all account creation and password-change handlers use explicit credentials with the server length limits', async () => {
    const entered = randomBytes(24).toString('base64url');
    const current = randomBytes(24).toString('base64url');
    const operations = [
      ['submitAddUser', '/api/auth/register', 'password', (app, value) => {
        app.openAddUserModal();
        app.settingsAddUserModal.name = 'Demo New Account';
        app.settingsAddUserModal.email = 'new-account@example.test';
        app.settingsAddUserModal.password = value;
      }],
      ['submitPasswordChange', '/api/auth/password', 'new_password', (app, value) => {
        app.currentUser = { name: 'Demo Account', role: 'member' };
        app.passwordChangeModal = { show: true, current, new: value, confirm: value, submitting: false };
      }],
      ['submitChangePassword', '/api/auth/change-password', 'new_password', (app, value) => {
        app.settingsPwModal = { show: true, current, next: value, confirm: value };
      }],
    ];
    for (const [method, endpoint, field, prepare] of operations) {
      for (const invalid of ['', 'a'.repeat(11), '😀'.repeat(6), '한'.repeat(342)]) {
        const local = runtime({ _dashboard_auth_token: 'synthetic-token' });
        prepare(local.app, invalid);
        await local.app[method]();
        assert.equal(local.requests.length, 0, method + ' rejects an invalid or missing new password');
      }
      const local = runtime({ _dashboard_auth_token: 'synthetic-token' });
      local.app.loadSettingsData = async () => {};
      prepare(local.app, entered);
      await local.app[method]();
      assert.equal(local.requests.length, 1, method);
      assert.equal(local.requests[0].url, 'http://offline.test' + endpoint);
      assert.equal(JSON.parse(local.requests[0].options.body)[field], entered);
      assert.ok(!JSON.stringify(local.toasts).includes(entered));
      if (method === 'submitAddUser') assert.equal(local.app.settingsAddUserModal.password, '');
    }
    const local = runtime();
    assert.equal(local.app.isValidNewPassword('😀'.repeat(12)), true);
    assert.equal(local.app.isValidNewPassword('a'.repeat(1024)), true);
    assert.equal(local.app.isValidNewPassword('a'.repeat(1025)), false);
    assert.match(read('static/index.html'), /type="password" x-model="settingsAddUserModal\.password"/);
    assert.doesNotMatch(read('static/index.html'), /새 비밀번호 \(6자 이상\)/);
  });

  await check('the generated CS page is readable and exactly reproducible from its source', () => {
    const source = read('src/cs_app.src.html').replace(/\r\n?/g, '\n');
    const digest = createHash('sha256').update(source).digest('hex');
    const notice = '<!-- Generated by scripts/build_cs_app.py from src/cs_app.src.html; source SHA-256: ' + digest + '. Edit the source, then rebuild. -->\n';
    assert.equal(read('src/cs_app.html'), notice + source);
    const scripts = [...source.matchAll(/<script\b[^>]*>([\s\S]*?)<\/script>/g)];
    assert.ok(scripts.length > 0);
    for (const [, script] of scripts) new vm.Script(script, { filename: 'cs_app.src.html' });
    assert.doesNotMatch(source, /https?:\/\//, 'CS requests use the installer origin');
    assert.match(read('static/partials/tab-notes.html'), /notesRecorderOpen \? \(teamConfig\.recorderUrl \|\| ''\) : ''/);
  });

  console.log(`Public defaults: ${count} checks passed; synthetic in-memory data only.`);
}

main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
