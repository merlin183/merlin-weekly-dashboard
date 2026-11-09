'use strict';

// 실제 프런트 코드를 합성 응답으로만 검사한다. 서버·DB·네트워크에는 접근하지 않는다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const read = file => fs.readFileSync(path.join(root, file), 'utf8');
const html = read('static/partials/tab-hr.html');
const plain = value => JSON.parse(JSON.stringify(value));
const tick = () => new Promise(resolve => setImmediate(resolve));
const hidden = ['샘플아', '샘플바', '샘플다'];
function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}
function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}
class FixedDate extends Date {
  constructor(...args) { super(...(args.length ? args : ['2026-11-05T03:00:00Z'])); }
  static now() { return Date.parse('2026-11-05T03:00:00Z'); }
}
function runtime(reply = () => ({ rows: [], records: [], coverage_warnings: [] })) {
  const storage = new Map([
    ['_dashboard_auth_token', 'synthetic-coverage-token'],
    ['_dashboard_user', JSON.stringify({ name: '검증자A', role: 'pm', user_id: 'offline' })],
  ]);
  const requests = [];
  const context = vm.createContext({
    console, Date: FixedDate,
    location: { origin: 'http://offline.test' },
    document: { hidden: false },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    setTimeout() {}, clearTimeout() {}, setInterval() {}, clearInterval() {},
    fetch: async (url, options = {}) => {
      const target = new URL(String(url));
      assert.equal(target.origin, 'http://offline.test');
      assert.equal(target.pathname, '/api/attendance/monthly');
      assert.equal(options.method || 'GET', 'GET', '검사는 조회만 수행한다');
      requests.push({ url: target.href, method: options.method || 'GET' });
      return { status: 200, ok: true, json: async () => reply(target) };
    },
  });
  context.window = context;
  for (const file of ['static/js/core.js', 'static/app.js']) vm.runInContext(read(file), context, { filename: file });
  const app = vm.runInContext('dashboard()', context);
  app.teamConfig.profileVisibility = { hiddenNames: [...hidden] };
  app.teamConfig.excludedLeaveBalanceNames = ['샘플가', '샘플나'];
  app.teamConfig.dailyOwnerExcludeNames = ['샘플가', '샘플나'];
  app.showToast = () => {};
  return { app, context, requests };
}
function evaluate(local, expression, scope = {}) {
  local.context.__app = local.app;
  local.context.__scope = scope;
  try {
    return vm.runInContext('(function () { with (__app) { with (__scope) { return (' + expression + '); } } })()', local.context);
  } finally { delete local.context.__app; delete local.context.__scope; }
}
function attr(tag, name) {
  const escaped = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = tag.match(new RegExp('(?:^|\\s)' + escaped + '="([^"]*)"'));
  assert.ok(match, name + ' 속성이 있어야 한다');
  return match[1];
}
const select = field => {
  const tag = (html.match(/<select\b[^>]*>[\s\S]*?<\/select>/g) || []).find(value => value.includes('x-model="' + field + '"'));
  assert.ok(tag, field);
  return tag;
};
function record(index, user_name = ['검증자A', '검증자B', '검증자A팀'][index % 3]) {
  return {
    key: 'attendance:' + index, id: index + 1, user_name, work_date: '2026-11-04',
    clock_in: '2026-11-04T09:00:00', clock_out: '2026-11-04T18:00:00',
    raw_status: '정상', status: '정상', note: '합성 원문 ' + index,
    has_attendance: true, is_missing: false, missing_clock_in: false, missing_clock_out: false,
    approved_half_days: [], approved_leaves: [],
  };
}

