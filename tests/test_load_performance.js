'use strict';

// 실제 프런트 코드를 메모리 VM에서 실행한다. 서버·DB·네트워크·실제 저장소는 사용하지 않는다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const html = read('static/index.html');
const plain = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function defaultReply(request) {
  if (request.path === '/api/approval-settings') return { type_approvers: {} };
  if (request.path === '/api/users/approvers') return { approvers: [] };
  if (request.path === '/api/approvals') return { items: [] };
  if (request.path === '/api/leave/requests') return { rows: [] };
  if (request.path === '/api/linear/my-issues') return { issues: [] };
  if (request.path === '/api/linear/projects') return { projects: [] };
  return { ok: true };
}
function runtime(reply = defaultReply, withObserver = false) {
  const storage = new Map([
    ['_dashboard_auth_token', 'offline-session-a'],
    ['_dashboard_user', JSON.stringify({ name: '테스트 담당자', role: 'pm', user_id: 'offline' })],
  ]);
  const requests = [], timers = [], intervals = [], observers = [];
  const clearedIntervals = new Set();
  const listeners = new Map();
  const document = {
    hidden: false,
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name, callback) => { if (listeners.get(name) === callback) listeners.delete(name); },
  };
  const context = vm.createContext({
    console, document,
    location: { origin: 'http://offline.test' },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout() {},
    setInterval: (callback, delay) => { intervals.push({ callback, delay }); return intervals.length; },
    clearInterval: id => clearedIntervals.add(id),
    confirm: () => true,
    fetch: async (url, options = {}) => {
      const target = new URL(String(url), 'http://offline.test');
      assert.equal(target.origin, 'http://offline.test');
      assert.ok(target.pathname.startsWith('/api/'));
      const request = { path: target.pathname, url: target.href, method: options.method || 'GET',
        token: options.headers && options.headers.Authorization, jsonReads: 0,
        body: options.body ? JSON.parse(options.body) : undefined };
      requests.push(request);
      const value = await reply(request);
      const status = value && value.responseStatus || 200;
      const body = value && value.responseStatus ? value.body : value;
      return { status, ok: status >= 200 && status < 300, json: async () => {
        request.jsonReads += 1;
        assert.equal(request.jsonReads, 1, '응답 본문은 한 번만 소비한다');
        return body;
      } };
    },
  });
  context.window = context;
  if (withObserver) {
    context.IntersectionObserver = class {
      constructor(callback, options) { this.callback = callback; this.options = options; this.disconnected = 0; observers.push(this); }
      observe(element) { this.element = element; }
      disconnect() { this.disconnected += 1; }
    };
  }
  for (const file of ['static/js/core.js', 'static/app.js']) vm.runInContext(read(file), context, { filename: file });
  const app = vm.runInContext('dashboard()', context);
  app.showToast = () => {};
  return { app, context, storage, requests, timers, intervals, observers, listeners, clearedIntervals };
}
function evaluate(context, app, expression, scope = {}) {
  context.__app = app;
  context.__scope = scope;
  try {
    return vm.runInContext('(function () { with (__app) { with (__scope) { return (' + expression + '); } } })()', context);
  } finally { delete context.__app; delete context.__scope; }
}
function lazyRegion(condition) {
  const tags = [...html.matchAll(/<\/?template\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/g)];
  const start = tags.findIndex(tag => tag[0].includes('x-if="' + condition + '"'));
  assert.ok(start >= 0, '지연 생성 경계: ' + condition);
  let depth = 1;
  for (let index = start + 1; index < tags.length; index += 1) {
    depth += tags[index][0].startsWith('</') ? -1 : 1;
    if (depth === 0) return html.slice(tags[start].index + tags[start][0].length, tags[index].index);
  }
  assert.fail('지연 생성 template이 닫혀 있어야 한다');
}
function linearComponent(local) {
  const section = html.slice(html.indexOf('<!-- ── Linear 프로젝트 현황 ── -->'));
  const match = section.match(/x-data="([^"]*)"\s+x-init="([^"]*)"/);
  assert.ok(match);
  assert.equal(match[2], 'initLinear($el)');
  const component = vm.runInContext('(' + match[1] + ')', local.context);
  const watches = new Map();
  component.activeTab = 'report';
  component.$watch = (name, callback) => watches.set(name, callback);
  return { component, watches };
}

