'use strict';

// 실제 core/app 코드를 격리 VM에서 검증한다. 서버·DB·브라우저 저장소에 접근하지 않는다.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const { createHash } = require('node:crypto');
const path = require('node:path');
const vm = require('node:vm');

const root = path.resolve(__dirname, '..');
const names = ['샘플가', '샘플나', '샘플다', '샘플라', '샘플마', '샘플바', '샘플사', '샘플아', '샘플자'];
const hidden = ['샘플아', '샘플바', '샘플다'];
const others = names.filter(name => !hidden.includes(name));
const plain = value => JSON.parse(JSON.stringify(value));
const read = relative => fs.readFileSync(path.join(root, relative), 'utf8');

function freeze(value) {
  if (value && typeof value === 'object' && !Object.isFrozen(value)) {
    Object.values(value).forEach(freeze);
    Object.freeze(value);
  }
  return value;
}

function runtime(respond = () => ({ ok: true })) {
  const storage = new Map([
    ['_dashboard_auth_token', 'synthetic-offline-token'],
    ['_dashboard_user', JSON.stringify({ name: '오프라인 검증자', role: 'pm', user_id: 'offline-test' })],
  ]);
  const requests = [];
  const timers = [];
  const intervals = [];
  const sandbox = {
    console,
    location: { origin: 'http://local.test' },
    localStorage: {
      getItem: key => storage.get(key) ?? null,
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: key => storage.delete(key),
    },
    document: { hidden: false },
    setTimeout: (callback, delay) => { timers.push({ callback, delay }); return timers.length; },
    clearTimeout() {},
    setInterval: (callback, delay) => { intervals.push({ callback, delay }); return intervals.length; },
    clearInterval() {},
    fetch: async (url, options = {}) => {
      assert.ok(String(url).startsWith('http://local.test/api/'), 'unexpected fetch target');
      requests.push({ url: String(url), method: options.method || 'GET',
        body: options.body ? JSON.parse(options.body) : undefined });
      return { ok: true, status: 200, json: async () => respond(String(url), options) };
    },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  for (const relative of ['static/js/core.js', 'static/app.js']) {
    vm.runInContext(read(relative), context, { filename: relative });
  }
  const app = vm.runInContext('dashboard()', context);
  app.teamConfig.profileVisibility = { hiddenNames: [...hidden] };
  app.teamConfig.excludedLeaveBalanceNames = ['샘플가', '샘플나'];
  app.teamConfig.dailyOwnerExcludeNames = ['샘플가', '샘플나'];
  return { app, context, storage, requests, timers, intervals };
}

function forExpression(relative, keyPart) {
  // 따옴표 안의 Alpine 화살표(>)까지 포함해 template 시작 태그를 읽는다.
  const tags = read(relative).match(/<template\b(?:[^"'<>]|"[^"]*"|'[^']*')*>/g) || [];
  const tag = tags.find(value => value.includes(keyPart));
  assert.ok(tag, `missing template ${keyPart} in ${relative}`);
  const match = tag.match(/\bx-for\s*=\s*(["'])(.*?)\1/);
  assert.ok(match, `missing x-for ${keyPart}`);
  return match[2];
}

function renderRows(context, app, relative, marker, scope = {}) {
  const expression = forExpression(relative, marker).split(/\s+in\s+/).slice(1).join(' in ');
  return evaluateExpression(context, app, expression, scope);
}

function evaluateExpression(context, app, expression, scope = {}) {
  context.__app = app;
  context.__scope = scope;
  try {
    return vm.runInContext('(function () { with (__app) { with (__scope) { return (' + expression + '); } } })()', context);
  } finally {
    delete context.__app;
    delete context.__scope;
  }
}

async function main() {
  const { app, context, storage, requests } = runtime();
  const policy = { hiddenNames: [...hidden] };
  const visibility = {
    isVisibleProfile: person => context.dashboardProfileVisibility.isVisibleProfile(person, policy),
    visibleProfiles: people => context.dashboardProfileVisibility.visibleProfiles(people, policy),
    memberOptions: (members, selected) => context.dashboardProfileVisibility.memberOptions(members, selected, policy),
  };
  let count = 0;
  async function check(label, run) {
    await run();
    count += 1;
    console.log('PASS ' + label);
  }

  await check('exact profile names are hidden; other employees remain visible', () => {
    assert.ok(visibility && typeof visibility.isVisibleProfile === 'function');
    for (const name of hidden) {
      assert.equal(visibility.isVisibleProfile(name), false);
      assert.equal(visibility.isVisibleProfile(' ' + name + ' '), false);
    }
    for (const name of [...others, '샘플아팀', '샘플바와 협업', '샘플다2']) {
      assert.equal(visibility.isVisibleProfile(name), true, name);
    }
  });

  await check('string and object profile projections never mutate frozen raw data', () => {
    const records = freeze(names.map((name, index) => ({ name, id: String(index), is_active: true })));
    const before = JSON.stringify(records);
    const visible = visibility.visibleProfiles(records);
    assert.deepEqual(plain(visible).map(row => row.name), others);
    assert.equal(JSON.stringify(records), before);
    assert.ok(visible.every(row => records.includes(row)), 'profile objects should remain original references');
    assert.deepEqual(plain(visibility.visibleProfiles(freeze([...names]))), others);
    for (const field of ['user_name', 'owner']) {
      const rows = freeze(names.map(name => ({ [field]: name })));
      assert.deepEqual(plain(visibility.visibleProfiles(rows)).map(row => row[field]), others);
      assert.deepEqual(plain(rows).map(row => row[field]), names);
    }
  });

  await check('new assignee choices hide profiles while stored historical assignees stay selected', () => {
    const raw = freeze([...names]);
    assert.deepEqual(plain(visibility.memberOptions(raw)), others);
    for (const selected of hidden) {
      const options = plain(visibility.memberOptions(raw, selected));
      assert.equal(options.filter(name => name === selected).length, 1);
      for (const name of hidden.filter(name => name !== selected)) assert.ok(!options.includes(name));
      assert.ok(others.every(name => options.includes(name)));
    }
    assert.deepEqual(plain(raw), names);
  });

  const templates = Object.fromEntries(names.map(name => [name, {
    role: '샘플 역할', color: '#64748b', sections: ['승인 기록 원문'],
  }]));
  app.teamConfig.members = [...names];
  app.teamConfig.memberReportTemplates = plain(templates);
  const configBefore = JSON.stringify(app.teamConfig);

  await check('actual dashboard profile getters hide all three without deleting raw members/templates', () => {
    assert.deepEqual(plain(app.visibleMembers), others);
    assert.deepEqual(plain(app.visibleMemberReportNames), others);
    assert.deepEqual(plain(app.memberReportNames), names);
    assert.equal(JSON.stringify(app.teamConfig), configBefore);
    assert.deepEqual(plain(app.visibleProfiles(names.map(name => ({ name })))).map(row => row.name), others);
  });

  await check('HR current profile rows and balances hide names while all raw HR data remains intact', () => {
    const rows = freeze(names.map((name, index) => ({ id: index + 1, user_name: name, status: '정상', total_days: 15 })));
    app.hrAttendance = rows;
    app.hrMonthly = rows;
    app.hrLeaveBalance = rows;
    assert.deepEqual(plain(app.visibleProfiles(app.hrAttendance)).map(row => row.user_name), others);
    assert.deepEqual(plain(app.visibleProfiles(app.hrMonthly)).map(row => row.user_name), others);
    assert.deepEqual(plain(app.hrLeaveBalanceVisible).map(row => row.user_name),
      others.filter(name => !['샘플가', '샘플나'].includes(name)));
    assert.strictEqual(app.hrAttendance, rows);
    assert.strictEqual(app.hrMonthly, rows);
    assert.strictEqual(app.hrLeaveBalance, rows);
    assert.deepEqual(plain(rows).map(row => row.user_name), names);
    assert.equal(forExpression('static/partials/tab-hr.html', "'att-'"), 'row in visibleProfiles(hrAttendance)');
    assert.equal(forExpression('static/partials/tab-hr.html', "'hms-'"), 'row in hrMonthlyVisible');
  });

  await check('monthly attendance selection switches and clears without mutating frozen history or other HR views', () => {
    const rows = freeze([...names, '샘플사팀'].map((name, index) => ({
      user_name: name, days: index + 1, avg_check_in: '09:00', avg_check_out: '18:00',
    })));
    const before = plain(rows);
    const attendance = app.hrAttendance;
    const balances = app.hrLeaveBalance;
    const actor = plain(app.currentUser);
    const requestCount = requests.length;
    app.hrMonthly = rows;
    const emptyMatch = read('static/partials/tab-hr.html').match(/<div\s+x-show="([^"]+)"[^>]*>데이터 없음<\/div>/);
    assert.ok(emptyMatch, 'monthly attendance empty state must be present');
    assert.equal(emptyMatch[1], '!hrMonthlyLoading && !hrMonthlyError && hrMonthlyVisible.length === 0');
    const visibleNames = [...others, '샘플사팀'];
    for (const [user, expected] of [
      ['', visibleNames],
      [' 샘플사 ', ['샘플사']],
      ['샘플마', ['샘플마']],
      ['', visibleNames],
      ['자료없는사용자', []],
      ...hidden.map(name => [name, []]),
      ['   ', visibleNames],
    ]) {
      app.hrUser = user;
      const rendered = renderRows(context, app, 'static/partials/tab-hr.html', "'hms-'");
      assert.deepEqual(plain(rendered).map(row => row.user_name), expected, user || 'no selection');
      assert.ok(rendered.every(row => rows.includes(row)), 'projected rows retain original references');
      assert.equal(evaluateExpression(context, app, emptyMatch[1]), expected.length === 0, 'empty state follows the table filter');
      assert.strictEqual(app.hrMonthly, rows);
      assert.deepEqual(plain(app.hrMonthly), before);
    }
    assert.strictEqual(app.hrAttendance, attendance);
    assert.strictEqual(app.hrLeaveBalance, balances);
    assert.deepEqual(plain(app.currentUser), actor);
    assert.equal(requests.length, requestCount, 'selection only changes the local projection');
    app.hrUser = '';
  });

  await check('month and year reloads retain the selected user and every original monthly API result', async () => {
    const periods = {
      '2026-10': freeze(names.map((name, index) => ({ user_name: name, days: index + 1, avg_check_in: '09:10' }))),
      '2026-11': freeze(names.map((name, index) => ({ user_name: name, days: index + 10, avg_check_in: '09:20' }))),
      '2027-1': freeze([{ user_name: '샘플마', days: 3 }, { user_name: '샘플아', days: 4 }]),
    };
    const original = plain(periods);
    const monthly = runtime(url => {
      const request = new URL(url);
      assert.equal(request.pathname, '/api/attendance/monthly');
      return { rows: periods[request.searchParams.get('year') + '-' + request.searchParams.get('month')] };
    });
    monthly.app.hrUser = '샘플사';
    for (const [year, month] of [['2026', '10'], ['2026', '11'], ['2027', '1']]) {
      monthly.app.hrMonthlyYear = year;
      monthly.app.hrMonthlyMonth = month;
      await monthly.app.loadMonthly();
      const rows = periods[year + '-' + month];
      assert.equal(monthly.app.hrUser, '샘플사');
      assert.strictEqual(monthly.app.hrMonthly, rows, 'the API result is kept whole');
      assert.deepEqual(plain(monthly.app.hrMonthlyVisible), plain(rows.filter(row => row.user_name === '샘플사')));
    }
    monthly.app.hrUser = '샘플마';
    assert.deepEqual(plain(monthly.app.hrMonthlyVisible), [{ user_name: '샘플마', days: 3 }]);
    monthly.app.hrUser = '';
    assert.deepEqual(plain(monthly.app.hrMonthlyVisible), [{ user_name: '샘플마', days: 3 }]);
    assert.deepEqual(plain(periods), original);
    assert.deepEqual(monthly.requests, ['2026&month=10', '2026&month=11', '2027&month=1'].map(period => ({
      url: 'http://local.test/api/attendance/monthly?year=' + period, method: 'GET', body: undefined,
    })));
  });

  await check('월간 상세와 반차 안내는 동일한 사용자 필터를 적용하며 원본·중복·승인 기록을 보존한다', () => {
    const records = freeze([...names, '샘플사팀'].map((name, index) => ({
      key: 'attendance-' + index, id: index + 1, user_name: name, work_date: '2026-11-04',
      clock_in: '2026-11-04T09:10:00', clock_out: '2026-11-04T17:00:00',
      raw_status: '조퇴', status: '정상', note: name + ' 원문', has_attendance: true,
      approved_half_days: [],
    })));
    const repeated = freeze({ ...records.find(row => row.user_name === '샘플사'), key: 'attendance-duplicate', id: 101 });
    const leaveOnly = freeze({
      key: 'leave-only', id: null, user_name: '샘플사', work_date: '2026-11-05',
      clock_in: null, clock_out: null, raw_status: null, status: '무급오전반차',
      has_attendance: false, approved_half_days: [{ id: 'approved-half', leave_type: '무급오전반차' }],
    });
    const raw = freeze([...records, repeated, leaveOnly]);
    const warnings = freeze(names.map(user_name => ({ id: user_name, user_name, leave_type: '오전반차' })));
    app.hrMonthlyRecords = raw;
    app.hrMonthlyHalfdayWarnings = warnings;
    const before = plain({ raw, warnings });
    for (const selected of ['', ' 샘플사 ', '샘플마', '샘플사팀', '없는사용자', ...hidden, '   ']) {
      app.hrUser = selected;
      const user = selected.trim();
      const expected = raw.filter(row => !hidden.includes(row.user_name) && (!user || row.user_name === user));
      const rendered = renderRows(context, app, 'static/partials/tab-hr.html', "'hmd-'");
      assert.deepEqual(plain(rendered), plain(expected));
      assert.ok(rendered.every(row => raw.includes(row)));
      assert.deepEqual(plain(app.hrMonthlyHalfdayWarningsVisible), plain(warnings.filter(row =>
        !hidden.includes(row.user_name) && (!user || row.user_name === user))));
      assert.strictEqual(app.hrMonthlyRecords, raw);
      assert.strictEqual(app.hrMonthlyHalfdayWarnings, warnings);
    }
    app.hrUser = '샘플사';
    assert.equal(app.hrMonthlyRecordsVisible.length, 3, '같은 날의 두 실기록과 반차 전용 행을 합치지 않는다');
    assert.equal(app.attendanceTime(leaveOnly.clock_in), '—');
    assert.equal(app.attendanceTime(leaveOnly.clock_out), '—');
    assert.equal(app.attendanceTime(records[0].clock_in), '09:10');
    assert.equal(app.attendanceTime(records[0].clock_out), '17:00');
    assert.equal(app.hrMonthlyRecordsVisible.at(-1).status, '무급오전반차');
    assert.deepEqual(plain({ raw: app.hrMonthlyRecords, warnings: app.hrMonthlyHalfdayWarnings }), before);
    app.hrUser = '';
  });

  await check('월간 응답의 상세·반차 안내를 원본으로 보존하고 연월 변경에도 선택을 유지한다', async () => {
    const periods = freeze({
      '10': { rows: [{ user_name: '샘플사', morning_half: 1, afternoon_half: 0 }],
        records: [{ key: 'oct', user_name: '샘플사', work_date: '2026-10-31', status: '오전반차', raw_status: '정상', approved_half_days: [{ id: 'oct-leave', leave_type: '오전반차' }] }],
        halfday_warnings: [{ id: 'warning', user_name: '샘플마', leave_type: '오후반차' }] },
      '11': { rows: [{ user_name: '샘플사', morning_half: 0, afternoon_half: 1 }],
        records: [{ key: 'nov', user_name: '샘플사', work_date: '2026-11-04', status: '지각 · 오후반차', raw_status: '지각', approved_half_days: [{ id: 'nov-leave', leave_type: '오후반차' }] }],
        halfday_warnings: [] },
    });
    const monthly = runtime(url => periods[new URL(url).searchParams.get('month')]);
    monthly.app.hrUser = '샘플사';
    for (const month of ['10', '11']) {
      monthly.app.hrMonthlyYear = '2026';
      monthly.app.hrMonthlyMonth = month;
      await monthly.app.loadMonthly();
      assert.strictEqual(monthly.app.hrMonthly, periods[month].rows);
      assert.strictEqual(monthly.app.hrMonthlyRecords, periods[month].records);
      assert.strictEqual(monthly.app.hrMonthlyHalfdayWarnings, periods[month].halfday_warnings);
      assert.equal(monthly.app.hrUser, '샘플사');
      assert.equal(monthly.app.hrMonthlyLoading, false);
      assert.equal(monthly.app.hrMonthlyError, '');
    }
  });

  await check('이전 월의 늦은 응답과 실패가 현재 월의 상세·선택·로딩 상태를 덮어쓰지 않는다', async () => {
    const pending = new Map();
    const monthly = runtime(url => new Promise((resolve, reject) => {
      pending.set(new URL(url).searchParams.get('month'), { resolve, reject });
    }));
    const toasts = [];
    monthly.app.showToast = (...args) => toasts.push(args);
    monthly.app.hrUser = '샘플사';
    monthly.app.hrMonthlyYear = '2026';
    monthly.app.hrMonthlyMonth = '10';
    const october = monthly.app.loadMonthly();
    await new Promise(resolve => setImmediate(resolve));
    monthly.app.hrMonthlyMonth = '11';
    const november = monthly.app.loadMonthly();
    await new Promise(resolve => setImmediate(resolve));
    const latest = freeze({ rows: [{ user_name: '샘플사', days: 4 }], records: [{ key: 'latest', user_name: '샘플사' }], halfday_warnings: [] });
    pending.get('11').resolve(latest);
    await november;
    pending.get('10').resolve({ rows: [], records: [], halfday_warnings: [] });
    await october;
    assert.strictEqual(monthly.app.hrMonthlyRecords, latest.records);
    assert.strictEqual(monthly.app.hrMonthly, latest.rows);
    assert.equal(monthly.app.hrUser, '샘플사');
    assert.equal(monthly.app.hrMonthlyLoading, false);
    assert.deepEqual(toasts, []);

    monthly.app.hrMonthlyMonth = '9';
    const staleFailure = monthly.app.loadMonthly();
    await new Promise(resolve => setImmediate(resolve));
    monthly.app.hrMonthlyMonth = '11';
    const current = monthly.app.loadMonthly();
    await new Promise(resolve => setImmediate(resolve));
    pending.get('9').reject(new Error('이전 월의 합성 오류'));
    await staleFailure;
    assert.equal(monthly.app.hrMonthlyLoading, true, '이전 요청 종료가 새 요청 로딩을 끄지 않는다');
    assert.deepEqual(toasts, []);
    pending.get('11').resolve(latest);
    await current;
    monthly.context.fetch = async () => ({ ok: false, status: 500 });
    await monthly.app.loadMonthly();
    assert.equal(monthly.app.hrMonthlyLoading, false);
    assert.match(monthly.app.hrMonthlyError, /월간 근태를 불러오지 못했습니다/);
    assert.equal(toasts.length, 1);
    assert.strictEqual(monthly.app.hrMonthlyRecords, latest.records, '실패해도 기존 원본을 변형하지 않는다');
  });

  await check('승인 반차 배지와 합성 행은 수동 상태·시간 수정 대상으로 바꾸지 않는다', async () => {
    const local = runtime();
    local.app.currentUser = { name: '샘플사', role: 'pm' };
    const half = freeze({ id: 9, user_name: '샘플사', status: '오전반차', raw_status: '조퇴', approved_half_days: [{ id: 'leave', leave_type: '오전반차' }] });
    const synthetic = freeze({ ...half, id: null, has_attendance: false });
    assert.equal(local.app.hasApprovedHalfDay(half), true);
    assert.equal(local.app.canEditAttendance(synthetic), false);
    await local.app.editAttendance(synthetic, 'clock_in');
    await local.app.editAttendanceStatus(synthetic, '정상');
    await local.app.deleteAttendance(synthetic);
    await local.app.editAttendanceStatus(half, '정상');
    assert.deepEqual(local.requests, []);
    assert.equal(half.raw_status, '조퇴');
    const markup = read('static/partials/tab-hr.html');
    assert.match(markup, /x-if="canEditAttendance\(row\) && !hasApprovedHalfDay\(row\)"/);
    assert.match(markup, /x-if="!canEditAttendance\(row\) \|\| hasApprovedHalfDay\(row\)"/);
    assert.doesNotMatch(markup, /<option value="조퇴">/);
    assert.doesNotMatch(read('static/index.html'), /지각·조퇴/);
  });

  await check('확인 필요 등 현재 상태를 명확히 표시하고 정상·지각 수정 선택지를 유지한다', () => {
    const markup = read('static/partials/tab-hr.html');
    const select = (markup.match(/<select\b[^>]*>[\s\S]*?<\/select>/g) || [])
      .find(value => value.includes('editAttendanceStatus(row, $event.target.value)'));
    assert.ok(select);
    const current = select.match(/<template x-if="([^"]+)">\s*(<option\b[^>]*><\/option>)/);
    assert.ok(current, '일반 선택지에 없는 현재 상태를 별도 표시한다');
    assert.match(current[2], /\bselected\b/);
    assert.match(current[2], /\bdisabled\b/);
    assert.match(select, /<option value="정상">정상<\/option>/);
    assert.match(select, /<option value="지각">지각<\/option>/);
    for (const status of ['확인 필요', '기타 원문 상태', '', '정상', '지각']) {
      const row = freeze({ id: 50, user_name: '샘플사', status, raw_status: '조퇴', approved_half_days: [] });
      assert.equal(evaluateExpression(context, app, current[1], { row }), !['정상', '지각'].includes(status));
      const label = current[2].match(/x-text="([^"]+)"/)[1];
      const value = current[2].match(/:value="([^"]+)"/)[1];
      assert.equal(evaluateExpression(context, app, label, { row }), status || '확인 필요');
      assert.equal(evaluateExpression(context, app, value, { row }), status || '');
      assert.equal(row.status, status);
      assert.equal(row.raw_status, '조퇴');
    }
  });

  await check('출퇴근·수정·삭제·연차 승인과 취소 성공 시 월간·일별·본인 현황을 다시 조회한다', async () => {
    for (const operation of ['clockIn', 'clockOut', 'editAttendance', 'editAttendanceStatus', 'deleteAttendance', 'approveLeave', 'cancelLeave', 'submitLeave']) {
      const local = runtime(() => ({ time: '09:00', status: '정상' }));
      local.app.currentUser = { name: '샘플사', role: 'pm' };
      local.app.hrUser = '샘플마';
      local.context.confirm = () => true;
      local.context.prompt = () => '10:15';
      local.app.showToast = () => {};
      const calls = [];
      for (const method of ['loadAttendance', 'loadMonthly', 'loadMyTodayAttendance', 'loadLeave']) {
        local.app[method] = async () => calls.push(method);
      }
      const record = freeze({ id: 42, user_name: '샘플마', work_date: '2026-11-04', clock_in: '2026-11-04T09:00:00', status: '정상', approved_half_days: [] });
      if (operation === 'editAttendance') await local.app[operation](record, 'clock_in');
      else if (operation === 'editAttendanceStatus') await local.app[operation](record, '지각');
      else if (operation === 'deleteAttendance') await local.app[operation](record);
      else if (operation === 'approveLeave') await local.app[operation]('sample-leave', '승인');
      else if (operation === 'cancelLeave') await local.app[operation]('sample-leave');
      else if (operation === 'submitLeave') {
        local.app.hrLeaveForm = { leave_type: '오전반차', start_date: '2026-11-04', end_date: '2026-11-04', days: 0.5, reason: '합성 사유' };
        await local.app[operation]();
      } else await local.app[operation]();
      assert.deepEqual(calls.filter(method => method !== 'loadLeave').sort(), ['loadAttendance', 'loadMonthly', 'loadMyTodayAttendance'].sort(), operation);
      assert.equal(calls.includes('loadLeave'), ['approveLeave', 'cancelLeave', 'submitLeave'].includes(operation), operation);
      assert.equal(local.app.currentUser.name, '샘플사');
      assert.equal(local.app.hrUser, '샘플마');
      assert.equal(record.status, '정상', '서버의 표시 상태를 원본 행에 임의 덮어쓰지 않는다');
      if (operation === 'clockIn' || operation === 'clockOut') assert.deepEqual(local.requests[0].body, {});
    }
  });

  await check('근태 수정과 반차 승인·취소 후 관리자 KPI를 갱신하고 PM은 관리자 API를 호출하지 않는다', async () => {
    for (const role of ['admin', 'pm']) {
      for (const operation of ['editAttendanceStatus', 'approveLeave', 'cancelLeave']) {
        const local = runtime(() => ({ hr_status: { attendance_issues: 7, leave_used: 1 } }));
        local.app.currentUser = { name: '합성 관리자', role };
        local.context.confirm = () => true;
        const calls = [];
        for (const method of ['loadAttendance', 'loadMonthly', 'loadMyTodayAttendance', 'loadLeave']) {
          local.app[method] = async () => calls.push(method);
        }
        if (operation === 'editAttendanceStatus') {
          await local.app.editAttendanceStatus(freeze({ id: 8, status: '정상', approved_half_days: [] }), '지각');
        } else if (operation === 'approveLeave') {
          await local.app.approveLeave('synthetic-halfday', '승인');
        } else await local.app.cancelLeave('synthetic-halfday');
        const summary = local.requests.filter(request => request.url.endsWith('/api/dashboard/executive-summary'));
        assert.equal(summary.length, role === 'admin' ? 1 : 0, role + ' ' + operation);
        assert.equal(local.app.executiveSummary.loaded, role === 'admin');
        if (role === 'admin') assert.equal(local.app.executiveSummary.hrStatus.attendanceIssues, 7);
        for (const method of ['loadAttendance', 'loadMonthly', 'loadMyTodayAttendance']) {
          assert.equal(calls.filter(value => value === method).length, 1, role + ' ' + operation + ' ' + method);
        }
      }
    }
  });

  const approvals = freeze(hidden.map((name, index) => ({
    id: 'approved-' + index, req_type: 'personal_expense', status: '승인',
    requester: name, pm_approver: '샘플바', admin_approver: '샘플아',
    pm_status: '승인', admin_status: '승인', detail: name + ' 승인 원문',
    amount: 1000 + index, cert_info: { name }, attachments: [{ name: name + ' 증빙.pdf', url: '/offline.pdf' }],
  })));
  const leaves = freeze(hidden.map((name, index) => ({
    id: 'leave-' + index, user_name: name, status: '승인',
    pm_status: '승인', admin_status: '승인', pm_approver: '샘플다', admin_approver: '샘플아',
    start_date: '2026-10-30', end_date: '2026-10-30', days: 1, reason: name + ' 기존 사유',
  })));
  const expenses = freeze(hidden.map((name, index) => ({
    rowNum: index + 1, manager: name, manager2: name, depositor: name,
    status: ['이체완료', '결제완료', '카드결제'][index], amount: 1000 + index,
    description: name + ' 기존 비용 기록',
  })));
  app.approvalItems = approvals;
  app.approvalFilter = 'all';
  app.approvalStatusFilter = '';
  app.hrLeaveRequests = leaves;
  app.expenseRows = expenses;

  await check('approved approval/leave/paid expense names, states and bodies remain exact', () => {
    assert.deepEqual(plain(app.filteredApprovals), plain(approvals));
    assert.strictEqual(app.hrLeaveRequests, leaves);
    assert.strictEqual(app.expenseRows, expenses);
    for (const record of app.filteredApprovals) assert.ok(hidden.includes(record.requester));
    assert.equal(forExpression('static/partials/tab-approval.html', "'apr-'"), 'item in filteredApprovals');
    assert.equal(forExpression('static/partials/tab-hr.html', "'lr-'"), 'req in hrLeaveRequests');
    assert.equal(forExpression('static/partials/tab-expenses.html', "'exp-'"), 'row in expenseRows');
  });

  await check('pending and rejected business history also remains visible', () => {
    const extra = freeze([
      { id: 'pending', requester: '샘플다', status: '대기', pm_status: '승인', admin_status: '대기' },
      { id: 'rejected', requester: '샘플아', status: '반려', pm_status: '반려', admin_status: '대기' },
    ]);
    app.approvalItems = freeze([...approvals, ...extra]);
    assert.deepEqual(plain(app.filteredApprovals), plain(app.approvalItems));
  });

  await check('calendar leave titles, attendees and memo authors keep their historical names', () => {
    app.calEvents = freeze([{
      id: 'historical-leave', title: '[연차] 샘플아', description: '승인된 기존 사유 샘플바·샘플다',
      startDate: '2026-10-30', endDate: '2026-10-31', allDay: true, attendees: ['샘플바', '샘플다'],
    }]);
    app.hrUser = '';
    app.holidaysMap = {};
    const event = app._eventsForDate('2026-10-30').find(row => row.id === 'historical-leave');
    assert.equal(event.title, '[연차] 샘플아');
    assert.equal(event.description, '승인된 기존 사유 샘플바·샘플다');
    assert.deepEqual(plain(app.calEvents[0].attendees), ['샘플바', '샘플다']);
    app.sharedMemos = freeze(hidden.map(name => ({ id: name, author: name, content: name + ' 원문' })));
    assert.deepEqual(plain(app.sharedMemos).map(row => row.author), hidden);
    assert.equal(forExpression('static/partials/tab-notes.html', "'catt-'"), 'att in (ev.attendees || [])');
    assert.equal(forExpression('static/partials/tab-notes.html', "'memo-'"), 'memo in sharedMemos');
  });

  await check('actual profile and picker template expressions hide all three without changing raw rows', () => {
    app.execList = freeze(names.map(name => ({ name })));
    app.slackUsersList = freeze(names.map((name, id) => ({ name, id })));
    app.settingsUsers = freeze(names.map((name, id) => ({ name, id, is_active: true })));
    for (const [relative, marker] of [
      ['static/index.html', "'mp-'"],
      ['static/index.html', "'exec-'"],
      ['static/index.html', "'cfg-'"],
      ['static/index.html', "'mr-'"],
      ['static/index.html', "'slack-u-'"],
      ['static/index.html', "'tro-'"],
      ['static/index.html', "'tpfo-'"],
      ['static/index.html', "'ta-'"],
      ['static/index.html', "'tb-'"],
      ['static/index.html', ':key="m"'],
      ['static/index.html', "'ro-'"],
      ['static/index.html', "'po-'"],
      ['static/index.html', "'fa-'"],
      ['static/partials/tab-expenses.html', "'exp-m-'"],
      ['static/partials/tab-hr.html', "'hr-u-'"],
      ['static/partials/tab-settings.html', "'us-'"],
      ['static/partials/tab-notes.html', "'calatt-'"],
      ['static/partials/tab-notes.html', "'note-a-'"],
      ['static/partials/tab-todos.html', "'tf-'"],
    ]) {
      const actual = plain(renderRows(context, app, relative, marker)).map(row => typeof row === 'string' ? row : row.name);
      assert.deepEqual(actual, others, relative + ' ' + marker);
    }
    for (const field of ['execList', 'slackUsersList', 'settingsUsers']) {
      assert.deepEqual(plain(app[field]).map(row => row.name), names);
    }
    for (const selected of hidden) {
      const actual = plain(renderRows(context, app, 'static/partials/tab-expenses.html', "'exp-m2-'", { row: { manager2: selected } }));
      assert.deepEqual(actual, [...others, selected]);
    }
    assert.match(read('static/index.html'), /x-show="isVisibleProfile\(assignee\)"/);
    assert.match(read('static/index.html'), /x-show="isVisibleProfile\(role\.owner\)"/);
  });

  await check('approver and calendar-person pickers hide profiles while source accounts and historical people remain intact', () => {
    app.hrApprovers = freeze(['pm', 'admin'].flatMap(role => names.map(name => ({ name, role }))));
    const before = plain(app.hrApprovers);
    for (const [role, marker] of [['pm', "'sett-pm-'"], ['admin', "'sett-ad-'"]]) {
      const actual = plain(renderRows(context, app, 'static/partials/tab-settings.html', marker));
      assert.deepEqual(actual, others.map(name => ({ name, role })));
    }
    assert.deepEqual(plain(app.hrApprovers), before);
    const people = freeze([...names]);
    assert.deepEqual(plain(renderRows(context, app, 'static/partials/tab-notes.html', "'calf-'", { allCalPersons: people })), others);
    assert.deepEqual(plain(people), names);
  });

  await check('daily profile cards hide names while raw owner getters, tasks, comments and cycle history keep every name', () => {
    const projects = freeze(names.map(name => ({ id: name, owner: name, title: name + ' 기존 업무', status: 'active' })));
    const results = freeze(hidden.map(name => ({ id: name + '-done', owner: name, title: name + ' 완료 이력', status: 'done' })));
    const comments = freeze(Object.fromEntries(hidden.map(name => [name, [{
      id: name + '-comment', author: name, content: name + ' 댓글 원문', createdAt: '2026-10-30T00:00:00Z',
    }]])));
    app.data.activeProjects.projects = projects;
    app.data.completedResults.items = results;
    app._commentsCache = comments;
    app.dailyFilter.excludeDone = false;
    const before = plain({ projects, results, comments });
    assert.deepEqual(plain(Object.keys(app.dailyOwners)), names);
    assert.deepEqual(plain(app.dailyOwnerNames), [...names].sort());
    assert.deepEqual(plain(renderRows(context, app, 'static/partials/tab-daily.html', "'daily-owner-'")), [...others].sort());
    for (const name of hidden) {
      assert.equal(app.dailyOwners[name].length, 2);
      const grouped = app.groupCommentsByWeek(name);
      assert.equal(grouped.length, 1);
      assert.deepEqual(plain(grouped[0].comments), plain(comments[name]));
    }
    assert.equal(forExpression('static/partials/tab-daily.html', "'cya-'"), '(issues, assignee) in filteredByAssignee(cycle.byAssignee)');
    assert.deepEqual(plain({ projects: app.data.activeProjects.projects, results: app.data.completedResults.items, comments: app._commentsCache }), before);
  });

  await check('all existing HTML CSS classes remain identical to the production baseline', () => {
    // 기존 화면의 class/:class 속성값. 공개 버전에서는 대표 색상 조건만 이름과 독립적으로 바뀐다.
    const signatures = {
      'static/index.html': 'a8ac9621c7fb9e81e91f1992f01e3619b709e143592a06c548c030d74e1053c9',
      'static/partials/tab-daily.html': '60ec16235d8975099a160ebc627892f266583cf8d01b0cd274338051a7d6cf50',
      'static/partials/tab-expenses.html': '1f59ef52169e38d446b26620dd2103861052fa7a7b9cb1e55389bfa83668e92b',
      'static/partials/tab-hr.html': '87ca03dfd087fb3bf6a7f1a545d1a648b2af2e1781f5c0ccfb7cf0248d8c8e80',
      'static/partials/tab-notes.html': '07ad269cf57ce90dd22eec7f8b7b6e61c8cab91e4019f659ed8b117f0f60cf52',
      'static/partials/tab-settings.html': '30e77a4948cf8221019b5bf9f48a00622f50d881a6b21ba8974ea41aef2b90c1',
      'static/partials/tab-todos.html': '8ec4a99db628866a048a84f6d2034d4d8e84b4d094be3bdfc0eeb3d6ad54820f',
    };
    for (const [relative, expected] of Object.entries(signatures)) {
      let markup = read(relative).replace(/\r\n/g, '\n');
      if (relative === 'static/partials/tab-hr.html') {
        const classValues = value => [...value.matchAll(/(?:^|\s):?class\s*=\s*("[^"]*"|'[^']*')/g)].map(match => match[1]);
        // 새 안내·페이지 영역만 분리하고, 기존 화면의 기준 해시는 유지한다.
        for (const label of ['근태 범위 안내', '연간 상세 페이지']) {
          const pattern = new RegExp('<!-- ' + label + ' 시작 -->[\\s\\S]*?<!-- ' + label + ' 끝 -->', 'g');
          const matches = [...markup.matchAll(pattern)];
          assert.equal(matches.length, 1, label + ' 경계를 하나만 허용한다');
          const allowed = label === '근태 범위 안내' ? ['"text-[10px] text-slate-400"'] : [
            '"mt-2 flex items-center gap-2 text-[11px]"',
            '"px-2 py-1 border border-slate-200 rounded disabled:opacity-30"',
            '"text-slate-500 tabular-nums"',
          ];
          for (const value of classValues(matches[0][0])) assert.ok(allowed.includes(value), '기존 안내·페이지 버튼 스타일만 재사용한다');
          markup = markup.replace(pattern, '');
        }
        // 적색 누락 표시 네 셀만 정상 상태의 기존 class로 역변환한다.
        for (const [marker, base, flag, normal, original] of [
          ['date', 'px-4 py-2 font-medium', 'is_missing', 'text-slate-700', 'px-4 py-2 font-medium text-slate-700'],
          ['clock-in', 'px-4 py-2 text-center tabular-nums transition', 'missing_clock_in', 'text-slate-600', 'px-4 py-2 text-center text-slate-600 tabular-nums transition'],
          ['clock-out', 'px-4 py-2 text-center tabular-nums transition', 'missing_clock_out', 'text-slate-600', 'px-4 py-2 text-center text-slate-600 tabular-nums transition'],
          ['status', 'px-4 py-2 text-center', 'is_missing', 'text-slate-600', 'px-4 py-2 text-center text-slate-600'],
        ]) {
          const tag = markup.match(new RegExp('<td data-hr-missing="' + marker + '"[^>]*>'));
          assert.ok(tag, marker);
          const requested = 'class="' + base + '" :class="row.' + flag + " === true ? 'text-red-600' : '" + normal + "'\"";
          assert.ok(tag[0].includes(requested), marker + '는 서버 플래그만 색상에 사용한다');
          markup = markup.replace(tag[0], tag[0].replace(requested, 'class="' + original + '"'));
        }
        // 이번 요청의 추가 영역만 분리한다. 기존 영역의 기준 해시는 재생성하지 않는다.
        const additions = [];
        for (const label of ['월간 조회 안내', '월간 상세']) {
          const pattern = new RegExp('<!-- ' + label + ' 시작 -->[\\s\\S]*?<!-- ' + label + ' 끝 -->', 'g');
          const matches = [...markup.matchAll(pattern)];
          assert.equal(matches.length, 1, label + ' 경계를 하나만 허용한다');
          additions.push(matches[0][0]);
          markup = markup.replace(pattern, '');
        }
        const afternoon = [...markup.matchAll(/<(th|td) data-hr-monthly-afternoon\b[^>]*>[\s\S]*?<\/\1>/g)];
        assert.equal(afternoon.length, 2, '오후반차 헤더와 셀만 추가한다');
        assert.match(afternoon[0][0], />오후반차<\/th>/);
        assert.match(afternoon[1][0], /x-text="row\.afternoon_half \|\| 0"/);
        const reused = new Set(classValues(markup));
        for (const addition of [...additions, ...afternoon.map(match => match[0])]) {
          for (const value of classValues(addition)) assert.ok(reused.has(value), '기존 HR 클래스만 재사용: ' + value);
        }
        assert.doesNotMatch(additions[1], /@click|@change|x-model/, '새 상세표는 읽기 전용이다');
        markup = markup.replace(/<(th|td) data-hr-monthly-afternoon\b[^>]*>[\s\S]*?<\/\1>/g, '');
      }
      const attrs = [...markup.matchAll(/(?:^|\s):?class\s*=\s*("[^"]*"|'[^']*')/g)].map(match => {
        let value = match[1];
        if (relative === 'static/partials/tab-hr.html') {
          // id 없는 행의 편집 금지와 승인 반차 배지 조건만 기존 조건으로 역변환한다.
          value = value.replaceAll('canEditAttendance(row)', '(isAdmin()||isPm())')
            .replaceAll('hasApprovedHalfDay(row)', "row.status === '조퇴'");
        }
        return value;
      });
      const actual = createHash('sha256').update(attrs.join('\n')).digest('hex');
      assert.equal(actual, expected, relative);
    }
  });

  await check('team and member template saves keep hidden profiles in raw payloads', async () => {
    app.saveTeam();
    const first = requests.filter(row => row.url.endsWith('/api/team-config')).at(-1);
    assert.ok(first);
    assert.deepEqual(first.body.members, names);
    assert.deepEqual(first.body.memberReportTemplates, templates);
    app.memberReportData = {};
    await app.saveMemberTemplates();
    const second = requests.filter(row => row.url.endsWith('/api/team-config')).at(-1);
    assert.deepEqual(second.body.members, names);
    assert.deepEqual(second.body.memberReportTemplates, templates);
    const stored = [...storage.values()].map(value => {
      try { return JSON.parse(value); } catch { return null; }
    }).find(value => value && Array.isArray(value.members));
    assert.deepEqual(stored.members, names);
    assert.equal(JSON.stringify(app.teamConfig), configBefore);
  });

  await check('saving existing approval routing retains configured hidden names and leaves history unchanged', async () => {
    app.approvalTypeApprovers = {
      leave: { pm: '샘플바', admin: '샘플아' },
      personal_expense: { pm: '샘플다', admin: '샘플나' },
    };
    const before = plain(app.approvalTypeApprovers);
    await app.saveApprovalTypeApprovers();
    const request = requests.filter(row => row.url.endsWith('/api/approval-settings')).at(-1);
    assert.ok(request);
    assert.deepEqual(request.body.type_approvers, before);
    assert.deepEqual(plain(app.approvalTypeApprovers), before);
    assert.deepEqual(plain(app.filteredApprovals), plain(app.approvalItems));
    assert.deepEqual(plain(app.hrLeaveRequests), plain(leaves));
  });

  await check('report save/export keeps hidden owners and approved record content', () => {
    app.data.activeProjects.projects = hidden.map(name => ({ id: name, owner: name, status: 'done', title: name + ' 원문' }));
    const before = plain(app.data);
    app.save();
    const request = requests.filter(row => row.url.endsWith('/api/report')).at(-1);
    assert.ok(request);
    assert.deepEqual(request.body.activeProjects, before.activeProjects);
    assert.deepEqual(JSON.parse(app.exportJson()).activeProjects, before.activeProjects);
    assert.deepEqual(plain(app.filteredApprovals), plain(app.approvalItems));
    assert.deepEqual(plain(app.hrLeaveRequests), plain(leaves));
    assert.deepEqual(plain(app.expenseRows), plain(expenses));
  });

  console.log(`Profile visibility: ${count} checks passed; offline only, no DB or server writes.`);
}

main().catch(error => {
  console.error(error.stack || error.message);
  process.exitCode = 1;
});