async function main() {
  let count = 0;
  async function check(label, run) { await run(); count += 1; console.log('PASS ' + label); }

  await check('정적 월 옵션은 현재 11월·다른 월·연간 상태와 하나만 일치하고 연간 제목을 표시한다', () => {
    const local = runtime();
    const monthSelect = select('hrMonthlyMonth');
    assert.doesNotMatch(monthSelect, /x-for=/, '월 옵션의 동적 생성 순서에 의존하지 않는다');
    const options = [...monthSelect.matchAll(/<option\b[^>]*>[\s\S]*?<\/option>/g)].map(match => match[0]);
    assert.deepEqual(options.map(option => attr(option, 'value')), [...Array.from({ length: 12 }, (_, index) => String(index + 1)), 'all']);
    assert.equal(local.app.hrMonthlyMonth, '11');
    for (const value of ['11', '1', '12', 'all', '2']) {
      local.app.hrMonthlyMonth = value;
      const selected = options.filter(option => evaluate(local, attr(option, ':selected')));
      assert.equal(selected.length, 1);
      assert.equal(attr(selected[0], 'value'), value);
      assert.equal(local.app.hrMonthlySummaryTitle, value === 'all' ? '연간 요약' : '월간 요약');
      assert.equal(local.app.hrMonthlyDetailTitle, value === 'all' ? '연간 출퇴근 상세' : '월간 출퇴근 상세');
    }
    assert.match(monthSelect, />전체\(연간\)<\/option>/);
    assert.match(html, /x-text="hrMonthlySummaryTitle"/);
    assert.match(html, /x-text="hrMonthlyDetailTitle"/);
  });

  await check('연간 전체 원본을 보존하며 표시만 100행씩 나누고 마지막 key까지 빠짐없이 탐색한다', () => {
    const local = runtime();
    const rows = freeze([...Array.from({ length: 3000 }, (_, index) => record(index)), ...hidden.map((name, index) => record(3000 + index, name))]);
    const before = JSON.stringify(rows);
    local.app.hrMonthlyMonth = 'all';
    local.app.hrMonthlyRecords = rows;
    assert.equal(local.app.hrMonthlyRecordsVisible.length, 3000);
    assert.equal(local.app.hrMonthlyDetailPageCount, 30);
    const keys = [];
    for (let page = 1; page <= local.app.hrMonthlyDetailPageCount; page += 1) {
      local.app.setHrMonthlyDetailPage(page);
      assert.equal(local.app.hrMonthlyRecordsPage.length, 100);
      assert.ok(local.app.hrMonthlyRecordsPage.every(row => rows.includes(row)), '표시 행은 원본 객체를 참조한다');
      keys.push(...local.app.hrMonthlyRecordsPage.map(row => row.key));
    }
    assert.deepEqual(keys, rows.filter(row => !hidden.includes(row.user_name)).map(row => row.key));
    assert.equal(new Set(keys).size, 3000);
    assert.equal(local.app.hrMonthlyDetailRange, '2901–3000 / 3000건');
    local.app.setHrMonthlyDetailPage(999);
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 30);
    local.app.setHrMonthlyDetailPage(-5);
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 1);
    assert.strictEqual(local.app.hrMonthlyRecords, rows);
    assert.equal(JSON.stringify(rows), before);
    assert.equal(local.requests.length, 0, '페이지 이동은 API를 호출하지 않는다');
    assert.match(html, /x-for="row in hrMonthlyRecordsPage" :key="'hmd-' \+ row\.key"/);
  });

  await check('사용자 필터를 페이지보다 먼저 적용하며 선택 변경은 첫 페이지로 복귀하고 월간은 전행을 표시한다', () => {
    const local = runtime();
    const rows = freeze([...Array.from({ length: 360 }, (_, index) => record(index)), ...hidden.map((name, index) => record(400 + index, name))]);
    local.app.hrMonthlyRecords = rows;
    local.app.hrMonthlyMonth = 'all';
    local.app.setHrMonthlyDetailPage(3);
    const actor = plain(local.app.currentUser);
    for (const user of ['검증자A', '검증자B', '검증자A팀', '', ...hidden, '없는 사용자']) {
      local.app.hrUser = user;
      evaluate(local, attr(select('hrUser'), '@change'));
      const expected = rows.filter(row => !hidden.includes(row.user_name) && (!user || row.user_name === user));
      assert.equal(local.app.hrMonthlyDetailCurrentPage, 1);
      assert.deepEqual(plain(local.app.hrMonthlyRecordsVisible), plain(expected));
      assert.deepEqual(plain(local.app.hrMonthlyRecordsPage), plain(expected.slice(0, 100)));
      local.app.setHrMonthlyDetailPage(2);
      assert.equal(local.app.hrMonthlyDetailCurrentPage, Math.min(2, Math.max(1, Math.ceil(expected.length / 100))));
    }
    local.app.hrUser = '';
    local.app.hrMonthlyMonth = '11';
    assert.equal(local.app.hrMonthlyRecordsPage.length, 360, '월간 전행 표시를 유지한다');
    assert.equal(local.app.hrMonthlyDetailPageCount, 1);
    assert.deepEqual(plain(local.app.currentUser), actor);
    assert.strictEqual(local.app.hrMonthlyRecords, rows);
    assert.equal(local.requests.length, 0);
    assert.match(attr(select('hrMonthlyYear'), '@change'), /^resetHrMonthlyDetailPage\(\); loadMonthly\(\)$/);
    assert.match(attr(select('hrMonthlyMonth'), '@change'), /^resetHrMonthlyDetailPage\(\); loadMonthly\(\)$/);
  });

  await check('A의 3페이지에서 B를 선택한 뒤 A로 돌아와도 저장된 페이지와 범위가 첫 페이지로 초기화된다', () => {
    const local = runtime();
    const rows = freeze(Array.from({ length: 900 }, (_, index) => record(index)));
    const before = JSON.stringify(rows);
    local.app.hrMonthlyRecords = rows;
    local.app.hrMonthlyMonth = 'all';
    const change = attr(select('hrUser'), '@change');
    assert.equal(change, 'resetHrMonthlyDetailPage()');
    const choose = user => {
      local.app.hrUser = user;
      evaluate(local, change);
      assert.equal(local.app.hrMonthlyDetailPage, 1, '표시 getter뿐 아니라 저장된 페이지를 초기화한다');
      assert.equal(local.app.hrMonthlyDetailPageScope,
        JSON.stringify([local.app.hrMonthlyYear, 'all', user]), '저장된 범위를 새 사용자로 바꾼다');
      assert.equal(local.app.hrMonthlyDetailCurrentPage, 1);
    };
    choose('검증자A');
    local.app.setHrMonthlyDetailPage(3);
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 3);
    choose('검증자B');
    local.app.setHrMonthlyDetailPage(2);
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 2);
    choose('검증자A');
    assert.deepEqual(plain(local.app.hrMonthlyRecordsPage), plain(rows.filter(row => row.user_name === '검증자A').slice(0, 100)));
    assert.strictEqual(local.app.hrMonthlyRecords, rows);
    assert.equal(JSON.stringify(rows), before);
    assert.equal(local.requests.length, 0);
  });

  await check('연간 API는 all을 명시하며 응답 원문·경고를 보존하고 같은 기간 재조회 후 페이지를 범위 내로 맞춘다', async () => {
    const first = freeze({ rows: [{ user_name: '검증자A', days: 2 }],
      records: Array.from({ length: 230 }, (_, index) => record(index, '검증자A')),
      halfday_warnings: [], coverage_warnings: [{ code: 'missing_hire_date', user_name: '검증자A' }],
      year: 2026, month: 'all', period_type: 'year' });
    const smaller = freeze({ ...first, records: first.records.slice(0, 120) });
    let current = first;
    const local = runtime(target => {
      assert.equal(target.searchParams.get('year'), '2026');
      assert.equal(target.searchParams.get('month'), 'all');
      return current;
    });
    local.app.hrUser = '검증자A';
    local.app.hrMonthlyMonth = 'all';
    await local.app.loadMonthly();
    assert.strictEqual(local.app.hrMonthly, first.rows);
    assert.strictEqual(local.app.hrMonthlyRecords, first.records);
    assert.strictEqual(local.app.hrMonthlyCoverageWarnings, first.coverage_warnings);
    local.app.setHrMonthlyDetailPage(3);
    current = smaller;
    await local.app.loadMonthly();
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 2);
    assert.equal(local.app.hrMonthlyDetailPage, 2);
    assert.equal(local.app.hrMonthlyRecordsPage.length, 20);
    assert.equal(local.app.hrMonthlyDetailRange, '101–120 / 120건');
    assert.equal(local.app.hrUser, '검증자A');
    assert.strictEqual(local.app.hrMonthlyRecords, smaller.records);
    assert.equal(first.records.length, 230);
    local.app.hrMonthlyYear = '2025';
    assert.equal(local.app.hrMonthlyDetailCurrentPage, 1, '연도 전환 즉시 이전 페이지를 표시하지 않는다');
  });

  await check('월간과 연간의 늦은 이전 응답은 새 기록·경고·페이지 상태를 덮지 않는다', async () => {
    for (const [previous, latest] of [['11', 'all'], ['all', '1']]) {
      const old = deferred(), fresh = deferred();
      const local = runtime(target => target.searchParams.get('month') === previous ? old.promise : fresh.promise);
      local.app.hrUser = '검증자A';
      local.app.hrMonthlyMonth = previous;
      const pending = local.app.loadMonthly();
      await tick();
      local.app.hrMonthlyMonth = latest;
      const next = local.app.loadMonthly();
      const response = freeze({ rows: [{ user_name: '검증자A', days: 1 }],
        records: [record(1, '검증자A')], halfday_warnings: [], coverage_warnings: [{ code: 'missing_holiday_calendar', year: 2026 }] });
      fresh.resolve(response); await next;
      old.resolve({ rows: [], records: [], coverage_warnings: [] }); await pending;
      assert.strictEqual(local.app.hrMonthlyRecords, response.records);
      assert.strictEqual(local.app.hrMonthlyCoverageWarnings, response.coverage_warnings);
      assert.equal(local.app.hrMonthlyMonth, latest);
      assert.equal(local.app.hrMonthlyDetailCurrentPage, 1);
      assert.equal(local.app.hrUser, '검증자A');
      assert.equal(local.app.hrMonthlyLoading, false);
    }
  });

  await check('빨강은 서버 플래그만 따르며 승인휴가·오늘·미래·입사 전·확인 보류 행을 재판정하지 않는다', () => {
    const local = runtime();
    const cells = Object.fromEntries(['date', 'clock-in', 'clock-out', 'status'].map(marker => {
      const tag = html.match(new RegExp('<td data-hr-missing="' + marker + '"[^>]*>'))[0];
      assert.doesNotMatch(attr(tag, 'class'), /text-(slate|red)-/, '정상 색과 빨강을 동시에 지정하지 않는다');
      return [marker, attr(tag, ':class')];
    }));
    const rows = freeze([
      { ...record(1), is_missing: true, missing_clock_in: true, missing_clock_out: true },
      { ...record(2), is_missing: true, missing_clock_in: false, missing_clock_out: true },
      { ...record(3), clock_in: null, clock_out: null, status: '휴가(연차)', approved_leaves: [{ id: 'leave', type: '연차' }] },
      { ...record(4), clock_out: null, status: '오후반차', approved_half_days: [{ id: 'half', leave_type: '오후반차' }] },
      ...['2026-11-05', '2026-11-06', '2020-01-01'].map((work_date, index) => ({ ...record(5 + index), work_date, clock_in: null, clock_out: null })),
    ]);
    const before = plain(rows);
    for (const row of rows) {
      for (const [marker, expression] of Object.entries(cells)) {
        const flag = marker === 'clock-in' ? row.missing_clock_in : marker === 'clock-out' ? row.missing_clock_out : row.is_missing;
        assert.equal(evaluate(local, expression, { row }), flag === true ? 'text-red-600' : marker === 'date' ? 'text-slate-700' : 'text-slate-600');
      }
    }
    assert.deepEqual(plain(rows), before);
  });

  await check('입사일·공휴일·휴가 안내는 선택·숨김 필터를 따르고 전역 연도 안내와 원본을 보존한다', () => {
    const local = runtime();
    const warnings = freeze([
      { code: 'missing_hire_date', user_name: '검증자A' },
      { code: 'conflicting_hire_dates', user_name: '검증자B' },
      ...hidden.map(user_name => ({ code: 'missing_hire_date', user_name })),
      { code: 'invalid_approved_leave', user_name: '검증자B', id: 'opaque-leave' },
      { code: 'missing_holiday_calendar', year: 2026 },
    ]);
    local.app.hrMonthlyCoverageWarnings = warnings;
    local.app.hrUser = '검증자A';
    assert.deepEqual(plain(local.app.hrMonthlyCoverageWarningsVisible), plain([warnings[0], warnings.at(-1)]));
    assert.equal(local.app.hasHrMonthlyCoverageWarning(['missing_hire_date']), true);
    assert.equal(local.app.hasHrMonthlyCoverageWarning(['invalid_approved_leave']), false);
    local.app.hrUser = '검증자B';
    assert.equal(local.app.hasHrMonthlyCoverageWarning(['conflicting_hire_dates']), true);
    assert.equal(local.app.hasHrMonthlyCoverageWarning(['invalid_approved_leave']), true);
    for (const user of hidden) {
      local.app.hrUser = user;
      assert.deepEqual(plain(local.app.hrMonthlyCoverageWarningsVisible), plain([warnings.at(-1)]));
    }
    assert.strictEqual(local.app.hrMonthlyCoverageWarnings, warnings);
    const notice = html.match(/<!-- 근태 범위 안내 시작 -->[\s\S]*?<!-- 근태 범위 안내 끝 -->/)[0];
    assert.doesNotMatch(notice, /x-text="[^"]*(?:user_name|\.id|reason|email)/);
    assert.match(notice, /입사일 확인이 필요한 직원/);
    assert.match(notice, /년 공휴일 정보가 없어/);
  });

  console.log(`근태 범위 표시: ${count}개 그룹 통과. 합성 VM 전용, 서버·DB·네트워크 쓰기 없음.`);
}
main().catch(error => { console.error(error.stack || error.message); process.exitCode = 1; });