async function main() {
  let count = 0;
  async function check(label, run) { await run(); count += 1; console.log('PASS ' + label); }

  await check('상단 조회는 리포트·팀·할일보다 먼저 시작하고 각 응답을 도착 즉시 반영한다', async () => {
    for (const first of ['report', 'team']) {
      const local = runtime();
      const report = deferred(), team = deferred(), todos = deferred();
      const calls = [];
      for (const method of ['_initGlobalErrorCapture', 'loadHolidaysIfNeeded', 'loadExecutiveSummary',
        'loadMyTodayAttendance', 'loadTodayMeetings', 'loadExecStatus', 'loadApprovals', 'loadMyHome',
        'loadCoreTree', 'loadProjectHierarchy', 'loadMemberReports', 'setMetricsRange', 'loadNotifications']) {
        local.app[method] = () => calls.push(method);
      }
      local.app.$watch = () => {};
      local.app.loadFromServer = () => { calls.push('report'); return report.promise; };
      local.app.loadTeamConfigFromServer = () => { calls.push('team'); return team.promise; };
      local.app.loadTodos = () => { calls.push('todos'); return todos.promise; };
      const serverReport = freeze({ ...plain(local.app.data), marker: '응답 원문' });
      const serverTeam = freeze({ ...plain(local.app.teamConfig), marker: '팀 원문' });
      let complete = false;
      const startup = local.app.init().then(() => { complete = true; });
      for (const upper of ['loadExecutiveSummary', 'loadMyTodayAttendance', 'loadTodayMeetings', 'loadExecStatus', 'loadApprovals', 'loadMyHome']) {
        assert.ok(calls.indexOf(upper) < calls.indexOf('report'), upper);
      }
      assert.equal(complete, false);
      if (first === 'report') {
        report.resolve(serverReport); await tick();
        assert.strictEqual(local.app.data, serverReport);
        assert.notStrictEqual(local.app.teamConfig, serverTeam);
        team.resolve(serverTeam);
      } else {
        team.resolve(serverTeam); await tick();
        assert.strictEqual(local.app.teamConfig, serverTeam);
        assert.notStrictEqual(local.app.data, serverReport);
        report.resolve(serverReport);
      }
      await startup;
      assert.equal(complete, true, '할일이 끝나지 않아도 초기화를 마친다');
      assert.strictEqual(local.app.data, serverReport);
      assert.strictEqual(local.app.teamConfig, serverTeam);
      assert.equal(calls.filter(value => value === 'loadExecStatus').length, 1);
      assert.equal(local.requests.length, 0, '초기 반영이 추가 쓰기를 만들지 않는다');
      todos.resolve();
    }
  });

  await check('느린 Linear·승인자 설정과 무관하게 내 결재와 결재 원본을 표시하고 동일 GET은 한 번만 소비한다', async () => {
    const approvals = deferred(), issues = deferred(), settings = deferred(), users = deferred();
    const items = freeze([
      { id: 'pending', requester: '테스트 담당자', status: '대기', req_type: 'book_purchase' },
      { id: 'approved', requester: '샘플아', status: '승인', title: '승인 원문', attachments: [] },
    ]);
    const local = runtime(request => {
      if (request.path === '/api/approvals') return approvals.promise;
      if (request.path === '/api/linear/my-issues') return issues.promise;
      if (request.path === '/api/approval-settings') return settings.promise;
      if (request.path === '/api/users/approvers') return users.promise;
      return defaultReply(request);
    });
    const all = local.app.loadApprovals();
    let homeDone = false;
    const home = local.app.loadMyHome().then(() => { homeDone = true; });
    assert.equal(local.requests.filter(request => request.path === '/api/approvals').length, 1);
    approvals.resolve({ items }); await tick();
    assert.strictEqual(local.app.approvalItems, items);
    assert.deepEqual(plain(local.app.myPendingApprovals).map(row => row.id), ['pending']);
    assert.equal(homeDone, false, '이슈가 늦어도 결재는 먼저 반영한다');
    issues.resolve({ issues: [{ id: 'issue', assignee: '테스트 담당자' }] });
    settings.resolve({ type_approvers: {} }); users.resolve({ approvers: [] });
    await Promise.all([all, home]);
    assert.equal(local.app.myIssues[0].id, 'issue');
    assert.equal(local.requests.find(request => request.path === '/api/approvals').jsonReads, 1);
    assert.deepEqual(plain(items[1]), { id: 'approved', requester: '샘플아', status: '승인', title: '승인 원문', attachments: [] });
    await local.app.loadApprovals();
    assert.equal(local.requests.filter(request => request.path === '/api/approvals').length, 2, '완료 응답을 장기 캐시하지 않는다');
  });

  await check('진행 중 결재 조회의 실패는 재시도할 수 있고 세션이 바뀌면 이전 응답을 적용하지 않는다', async () => {
    let fail = true;
    const local = runtime(request => {
      if (request.path === '/api/approvals' && fail) throw new Error('합성 조회 실패');
      return defaultReply(request);
    });
    await local.app.loadApprovals(); fail = false; await local.app.loadApprovals();
    assert.equal(local.requests.filter(request => request.path === '/api/approvals').length, 2);
    const old = deferred();
    let reads = 0;
    const sessions = runtime(request => {
      if (request.path === '/api/approvals') return ++reads === 1 ? old.promise : { items: [{ id: 'new-session' }] };
      return defaultReply(request);
    });
    const previous = sessions.app.loadApprovals();
    sessions.storage.set('_dashboard_auth_token', 'offline-session-b');
    await sessions.app.loadApprovals();
    old.resolve({ items: [{ id: 'old-session' }] }); await previous;
    assert.equal(sessions.app.approvalItems[0].id, 'new-session');
    assert.deepEqual(sessions.requests.filter(request => request.path === '/api/approvals').map(request => request.token),
      ['Bearer offline-session-a', 'Bearer offline-session-b']);
  });

  await check('저장·삭제·승인 성공 뒤 새 결재를 조회하고 늦은 이전 응답이 최신 목록을 덮지 않는다', async () => {
    for (const operation of ['submitApproval', 'deleteApproval', 'submitDecide']) {
      const old = deferred();
      let reads = 0;
      const latest = freeze([{ id: 'fresh', requester: '테스트 담당자', status: '대기', req_type: 'book_purchase' }]);
      const local = runtime(request => {
        if (request.path === '/api/approvals' && request.method === 'GET') return ++reads === 1 ? old.promise : { items: latest };
        return defaultReply(request);
      });
      const previous = local.app.loadApprovals();
      if (operation === 'submitApproval') {
        local.app.approvalModal = { id: 'edit', show: true, req_type: 'book_purchase', title: '합성 도서', detail: '', attachments: [], book_info: { book_name: '샘플', purpose: '검증' } };
        await local.app.submitApproval();
      } else if (operation === 'deleteApproval') await local.app.deleteApproval('delete');
      else {
        local.app.approvalDecideModal = { id: 'decide', decision: '승인', note: '합성 의견' };
        await local.app.submitDecide();
      }
      assert.equal(reads, 2, operation + ': 진행 중인 옛 GET에 합류하지 않는다');
      assert.strictEqual(local.app.approvalItems, latest);
      old.resolve({ items: [{ id: 'stale', status: '대기' }] }); await previous;
      assert.strictEqual(local.app.approvalItems, latest);
      assert.equal(local.app.myPendingApprovals[0].id, 'fresh');
      assert.equal(local.requests.filter(request => request.method !== 'GET').length, 1);
    }
  });

  await check('숨긴 JSON은 직렬화하지 않고 열었을 때만 같은 원문을 표시한다', () => {
    const local = runtime();
    const expression = html.match(/<pre\b[^>]*x-text="(showJsonModal \? exportJson\(\) : '')"/)[1];
    const original = local.app.exportJson.bind(local.app);
    let calls = 0;
    local.app.exportJson = () => { calls += 1; return original(); };
    for (let index = 0; index < 3; index += 1) assert.equal(evaluate(local.context, local.app, expression), '');
    assert.equal(calls, 0);
    local.app.showJsonModal = true;
    assert.equal(evaluate(local.context, local.app, expression), JSON.stringify(local.app.data, null, 2));
    assert.equal(calls, 1);
    local.app.showJsonModal = false;
    assert.equal(evaluate(local.context, local.app, expression), '');
    assert.equal(calls, 1);
  });

  await check('닫힌 안건·AI·보고 설정·보고 본문은 처음 열 때 생성하고 닫아도 상태와 원문을 유지한다', () => {
    const local = runtime();
    const before = plain({ data: local.app.data, team: local.app.teamConfig });
    for (const [condition, flag, field, value] of [
      ["pastMeetingsMounted || meetingTab === 'past'", 'pastMeetingsMounted', 'meetingTab', 'past'],
      ["aiSummaryMounted || meetingTab === 'aiSummary'", 'aiSummaryMounted', 'meetingTab', 'aiSummary'],
      ['memberReportConfigMounted || memberReportConfigOpen', 'memberReportConfigMounted', 'memberReportConfigOpen', true],
    ]) {
      const region = lazyRegion(condition);
      assert.ok(region.trim().startsWith('<div '));
      assert.ok(region.trim().endsWith('</div>'));
      assert.equal(evaluate(local.context, local.app, condition), false);
      local.app[field] = value;
      assert.equal(evaluate(local.context, local.app, condition), true);
      const init = region.match(/<div\b[^>]*x-init="([^"]+)"/)[1];
      assert.equal(init, flag + ' = true');
      evaluate(local.context, local.app, init);
      local.app[field] = field === 'meetingTab' ? 'current' : false;
      assert.equal(evaluate(local.context, local.app, condition), true, '처음 연 뒤에는 DOM을 제거하지 않는다');
    }
    const body = lazyRegion('bodyMounted || open');
    assert.match(body, /x-model="sec\.content"/);
    const scope = { bodyMounted: false, open: false, draft: '저장 전 원문', expanded: true };
    assert.equal(evaluate(local.context, local.app, 'bodyMounted || open', scope), false);
    scope.open = true;
    evaluate(local.context, local.app, body.match(/<div\b[^>]*x-init="([^"]+)"/)[1], scope);
    scope.open = false;
    assert.equal(evaluate(local.context, local.app, 'bodyMounted || open', scope), true);
    assert.equal(scope.draft, '저장 전 원문');
    assert.equal(scope.expanded, true);
    assert.deepEqual(plain({ data: local.app.data, team: local.app.teamConfig }), before);
  });

  await check('Linear는 기존 열린 기본값을 유지하되 화면 근처에서만 시작하고 중복 초기화·숨김 조회를 막는다', async () => {
    const local = runtime(defaultReply, true);
    const { component, watches } = linearComponent(local);
    assert.equal(component.linearSectionOpen, true);
    const element = {};
    component.initLinear(element); component.initLinear(element);
    assert.equal(local.observers.length, 1);
    assert.equal(local.observers[0].element, element);
    assert.equal(local.requests.length, 0);
    const observer = local.observers[0];
    component.linearSectionOpen = false;
    observer.callback([{ isIntersecting: true }]);
    assert.equal(local.requests.length, 0, '접혀 있을 때 외부 조회를 시작하지 않는다');
    component.linearSectionOpen = true;
    watches.get('linearSectionOpen')(); await tick();
    assert.equal(local.requests.length, 2);
    assert.equal(local.intervals.length, 1);
    component.initLinear(element);
    assert.equal(local.intervals.length, 1);
    const poll = local.intervals[0].callback;
    component.activeTab = 'hr'; poll();
    component.activeTab = 'report'; local.context.document.hidden = true; poll();
    local.context.document.hidden = false; observer.callback([{ isIntersecting: false }]); poll();
    assert.equal(local.requests.length, 2);
    observer.callback([{ isIntersecting: true }]); poll(); await tick();
    assert.equal(local.requests.length, 4);
    component.destroy();
    assert.equal(observer.disconnected, 1);
    assert.ok(local.clearedIntervals.has(component.linearTimer));
    assert.equal(local.listeners.has('visibilitychange'), false);
    observer.callback([{ isIntersecting: true }]); poll(); await tick();
    assert.equal(local.requests.length, 4, '정리 후 콜백도 조회를 다시 시작하지 않는다');
  });

  await check('관측 API가 없는 환경도 열린 Linear를 조회하며 비활성 탭·숨김 문서는 복귀 후 시작한다', async () => {
    for (const paused of ['tab', 'document']) {
      const local = runtime();
      const { component, watches } = linearComponent(local);
      if (paused === 'tab') component.activeTab = 'hr';
      else local.context.document.hidden = true;
      component.initLinear({});
      assert.equal(local.requests.length, 0);
      if (paused === 'tab') { component.activeTab = 'report'; watches.get('activeTab')(); }
      else { local.context.document.hidden = false; local.listeners.get('visibilitychange')(); }
      await tick();
      assert.equal(local.requests.length, 2);
      assert.equal(local.intervals.length, 1);
      component.destroy();
    }
  });

  console.log(`초기 로드 성능: ${count}개 그룹 통과. 메모리 VM 전용, 서버·DB·네트워크 쓰기 없음.`);
}

main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
