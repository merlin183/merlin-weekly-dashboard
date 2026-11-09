// 주간 업무 보고 대시보드 - 편집 가능 버전
// localStorage + 서버 동기화 기반 데이터 영속성
// ⚠️ 인증/fetch/첨부 전역 유틸(_getAuthToken·_authHeaders·_authFetch·openAttachment·proxyUrl·
//    AUTH_TOKEN_KEY)은 static/js/core.js 로 분리됨 — index.html에서 app.js보다 먼저 로드.

function dashboard() {
  const STORAGE_KEY = 'weeklyReportData';
  const API_BASE = location.origin;
  // index.html 인라인 Alpine 식(x-init 등)에서도 참조할 수 있게 전역 노출
  // (함수 스코프 const라 Alpine이 못 봐서 '설정' 탭에서 API_BASE undefined 예외가 났음)
  window.API_BASE = API_BASE;

  // 같은 세션에서 진행 중인 결재 목록 조회만 공유한다. 완료된 응답은 캐시하지 않는다.
  let approvalReadInFlight = null;
  let approvalReadToken = '';
  let approvalReadGeneration = 0;
  function invalidateApprovalReads() {
    approvalReadGeneration += 1;
    approvalReadInFlight = null;
  }
  function readApprovals() {
    const token = _getAuthToken();
    if (token !== approvalReadToken) {
      invalidateApprovalReads();
      approvalReadToken = token;
    }
    if (!approvalReadInFlight) {
      const generation = approvalReadGeneration;
      const pending = _authFetch(API_BASE + '/api/approvals')
        .then(async res => ({ data: res.ok ? await res.json() : null, token, generation }))
        .finally(() => { if (approvalReadInFlight === pending) approvalReadInFlight = null; });
      approvalReadInFlight = pending;
    }
    return approvalReadInFlight;
  }
  function isCurrentApprovalRead(result) {
    return result && result.token === _getAuthToken() && result.generation === approvalReadGeneration;
  }

  function uid() {
    return Date.now().toString(36) + Math.random().toString(36).substr(2, 5);
  }

  // 새 설치에는 업무 지표를 넣지 않는다. 관리자가 직접 지표를 추가한다.
  function getDefaultBizMetrics() {
    return [];
  }

  function load() {
    try {
      const s = localStorage.getItem(STORAGE_KEY);
      if (!s) return null;
      const d = JSON.parse(s);
      // 마이그레이션: 신규 필드 보완
      if (!d.meetingAgenda) d.meetingAgenda = { title: '팀 내 회의', subtitle: '주간 회의 안건', items: [] };
      // 삭제 id 전역 기록 — 동시 편집 병합 시 삭제 항목이 되살아나지 않도록 함
      if (!Array.isArray(d._deletedIds)) d._deletedIds = [];
      if (!d.directions) d.directions = { title: '방향성', subtitle: '우선순위별 핵심 방향', items: [] };
      // 마이그레이션: AI 요약 데이터 구조
      if (!d.aiSummaries) d.aiSummaries = {};
      if (!d.aiPromptConfig) d.aiPromptConfig = { current: '', versions: [] };
      if (!d.dailyComments) d.dailyComments = {};
      if (!d.teamPriorities) d.teamPriorities = [];
      if (!d.dailyMemos) d.dailyMemos = {};
      return d;
    } catch { return null; }
  }

  function getDefault() {
    return {
      _version: 2,
      _lastModified: new Date().toISOString(),
      metricSections: [
        { id: 'biz', number: '01', title: '전사 핵심 비즈니스 지표', subtitle: '생존 및 획득', metrics: getDefaultBizMetrics() },
        { id: 'ux', number: '02', title: '고객 경험 & 리텐션', subtitle: '체력 및 피로도', metrics: [] },
      ],
      meetingAgenda: { title: '팀 내 회의', subtitle: '주간 회의 안건', items: [] },
      _deletedIds: [],
      aiSummaries: {},
      aiPromptConfig: {
        current: '',
        versions: [],
      },
      directions: { title: '방향성', subtitle: '우선순위별 핵심 방향', items: [] },
      completedResults: { title: '완료 프로젝트 결과 추적', subtitle: '과거의 성적표', items: [] },
      activeProjects: { title: '진행 중인 핵심 프로젝트', subtitle: '미래의 투자', projects: [] },
      dailyComments: {},
      teamPriorities: [],  // [{id, rank, label, description, projectIds:[]}]
      kanbanColumns: [
        { id: 'backlog', name: 'Backlog', dotColor: 'bg-gray-400', issues: [] },
        { id: 'todo', name: 'Todo', dotColor: 'bg-amber-400', issues: [] },
        { id: 'in_progress', name: 'In Progress', dotColor: 'bg-blue-500', issues: [] },
        { id: 'done', name: 'Done', dotColor: 'bg-emerald-500', issues: [] },
      ],
    };
  }

  // 업무 영역 섹션 → 담당자 → 질문 계층 구조
  const TEAM_CONFIG_KEY = 'weeklyReportTeamConfig';

  // 팀 설정 버전 — 코드에서 섹션 추가/변경 시 올려주면 자동 머지
  const TEAM_CONFIG_VERSION = 4;

  function loadTeamConfig() {
    try {
      const stored = localStorage.getItem(TEAM_CONFIG_KEY);
      if (stored) {
        const parsed = JSON.parse(stored);
        // 버전이 다르면 코드 기본값에서 누락된 섹션을 머지
        if (parsed._version !== TEAM_CONFIG_VERSION) {
          return _ensureCompany(mergeTeamConfig(parsed, getTeamConfig()));
        }
        // 버전 같아도 구버전 데이터엔 company 누락 가능 — 항상 6키 보장
        return _ensureCompany(parsed);
      }
    } catch(e) {}
    const cfg = getTeamConfig();
    cfg._version = TEAM_CONFIG_VERSION;
    return _ensureCompany(cfg);
  }

  // 기존 저장된 설정에 코드 기본값의 누락 섹션/역할을 추가
  function mergeTeamConfig(stored, defaults) {
    // 회사 정보 6개 키 보장 — 구버전 데이터에 company 없거나 빈 객체여도 reactive
    _ensureCompany(stored);
    const storedMap = new Map(stored.sections.map(s => [s.section, s]));
    defaults.sections.forEach(sec => {
      if (!storedMap.has(sec.section)) {
        // 새 섹션 추가
        stored.sections.push(sec);
      } else {
        // 기존 섹션 → 새 역할(owner+theme) 머지
        const existing = storedMap.get(sec.section);
        const existingRoles = new Set(existing.roles.map(r => r.owner + '::' + r.theme));
        sec.roles.forEach(role => {
          if (!existingRoles.has(role.owner + '::' + role.theme)) {
            existing.roles.push(role);
          }
        });
      }
    });
    // categoryOrder 머지 (없으면 기본값 세팅)
    if (!stored.categoryOrder) {
      stored.categoryOrder = defaults.categoryOrder;
    }
    // 멤버도 머지
    const existingMembers = new Set(stored.members || []);
    (defaults.members || []).forEach(m => {
      if (!existingMembers.has(m)) stored.members.push(m);
    });
    stored._version = TEAM_CONFIG_VERSION;
    localStorage.setItem(TEAM_CONFIG_KEY, JSON.stringify(stored));
    return stored;
  }

  // 회사 정보 (PDF 인쇄용) 6개 키 보장 — undefined/null이면 빈 문자열로
  function _ensureCompany(tc) {
    if (!tc || typeof tc !== 'object') return tc;
    for (const key of ['members', 'sections', 'categoryOrder']) {
      if (!Array.isArray(tc[key])) tc[key] = [];
    }
    if (!tc.company || typeof tc.company !== 'object') tc.company = {};
    for (const k of ['name', 'ceo', 'address', 'bizNo', 'phone', 'stampUrl']) {
      if (typeof tc.company[k] !== 'string') tc.company[k] = '';
    }
    return tc;
  }

  function getTeamConfig() {
    return _ensureCompany({
      _version: TEAM_CONFIG_VERSION,
      company: { name: '', ceo: '', address: '', bizNo: '', phone: '', stampUrl: '' },
      categoryOrder: [],
      members: [],
      sections: [],
      profileVisibility: { hiddenNames: [] },
      approvalDefaults: { primary: '', reference: '' },
      executiveNames: [],
      excludedLeaveBalanceNames: [],
      dailyOwnerExcludeNames: [],
      ownerColors: {},
      recorderUrl: '',
    });
  }

  // 현재 로그인한 사용자 정보 로드
  function _loadCurrentUser() {
    try {
      const s = localStorage.getItem('_dashboard_user');
      if (s) return JSON.parse(s);
    } catch(e) {}
    return { name: '', email: '', role: '', user_id: '' };
  }

  const instance = {
    data: load() || getDefault(),

    // 현재 사용자 정보 (RBAC)
    currentUser: _loadCurrentUser(),
    passwordChangeModal: { show: false, current: '', new: '', confirm: '', submitting: false },

    // ── 인앱 알림 ──
    notif: { open: false, items: [], unread: 0, loading: false, _timer: null },
    async loadNotifications() {
      if (!this.currentUser || !this.currentUser.name || this.isLegacy()) return;
      try {
        this.notif.loading = true;
        const res = await _authFetch(API_BASE + '/api/notifications?limit=30');
        if (res.ok) {
          const d = await res.json();
          this.notif.items = d.items || [];
          this.notif.unread = d.unread || 0;
        }
      } catch (e) {} finally {
        this.notif.loading = false;
      }
    },
    toggleNotifications() {
      this.notif.open = !this.notif.open;
      if (this.notif.open) this.loadNotifications();
    },
    openNotification(n) {
      // 1) 네비게이션 먼저 — 렉 방지(읽음 처리는 백그라운드). 링크 tab 별칭을 실제 탭으로 매핑.
      if (n.link) {
        try {
          const m = n.link.match(/[?&]tab=([^&]+)/);
          if (m && m[1]) {
            // 알림 링크의 tab 값 → 실제 activeTab(+ 하위 탭) 매핑
            const MAP = {
              leave: { tab: 'hr', hrTab: 'leave' },
              attendance: { tab: 'hr', hrTab: 'attendance' },
              calendar: { tab: 'hr', hrTab: 'calendar' },
              approvals: { tab: 'approval' },
              approval: { tab: 'approval' },
              expenses: { tab: 'expenses' },
              'member-reports': { tab: 'report' },
              report: { tab: 'report' },
            };
            const VALID = ['approval', 'daily', 'expenses', 'hr', 'kanban', 'notes', 'report', 'settings', 'team-project', 'todos'];
            const t = MAP[m[1]] || { tab: m[1] };
            if (VALID.includes(t.tab)) {
              this.activeTab = t.tab;
              if (t.hrTab) this.hrTab = t.hrTab;
            }
          }
        } catch (e) {}
      }
      this.notif.open = false;
      // 2) 읽음 처리는 비동기로(네비게이션 막지 않음)
      if (!n.is_read) {
        n.is_read = true;
        this.notif.unread = Math.max(0, this.notif.unread - 1);
        _authFetch(API_BASE + '/api/notifications/' + n.id + '/read', { method: 'POST' }).catch(() => {});
      }
    },
    async markAllNotificationsRead() {
      try {
        await _authFetch(API_BASE + '/api/notifications/read-all', { method: 'POST' });
        this.notif.items.forEach(n => n.is_read = true);
        this.notif.unread = 0;
      } catch (e) {}
    },
    async deleteNotif(id) {
      try {
        await _authFetch(API_BASE + '/api/notifications/' + id, { method: 'DELETE' });
        const idx = this.notif.items.findIndex(x => x.id === id);
        if (idx >= 0) {
          const removed = this.notif.items.splice(idx, 1)[0];
          if (removed && !removed.is_read) this.notif.unread = Math.max(0, this.notif.unread - 1);
        }
      } catch (e) {}
    },
    fmtRelativeTime(iso) {
      if (!iso) return '';
      try {
        const t = new Date(iso.replace(' ', 'T'));
        const diff = (Date.now() - t.getTime()) / 1000;
        if (diff < 60) return '방금';
        if (diff < 3600) return Math.floor(diff/60) + '분 전';
        if (diff < 86400) return Math.floor(diff/3600) + '시간 전';
        if (diff < 86400*7) return Math.floor(diff/86400) + '일 전';
        return t.toLocaleDateString('ko-KR');
      } catch (e) { return ''; }
    },

    // ── 활동 로그 (admin/pm) ──
    activityLogs: { items: [], total: 0, limit: 100, offset: 0, loading: false,
                    filter: { user: '', action: '', target_type: '', start: '', end: '' } },
    async loadActivityLogs(reset = false) {
      if (!this.hasRole(['admin', 'pm'])) return;
      if (reset) this.activityLogs.offset = 0;
      const f = this.activityLogs.filter;
      const qs = new URLSearchParams();
      qs.set('limit', this.activityLogs.limit);
      qs.set('offset', this.activityLogs.offset);
      if (f.user) qs.set('user', f.user);
      if (f.action) qs.set('action', f.action);
      if (f.target_type) qs.set('target_type', f.target_type);
      if (f.start) qs.set('start', f.start);
      if (f.end) qs.set('end', f.end);
      try {
        this.activityLogs.loading = true;
        const res = await _authFetch(API_BASE + '/api/activity-logs?' + qs.toString());
        if (res.ok) {
          const d = await res.json();
          this.activityLogs.items = d.items || [];
          this.activityLogs.total = d.total || 0;
        }
      } catch (e) {} finally {
        this.activityLogs.loading = false;
      }
    },
    activityLogsNextPage() {
      this.activityLogs.offset += this.activityLogs.limit;
      this.loadActivityLogs(false);
    },
    activityLogsPrevPage() {
      this.activityLogs.offset = Math.max(0, this.activityLogs.offset - this.activityLogs.limit);
      this.loadActivityLogs(false);
    },
    async exportActivityLogsCsv() {
      const f = this.activityLogs.filter;
      const qs = new URLSearchParams();
      if (f.user) qs.set('user', f.user);
      if (f.action) qs.set('action', f.action);
      if (f.target_type) qs.set('target_type', f.target_type);
      if (f.start) qs.set('start', f.start);
      if (f.end) qs.set('end', f.end);
      try {
        const res = await _authFetch(API_BASE + '/api/activity-logs/export?' + qs.toString());
        if (!res.ok) throw new Error('HTTP ' + res.status);
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'activity_logs_' + new Date().toISOString().slice(0,10) + '.csv';
        document.body.appendChild(a); a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
        this.showToast && this.showToast('success', 'CSV 다운로드 완료');
      } catch (e) {
        this.showToast && this.showToast('error', 'CSV 내보내기 실패: ' + e.message);
      }
    },
    fmtActivityDetail(d) {
      if (!d || typeof d !== 'object') return '';
      try {
        return Object.entries(d).map(([k,v]) => {
          const s = (v === null || v === undefined) ? '' : (typeof v === 'object' ? JSON.stringify(v) : String(v));
          return k + ': ' + s;
        }).join(' · ');
      } catch (e) { return ''; }
    },

    // 역할 기반 접근 제어 헬퍼
    isAdmin() { return this.currentUser.role === 'admin'; },
    isPm() { return this.currentUser.role === 'pm'; },
    isMember() { return this.currentUser.role === 'member'; },
    hasRole(roles) { return roles.includes(this.currentUser.role); },
    isLegacy() { return this.currentUser && this.currentUser.user_id === 'legacy'; },

    // 표시용 목록만 분리한다. 저장·동기화에는 기존 원본 배열과 템플릿을 그대로 사용한다.
    isVisibleProfile(person) { return window.dashboardProfileVisibility.isVisibleProfile(person, this.teamConfig.profileVisibility); },
    visibleProfiles(people) { return window.dashboardProfileVisibility.visibleProfiles(people, this.teamConfig.profileVisibility); },
    memberOptions(members, selectedName = '') { return window.dashboardProfileVisibility.memberOptions(members, selectedName, this.teamConfig.profileVisibility); },
    get visibleMembers() { return this.visibleProfiles(this.teamConfig.members); },
    get visibleMemberReportNames() { return this.visibleProfiles(this.memberReportNames); },

    // ── 경영 KPI 대시보드 (admin 전용) ──
    executiveSummary: {
      revenue: 0, prevRevenue: 0, revenueTrend: 0, revenueLabel: '',
      projectCompletion: { completed: 0, in_progress: 0, total: 0, rate: 0, quarterLabel: '' },
      hrStatus: { attendanceIssues: 0, leaveUsed: 0 },
      pendingApprovals: 0,
      loaded: false,
    },
    async loadExecutiveSummary() {
      if (!this.isAdmin()) return;
      try {
        const res = await _authFetch(API_BASE + '/api/dashboard/executive-summary');
        if (res.ok) {
          const d = await res.json();
          const hr = d.hr_status || {};
          this.executiveSummary = {
            revenue: d.revenue || 0,
            prevRevenue: d.prev_revenue || 0,
            revenueTrend: d.revenue_trend || 0,
            revenueLabel: d.revenue_label || '',
            projectCompletion: (() => {
              const pc = d.project_completion || {};
              return {
                completed: pc.completed || 0, in_progress: pc.in_progress || 0,
                total: pc.total || 0, rate: pc.rate || 0, quarterLabel: pc.quarter_label || '',
              };
            })(),
            hrStatus: { attendanceIssues: hr.attendance_issues || 0, leaveUsed: hr.leave_used || 0 },
            pendingApprovals: d.pending_approvals || 0,
            loaded: true,
          };
        }
      } catch (e) { console.error('[executive] 요약 로드 실패', e); }
    },
    fmtKrw(n) {
      if (!n) return '₩0';
      return '₩' + Number(n).toLocaleString('ko-KR');
    },
    scrollToSection(id) {
      // 주간 리포트 탭에 있는 섹션으로 부드럽게 스크롤
      if (this.activeTab !== 'report') this.activeTab = 'report';
      this.$nextTick(() => {
        const el = document.getElementById(id);
        if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
      });
    },

    // 로그아웃
    async logoutUser() {
      try {
        const response = await _authFetch(API_BASE + '/api/auth/logout', { method: 'POST' });
        if (!response.ok) throw new Error('로그아웃 실패');
      } catch (error) {
        this.showToast('error', '로그아웃을 완료하지 못했습니다. 다시 시도해주세요.');
        return;
      }
      localStorage.removeItem('_dashboard_auth_token');
      localStorage.removeItem('_dashboard_user');
      location.reload();
    },

    // 모든 새 계정·비밀번호 변경은 서버와 같은 길이/UTF-8 크기 제한을 사용한다.
    isValidNewPassword(password) {
      if (typeof password !== 'string' || Array.from(password).length < 12) {
        this.showToast('warning', '새 비밀번호는 12자 이상이어야 합니다.');
        return false;
      }
      if (new TextEncoder().encode(password).length > 1024) {
        this.showToast('warning', '새 비밀번호는 UTF-8 기준 1024바이트 이하여야 합니다.');
        return false;
      }
      return true;
    },

    // 본인 비밀번호 변경
    async submitPasswordChange() {
      const m = this.passwordChangeModal;
      if (m.submitting) return;
      if (!this.currentUser || !this.currentUser.name) { this.showToast('warning', '개인 로그인이 필요합니다.'); return; }
      if (!m.current || !m.new) { this.showToast('warning', '현재/새 비밀번호를 모두 입력해주세요.'); return; }
      if (!this.isValidNewPassword(m.new)) return;
      if (m.new === m.current) { this.showToast('info', '현재 비밀번호와 동일합니다.'); return; }
      if (m.new !== m.confirm) { this.showToast('warning', '새 비밀번호와 확인이 일치하지 않습니다.'); return; }
      m.submitting = true;
      try {
        const res = await _authFetch(API_BASE + '/api/auth/password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: m.current, new_password: m.new }),
        });
        if (res.ok) {
          this.showToast('warning', '비밀번호 변경 완료. 다음 로그인부터 새 비밀번호를 사용하세요.');
          this.passwordChangeModal = { show: false, current: '', new: '', confirm: '', submitting: false };
          return;
        }
        const d = await res.json().catch(() => ({}));
        this.showToast('error', d.error || '변경 실패');
      } catch (e) {
        this.showToast('error', '변경 실패: ' + e.message);
      } finally {
        m.submitting = false;
      }
    },

    // ── 공휴일 (전 사용자 조회 가능, 모든 캘린더 공통 사용) ──
    holidaysMap: {},  // { 'YYYY-MM-DD': '공휴일 이름' }
    async loadHolidaysIfNeeded() {
      if (Object.keys(this.holidaysMap).length > 0) return;
      // localStorage 캐시 우선 — 즉시 적용(깜빡임 제거). 7일 신선하면 서버 재조회 생략.
      try {
        const c = JSON.parse(localStorage.getItem('_holidaysCache') || 'null');
        if (c && c.map && c.ts && (Date.now() - c.ts < 7 * 24 * 60 * 60 * 1000)) {
          this.holidaysMap = c.map;
          return;
        }
      } catch {}
      try {
        const res = await _authFetch(API_BASE + '/api/holidays');
        if (res.ok) {
          const d = await res.json();
          const m = {};
          (d.holidays || []).forEach(h => { m[h.date] = h.name || '공휴일'; });
          this.holidaysMap = m;
          try { localStorage.setItem('_holidaysCache', JSON.stringify({ map: m, ts: Date.now() })); } catch {}
        }
      } catch {}
    },
    isHoliday(date) { return !!this.holidaysMap[date]; },
    holidayName(date) { return this.holidaysMap[date] || ''; },

    // ── 토스트 알림 시스템 ──
    toasts: [],
    showToast(type, message, timeout = 5000) {
      const id = Date.now().toString(36) + Math.random().toString(36).substr(2, 4);
      this.toasts.push({ id, type, message, timeout });
      if (timeout > 0) setTimeout(() => this.dismissToast(id), timeout);
    },
    dismissToast(id) {
      this.toasts = this.toasts.filter(t => t.id !== id);
    },
    // 글로벌 unhandledrejection → 서버 로그 + 토스트 (한 번만 등록)
    _initGlobalErrorCapture() {
      if (window.__errorCaptureInit) return;
      window.__errorCaptureInit = true;
      const self = this;
      window.addEventListener('unhandledrejection', (ev) => {
        try {
          self.showToast('error', '처리 안 된 오류: ' + (ev.reason?.message || ev.reason || 'unknown'));
          _authFetch(API_BASE + '/api/system/error-log', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              type: 'unhandledrejection',
              message: String(ev.reason?.message || ev.reason || ''),
              stack: ev.reason?.stack || '',
              url: 'tab=' + (self.activeTab || '?') + (self.hrTab ? '/' + self.hrTab : '') + ' @ ' + location.href,
            }),
          }).catch(() => {});
        } catch {}
      });
      window.addEventListener('error', (ev) => {
        try {
          _authFetch(API_BASE + '/api/system/error-log', {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({
              type: 'window.error',
              message: ev.message || '',
              stack: ev.error?.stack || '',
              url: 'tab=' + (self.activeTab || '?') + (self.hrTab ? '/' + self.hrTab : '') + ' @ ' + (ev.filename || location.href),
            }),
          }).catch(() => {});
        } catch {}
      });
    },

    // UI 상태
    loading: false,
    activeTab: 'report',
    lastUpdated: new Date().toLocaleTimeString('ko-KR'),
    showJsonModal: false,
    devMode: false,

    // 담당자 관리 패널
    showMemberPanel: false,
    newMemberName: '',
    addMember() {
      const name = this.newMemberName.trim();
      if (!name) return;
      if (this.teamConfig.members.includes(name)) { this.newMemberName = ''; return; }
      this.teamConfig.members.push(name);
      if (this.teamConfig.memberReportTemplates && !this.teamConfig.memberReportTemplates[name]) {
        this.teamConfig.memberReportTemplates[name] = {
          role: '팀원', color: '#6b7280', sections: ['이번 주 진행', '다음 주 계획', '공유 사항'],
        };
      }
      this.newMemberName = '';
      this.saveTeam();
    },
    removeMember(name) {
      if (!confirm(`"${name}" 담당자를 삭제하시겠습니까?`)) return;
      this.teamConfig.members = this.teamConfig.members.filter(m => m !== name);
      this.saveTeam();
    },

    // 편집 모달
    modal: { show: false, type: '', sectionId: '', itemIdx: -1 },
    form: {},

    // 백업 관리
    backupPanel: { show: false, fileType: 'report', list: [], loading: false, preview: null, previewFile: '' },

    async openBackupPanel() {
      this.backupPanel.show = true;
      this.backupPanel.fileType = 'report';
      await this.loadBackups();
    },
    async loadBackups() {
      this.backupPanel.loading = true;
      this.backupPanel.preview = null;
      this.backupPanel.previewFile = '';
      try {
        const res = await _authFetch(API_BASE +'/api/backups?file=' + this.backupPanel.fileType);
        if (res.ok) {
          const data = await res.json();
          this.backupPanel.list = data.backups || [];
        }
      } catch { this.backupPanel.list = []; }
      this.backupPanel.loading = false;
    },
    async previewBackup(filename) {
      try {
        const res = await _authFetch(API_BASE +'/api/backups/' + filename);
        if (res.ok) {
          const data = await res.json();
          this.backupPanel.preview = JSON.stringify(data, null, 2).substring(0, 5000);
          this.backupPanel.previewFile = filename;
        }
      } catch { /* 무시 */ }
    },
    async restoreBackup(filename, label) {
      if (!confirm(`"${label}" 백업으로 복원하시겠습니까?\n\n현재 데이터는 자동 백업된 후 복원됩니다.`)) return;
      try {
        const res = await _authFetch(API_BASE +'/api/backups/restore/' + filename, { method: 'POST' });
        if (res.ok) {
          this.showToast('success', '복원 완료! 페이지를 새로고침합니다.');
          location.reload();
        } else {
          this.showToast('error', '복원 실패: ' + await res.text());
        }
      } catch (e) { this.showToast('error', '복원 실패: ' + e.message); }
    },
    async deleteBackup(filename) {
      if (!confirm('이 백업을 삭제하시겠습니까?')) return;
      try {
        await _authFetch(API_BASE +'/api/backups/' + filename, { method: 'DELETE' });
        await this.loadBackups();
      } catch { /* 무시 */ }
    },

    // 프로젝트 필터/상태
    projectFilter: 'active',
    // 완료 결과 필터
    resultFilter: 'active',
    projectAttachUploading: false,

    // 지표 자동 조회 상태
    metricsLoading: false,
    metricsStartDate: '',
    metricsEndDate: '',
    metricsError: '',

    // 빠른 날짜 범위 설정
    setMetricsRange(preset) {
      const today = new Date();
      let start, end;
      switch (preset) {
        case '3d':
          end = new Date(today); end.setDate(end.getDate() - 1);
          start = new Date(end); start.setDate(start.getDate() - 2);
          break;
        case '1w':
          // 최근 7일 (어제 종료) — 3일·1개월과 동일한 롤링 윈도우
          end = new Date(today); end.setDate(end.getDate() - 1);
          start = new Date(end); start.setDate(start.getDate() - 6);
          break;
        case '2w':
          // 최근 14일 (어제 종료)
          end = new Date(today); end.setDate(end.getDate() - 1);
          start = new Date(end); start.setDate(start.getDate() - 13);
          break;
        case '1m':
          end = new Date(today); end.setDate(end.getDate() - 1);
          start = new Date(end); start.setDate(start.getDate() - 29);
          break;
      }
      const fmt = d => d.toISOString().split('T')[0];
      this.metricsStartDate = fmt(start);
      this.metricsEndDate = fmt(end);
    },

    // [DB 저장] 원격 API → 로컬 SQLite 저장 (일별 데이터)
    metricsCacheSaving: false,

    async saveMetricsCache() {
      if (!this.metricsStartDate || !this.metricsEndDate) {
        this.metricsError = '날짜를 선택해주세요';
        return;
      }
      this.metricsCacheSaving = true;
      this.metricsError = '';
      try {
        // 1) 원격 API에서 일별 데이터 가져오기
        const resp = await _authFetch(
          `${API_BASE}/api/metrics/table?start_date=${this.metricsStartDate}&end_date=${this.metricsEndDate}`
        );
        if (!resp.ok) {
          const err = await resp.json().catch(() => ({}));
          throw new Error(err.error || `원격 조회 실패 (${resp.status})`);
        }
        const result = await resp.json();
        const rows = result.rows || [];
        if (!rows.length) throw new Error('데이터가 없습니다');

        // 2) 로컬 DB에 저장
        const saveResp = await _authFetch(`${API_BASE}/api/metrics/cache`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ rows }),
        });
        if (!saveResp.ok) throw new Error('로컬 저장 실패');
        const saveResult = await saveResp.json();
        this.metricsError = `${saveResult.saved}일치 저장 완료`;
        setTimeout(() => { if (this.metricsError.includes('저장 완료')) this.metricsError = ''; }, 3000);
      } catch (e) {
        this.metricsError = e.message;
      } finally {
        this.metricsCacheSaving = false;
      }
    },

    // [DB 조회] 로컬 SQLite → 카드 4개 갱신
    metricsLoading: false,

    async fetchMetrics() {
      // 날짜 미선택 시 지난주 자동 설정
      if (!this.metricsStartDate || !this.metricsEndDate) {
        this.setMetricsRange('1w');
      }
      this.metricsLoading = true;
      this.metricsError = '';
      try {
        // 자동 집계 API 호출 (운영 DB 기반)
        const res = await _authFetch(
          `${API_BASE}/api/metrics/auto?start_date=${this.metricsStartDate}&end_date=${this.metricsEndDate}`
        );
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || '자동 집계 실패');
        }
        const d = await res.json();
        const metrics = (d.metrics || []).map(m => ({ ...m, id: uid() }));

        // Alpine 반응성을 위해 metricSections 배열 자체를 교체
        const idx = this.data.metricSections.findIndex(s => s.id === 'biz');
        if (idx >= 0) {
          this.data.metricSections[idx] = { ...this.data.metricSections[idx], metrics };
          this.data.metricSections = [...this.data.metricSections];
          this.save();
        } else {
          // biz 섹션 없으면 새로 추가
          this.data.metricSections.unshift({
            id: 'biz', number: '01', title: '전사 핵심 비즈니스 지표',
            subtitle: '자동 집계 (운영 DB)', metrics,
          });
          this.data.metricSections = [...this.data.metricSections];
          this.save();
        }
      } catch (e) {
        this.metricsError = e.message;
        // 실패 시 기존 캐시 로직 fallback
        try {
          const cacheResp = await _authFetch(
            `${API_BASE}/api/metrics/cache?start_date=${this.metricsStartDate}&end_date=${this.metricsEndDate}`
          );
          if (cacheResp.ok) {
            const cache = await cacheResp.json();
            if (cache.rows.length) {
              const rows = cache.rows;
              const revenue = rows.reduce((s, r) => s + (r.revenue || 0), 0);
              const adSpend = rows.reduce((s, r) => s + (r.ad_spend || 0), 0);
              const apiCost = rows.reduce((s, r) => s + (r.api_cost || 0), 0);
              const fmt = n => '₩' + Number(n).toLocaleString('ko-KR');
              const biz = this.data.metricSections.find(s => s.id === 'biz');
              if (biz) {
                biz.metrics = [{
                  id: uid(), label: '매출 (캐시)', value: fmt(revenue),
                  prevValue: '', trend: 0, status: 'yellow',
                  subLabel: `광고비 ${fmt(adSpend)} | API ${fmt(apiCost)}`,
                  detail: '자동 집계 실패 — 캐시 데이터', alert: true,
                }];
                this.save();
              }
              this.metricsError = '자동 집계 실패 → 캐시 데이터 표시 중';
            }
          }
        } catch {}
      } finally {
        this.metricsLoading = false;
      }
    },

    // [일별/주차별] 로컬 SQLite → 테이블 표시
    metricsTableMode: null,
    metricsTableData: [],
    metricsTableLoading: false,
    metricsTableSource: '',

    async fetchMetricsTable(mode) {
      if (!this.metricsStartDate || !this.metricsEndDate) {
        this.metricsError = '날짜를 먼저 선택해주세요';
        return;
      }
      // 같은 모드 재클릭 → 토글 닫기
      if (this.metricsTableMode === mode && this.metricsTableData.length > 0) {
        this.metricsTableMode = null;
        return;
      }
      this.metricsTableMode = mode;
      this.metricsTableLoading = true;
      this.metricsError = '';
      this.metricsTableSource = '';
      try {
        // 로컬 DB에서 일별 데이터 조회
        const cacheResp = await _authFetch(
          `${API_BASE}/api/metrics/cache?start_date=${this.metricsStartDate}&end_date=${this.metricsEndDate}`
        );
        if (!cacheResp.ok) throw new Error('로컬 DB 조회 실패');
        const cache = await cacheResp.json();
        if (!cache.rows.length) {
          this.metricsError = '로컬 DB에 데이터 없음. 먼저 [DB 저장]을 해주세요.';
          this.metricsTableMode = null;
          return;
        }

        if (mode === 'daily') {
          this.metricsTableData = cache.rows;
        } else {
          // 주차별: 일별 데이터를 주 단위로 묶어서 합산
          const weeks = [];
          let weekRows = [];
          let weekStart = null;
          for (const row of cache.rows) {
            const d = new Date(row.date);
            if (!weekStart || d.getDay() === 1) { // 월요일 시작
              if (weekRows.length) weeks.push({ start: weekStart, rows: weekRows });
              weekStart = row.date;
              weekRows = [row];
            } else {
              weekRows.push(row);
            }
          }
          if (weekRows.length) weeks.push({ start: weekStart, rows: weekRows });

          this.metricsTableData = weeks.map(w => {
            const rs = w.rows;
            const lastDate = rs[rs.length - 1].date;
            const revenue = rs.reduce((s, r) => s + (r.revenue || 0), 0);
            const adSpend = rs.reduce((s, r) => s + (r.ad_spend || 0), 0);
            const apiCost = rs.reduce((s, r) => s + (r.api_cost || 0), 0);
            const profit = revenue - adSpend - apiCost;
            const totalCost = adSpend + apiCost;
            return {
              date: `${w.start} ~ ${lastDate}`,
              revenue,
              crm_revenue: rs.reduce((s, r) => s + (r.crm_revenue || 0), 0),
              buyers: rs.reduce((s, r) => s + (r.buyers || 0), 0),
              ad_spend: adSpend,
              api_cost: apiCost,
              api_per_buyer: (() => { const b = rs.reduce((s, r) => s + (r.buyers || 0), 0); return b > 0 ? Math.round(apiCost / b) : 0; })(),
              profit,
              roas: totalCost > 0 ? Math.round(revenue / totalCost * 100) : 0,
              users: rs.reduce((s, r) => s + (r.users || 0), 0),
              sessions: rs.reduce((s, r) => s + (r.sessions || 0), 0),
              new_visitor_ratio: (() => {
                const u = rs.reduce((s, r) => s + (r.users || 0), 0);
                const ss = rs.reduce((s, r) => s + (r.sessions || 0), 0);
                return ss > 0 ? +(u / ss * 100).toFixed(2) : 0;
              })(),
            };
          });
        }
        this.metricsTableSource = 'local';
      } catch (e) {
        this.metricsError = e.message;
        this.metricsTableMode = null;
      } finally {
        this.metricsTableLoading = false;
      }
    },

    // 로컬 DB 초기화
    async resetMetricsCache() {
      if (!confirm('로컬 DB의 모든 지표 데이터를 삭제합니다. 계속하시겠습니까?')) return;
      try {
        const resp = await _authFetch(`${API_BASE}/api/metrics/cache`, { method: 'DELETE' });
        if (!resp.ok) throw new Error('초기화 실패');
        this.metricsTableData = [];
        this.metricsTableMode = null;
        this.metricsError = 'DB 초기화 완료';
        setTimeout(() => { if (this.metricsError.includes('초기화')) this.metricsError = ''; }, 3000);
      } catch (e) {
        this.metricsError = e.message;
      }
    },

    // 숫자 포맷 (천 단위 콤마)
    fmtNum(n) {
      if (n == null) return '-';
      return Number(n).toLocaleString('ko-KR');
    },

    // ── 대표 현황 ──
    execList: [],
    _execStatusItems: [],
    syncExecProfiles() {
      const configured = Array.isArray(this.teamConfig.executiveNames) ? this.teamConfig.executiveNames : [];
      const names = [...new Set([...configured, ...this._execStatusItems.map(item => item.name)].filter(Boolean))];
      const existing = new Map(this.execList.map(item => [item.name, item]));
      this.execList = names.map(name => existing.get(name) || {
        name, current_tasks: '', meeting_notes: '', _loaded_tasks: '', _loaded_notes: '',
      });
    },
    execSelectedWeek: '',
    execWeekOptions: [],
    get execCurrentWeek() {
      const d = new Date();
      const m = d.getMonth() + 1;
      const w = Math.ceil(d.getDate() / 7);
      return m + '월 ' + w + '주차';
    },
    get execWeek() {
      return this.execSelectedWeek || this.execCurrentWeek;
    },
    get execIsCurrentWeek() {
      return this.execWeek === this.execCurrentWeek;
    },
    execLoading: false,
    async loadExecStatus() {
      this.execLoading = true;
      try {
        const allRes = await _authFetch(API_BASE + '/api/exec-status');
        if (allRes.ok) {
          const allData = await allRes.json();
          const items = allData.items || [];
          this._execStatusItems = items;
          this.syncExecProfiles();
          const weeks = new Set();
          for (const item of items) { weeks.add(item.week_label); }
          weeks.add(this.execCurrentWeek);
          this.execWeekOptions = [...weeks].sort().reverse();
          const weekItems = items.filter(i => i.week_label === this.execWeek);
          // 전주 자동 가져오기: 현재 주차에 데이터 없으면 직전 주차 내용을 자동 채움
          const sortedWeeks = [...this.execWeekOptions].sort();
          const currentIdx = sortedWeeks.indexOf(this.execWeek);
          const prevWeek = currentIdx > 0 ? sortedWeeks[currentIdx - 1] : '';
          const prevItems = prevWeek ? items.filter(i => i.week_label === prevWeek) : [];
          for (const exec of this.execList) {
            const found = weekItems.find(i => i.name === exec.name);
            let tasks = found ? (found.current_tasks || '') : '';
            let notes = found ? (found.meeting_notes || '') : '';
            exec._fromPrevWeek = false;
            // 현재 주차에 비어있고 전주 데이터가 있으면 자동 채움
            if (!tasks && !notes && this.execIsCurrentWeek) {
              const prev = prevItems.find(i => i.name === exec.name);
              if (prev && (prev.current_tasks || prev.meeting_notes)) {
                tasks = prev.current_tasks || '';
                notes = prev.meeting_notes || '';
                exec._fromPrevWeek = true;
              }
            }
            exec.current_tasks = tasks;
            exec.meeting_notes = notes;
            exec._loaded_tasks = tasks;
            exec._loaded_notes = notes;
          }
        }
      } catch {} finally {
        this.execLoading = false;
      }
    },
    async changeExecWeek(week) {
      this.execSelectedWeek = week;
      await this.loadExecStatus();
    },
    async saveExecStatus(exec) {
      // dirty check: 로드된 값과 동일하면 저장 스킵 (빈 문자열 덮어쓰기 방지)
      if (exec.current_tasks === exec._loaded_tasks && exec.meeting_notes === exec._loaded_notes) return;
      try {
        const res = await _authFetch(API_BASE + '/api/exec-status', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: exec.name, week_label: this.execWeek, current_tasks: exec.current_tasks, meeting_notes: exec.meeting_notes }),
        });
        if (res.ok) {
          exec._loaded_tasks = exec.current_tasks;
          exec._loaded_notes = exec.meeting_notes;
          if (!this.execWeekOptions.includes(this.execWeek)) {
            this.execWeekOptions.unshift(this.execWeek);
          }
        } else {
          this.showToast('error', '저장 실패. 다시 시도해주세요.');
        }
      } catch {
        this.showToast('error', '저장 중 오류가 발생했습니다.');
      }
    },

    // ── 프로젝트 계층 관리 (A+C 하이브리드) ──
    projHierarchy: {
      projects: [],      // Linear에서 가져온 전체 프로젝트 목록
      initiatives: [],   // Linear Initiative 목록
      tree: [],          // 트리 구조 (parent-child)
      unassigned: [],    // 부모 미지정 프로젝트
      hierarchy: [],     // hierarchy 테이블 원본 데이터
      loading: false,
      configOpen: false, // 관리 UI 토글
    },
    projHideCompleted: true, // 완료/취소/백로그 숨기기 (기본 ON)

    // Initiative별로 프로젝트를 묶은 트리 (Initiative → 하위 Project)
    get projTreeWithInitiatives() {
      const inits = this.projHierarchy.initiatives || [];
      const tree = this.projHierarchy.tree || [];
      if (!inits.length) return tree;

      // Initiative ID → Initiative 매핑
      const initMap = {};
      for (const init of inits) {
        initMap[init.id] = { ...init, _type: 'initiative', children: [] };
      }

      // 루트 프로젝트를 Initiative에 매핑
      const used = new Set();
      for (const node of tree) {
        const initIds = node.initiativeIds || [];
        if (initIds.length > 0 && initMap[initIds[0]]) {
          initMap[initIds[0]].children.push(node);
          used.add(node.id);
        }
      }

      // Initiative 노드만 표시 (Initiative 미소속 프로젝트는 숨김)
      const result = [];
      for (const init of inits) {
        result.push(initMap[init.id]);
      }
      return result;
    },

    // 완료/취소/백로그 필터 적용된 트리
    get projFilteredTree() {
      if (!this.projHideCompleted) return this.projTreeWithInitiatives;
      const hide = new Set(['completed', 'canceled', 'cancelled', 'backlog']);
      const filterNode = (node) => {
        if (node._type === 'initiative') {
          const children = node.children.map(filterNode).filter(Boolean);
          return { ...node, children };
        }
        if (hide.has(node.state)) return null;
        const children = (node.children || []).map(filterNode).filter(Boolean);
        return { ...node, children };
      };
      return this.projTreeWithInitiatives.map(filterNode).filter(Boolean);
    },

    // Initiative 이름으로 프로젝트가 속한 Initiative 찾기
    getProjectInitiativeName(projectId) {
      for (const init of (this.projHierarchy.initiatives || [])) {
        if ((init.projectIds || []).includes(projectId)) return init.name;
      }
      return '';
    },

    async loadProjectHierarchy() {
      this.projHierarchy.loading = true;
      try {
        const res = await _authFetch(API_BASE + '/api/project-hierarchy');
        if (res.ok) {
          const d = await res.json();
          this.projHierarchy.projects = d.projects || [];
          this.projHierarchy.initiatives = d.initiatives || [];
          this.projHierarchy.tree = d.tree || [];
          this.projHierarchy.unassigned = d.unassigned || [];
          this.projHierarchy.hierarchy = d.hierarchy || [];
        }
      } catch {} finally {
        this.projHierarchy.loading = false;
      }
    },

    async saveProjectParent(projectId, parentId) {
      // 프로젝트 부모 할당
      try {
        const res = await _authFetch(API_BASE + '/api/project-hierarchy', {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            linear_project_id: projectId,
            parent_project_id: parentId || null,
            sort_order: 0,
          }),
        });
        if (res.ok) {
          await this.loadProjectHierarchy();
        } else {
          const err = await res.json().catch(() => ({}));
          this.showToast('error', err.error || '저장 실패');
        }
      } catch {
        this.showToast('error', '저장 중 오류가 발생했습니다.');
      }
    },

    async removeFromHierarchy(projectId) {
      // 계층에서 제거
      if (!confirm('이 프로젝트를 계층에서 제거하시겠습니까? (Linear 프로젝트는 유지됩니다)')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/project-hierarchy/' + projectId, {
          method: 'DELETE',
        });
        if (res.ok) {
          await this.loadProjectHierarchy();
        }
      } catch {}
    },

    // 프로젝트 계층 트리 펼치기/접기
    projHierarchyAllExpanded: false,
    expandAllProjHierarchy() {
      this.projHierarchyAllExpanded = true;
      const expand = (nodes) => {
        for (const n of nodes) {
          this.initExpanded['ph-' + n.id] = true;
          expand(n.children || []);
        }
      };
      // 실제 렌더되는 트리(이니셔티브 포함) 기준 — projHierarchy.tree는 비어있을 수 있음
      expand(this.projTreeWithInitiatives);
    },
    collapseAllProjHierarchy() {
      this.projHierarchyAllExpanded = false;
      const collapse = (nodes) => {
        for (const n of nodes) {
          this.initExpanded['ph-' + n.id] = false;
          collapse(n.children || []);
        }
      };
      collapse(this.projTreeWithInitiatives);
    },

    // 관리 UI: 부모 선택 드롭다운에서 자기 자신과 자식을 제외한 프로젝트 목록
    getParentOptions(projectId) {
      // 자기 자신과 자식 프로젝트 ID 수집
      const descendants = new Set();
      const findDescendants = (nodes) => {
        for (const n of nodes) {
          if (n.id === projectId) {
            // 이 노드의 모든 자식 수집
            const collectChildren = (node) => {
              descendants.add(node.id);
              for (const c of node.children || []) collectChildren(c);
            };
            collectChildren(n);
            return;
          }
          findDescendants(n.children || []);
        }
      };
      findDescendants(this.projHierarchy.tree);
      descendants.add(projectId);
      return this.projHierarchy.projects.filter(p => !descendants.has(p.id));
    },

    // 현재 부모 ID 가져오기
    getProjectParentId(projectId) {
      const h = this.projHierarchy.hierarchy.find(h => h.linear_project_id === projectId);
      return h ? (h.parent_project_id || '') : '';
    },

    // 프로젝트 health 도트 색상 반환
    projHealthDot(health) {
      if (health === 'onTrack') return 'bg-emerald-500';
      if (health === 'atRisk') return 'bg-amber-400';
      if (health === 'offTrack') return 'bg-red-500';
      return 'bg-slate-300';
    },

    // 프로젝트 health 한국어 라벨
    projHealthLabel(health) {
      if (health === 'onTrack') return '정상';
      if (health === 'atRisk') return '주의';
      if (health === 'offTrack') return '위험';
      return '';
    },

    // 의존성 텍스트 포맷
    projBlocksText(blocks) {
      if (!blocks || blocks.length === 0) return '';
      return '\u2192 blocks: ' + blocks.map(b => b.name).join(', ');
    },
    projBlockedByText(blockedBy) {
      if (!blockedBy || blockedBy.length === 0) return '';
      return '\u2190 blocked by: ' + blockedBy.map(b => b.name).join(', ');
    },

    // 최근 업데이트 날짜 포맷
    formatUpdateDate(dateStr) {
      if (!dateStr) return '';
      try {
        const d = new Date(dateStr);
        return d.toLocaleDateString('ko-KR', { month: 'short', day: 'numeric' }) + ' ' +
               d.toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit' });
      } catch { return dateStr; }
    },

    // 프로젝트 state 배지 색상
    projStateBadge(state) {
      const map = {
        'planned': 'bg-slate-100 text-slate-600',
        'started': 'bg-blue-100 text-blue-700',
        'paused': 'bg-amber-100 text-amber-700',
        'completed': 'bg-emerald-100 text-emerald-700',
        'canceled': 'bg-red-100 text-red-700',
        'backlog': 'bg-slate-100 text-slate-500',
      };
      return map[state] || 'bg-slate-100 text-slate-600';
    },

    // 프로젝트 state 한국어 변환
    projStateLabel(state) {
      const map = {
        'planned': '계획',
        'started': '진행중',
        'paused': '일시정지',
        'completed': '완료',
        'canceled': '취소',
        'backlog': '백로그',
      };
      return map[state] || state || '—';
    },

    // ── 코어목표 + 가설 트리 ──
    coreTree: [],
    coreFilter: '',
    coreEditId: null,
    coreEditValue: '',
    get coreTreeFiltered() {
      if (!this.coreFilter) return this.coreTree;
      const f = this.coreFilter;
      const hasMatch = (node) => {
        if (node.type === 'hypothesis' && f === 'Y' && node.adopted === 'Y') return true;
        if (node.type === 'hypothesis' && f === 'N' && node.adopted === 'N') return true;
        if (f === 'active' && node.status === '진행중') return true;
        return (node.children || []).some(hasMatch);
      };
      return this.coreTree.filter(hasMatch);
    },
    async loadCoreTree() {
      try {
        const res = await _authFetch(API_BASE + '/api/core-tree');
        if (res.ok) { this.coreTree = (await res.json()).tree || []; }
      } catch {}
    },
    coreSyncing: false,
    coreSyncSummary: '',

    // ══════════════════════════════
    // 팀원별 주간 업무 보고
    // ══════════════════════════════
    // 이름과 담당 업무는 설치자가 설정한다. 새 팀원에게는 빈 표준 보고 양식을 제공한다.
    _defaultMemberTemplates: {},
    get memberReportTemplates() {
      if (this.teamConfig.memberReportTemplates) return this.teamConfig.memberReportTemplates;
      return Object.fromEntries((this.teamConfig.members || []).map(name => [name, {
        role: '팀원', color: '#6b7280', sections: ['이번 주 진행', '다음 주 계획', '공유 사항'],
      }]));
    },
    memberReportConfigOpen: false,
    memberReportConfigMounted: false,
    memberTemplateSaveState: '',   // '' | 'saving' | 'saved' | 'error'
    // 항목 편집(템플릿) 변경을 이미 불러온 보고 내용에 즉시 반영 (새로고침 없이도 카드에 표시)
    reconcileMemberReportSections() {
      for (const name of this.memberReportNames) {
        const d = this.memberReportData[name];
        if (!d || !Array.isArray(d.sections)) continue;
        const tpl = this.memberReportTemplates[name];
        if (!tpl) continue;
        const byLabel = {};
        d.sections.forEach(s => { byLabel[s.label] = s.content; });
        // 템플릿 순서/라벨에 맞춰 재구성하되, 입력된 내용은 라벨 기준으로 보존
        d.sections = (tpl.sections || []).map(label => ({
          label, content: byLabel[label] || '',
        }));
      }
    },
    async saveMemberTemplates() {
      if (!this.teamConfig.memberReportTemplates) {
        this.teamConfig.memberReportTemplates = JSON.parse(JSON.stringify(this.memberReportTemplates));
      }
      // 변경된 항목을 보고 카드에 즉시 반영
      this.reconcileMemberReportSections();
      this.memberTemplateSaveState = 'saving';
      this.teamConfig._version = TEAM_CONFIG_VERSION;
      localStorage.setItem(TEAM_CONFIG_KEY, JSON.stringify(this.teamConfig));
      try {
        const res = await _authFetch(API_BASE + '/api/team-config', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(this.teamConfig),
        });
        this.memberTemplateSaveState = res.ok ? 'saved' : 'error';
      } catch {
        this.memberTemplateSaveState = 'error';
      }
      if (this.memberTemplateSaveState === 'saved') {
        setTimeout(() => {
          if (this.memberTemplateSaveState === 'saved') this.memberTemplateSaveState = '';
        }, 2500);
      }
    },
    addMemberSection(name) {
      if (!this.teamConfig.memberReportTemplates) {
        this.teamConfig.memberReportTemplates = JSON.parse(JSON.stringify(this.memberReportTemplates));
      }
      const tpl = this.teamConfig.memberReportTemplates[name];
      if (tpl) {
        const label = prompt('추가할 항목 제목:');
        if (label && label.trim()) {
          tpl.sections.push(label.trim());
          this.saveMemberTemplates();
        }
      }
    },
    removeMemberSection(name, idx) {
      if (!this.teamConfig.memberReportTemplates) return;
      const tpl = this.teamConfig.memberReportTemplates[name];
      if (tpl && confirm(`"${tpl.sections[idx]}" 항목을 삭제하시겠습니까?`)) {
        tpl.sections.splice(idx, 1);
        this.saveMemberTemplates();
      }
    },
    moveMemberSection(name, idx, dir) {
      if (!this.teamConfig.memberReportTemplates) {
        this.teamConfig.memberReportTemplates = JSON.parse(JSON.stringify(this.memberReportTemplates));
      }
      const tpl = this.teamConfig.memberReportTemplates[name];
      if (!tpl) return;
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= tpl.sections.length) return;
      // 순서 교환
      const tmp = tpl.sections[idx];
      tpl.sections[idx] = tpl.sections[newIdx];
      tpl.sections[newIdx] = tmp;
      this.saveMemberTemplates();
    },
    editMemberSection(name, idx) {
      if (!this.teamConfig.memberReportTemplates) {
        this.teamConfig.memberReportTemplates = JSON.parse(JSON.stringify(this.memberReportTemplates));
      }
      const tpl = this.teamConfig.memberReportTemplates[name];
      if (!tpl) return;
      const oldLabel = tpl.sections[idx];
      const newLabel = prompt('항목 제목 수정:', oldLabel);
      if (newLabel !== null && newLabel.trim() && newLabel.trim() !== oldLabel) {
        tpl.sections[idx] = newLabel.trim();
        // 이미 입력된 보고 내용의 라벨도 함께 변경 → 이름 바꿔도 내용 보존
        const d = this.memberReportData[name];
        if (d && Array.isArray(d.sections)) {
          const sec = d.sections.find(s => s.label === oldLabel);
          if (sec) sec.label = newLabel.trim();
        }
        this.saveMemberTemplates();
      }
    },
    editMemberRole(name) {
      if (!this.teamConfig.memberReportTemplates) {
        this.teamConfig.memberReportTemplates = JSON.parse(JSON.stringify(this.memberReportTemplates));
      }
      const tpl = this.teamConfig.memberReportTemplates[name];
      if (!tpl) return;
      const newRole = prompt('역할명 수정:', tpl.role);
      if (newRole !== null && newRole.trim()) {
        tpl.role = newRole.trim();
        this.saveMemberTemplates();
      }
    },
    memberReportData: {},   // { name: { sections: [{label, content}], attachments: [{name,url}], updated_at, _loaded } }
    memberReportWeek: '',   // 별도 주차 (비어있으면 execWeek 따라감)
    memberReportWeeks: [],  // 주차 옵션
    memberReportLoading: false,
    get memberReportActiveWeek() {
      return this.memberReportWeek || this.execWeek;
    },
    get memberReportNames() {
      return Object.keys(this.memberReportTemplates);
    },
    memberReportSections(name) {
      const d = this.memberReportData[name];
      if (d && d.sections && d.sections.length > 0) return d.sections;
      // DB에 없으면 템플릿 기본값
      const tpl = this.memberReportTemplates[name];
      return (tpl?.sections || []).map(s => ({ label: s, content: '' }));
    },
    memberReportUpdatedAt(name) {
      const d = this.memberReportData[name];
      return d?.updated_at || '';
    },
    memberReportIsEmpty(name) {
      const secs = this.memberReportSections(name);
      return secs.every(s => !s.content || !s.content.trim());
    },
    async loadMemberReports() {
      this.memberReportLoading = true;
      try {
        const res = await _authFetch(API_BASE + '/api/member-reports?week=' + encodeURIComponent(this.memberReportActiveWeek));
        if (res.ok) {
          const d = await res.json();
          this.memberReportWeeks = d.weeks || [];
          if (!this.memberReportWeeks.includes(this.execCurrentWeek)) {
            this.memberReportWeeks.unshift(this.execCurrentWeek);
          }
          // 데이터 매핑
          const newData = {};
          for (const name of this.memberReportNames) {
            const found = (d.items || []).find(i => i.name === name);
            const tpl = this.memberReportTemplates[name];
            const tplLabels = (tpl && Array.isArray(tpl.sections)) ? tpl.sections : null;
            let sections;
            if (found && Array.isArray(found.sections) && found.sections.length > 0) {
              if (tplLabels) {
                // 구조(항목 목록·순서)는 템플릿이 기준 — 삭제된 항목은 제외, 새 항목 추가.
                // 입력된 내용은 라벨 기준으로 보존.
                const byLabel = {};
                found.sections.forEach(s => { byLabel[s.label] = s.content; });
                sections = tplLabels.map(label => ({ label, content: byLabel[label] || '' }));
              } else {
                // 템플릿이 없으면 저장된 데이터 그대로
                sections = [...found.sections];
              }
            } else {
              sections = (tplLabels || []).map(s => ({ label: s, content: '' }));
            }
            const attachments = (found && found.attachments) ? found.attachments : [];
            newData[name] = {
              sections: sections,
              attachments: attachments,
              updated_at: found?.updated_at || '',
              _loaded: JSON.stringify({ sections, attachments }),
            };
          }
          this.memberReportData = newData;
        }
      } catch {} finally {
        this.memberReportLoading = false;
      }
    },
    async uploadMemberAttachment(name, ev) {
      const file = ev.target.files[0];
      if (!file) return;
      const d = this.memberReportData[name];
      if (!d) { this.showToast('error', '보고 데이터를 찾을 수 없습니다'); return; }
      const fd = new FormData();
      fd.append('file', file);
      try {
        const res = await _authFetch(API_BASE + '/api/upload', { method: 'POST', body: fd });
        if (res.ok) {
          const r = await res.json();
          // Alpine 반응성을 위해 새 배열로 교체
          const atts = [...(d.attachments || []), { name: r.name || file.name, url: r.url }];
          this.memberReportData[name] = { ...d, attachments: atts };
          await this.saveMemberReport(name, true);
        } else {
          this.showToast('error', '파일 업로드 실패: ' + res.status);
        }
      } catch (e) { this.showToast('error', '파일 업로드 오류: ' + e.message); }
      ev.target.value = '';
    },
    removeMemberAttachment(name, idx) {
      const d = this.memberReportData[name];
      if (!d || !d.attachments) return;
      const atts = d.attachments.filter((_, i) => i !== idx);
      this.memberReportData[name] = { ...d, attachments: atts };
      this.saveMemberReport(name, true);
    },
    async saveMemberReport(name, force) {
      const d = this.memberReportData[name];
      if (!d) return;
      // dirty check (force=true면 무조건 저장)
      if (!force && JSON.stringify({ sections: d.sections, attachments: d.attachments || [] }) === d._loaded) return;
      try {
        const res = await _authFetch(API_BASE + '/api/member-reports', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name, week_label: this.memberReportActiveWeek, sections: d.sections, attachments: d.attachments || [] }),
        });
        if (res.ok) {
          const r = await res.json();
          d.updated_at = r.updated_at || '';
          d._loaded = JSON.stringify({ sections: d.sections, attachments: d.attachments || [] });
          if (!this.memberReportWeeks.includes(this.memberReportActiveWeek)) {
            this.memberReportWeeks.unshift(this.memberReportActiveWeek);
          }
        } else {
          this.showToast('error', '보고 저장 실패');
        }
      } catch { this.showToast('error', '보고 저장 중 오류'); }
    },
    async changeMemberReportWeek(week) {
      this.memberReportWeek = week;
      await this.loadMemberReports();
    },

    // ══════════════════════════════
    // 전자결제 (승인 요청)
    // ══════════════════════════════
    approvalItems: [],
    approvalFilter: 'all',      // all / mine / inbox
    approvalStatusFilter: '',   // ''=전체, 대기/승인/반려
    // 2단계 결재: 종류별 {pm, admin} 기본 승인자
    approvalTypeApprovers: {
      personal_expense: { pm: '', admin: '' },
      cert_employment: { pm: '', admin: '' },
      book_purchase: { pm: '', admin: '' },  // 도서구입 = 대표(admin) 단독 1단계
      leave: { pm: '', admin: '' },          // 연차 = 1차 PM → 2차 대표
    },
    approvalConfigOpen: false,
    approvalModal: {
      show: false, id: null, req_type: 'personal_expense', title: '',
      detail: '', attachments: [], items: [],
      cert_info: { name: '', ssn: '', address: '', purpose: '' },
      book_info: { book_name: '', author_publisher: '', purchase_link: '', purpose: '', department: '' },
    },
    // 대표 단독 1단계 결재 유형 (현재 없음 — 전 유형 2단계 운영)
    singleStageTypes: [],
    isSingleStageType(t) { return (this.singleStageTypes || []).includes(t); },
    approvalDecideModal: { show: false, id: null, decision: '', note: '' },
    approvalDetailModal: { show: false, item: null },
    openApprovalDetail(item) { this.approvalDetailModal = { show: true, item }; },
    approvalTypeLabel(t) {
      return { personal_expense: '개인경비', cert_employment: '재직증명서', book_purchase: '도서 구입', leave: '연차' }[t] || t;
    },
    // 현재 결재 종류의 기본 승인자 (pm/admin) 문자열 — 신청 폼 안내용
    approvalPairFor(t) {
      const p = this.approvalTypeApprovers[t] || {};
      return { pm: p.pm || '', admin: p.admin || '' };
    },
    // 결재 항목의 현재 stage 판정 — 'pm' / 'admin' / 'done' / 'rejected'
    approvalStageOf(item) {
      if (!item) return 'done';
      const ps = item.pm_status || '대기';
      const as = item.admin_status || '대기';
      if (ps === '반려' || as === '반려') return 'rejected';
      if (ps === '대기') return 'pm';
      if (ps === '승인' && as === '대기') return 'admin';
      return 'done';
    },
    // 본인이 처리해야 할 항목인지
    isMyInbox(item) {
      const me = this.approvalCurrentUser;
      if (!me || !item) return false;
      const stage = this.approvalStageOf(item);
      if (stage === 'pm') return item.pm_approver === me;
      if (stage === 'admin') return item.admin_approver === me;
      return false;
    },
    // 내 홈 "내 결재" 통합용 — 전자결재/연차 모두 처리 대상 판정
    isMyApprovalInbox(a) {
      if (!a) return false;
      // 내 홈 카드 — 연차는 대리 결재를 제외하고 본인 차례만 표시한다.
      return a._kind === 'leave' ? this.isMyLeaveTurnStrict(a._raw || a) : this.isMyInbox(a._raw || a);
    },
    // 내 홈 "내 결재" 클릭 — 종류에 맞는 탭으로 이동
    openMyApproval(a) {
      if (a && a._kind === 'leave') { this.activeTab = 'hr'; this.hrTab = 'leave'; }
      else { this.activeTab = 'approval'; this.openApprovalDetail(a._raw || a); }
    },
    get approvalTotal() {
      return (this.approvalModal.items || []).reduce((s, it) => s + (Number(it.amount) || 0), 0);
    },
    get approvalTotalFormatted() {
      return Math.round(this.approvalTotal).toLocaleString('ko-KR');
    },
    addApprovalItem() {
      const today = new Date().toISOString().slice(0,10);
      this.approvalModal.items.push({ date: today, category: '식대', amount: 0, vendor: '', memo: '', receipt: null });
    },
    removeApprovalItem(idx) {
      this.approvalModal.items.splice(idx, 1);
    },
    async uploadApprovalItemReceipt(idx, ev) {
      const file = ev.target.files[0];
      if (!file) return;
      const fd = new FormData(); fd.append('file', file);
      try {
        const res = await _authFetch(API_BASE + '/api/upload', { method: 'POST', body: fd });
        if (res.ok) {
          const d = await res.json();
          this.approvalModal.items[idx].receipt = { name: d.name || file.name, url: d.url };
        }
      } catch {}
      ev.target.value = '';
    },
    get approvalCurrentUser() { return (this.currentUser && this.currentUser.name) || ''; },
    async loadApprovalSettings() {
      try {
        const res = await _authFetch(API_BASE + '/api/approval-settings');
        if (res.ok) {
          const d = await res.json();
          const ta = (d && d.type_approvers) || {};
          // 구조 보정: 어떤 값이든 {pm, admin}로
          const norm = {};
          for (const k of Object.keys(this.approvalTypeApprovers)) {
            const v = ta[k];
            if (v && typeof v === 'object') norm[k] = { pm: v.pm || '', admin: v.admin || '' };
            else if (typeof v === 'string') norm[k] = { pm: v, admin: '' };
            else norm[k] = { pm: '', admin: '' };
          }
          this.approvalTypeApprovers = norm;
        }
      } catch {}
    },
    async saveApprovalTypeApprovers() {
      if (!this.isAdmin() && !this.isPm()) return;
      try {
        const res = await _authFetch(API_BASE + '/api/approval-settings', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ type_approvers: this.approvalTypeApprovers }),
        });
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '저장 실패');
        }
      } catch {}
    },
    get filteredApprovals() {
      const me = this.approvalCurrentUser;
      let items = this.approvalItems;
      if (this.approvalFilter === 'mine' && me) {
        items = items.filter(i => i.requester === me);
      } else if (this.approvalFilter === 'inbox' && me) {
        items = items.filter(i => this.isMyInbox(i));
      }
      if (this.approvalStatusFilter) {
        items = items.filter(i => i.status === this.approvalStatusFilter);
      }
      return items;
    },
    get approvalInboxCount() {
      const me = this.approvalCurrentUser;
      if (!me) return 0;
      return this.approvalItems.filter(i => this.isMyInbox(i)).length;
    },
    async loadApprovals() {
      try {
        const tasks = [
          readApprovals().then(result => {
            if (isCurrentApprovalRead(result) && result.data) this.approvalItems = result.data.items || [];
          }),
          this.loadApprovalSettings(),
        ];
        // 승인자 후보(admin/pm) — 결재 모달에서 사용
        if (!this.hrApprovers || this.hrApprovers.length === 0) {
          tasks.push(_authFetch(API_BASE + '/api/users/approvers').then(async r => {
            if (r.ok) this.hrApprovers = (await r.json()).approvers || [];
          }));
        }
        await Promise.all(tasks);
      } catch {}
    },
    openApprovalModal(item) {
      if (item) {
        const copy = JSON.parse(JSON.stringify(item));
        const ci = copy.cert_info || {};
        const bi = copy.book_info || {};
        this.approvalModal = {
          show: true, id: item.id,
          req_type: copy.req_type || 'personal_expense',
          title: copy.title || '',
          detail: copy.detail || '',
          attachments: copy.attachments || [],
          items: copy.items || [],
          cert_info: {
            name: ci.name || '', ssn: ci.ssn || '',
            address: ci.address || '', purpose: ci.purpose || '',
          },
          book_info: {
            book_name: bi.book_name || '', author_publisher: bi.author_publisher || '',
            purchase_link: bi.purchase_link || '', purpose: bi.purpose || '',
            department: bi.department || '',
          },
        };
      } else {
        const t = 'personal_expense';
        this.approvalModal = {
          show: true, id: null, req_type: t,
          title: '', detail: '',
          attachments: [],
          items: [{ date: new Date().toISOString().slice(0,10), category: '식대', amount: 0, vendor: '', memo: '', receipt: null }],
          cert_info: {
            name: (this.currentUser && this.currentUser.name) || '',
            ssn: '', address: '', purpose: '',
          },
          book_info: { book_name: '', author_publisher: '', purchase_link: '', purpose: '', department: '' },
        };
      }
    },
    onApprovalTypeChange() {
      const t = this.approvalModal.req_type;
      if (t === 'cert_employment') {
        this.approvalModal.items = [];
        // 이름이 비었으면 로그인 사용자로 기본값
        if (!this.approvalModal.cert_info.name) {
          this.approvalModal.cert_info.name = (this.currentUser && this.currentUser.name) || '';
        }
      } else if (t === 'book_purchase') {
        this.approvalModal.items = [];
      } else if (t === 'personal_expense' && (!this.approvalModal.items || this.approvalModal.items.length === 0)) {
        this.addApprovalItem();
      }
    },
    async uploadApprovalAttachment(ev) {
      const file = ev.target.files[0];
      if (!file) return;
      const fd = new FormData();
      fd.append('file', file);
      try {
        const res = await _authFetch(API_BASE + '/api/upload', { method: 'POST', body: fd });
        if (res.ok) {
          const d = await res.json();
          this.approvalModal.attachments.push({ name: d.name || file.name, url: d.url });
        }
      } catch {}
      ev.target.value = '';
    },
    removeApprovalAttachment(idx) {
      this.approvalModal.attachments.splice(idx, 1);
    },
    async submitApproval() {
      const m = this.approvalModal;
      const single = this.isSingleStageType(m.req_type);
      if (!this.approvalCurrentUser) { this.showToast('warning', '개인 로그인이 필요합니다.'); return; }
      // 도서구입은 도서명이 곧 제목 — 제목 자동 채움
      if (m.req_type === 'book_purchase') {
        m.title = (m.book_info && m.book_info.book_name || '').trim();
      }
      if (!m.title) { this.showToast('warning', '제목을 입력하세요'); return; }
      // 2단계 결재 — 승인자는 설정값 자동 사용 (요청자 변경 불가). 1단계(도서구입)는 대표만 필요(미설정 시 서버 기본값).
      const pair = this.approvalPairFor(m.req_type);
      if (!m.id && !single && (!pair.pm || !pair.admin)) {
        this.showToast('warning', `'${this.approvalTypeLabel(m.req_type)}' 결재 PM·관리자 기본값이 비어 있습니다. 관리자에게 전자결재 설정을 요청하세요.`);
        return;
      }
      if (m.req_type === 'personal_expense') {
        if (!m.items || m.items.length === 0) { this.showToast('warning', '개인경비는 항목을 1개 이상 추가하세요'); return; }
        for (const it of m.items) {
          if (!it.date) { this.showToast('warning', '항목에 날짜를 모두 입력하세요'); return; }
          if (Number(it.amount) <= 0) { this.showToast('info', '항목 금액은 0보다 커야 합니다'); return; }
        }
      }
      if (m.req_type === 'cert_employment') {
        const ci = m.cert_info || {};
        if (!(ci.name || '').trim()) { this.showToast('warning', '재직증명서: 이름을 입력하세요'); return; }
        if (!(ci.purpose || '').trim()) { this.showToast('warning', '재직증명서: 용도를 입력하세요'); return; }
      }
      if (m.req_type === 'book_purchase') {
        const bi = m.book_info || {};
        if (!(bi.book_name || '').trim()) { this.showToast('warning', '도서 구입: 도서명을 입력하세요'); return; }
        if (!(bi.purpose || '').trim()) { this.showToast('warning', '도서 구입: 구입목적을 입력하세요'); return; }
      }
      const body = {
        req_type: m.req_type, title: m.title,
        detail: m.detail,
        attachments: m.attachments,
      };
      if (m.req_type === 'personal_expense') body.items = m.items;
      if (m.req_type === 'cert_employment') body.cert_info = m.cert_info;
      if (m.req_type === 'book_purchase') body.book_info = m.book_info;
      const url = m.id ? `/api/approvals/${m.id}` : '/api/approvals';
      const method = m.id ? 'PUT' : 'POST';
      try {
        const res = await _authFetch(API_BASE + url, {
          method, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (res.ok) {
          invalidateApprovalReads();
          this.approvalModal.show = false;
          await Promise.all([this.loadApprovals(), this.loadMyHome()]);
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '저장 실패');
        }
      } catch (e) { this.showToast('error', '저장 실패: ' + e.message); }
    },
    async deleteApproval(item) {
      // item 객체 또는 id 문자열 모두 허용 (하위호환)
      const it = (item && typeof item === 'object') ? item : null;
      const id = it ? it.id : item;
      const label = it ? `${this.approvalTypeLabel(it.req_type)} · ${it.title || ''}${it.requester ? ' (' + it.requester + ')' : ''}` : '이 결재';
      if (!confirm(`${label}\n삭제하시겠습니까? 되돌릴 수 없습니다.`)) return;
      try {
        const res = await _authFetch(API_BASE + `/api/approvals/${id}`, { method: 'DELETE' });
        if (res.ok) {
          invalidateApprovalReads();
          this.showToast('success', '삭제되었습니다');
          await Promise.all([this.loadApprovals(), this.loadMyHome()]);
          if (this.approvalDetailModal && this.approvalDetailModal.show) this.approvalDetailModal.show = false;
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '삭제 실패');
        }
      } catch { this.showToast('error', '삭제 중 오류가 발생했습니다'); }
    },
    openDecideModal(id, decision) {
      this.approvalDecideModal = { show: true, id, decision, note: '' };
    },

    // ── 사업자등록증 ──
    businessLicense: { exists: false, filename: '', size: 0 },
    async loadBusinessLicenseInfo() {
      if (this.isLegacy && this.isLegacy()) return;
      try {
        const res = await _authFetch(API_BASE + '/api/company/business-license/info');
        if (res.ok) {
          const d = await res.json();
          this.businessLicense = { exists: !!d.exists, filename: d.filename || '', size: d.size || 0 };
        }
      } catch (e) {}
    },
    async downloadBusinessLicense() {
      try {
        const res = await _authFetch(API_BASE + '/api/company/business-license');
        if (!res.ok) {
          const d = await res.json().catch(() => ({}));
          this.showToast && this.showToast('error', d.error || '다운로드 실패');
          return;
        }
        const blob = await res.blob();
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = this.businessLicense.filename || '사업자등록증.pdf';
        document.body.appendChild(a); a.click();
        setTimeout(() => { URL.revokeObjectURL(a.href); a.remove(); }, 200);
      } catch (e) {
        this.showToast && this.showToast('error', '다운로드 실패: ' + e.message);
      }
    },

    // 회사 정보 저장 (PDF 인쇄용) — 설정 탭에서 호출
    saveCompanyInfo() {
      _ensureCompany(this.teamConfig);
      // 빈 값은 trim 처리, undefined/null 방어
      const c = this.teamConfig.company;
      for (const k of ['name', 'ceo', 'address', 'bizNo', 'phone', 'stampUrl']) {
        c[k] = (typeof c[k] === 'string' ? c[k] : '').trim();
      }
      _saveToServer('/api/team-config', this.teamConfig);
      localStorage.setItem(TEAM_CONFIG_KEY, JSON.stringify(this.teamConfig));
      this.showToast && this.showToast('success', '회사 정보 저장됨 — 다음 발급부터 적용');
    },
    async submitDecide() {
      if (!this.isAdmin() && !this.isPm()) { this.showToast('info', '관리자/PM만 처리 가능합니다.'); return; }
      const m = this.approvalDecideModal;
      try {
        const res = await _authFetch(API_BASE + `/api/approvals/${m.id}/decide`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ decision: m.decision, note: m.note }),
        });
        if (res.ok) {
          invalidateApprovalReads();
          this.approvalDecideModal.show = false;
          await Promise.all([this.loadApprovals(), this.loadMyHome()]);
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '처리 실패');
        }
      } catch (e) { this.showToast('error', '처리 실패: ' + e.message); }
    },

    // ── PDF 인쇄 (재직증명서 / 개인경비) ──
    // 새 창에 인쇄 친화적 HTML을 그린 뒤 자동으로 인쇄 다이얼로그 열기.
    // 사용자는 "PDF로 저장"을 선택하면 PDF 파일이 생성됨.
    openApprovalPrint(item) {
      if (!item) return;
      const co = (this.teamConfig && this.teamConfig.company) || {};
      const company = {
        name: co.name || '회사명 미설정',
        ceo: co.ceo || '',
        address: co.address || '',
        bizNo: co.bizNo || '',
        phone: co.phone || '',
        stampUrl: co.stampUrl || '',
      };
      const today = new Date();
      const ymd = today.getFullYear() + '년 ' + (today.getMonth()+1) + '월 ' + today.getDate() + '일';
      const escapeHtml = (s) => String(s == null ? '' : s)
        .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
      const fmtKrw = (n) => Number(n || 0).toLocaleString();

      let body = '';
      if (item.req_type === 'cert_employment') {
        const c = item.cert_info || {};
        body = `
          <h1 class="doc-title">재 직 증 명 서</h1>
          <table class="info-table">
            <tr><th>성 명</th><td>${escapeHtml(c.name || item.requester)}</td></tr>
            <tr><th>주민등록번호</th><td>${escapeHtml(c.ssn || '')}</td></tr>
            <tr><th>주 소</th><td>${escapeHtml(c.address || '')}</td></tr>
            <tr><th>회 사 명</th><td>${escapeHtml(company.name)}</td></tr>
            <tr><th>발 급 일</th><td>${ymd}</td></tr>
            <tr><th>용 도</th><td>${escapeHtml(c.purpose || '')}</td></tr>
          </table>
          <p class="statement">위 사람은 본 회사에 재직 중임을 증명합니다.</p>
          <div class="signature-block">
            <p class="date-line">${ymd}</p>
            <p class="company-name">${escapeHtml(company.name)}${company.ceo ? ' 대표 ' + escapeHtml(company.ceo) : ''}
              <span class="stamp">${company.stampUrl ? '<img src="'+escapeHtml(company.stampUrl)+'" />' : '(인)'}</span>
            </p>
            ${company.address ? '<p class="company-meta">' + escapeHtml(company.address) + '</p>' : ''}
            ${company.bizNo ? '<p class="company-meta">사업자등록번호: ' + escapeHtml(company.bizNo) + '</p>' : ''}
          </div>`;
      } else if (item.req_type === 'personal_expense') {
        const items = item.items || [];
        const total = items.reduce((s, it) => s + Number(it.amount || 0), 0);
        body = `
          <h1 class="doc-title">개인 경비 정산서</h1>
          <table class="info-table">
            <tr><th>제 목</th><td>${escapeHtml(item.title)}</td></tr>
            <tr><th>요 청 자</th><td>${escapeHtml(item.requester)}</td></tr>
            <tr><th>신 청 일</th><td>${escapeHtml((item.created_at||'').slice(0,16).replace('T',' '))}</td></tr>
            <tr><th>결재 상태</th><td>승인됨 · 1차 PM ${escapeHtml(item.pm_approver||'')} ${item.pm_decided_at?'('+(item.pm_decided_at||'').slice(0,10)+')':''} · 2차 관리자 ${escapeHtml(item.admin_approver||'')} ${item.admin_decided_at?'('+(item.admin_decided_at||'').slice(0,10)+')':''}</td></tr>
            <tr><th>합 계</th><td class="total">${fmtKrw(item.amount || total)} 원</td></tr>
          </table>
          <h2 class="section-title">사용 내역</h2>
          <table class="items-table">
            <thead>
              <tr>
                <th style="width:90px;">날짜</th>
                <th style="width:80px;">분류</th>
                <th style="width:110px;" class="amount">금액 (원)</th>
                <th>사용처</th>
                <th>메모</th>
                <th style="width:90px;">영수증</th>
              </tr>
            </thead>
            <tbody>
              ${items.map(it => `
                <tr>
                  <td>${escapeHtml(it.date || '')}</td>
                  <td>${escapeHtml(it.category || '')}</td>
                  <td class="amount">${fmtKrw(it.amount)}</td>
                  <td>${escapeHtml(it.vendor || '')}</td>
                  <td>${escapeHtml(it.memo || '')}</td>
                  <td>${it.receipt && it.receipt.url ? '첨부' : '-'}</td>
                </tr>`).join('')}
              <tr class="total-row">
                <td colspan="2">합계</td>
                <td class="amount">${fmtKrw(item.amount || total)}</td>
                <td colspan="3"></td>
              </tr>
            </tbody>
          </table>
          ${item.detail ? '<h2 class="section-title">메모</h2><p class="memo">' + escapeHtml(item.detail).replace(/\n/g,'<br>') + '</p>' : ''}
          <div class="signature-block">
            <p class="date-line">${ymd}</p>
            <p class="company-name">${escapeHtml(company.name)}${company.ceo ? ' 대표 ' + escapeHtml(company.ceo) : ''}
              <span class="stamp">${company.stampUrl ? '<img src="'+escapeHtml(company.stampUrl)+'" />' : '(인)'}</span>
            </p>
          </div>`;
      } else if (item.req_type === 'book_purchase') {
        const b = item.book_info || {};
        const createdYmd = (item.created_at || '').slice(0, 10);
        const pmYmd = (item.pm_decided_at || '').slice(0, 10);
        const adminYmd = (item.admin_decided_at || '').slice(0, 10);
        const link = b.purchase_link || '';
        const cellTh = 'border:1px solid #94a3b8; background:#f1f5f9; padding:6px 18px; font-weight:700; color:#475569;';
        body = `
          <h1 class="doc-title">도서 구입 신청서</h1>
          <div style="display:flex; justify-content:space-between; align-items:flex-start; gap:20px; margin-bottom:24px;">
            <table class="info-table" style="flex:1; margin-bottom:0;">
              <tr><th style="width:110px;">문서번호</th><td>${escapeHtml(item.id || '')}</td></tr>
              <tr><th>작성일자</th><td>${escapeHtml(createdYmd)}</td></tr>
              <tr><th>신청부서</th><td>${escapeHtml(b.department || '')}</td></tr>
              <tr><th>신청자</th><td>${escapeHtml(item.requester || '')}</td></tr>
            </table>
            <table style="border-collapse:collapse; font-size:13px;">
              <tr>
                <td rowspan="3" style="border:1px solid #94a3b8; background:#f1f5f9; padding:10px 6px; text-align:center; font-weight:700; color:#475569; writing-mode:vertical-rl; letter-spacing:6px;">결재선</td>
                <th style="${cellTh}">1차 (PM)</th>
                <th style="${cellTh}">2차 (대표)</th>
              </tr>
              <tr>
                <td style="border:1px solid #94a3b8; padding:16px 20px; text-align:center; min-width:84px;">${escapeHtml(item.pm_approver || '')}</td>
                <td style="border:1px solid #94a3b8; padding:16px 20px; text-align:center; min-width:84px;">${escapeHtml(item.admin_approver || '')}</td>
              </tr>
              <tr>
                <td style="border:1px solid #94a3b8; padding:4px 8px; text-align:center; font-size:11px; color:#64748b;">${escapeHtml(pmYmd)}</td>
                <td style="border:1px solid #94a3b8; padding:4px 8px; text-align:center; font-size:11px; color:#64748b;">${escapeHtml(adminYmd)}</td>
              </tr>
            </table>
          </div>
          <table class="info-table">
            <tr><th style="width:110px;">도서명</th><td>${escapeHtml(b.book_name || item.title)}</td></tr>
            <tr><th>저자/출판사</th><td>${escapeHtml(b.author_publisher || '')}</td></tr>
            <tr><th>구입처 / 링크</th><td>${link ? '<a href="' + escapeHtml(link) + '">' + escapeHtml(link) + '</a>' : '-'}</td></tr>
            <tr><th>구입목적</th><td>${escapeHtml(b.purpose || '')}</td></tr>
          </table>
          ${item.detail ? '<h2 class="section-title">메모</h2><p class="memo">' + escapeHtml(item.detail).replace(/\n/g, '<br>') + '</p>' : ''}`;
      } else {
        return;
      }
      const docTitle = { cert_employment: '재직증명서', personal_expense: '개인경비 정산서', book_purchase: '도서 구입 신청서' }[item.req_type] || '결재 문서';

      const html = `<!DOCTYPE html>
<html lang="ko"><head><meta charset="UTF-8">
<title>${docTitle} — ${escapeHtml(item.title)}</title>
<style>
  @page { size: A4; margin: 22mm 18mm; }
  * { box-sizing: border-box; }
  body { font-family: 'Pretendard', 'Apple SD Gothic Neo', 'Malgun Gothic', sans-serif;
         color: #1e293b; line-height: 1.6; margin: 0; padding: 24px 28px; max-width: 800px; }
  .toolbar { position: fixed; top: 12px; right: 12px; display: flex; gap: 8px; z-index: 100; }
  .toolbar button { padding: 8px 14px; font-size: 13px; border-radius: 6px;
                    border: 1px solid #cbd5e1; background: #fff; cursor: pointer; }
  .toolbar button.primary { background: #1e293b; color: white; border-color: #1e293b; }
  @media print { .toolbar { display: none !important; } body { padding: 0; } }
  .doc-title { text-align: center; font-size: 28px; font-weight: 800; letter-spacing: 8px;
               margin: 12px 0 32px; padding-bottom: 16px; border-bottom: 2px solid #1e293b; }
  .info-table { width: 100%; border-collapse: collapse; margin-bottom: 24px; }
  .info-table th, .info-table td { border: 1px solid #94a3b8; padding: 10px 14px; font-size: 14px; }
  .info-table th { background: #f1f5f9; width: 130px; text-align: center; font-weight: 700; color: #475569; }
  .info-table td { background: #fff; color: #1e293b; }
  .info-table td.total { font-weight: 800; color: #1e40af; font-size: 16px; }
  .statement { text-align: center; font-size: 15px; margin: 32px 0 24px; line-height: 2; }
  .signature-block { text-align: center; margin-top: 56px; padding-top: 24px; }
  .signature-block .date-line { font-size: 15px; margin-bottom: 12px; }
  .signature-block .company-name { font-size: 17px; font-weight: 700; margin-bottom: 6px; }
  .signature-block .company-meta { font-size: 12px; color: #64748b; margin: 2px 0; }
  .signature-block .stamp { display: inline-block; margin-left: 14px; font-weight: 700; color: #b91c1c; }
  .signature-block .stamp img { height: 56px; vertical-align: middle; }
  .section-title { font-size: 15px; font-weight: 700; margin: 24px 0 10px;
                   padding-left: 8px; border-left: 4px solid #1e293b; }
  .items-table { width: 100%; border-collapse: collapse; margin-bottom: 16px; }
  .items-table th, .items-table td { border: 1px solid #cbd5e1; padding: 8px 10px; font-size: 12px; }
  .items-table th { background: #f1f5f9; font-weight: 700; color: #475569; }
  .items-table td { color: #1e293b; }
  .items-table td.amount, .items-table th.amount { text-align: right; font-variant-numeric: tabular-nums; }
  .items-table tr.total-row td { background: #f8fafc; font-weight: 800; }
  .items-table tr.total-row td.amount { color: #1e40af; font-size: 13px; }
  .memo { font-size: 13px; background: #f8fafc; padding: 12px 14px; border-radius: 6px; border: 1px solid #e2e8f0; white-space: pre-wrap; }
</style></head><body>
<div class="toolbar">
  <button onclick="window.print()" class="primary">인쇄 / PDF 저장</button>
  <button onclick="window.close()">닫기</button>
</div>
${body}
<script>setTimeout(() => window.print(), 400);</script>
</body></html>`;

      const w = window.open('', '_blank', 'width=900,height=1100');
      if (!w) {
        this.showToast('error', '팝업 차단 — 브라우저 팝업을 허용해주세요');
        return;
      }
      w.document.open();
      w.document.write(html);
      w.document.close();
    },
    async syncCoreTreeFromLinear(silent = false) {
      if (this.coreSyncing) return;
      this.coreSyncing = true;
      try {
        const res = await _authFetch(API_BASE + '/api/core-tree/sync-from-linear', { method: 'POST' });
        if (res.ok) {
          const d = await res.json();
          this.coreSyncSummary = `생성 ${d.created} · 갱신 ${d.updated} · 삭제 ${d.deleted}`;
          await this.loadCoreTree();
          if (!silent) {
            setTimeout(() => { this.coreSyncSummary = ''; }, 5000);
          }
        }
      } catch {} finally {
        this.coreSyncing = false;
      }
    },
    // 추가 팝업 상태
    coreAddModal: { show: false, parentId: null, parentType: '', type: '', title: '', author: '', assignee: '', description: '', mode: 'new' },

    _findCoreNodeType(id) {
      // 트리에서 노드 타입 찾기
      const find = (nodes) => {
        for (const n of nodes) {
          if (n.id === id) return n.type;
          const r = find(n.children || []);
          if (r) return r;
        }
        return null;
      };
      return find(this.coreTree) || 'core';
    },

    openCoreAddModal(parentId, type) {
      const parentType = parentId ? this._findCoreNodeType(parentId) : 'root';
      this.coreAddModal = { show: true, parentId, parentType, type, title: '', author: '', assignee: '', description: '', mode: 'new' };
      // core / hypothesis / task 모두 Linear 목록 필요 (Initiative/Project/Issue)
      this.loadLinearListsForCore();
    },

    async selectLinearForCore(item, type) {
      // Linear에서 선택 → 바로 생성 + 연결 (auto_linear는 끔)
      const body = {
        parent_id: this.coreAddModal.parentId,
        type: this.coreAddModal.type,
        title: item.name || item.title,
        week_label: this.execWeek,
      };
      if (type === 'initiative') {
        body.linear_project_id = item.id;
        body.linear_project_name = '🏢 ' + item.name;
      } else if (type === 'project') {
        body.linear_project_id = item.id;
        body.linear_project_name = '📁 ' + item.name;
      } else if (type === 'milestone') {
        body.linear_project_id = item.id;
        body.linear_project_name = '🎯 ' + item.name;
      } else {
        // issue
        body.linear_issue_id = item.id;
        body.linear_issue_identifier = item.identifier || '';
        body.linear_project_name = '📋 [' + (item.identifier || '') + '] ' + (item.title || '');
        body.assignee = item.assignee || '';
      }
      try {
        const res = await _authFetch(API_BASE + '/api/core-tree', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (res.ok) { this.coreAddModal.show = false; await this.loadCoreTree(); }
      } catch {}
    },

    async submitCoreAddModal() {
      const m = this.coreAddModal;
      if (!m.title) { this.showToast('warning', '제목을 입력해주세요'); return; }
      const body = { parent_id: m.parentId, type: m.type, title: m.title, week_label: this.execWeek, description: m.description, auto_linear: true };
      if (m.type === 'core' || m.type === 'hypothesis') body.author = m.author;
      if (m.type === 'task') body.assignee = m.assignee;
      try {
        const res = await _authFetch(API_BASE + '/api/core-tree', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body),
        });
        if (res.ok) { this.coreAddModal.show = false; await this.loadCoreTree(); }
      } catch {}
    },

    async addCoreNode(parentId, type) {
      // 모든 타입 (core / hypothesis / task) 공통으로 모달 팝업 사용
      this.openCoreAddModal(parentId, type);
    },
    async updateCoreNode(id, fields) {
      try {
        await _authFetch(API_BASE + '/api/core-tree/' + id, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(fields),
        });
      } catch {}
    },
    // Linear 프로젝트/이슈 목록 (연결용)
    linearInitiativesList: [],  // Initiative 목록
    linearProjectsList: [],
    linearIssuesList: [],
    linearMilestonesList: [],   // Milestone 목록 (수동 연결용, 향후 확장)
    coreLinkingId: null,
    async loadLinearListsForCore() {
      try {
        // 프로젝트 + 이슈
        const res = await _authFetch(API_BASE + '/api/linear/projects');
        if (res.ok) {
          const d = await res.json();
          this.linearProjectsList = (d.projects || []).map(p => ({ id: p.id, name: p.name }));
          const allIssues = [];
          for (const p of d.projects || []) {
            for (const i of p.issues || []) {
              allIssues.push({ id: i.id, identifier: i.identifier, title: i.title, project: p.name, assignee: i.assignee || '' });
            }
          }
          this.linearIssuesList = allIssues;
        }
        // Initiative
        const initRes = await _authFetch(API_BASE + '/api/linear/initiatives');
        if (initRes.ok) {
          const id = await initRes.json();
          this.linearInitiativesList = (id.initiatives || []).map(i => ({ id: i.id, name: i.name, targetDate: i.targetDate }));
        }
      } catch {}
    },
    async linkLinearInitiative(nodeId) {
      await this.loadLinearListsForCore();
      this.coreLinkingId = 'init-' + nodeId;
    },
    async linkLinearProject(nodeId) {
      await this.loadLinearListsForCore();
      this.coreLinkingId = 'proj-' + nodeId;
    },
    async linkLinearIssue(nodeId) {
      await this.loadLinearListsForCore();
      this.coreLinkingId = 'issue-' + nodeId;
    },
    async saveCoreLink(nodeId, type, selectedId) {
      if (type === 'initiative') {
        const init = this.linearInitiativesList.find(i => i.id === selectedId);
        await this.updateCoreNode(nodeId, { linear_project_id: selectedId, linear_project_name: init ? '🏢 ' + init.name : '' });
      } else if (type === 'project') {
        const proj = this.linearProjectsList.find(p => p.id === selectedId);
        await this.updateCoreNode(nodeId, { linear_project_id: selectedId, linear_project_name: proj ? '📁 ' + proj.name : '' });
      } else if (type === 'milestone') {
        const ms = this.linearMilestonesList.find(m => m.id === selectedId);
        await this.updateCoreNode(nodeId, { linear_project_id: selectedId, linear_project_name: ms ? '🎯 ' + ms.name : '' });
      } else {
        // type === 'issue'
        const issue = this.linearIssuesList.find(i => i.id === selectedId);
        await this.updateCoreNode(nodeId, {
          linear_issue_id: selectedId,
          linear_issue_identifier: issue ? issue.identifier : '',
          linear_project_name: issue ? ('📋 [' + issue.identifier + '] ' + (issue.title || '')) : '',
        });
      }
      this.coreLinkingId = null;
      await this.loadCoreTree();
    },
    async unlinkCore(nodeId, type) {
      if (type === 'issue') {
        await this.updateCoreNode(nodeId, { linear_issue_id: '', linear_issue_identifier: '', linear_project_name: '' });
      } else {
        // project / initiative / milestone 모두 linear_project_id에 저장됨
        await this.updateCoreNode(nodeId, { linear_project_id: '', linear_project_name: '' });
      }
      await this.loadCoreTree();
    },

    async deleteCoreNode(id) {
      if (!confirm('삭제하시겠습니까? 하위 항목도 모두 삭제됩니다.')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/core-tree/' + id, { method: 'DELETE' });
        if (res.ok) await this.loadCoreTree();
      } catch {}
    },

    // 팀 설정 (localStorage 저장)
    teamConfig: Object.assign({ slackMembers: {}, slackBotToken: '' }, loadTeamConfig()),

    // 팀 설정 저장
    saveTeam() {
      this.teamConfig._version = TEAM_CONFIG_VERSION;
      localStorage.setItem(TEAM_CONFIG_KEY, JSON.stringify(this.teamConfig));
      // 서버에 비동기 저장
      _saveToServer('/api/team-config', this.teamConfig);
    },

    // 팀 설정 초기값 복원
    resetTeamConfig() {
      this.teamConfig = getTeamConfig();
      this.saveTeam();
    },

    // ══════════════════════════════
    // 할일 관리
    // ══════════════════════════════
    todos: [],
    todoFilter: 'all', // 'all' 또는 멤버 이름
    todoModal: { show: false, idx: -1 },
    todoForm: {},
    slackSettingsModal: false,

    // 할일 데이터 로드
    async loadTodos() {
      try {
        const res = await _authFetch(API_BASE +'/api/todos');
        if (res.ok) {
          const data = await res.json();
          this.todos = data.todos || [];
        }
      } catch { /* 무시 */ }
    },

    // ── 주간 퍼널 점검 체크리스트 ──
    checklist: { sections: [], weeks: {} },
    checklistWeek: '',
    checklistLoaded: false,
    _CHECKLIST_CYCLE: ['', '✅', '⏸', '⏳', '➖'],

    _mondayOf(d) {
      const dt = d ? new Date(d) : new Date();
      const day = (dt.getDay() + 6) % 7; // 0=월
      dt.setDate(dt.getDate() - day);
      return dt.getFullYear() + '-' + String(dt.getMonth() + 1).padStart(2, '0') + '-' + String(dt.getDate()).padStart(2, '0');
    },

    async loadChecklist(_retry) {
      if (!_retry) this._loadChecklistUI();
      let ok = false;
      try {
        const res = await _authFetch(API_BASE + '/api/weekly-checklist');
        if (res.ok) {
          const d = await res.json();
          d.sections = d.sections || [];
          d.weeks = d.weeks || {};
          this.checklist = d;
          ok = true;
        }
      } catch { /* 무시 */ }
      // 초기 동시요청 폭주로 일시 실패(빈 결과) 시 1회 재시도 — 데이터 유실 없이 채움
      if (!ok && !(this.checklist.sections || []).length && !_retry) {
        setTimeout(() => this.loadChecklist(true), 2000);
      }
      const wk = this._mondayOf();
      if (!this.checklist.weeks[wk]) this.checklist.weeks[wk] = {};
      this.checklistWeek = wk;
      this.checklistLoaded = true;
    },

    saveChecklist() {
      this.checklist.weeks = this.checklist.weeks || {};
      _saveToServer('/api/weekly-checklist', this.checklist);
    },

    checklistWeekKeys() {
      const keys = Object.keys(this.checklist.weeks || {});
      const cur = this._mondayOf();
      if (!keys.includes(cur)) keys.push(cur);
      return keys.sort().reverse(); // 최신 먼저
    },

    newChecklistWeek() {
      const wk = this._mondayOf();
      if (!this.checklist.weeks[wk]) { this.checklist.weeks[wk] = {}; this.saveChecklist(); }
      this.checklistWeek = wk;
    },

    _weekMap() {
      if (!this.checklist.weeks[this.checklistWeek]) this.checklist.weeks[this.checklistWeek] = {};
      return this.checklist.weeks[this.checklistWeek];
    },

    itemState(itemId) {
      // 읽기 전용(렌더 중 호출) — mutation 금지
      const wm = (this.checklist.weeks || {})[this.checklistWeek] || {};
      return wm[itemId] || { s: '', by: '' };
    },

    cycleStatus(itemId) {
      const wm = this._weekMap();
      const cur = (wm[itemId] && wm[itemId].s) || '';
      const i = this._CHECKLIST_CYCLE.indexOf(cur);
      const next = this._CHECKLIST_CYCLE[(i + 1) % this._CHECKLIST_CYCLE.length];
      wm[itemId] = { ...(wm[itemId] || {}), s: next };
      this.saveChecklist();
    },

    setItemBy(itemId, name) {
      const wm = this._weekMap();
      wm[itemId] = { ...(wm[itemId] || { s: '' }), by: name };
      this.saveChecklist();
    },

    // 같은 상태가 현재 주부터 과거로 몇 주 연속인지. status=''(안봄)은 미입력/빈 상태.
    streakCount(itemId, status) {
      const keys = Object.keys(this.checklist.weeks || {}).filter(k => k <= this.checklistWeek).sort().reverse();
      let n = 0;
      for (const k of keys) {
        const s = (this.checklist.weeks[k][itemId] || {}).s || '';
        if (s === status) n++; else break;
      }
      return n;
    },
    // ⏳×N 배지용 (못함 연속)
    missedCount(itemId) { return this.streakCount(itemId, '⏳'); },

    statusLabel(s) {
      return ({ '✅': '확인함', '⏸': '할 시기가 아님', '⏳': '수행 불가', '➖': '해당없음' })[s] || '아직 안 봄';
    },
    // 상태 드롭다운 옵션 (◯안봄=빈 문자열)
    _STATUS_OPTIONS: [
      { s: '', label: '◯ 안 봄' },
      { s: '✅', label: '✅ 확인함' },
      { s: '⏸', label: '⏸ 할 시기가 아님' },
      { s: '⏳', label: '⏳ 수행 불가' },
      { s: '➖', label: '➖ 해당없음' },
    ],
    // 드롭다운에서 상태 직접 지정
    setItemStatus(itemId, status) {
      const wm = this._weekMap();
      wm[itemId] = { ...(wm[itemId] || {}), s: status };
      this.saveChecklist();
    },
    // 섹션 순서 이동 (dir: -1 위, +1 아래)
    moveChecklistSection(idx, dir) {
      const arr = this.checklist.sections;
      const j = idx + dir;
      if (j < 0 || j >= arr.length) return;
      [arr[idx], arr[j]] = [arr[j], arr[idx]];
      this.saveChecklist();
    },
    // 담당자별 색상 (배지 + 좌측 보더). 미지정/기타는 amber.
    // 담당자 색상 팔레트(12색). 클래스는 리터럴 문자열이라 tailwind 정적빌드가 스캔함.
    _OWNER_PALETTE: [
      { badge: 'bg-slate-100 text-slate-600 border-slate-200', bar: 'border-l-slate-400' },        // 0
      { badge: 'bg-blue-50 text-blue-700 border-blue-200', bar: 'border-l-blue-400' },             // 1
      { badge: 'bg-pink-50 text-pink-700 border-pink-200', bar: 'border-l-pink-400' },             // 2
      { badge: 'bg-emerald-50 text-emerald-700 border-emerald-200', bar: 'border-l-emerald-400' }, // 3
      { badge: 'bg-violet-50 text-violet-700 border-violet-200', bar: 'border-l-violet-400' },     // 4
      { badge: 'bg-red-50 text-red-700 border-red-200', bar: 'border-l-red-400' },                 // 5
      { badge: 'bg-amber-50 text-amber-700 border-amber-200', bar: 'border-l-amber-400' },         // 6
      { badge: 'bg-lime-50 text-lime-700 border-lime-200', bar: 'border-l-lime-400' },             // 7
      { badge: 'bg-cyan-50 text-cyan-700 border-cyan-200', bar: 'border-l-cyan-400' },             // 8
      { badge: 'bg-indigo-50 text-indigo-700 border-indigo-200', bar: 'border-l-indigo-400' },     // 9
      { badge: 'bg-fuchsia-50 text-fuchsia-700 border-fuchsia-200', bar: 'border-l-fuchsia-400' }, // 10
      { badge: 'bg-teal-50 text-teal-700 border-teal-200', bar: 'border-l-teal-400' },             // 11
    ],
    // 설치자가 지정한 색상이 없으면 이름 해시로 기존 팔레트 안에서 안정 배정한다.
    get _OWNER_FIXED() { return this.teamConfig.ownerColors || {}; },
    ownerStyle(owner) {
      // 미지정 담당자는 중립 회색
      if (!owner) return { badge: 'bg-slate-50 text-slate-400 border-slate-200', bar: 'border-l-slate-200' };
      const p = this._OWNER_PALETTE;
      const fixed = this._OWNER_FIXED[owner];
      if (Number.isInteger(fixed) && fixed >= 0 && fixed < p.length) return p[fixed];
      // 고정 매핑에 없는 이름(신규 추가 등)은 이름 해시로 안정 배정
      let h = 0;
      for (let i = 0; i < owner.length; i++) h = (h * 31 + owner.charCodeAt(i)) >>> 0;
      return p[h % p.length];
    },
    // 상태 필터 ('__all'=전체, ''=안봄)
    checklistFilter: '__all',
    matchesFilter(itemId) {
      return this.checklistFilter === '__all' || (this.itemState(itemId).s || '') === this.checklistFilter;
    },
    sectionHasMatch(sec) {
      if (this.checklistFilter === '__all') return true;
      return (sec.items || []).some(it => (this.itemState(it.id).s || '') === this.checklistFilter);
    },

    // ── 구조 편집 (모든 로그인 사용자) ──
    addChecklistSection() {
      this.checklist.sections.push({ id: 'c' + uid(), title: '새 섹션', owner: '', purpose: '', items: [] });
      this.saveChecklist();
    },
    removeChecklistSection(idx) {
      if (!confirm('이 섹션을 삭제할까요? (모든 주차의 항목 상태도 함께 사라집니다)')) return;
      const sec = this.checklist.sections[idx];
      // 서버 병합 시 부활 방지용 tombstone(섹션 + 그 안 항목 전부)
      this.checklist._deletedSectionIds = this.checklist._deletedSectionIds || [];
      this.checklist._deletedItemIds = this.checklist._deletedItemIds || [];
      if (sec) {
        this.checklist._deletedSectionIds.push(sec.id);
        (sec.items || []).forEach(it => this.checklist._deletedItemIds.push(it.id));
      }
      this.checklist.sections.splice(idx, 1);
      this.saveChecklist();
    },
    addChecklistItem(sec) {
      sec.items = sec.items || [];
      sec.items.push({ id: 'c' + uid(), text: '새 항목' });
      this.saveChecklist();
    },
    removeChecklistItem(sec, itemIdx) {
      const it = sec.items[itemIdx];
      this.checklist._deletedItemIds = this.checklist._deletedItemIds || [];
      if (it) this.checklist._deletedItemIds.push(it.id);
      sec.items.splice(itemIdx, 1);
      this.saveChecklist();
    },

    // ── 접기/펼치기 (UI 상태만 — localStorage, 서버 데이터 아님) ──
    checklistOpen: false,
    checklistSecOpen: {},
    _loadChecklistUI() {
      try {
        const ui = JSON.parse(localStorage.getItem('weeklyChecklistUI') || '{}');
        this.checklistOpen = !!ui.open;
        this.checklistSecOpen = ui.secs || {};
      } catch { /* 무시 */ }
    },
    _saveChecklistUI() {
      try { localStorage.setItem('weeklyChecklistUI', JSON.stringify({ open: this.checklistOpen, secs: this.checklistSecOpen })); } catch { /* 무시 */ }
    },
    toggleChecklist() { this.checklistOpen = !this.checklistOpen; this._saveChecklistUI(); },
    toggleChecklistSec(id) { this.checklistSecOpen[id] = !this.checklistSecOpen[id]; this._saveChecklistUI(); },
    isChecklistSecOpen(id) { return !!this.checklistSecOpen[id]; },
    allChecklistItems() { return (this.checklist.sections || []).flatMap(s => s.items || []); },
    checklistDone(items) {
      const wm = (this.checklist.weeks || {})[this.checklistWeek] || {};
      return (items || []).filter(it => (wm[it.id] || {}).s === '✅').length;
    },

    // ── 2주+ 연속 지속 상태 요약(상단바) — ✅확인 제외 4개 ──
    _OVERDUE: [
      { key: 'pause', s: '⏸', label: '할 시기가 아님' },
      { key: 'miss', s: '⏳', label: '수행 불가' },
      { key: 'na', s: '➖', label: '해당없음' },
      { key: 'unseen', s: '', label: '안 봄', icon: '◯' },
    ],
    overdueOpen: '',  // 펼쳐진 칩 key ('' = 닫힘)
    toggleOverdue(key) { this.overdueOpen = this.overdueOpen === key ? '' : key; },
    // 해당 상태가 2주 이상 연속인 항목 수(전체)
    overdueCount(status) {
      return this.allChecklistItems().filter(it => this.streakCount(it.id, status) >= 2).length;
    },
    // 섹션별 내역 [{title, owner, count, items:[text...]}] (해당 항목 있는 섹션만)
    overdueBySection(status) {
      const out = [];
      for (const sec of (this.checklist.sections || [])) {
        const hit = (sec.items || []).filter(it => this.streakCount(it.id, status) >= 2);
        if (hit.length > 0) out.push({ title: sec.title, owner: sec.owner, count: hit.length, items: hit.map(it => it.text) });
      }
      return out;
    },

    // 할일 데이터 저장
    async saveTodos() {
      const payload = { todos: this.todos };
      localStorage.setItem('weeklyReportTodos', JSON.stringify(payload));
      _saveToServer('/api/todos', payload);
    },

    // 필터링된 할일 목록
    get filteredTodos() {
      if (this.todoFilter === 'all') return this.todos;
      return this.todos.filter(t => t.assignee === this.todoFilter);
    },

    // 상태별 할일 수
    todoCountByStatus(status) {
      const list = this.todoFilter === 'all' ? this.todos : this.filteredTodos;
      return list.filter(t => t.status === status).length;
    },

    // 할일 모달 열기
    openTodo(idx) {
      if (idx === -1) {
        this.todoForm = {
          title: '', description: '', assignee: '', assignedBy: '',
          dueDate: '', status: 'todo', priority: 'medium',
        };
      } else {
        this.todoForm = { ...this.todos[idx] };
      }
      this.todoModal = { show: true, idx };
    },

    // 할일 저장
    async saveTodo() {
      const item = { ...this.todoForm };
      item.id = item.id || uid();
      const isNew = this.todoModal.idx === -1;
      if (isNew) {
        item.createdAt = new Date().toISOString();
        item.completedAt = null;
        this.todos.push(item);
      } else {
        // 완료 처리 시 completedAt 기록
        const old = this.todos[this.todoModal.idx];
        if (item.status === 'done' && old.status !== 'done') {
          item.completedAt = new Date().toISOString();
        }
        this.todos.splice(this.todoModal.idx, 1, item);
      }
      this.todoModal.show = false;
      await this.saveTodos();

      // 새 할일 배정 시 Slack DM 알림
      if (isNew && item.assignee) {
        await this._sendTodoSlackNotify(item, 'assign');
      }
    },

    // 할일 상태 토글 (카드에서 체크박스 클릭)
    async toggleTodoStatus(idx) {
      const todo = this.filteredTodos[idx];
      // 원본 배열에서 찾기
      const realIdx = this.todos.findIndex(t => t.id === todo.id);
      if (realIdx === -1) return;
      if (this.todos[realIdx].status === 'done') {
        this.todos[realIdx].status = 'todo';
        this.todos[realIdx].completedAt = null;
      } else {
        this.todos[realIdx].status = 'done';
        this.todos[realIdx].completedAt = new Date().toISOString();
      }
      await this.saveTodos();
    },

    // 할일 삭제
    async removeTodo(idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      const todo = this.filteredTodos[idx];
      const realIdx = this.todos.findIndex(t => t.id === todo.id);
      if (realIdx > -1) {
        this.todos.splice(realIdx, 1);
        await this.saveTodos();
      }
    },

    // Slack DM 알림 전송 (토큰은 서버 환경변수에서 사용)
    async _sendTodoSlackNotify(todo, type) {
      // 서버가 assignee 이름으로 ext_users.slack_id 조회 → spoof 차단
      if (!todo.assignee) return;
      let message = '';
      if (type === 'assign') {
        const by = todo.assignedBy ? ` (배정자: ${todo.assignedBy})` : '';
        const due = todo.dueDate ? `\n마감일: ${todo.dueDate}` : '';
        message = `[새 할일] ${todo.title}${by}${due}`;
      }
      if (!message) return;
      try {
        await _authFetch(API_BASE +'/api/slack/notify', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ assignee: todo.assignee, message }),
        });
      } catch { /* slack_id 미등록·토큰 미설정 등은 조용히 skip */ }
    },

    // Slack 모달 — admin 전용. ext_users 기반 slack_id 매핑.
    slackUsersList: [],
    slackUsersLoading: false,
    async openSlackSettings() {
      this.slackSettingsModal = true;
      if (!this.isAdmin()) return;
      this.slackUsersLoading = true;
      try {
        const res = await _authFetch(API_BASE + '/api/auth/users');
        if (res.ok) {
          const d = await res.json();
          // admin 우선, 그다음 pm, member 순
          const order = { admin: 0, pm: 1, member: 2 };
          this.slackUsersList = (d.users || [])
            .map(u => ({ ...u, slack_id: u.slack_id || '', _saveStatus: '' }))
            .sort((a, b) => (order[a.role] - order[b.role]) || a.name.localeCompare(b.name, 'ko'));
        }
      } catch {}
      this.slackUsersLoading = false;
    },
    async saveSlackUserId(u) {
      if (!u || !u.id) return;
      u._saveStatus = 'saving';
      try {
        const res = await _authFetch(API_BASE + '/api/auth/users/' + u.id, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ slack_id: (u.slack_id || '').trim() }),
        });
        u._saveStatus = res.ok ? 'ok' : 'err';
        setTimeout(() => { u._saveStatus = ''; }, 2000);
      } catch { u._saveStatus = 'err'; }
    },

    // Slack 봇 연결 테스트 (토큰은 서버 환경변수에서 사용)
    slackTestResult: '',
    async testSlackConnection() {
      this.slackTestResult = '테스트 중...';
      try {
        const res = await _authFetch(API_BASE +'/api/slack/test', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const data = await res.json();
        if (data.ok) {
          this.slackTestResult = `연결 성공! 팀: ${data.team}, 봇: ${data.bot}`;
        } else {
          this.slackTestResult = `연결 실패: ${data.error}`;
        }
      } catch {
        this.slackTestResult = '서버 연결 실패';
      }
    },

    // 할일 우선순위 표시
    todoPriorityClass(p) {
      return { high: 'bg-red-100 text-red-600', medium: 'bg-amber-100 text-amber-700', low: 'bg-slate-100 text-slate-500' }[p] || '';
    },
    todoPriorityLabel(p) {
      return { high: '높음', medium: '보통', low: '낮음' }[p] || '';
    },

    // 마감일까지 남은 일수
    todoDaysLeft(dueDate) {
      if (!dueDate) return null;
      const due = new Date(dueDate + 'T23:59:59');
      const now = new Date();
      const diff = Math.ceil((due - now) / (1000 * 60 * 60 * 24));
      return diff;
    },

    todoDueBadge(dueDate) {
      const days = this.todoDaysLeft(dueDate);
      if (days === null) return '';
      if (days < 0) return 'text-red-600 bg-red-50';
      if (days <= 1) return 'text-orange-600 bg-orange-50';
      if (days <= 3) return 'text-amber-600 bg-amber-50';
      return 'text-slate-500 bg-slate-50';
    },

    todoDueText(dueDate) {
      const days = this.todoDaysLeft(dueDate);
      if (days === null) return '';
      if (days < 0) return `${Math.abs(days)}일 초과`;
      if (days === 0) return '오늘 마감';
      if (days === 1) return '내일 마감';
      return `D-${days}`;
    },

    // === 팀 설정 CRUD ===
    addSection(category) {
      this.teamConfig.sections.push({ section: '새 영역', category, roles: [] });
      this.saveTeam();
    },

    deleteSection(sec) {
      const idx = this.teamConfig.sections.indexOf(sec);
      if (idx > -1) { this.teamConfig.sections.splice(idx, 1); this.saveTeam(); }
    },

    deleteCategory(category) {
      this.teamConfig.sections = this.teamConfig.sections.filter(s => s.category !== category);
      if (this.teamConfig.categoryOrder) {
        this.teamConfig.categoryOrder = this.teamConfig.categoryOrder.filter(c => c !== category);
      }
      this.saveTeam();
    },

    // 카테고리 순서 이동 (dir: -1=왼쪽, +1=오른쪽)
    moveCategoryOrder(idx, dir) {
      const arr = this.teamConfig.categoryOrder;
      if (!arr) return;
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= arr.length) return;
      [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
      this.saveTeam();
    },

    addRole(sec) {
      sec.roles.push({ owner: '', theme: '', questions: [] });
      this.saveTeam();
    },

    deleteRole(sec, rIdx) {
      sec.roles.splice(rIdx, 1);
      this.saveTeam();
    },

    addQuestion(role) {
      role.questions.push('');
      this.saveTeam();
    },

    deleteQuestion(role, qIdx) {
      role.questions.splice(qIdx, 1);
      if (role.answers) role.answers.splice(qIdx, 1);
      this.saveTeam();
    },

    addAnswer(role, qIdx) {
      if (!role.answers) role.answers = [];
      while (role.answers.length <= qIdx) role.answers.push('');
      this.saveTeam();
    },

    deleteAnswer(role, qIdx) {
      if (role.answers) { role.answers[qIdx] = ''; this.saveTeam(); }
    },

    // 카테고리별 섹션 그룹핑 (칸반 뷰용)
    get categorizedSections() {
      const order = this.teamConfig.categoryOrder || [];
      const map = {};
      this.teamConfig.sections.forEach(sec => {
        if (!map[sec.category]) map[sec.category] = [];
        map[sec.category].push(sec);
      });
      // categoryOrder 순서 유지 + 새로 추가된 카테고리는 뒤에
      const ordered = order.filter(c => map[c]);
      const rest = Object.keys(map).filter(c => !order.includes(c));
      // 새 카테고리를 categoryOrder에 자동 추가
      if (rest.length > 0) {
        this.teamConfig.categoryOrder = [...order, ...rest];
      }
      return [...ordered, ...rest].map(c => ({ category: c, sections: map[c] }));
    },

    // 섹션 → 해당 담당자의 role 목록 (섹션 선택 시)
    get sectionRoles() {
      const sec = this.teamConfig.sections.find(s => s.section === this.form.section);
      return sec ? sec.roles : [];
    },

    // 현재 선택된 role의 질문 목록
    get currentQuestions() {
      const sec = this.teamConfig.sections.find(s => s.section === this.form.section);
      if (!sec) return [];
      const role = sec.roles.find(r => r.owner === this.form.owner && r.theme === this.form.theme);
      return role ? role.questions : [];
    },

    // 섹션 변경 시 담당자/테마 자동 설정
    onSectionChange() {
      const sec = this.teamConfig.sections.find(s => s.section === this.form.section);
      if (!sec) { this.form.owner = ''; this.form.theme = ''; this.form.category = ''; return; }
      this.form.category = sec.category;
      if (sec.roles.length === 1) {
        this.form.owner = sec.roles[0].owner;
        this.form.theme = sec.roles[0].theme;
        this._initAnswers(sec.roles[0].questions.length);
      } else {
        this.form.owner = '';
        this.form.theme = '';
      }
    },

    // 담당자 변경 시 테마 자동 설정
    onRoleChange() {
      const sec = this.teamConfig.sections.find(s => s.section === this.form.section);
      if (!sec) return;
      const role = sec.roles.find(r => r.owner === this.form.owner);
      if (role) {
        this.form.theme = role.theme;
        this._initAnswers(role.questions.length);
      }
    },

    // answers 배열 초기화 (기존 답변 유지)
    _initAnswers(count) {
      if (!this.form.answers) this.form.answers = [];
      while (this.form.answers.length < count) this.form.answers.push('');
    },

    // 카테고리별 색상
    categoryColor(cat) {
      return {
        '엔지니어링': 'bg-violet-100 text-violet-700',
        '사용자 경험': 'bg-rose-100 text-rose-700',
        '데이터 분석': 'bg-cyan-100 text-cyan-700',
        '협업 효율': 'bg-lime-100 text-lime-700',
        '법무': 'bg-amber-100 text-amber-700',
      }[cat] || 'bg-slate-100 text-slate-600';
    },

    // 카테고리별 칸반 컬럼 상단 보더 색상
    categoryBorderColor(cat) {
      return {
        '사용자 경험': 'border-rose-400',
        '데이터 분석': 'border-cyan-400',
        '협업 효율': 'border-lime-400',
        '엔지니어링': 'border-violet-400',
        '법무': 'border-amber-400',
      }[cat] || 'border-slate-300';
    },

    // 카테고리별 도트 색상
    categoryDotColor(cat) {
      return {
        '사용자 경험': 'bg-rose-400',
        '데이터 분석': 'bg-cyan-400',
        '협업 효율': 'bg-lime-400',
        '엔지니어링': 'bg-violet-400',
        '법무': 'bg-amber-400',
      }[cat] || 'bg-slate-400';
    },

    // Q 배지 색상
    qBadgeColor(idx) {
      return ['text-amber-700 bg-amber-100', 'text-emerald-700 bg-emerald-100', 'text-blue-700 bg-blue-100', 'text-violet-700 bg-violet-100'][idx] || 'text-slate-700 bg-slate-100';
    },

    // 드래그 앤 드롭 상태
    dragState: { type: null, fromIdx: -1 },

    tabs: [
      { id: 'report', label: '주간 리포트' },
      // { id: 'daily', label: '일일 회의' },  // 2026-05-29 숨김 (요청)
      { id: 'notes', label: '회의록' },
      // { id: 'team-project', label: '팀 프로젝트' },
      // { id: 'todos', label: '할일' },
      { id: 'expenses', label: '비용 관리' },
      { id: 'hr', label: '근태/연차' },
      { id: 'approval', label: '전자결재' },
      { id: 'settings', label: '설정' },
    ],

    // ── 설정 탭 state ──
    settingsPwModal: { show: false, current: '', next: '', confirm: '' },
    settingsResetTarget: { user_id: '', name: '' },
    settingsSlackToken: '',
    settingsSlackSaving: false,
    settingsSystemInfo: null,
    settingsBackups: [],
    settingsAiBusy: false,
    settingsAiSummary: '',
    settingsAnomalies: [],
    settingsUsers: [],
    settingsExt: { holidays: [], errorLogs: [], aiHistory: [], systemSettings: null },
    settingsAddUserModal: { show: false, name: '', email: '', role: 'member', password: '', slack_id: '', hire_date: '', leave_start_date: '' },
    settingsHolidayForm: { date: '', name: '' },
    async loadSettingsData() {
      // admin/pm이면 시스템 정보 + 백업 + 사용자 목록 + 확장 데이터 로드
      if (!this.isAdmin() && !this.isPm()) return;
      try {
        const [si, bk, us, ho, el, ah, ss] = await Promise.all([
          _authFetch(API_BASE + '/api/system/info').then(r => r.ok ? r.json() : null),
          _authFetch(API_BASE + '/api/system/backups').then(r => r.ok ? r.json() : { backups: [] }),
          _authFetch(API_BASE + '/api/auth/users').then(r => r.ok ? r.json() : { users: [] }),
          _authFetch(API_BASE + '/api/holidays').then(r => r.ok ? r.json() : { holidays: [] }),
          _authFetch(API_BASE + '/api/system/error-logs?limit=50').then(r => r.ok ? r.json() : { logs: [] }),
          _authFetch(API_BASE + '/api/ai/summaries?limit=10').then(r => r.ok ? r.json() : { summaries: [] }),
          _authFetch(API_BASE + '/api/system/settings').then(r => r.ok ? r.json() : null),
        ]);
        this.settingsSystemInfo = si;
        this.settingsBackups = (bk && bk.backups) || [];
        this.settingsUsers = (us && us.users) || [];
        this.settingsExt = {
          holidays: (ho && ho.holidays) || [],
          errorLogs: (el && el.logs) || [],
          aiHistory: (ah && ah.summaries) || [],
          systemSettings: ss,
        };
      } catch {}
    },
    async submitChangePassword() {
      const m = this.settingsPwModal;
      if (!m.current || !m.next) { this.showToast('warning', '현재/새 비밀번호를 모두 입력하세요'); return; }
      if (m.next !== m.confirm) { this.showToast('warning', '새 비밀번호 확인이 일치하지 않습니다'); return; }
      if (!this.isValidNewPassword(m.next)) return;
      try {
        const res = await _authFetch(API_BASE + '/api/auth/change-password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ current_password: m.current, new_password: m.next }),
        });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', '비밀번호가 변경되었습니다');
          this.settingsPwModal = { show: false, current: '', next: '', confirm: '' };
        } else {
          this.showToast('error', d.error || '변경 실패');
        }
      } catch (e) { this.showToast('error', '변경 실패: ' + e.message); }
    },
    async resetUserPassword(user) {
      if (!user || !user.id) return;
      const newPassword = prompt(`${user.name} 님의 새 비밀번호를 입력하세요 (12자 이상):`);
      if (newPassword === null) return;
      if (!this.isValidNewPassword(newPassword)) return;
      const repeated = prompt('새 비밀번호를 다시 입력하세요:');
      if (repeated === null) return;
      if (newPassword !== repeated) {
        this.showToast('warning', '새 비밀번호 확인이 일치하지 않습니다.');
        return;
      }
      if (!confirm(`${user.name} 님의 비밀번호를 입력한 새 값으로 변경할까요?`)) return;
      try {
        const res = await _authFetch(API_BASE + '/api/auth/reset-password', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ user_id: user.id, new_password: newPassword }),
        });
        const d = await res.json().catch(() => ({}));
        if (res.ok) this.showToast('success', `${d.name || user.name} 비밀번호 변경 완료`);
        else this.showToast('error', d.error || '변경 실패');
      } catch (e) { this.showToast('error', '변경 실패: ' + e.message); }
    },
    async saveSlackToken() {
      if (!this.settingsSlackToken || !this.settingsSlackToken.startsWith('xoxb-')) {
        this.showToast('warning', 'xoxb- 로 시작하는 봇 토큰을 입력하세요');
        return;
      }
      this.settingsSlackSaving = true;
      try {
        const res = await _authFetch(API_BASE + '/api/slack/setup', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ bot_token: this.settingsSlackToken }),
        });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', `Slack 연결됨: ${d.team || ''} / ${d.bot || ''}`);
          this.settingsSlackToken = '';
          await this.loadSettingsData();
        } else this.showToast('error', d.error || '연결 실패');
      } catch (e) { this.showToast('error', '연결 실패: ' + e.message); }
      this.settingsSlackSaving = false;
    },
    async createManualBackup() {
      try {
        const res = await _authFetch(API_BASE + '/api/system/backup', { method: 'POST' });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', `백업 생성: ${d.backup_id}`);
          await this.loadSettingsData();
        } else this.showToast('error', d.error || '백업 실패');
      } catch (e) { this.showToast('error', '백업 실패: ' + e.message); }
    },
    async restoreBackup(backupId) {
      if (!confirm(`백업 ${backupId} 으로 리포트를 복원할까요? 현재 데이터는 자동 백업됩니다.`)) return;
      try {
        const res = await _authFetch(API_BASE + `/api/system/restore/${backupId}`, { method: 'POST' });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', '복원 완료 — 페이지 새로고침');
          setTimeout(() => location.reload(), 1200);
        } else this.showToast('error', d.error || '복원 실패');
      } catch (e) { this.showToast('error', '복원 실패: ' + e.message); }
    },
    async clearLinearCache() {
      try {
        const res = await _authFetch(API_BASE + '/api/system/cache-clear', { method: 'POST' });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', `캐시 ${d.cleared_entries}건 초기화`);
          await this.loadSettingsData();
        } else this.showToast('error', d.error || '실패');
      } catch (e) { this.showToast('error', '실패: ' + e.message); }
    },
    async runWeeklySummary() {
      this.settingsAiBusy = true; this.settingsAiSummary = '';
      try {
        const res = await _authFetch(API_BASE + '/api/ai/weekly-summary', { method: 'POST' });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.settingsAiSummary = d.summary || '';
          this.showToast('success', `요약 생성 완료 (입력 ${d.input_chars} chars)`);
        } else this.showToast('error', d.error || 'AI 요약 실패');
      } catch (e) { this.showToast('error', 'AI 요약 실패: ' + e.message); }
      this.settingsAiBusy = false;
    },
    async runAnomalyCheck() {
      this.settingsAiBusy = true; this.settingsAnomalies = [];
      try {
        const res = await _authFetch(API_BASE + '/api/ai/anomaly-check', { method: 'POST' });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.settingsAnomalies = d.anomalies || [];
          this.showToast('success', `이상 ${this.settingsAnomalies.length}건 감지 (고심각도 ${d.high_severity})`);
        } else this.showToast('error', d.error || '이상 감지 실패');
      } catch (e) { this.showToast('error', '이상 감지 실패: ' + e.message); }
      this.settingsAiBusy = false;
    },
    // ── 사용자 관리 (인라인 편집 + 추가) ──
    async saveUserField(u, field, value) {
      if (!u || !u.id) return;
      try {
        const res = await _authFetch(API_BASE + `/api/auth/users/${u.id}`, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ [field]: value }),
        });
        if (res.ok) {
          u[field] = value;
          this.showToast('success', `${u.name} ${field} 저장`);
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '저장 실패');
        }
      } catch (e) { this.showToast('error', '저장 실패: ' + e.message); }
    },
    async toggleUserActive(u) {
      const newVal = !u.is_active;
      const verb = newVal ? '활성화' : '비활성화 (즉시 강제 로그아웃)';
      if (!confirm(`${u.name} 님을 ${verb}하시겠어요?`)) return;
      await this.saveUserField(u, 'is_active', newVal);
      await this.loadSettingsData();
    },
    openAddUserModal() {
      this.settingsAddUserModal = { show: true, name: '', email: '', role: 'member', password: '', slack_id: '', hire_date: '', leave_start_date: '' };
    },
    async submitAddUser() {
      const m = this.settingsAddUserModal;
      if (!m.name || !m.email) { this.showToast('warning', '이름·이메일은 필수'); return; }
      if (!this.isValidNewPassword(m.password)) return;
      try {
        const res = await _authFetch(API_BASE + '/api/auth/register', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(m),
        });
        const d = await res.json().catch(() => ({}));
        if (res.ok) {
          this.showToast('success', `${m.name} 등록 완료`);
          this.settingsAddUserModal.show = false;
          this.settingsAddUserModal.password = '';
          await this.loadSettingsData();
        } else {
          this.showToast('error', d.error || '등록 실패');
        }
      } catch (e) { this.showToast('error', '등록 실패: ' + e.message); }
    },

    // ── 공휴일 ──
    async addHoliday() {
      const f = this.settingsHolidayForm;
      if (!f.date) { this.showToast('warning', '날짜를 선택하세요'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/holidays', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(f),
        });
        if (res.ok) {
          this.showToast('success', `공휴일 추가 ${f.date}`);
          this.settingsHolidayForm = { date: '', name: '' };
          await this.loadSettingsData();
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '추가 실패');
        }
      } catch (e) { this.showToast('error', '실패: ' + e.message); }
    },
    async deleteHoliday(date) {
      if (!confirm(`${date} 공휴일을 삭제할까요?`)) return;
      try {
        const res = await _authFetch(API_BASE + `/api/holidays/${date}`, { method: 'DELETE' });
        if (res.ok) { this.showToast('success', '삭제'); await this.loadSettingsData(); }
      } catch (e) { this.showToast('error', '실패: ' + e.message); }
    },

    // ── 통합 시스템 설정 저장 (스케줄/백업갯수/API키/에러채널) ──
    async saveSystemSettings(payload) {
      try {
        const res = await _authFetch(API_BASE + '/api/system/settings', {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload),
        });
        if (res.ok) { this.showToast('success', '설정 저장됨'); await this.loadSettingsData(); }
        else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '저장 실패');
        }
      } catch (e) { this.showToast('error', '저장 실패: ' + e.message); }
    },

    // ── 데이터 내보내기 ──
    exportDownload(kind) {
      const token = _getAuthToken();
      const url = API_BASE + `/api/export/${kind}`;
      // fetch + blob 방식 (Authorization header 포함)
      fetch(url, { headers: { 'Authorization': 'Bearer ' + token } })
        .then(r => {
          if (!r.ok) throw new Error(r.status);
          return r.blob().then(b => ({ blob: b, name: (r.headers.get('Content-Disposition') || '').match(/filename="([^"]+)"/)?.[1] || `${kind}.csv` }));
        })
        .then(({ blob, name }) => {
          const a = document.createElement('a');
          a.href = URL.createObjectURL(blob);
          a.download = name;
          a.click();
          this.showToast('success', `${name} 다운로드`);
        })
        .catch(e => this.showToast('error', '다운로드 실패: ' + e.message));
    },

    async sendSlackReminder() {
      if (!confirm('전체 활성 멤버에게 주간 보고 리마인더 DM을 보낼까요?')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/slack/remind-report', {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({}),
        });
        const d = await res.json().catch(() => ({}));
        if (res.ok) this.showToast('success', `${d.sent}/${d.total} 발송 완료`);
        else this.showToast('error', d.error || '발송 실패');
      } catch (e) { this.showToast('error', '발송 실패: ' + e.message); }
    },
    get webhookUrl() {
      return `${location.origin}/api/webhooks/linear`;
    },

    // ── F. 내 홈 — 멤버/PM 전용 (내 이슈/내 결재) ──
    myIssues: [],
    myPendingApprovals: [],
    _myHomeRequestSeq: 0,
    async loadMyHome() {
      if (this.isLegacy() || !this.currentUser) return;
      const seq = ++this._myHomeRequestSeq;
      const token = _getAuthToken();
      const meName = this.currentUser.name;
      const isCurrent = () => seq === this._myHomeRequestSeq && token === _getAuthToken() && meName === this.currentUser.name;
      // 외부 Linear 조회가 늦어도 내 결재는 도착한 즉시 표시한다.
      const issuesTask = _authFetch(API_BASE + '/api/linear/my-issues')
        .then(r => r.ok ? r.json() : { issues: [] }).catch(() => ({ issues: [] }))
        .then(mi => {
          if (!isCurrent()) return;
          const all = mi.issues || mi.byAssignee || [];
          if (Array.isArray(all)) {
            this.myIssues = all.filter(x => (x.assignee || '').includes(meName) || x.assignee === meName).slice(0, 8);
          } else if (typeof all === 'object') {
            const arr = all[meName] || [];
            this.myIssues = arr.slice(0, 8);
          }
        });
      const pendingTask = Promise.all([
        readApprovals().catch(() => null),
        _authFetch(API_BASE + '/api/leave/requests').then(r => r.ok ? r.json() : { rows: [] }).catch(() => ({ rows: [] })),
      ]).then(([result, lv]) => {
        if (!isCurrent() || (result && !isCurrentApprovalRead(result))) return;
        const ap = (result && result.data) || { items: [] };
        const approvals = (ap.items || []).filter(i => {
          if (i.status !== '대기') return false;
          return i.requester === meName || (this.isMyInbox ? this.isMyInbox(i) : false);
        }).map(i => ({ ...i, _kind: 'approval', _raw: i, _label: this.approvalTypeLabel(i.req_type) }));
        const leaves = (lv.rows || lv.items || []).filter(r => {
          if (r.status !== '대기') return false;
          return r.user_name === meName || this.isMyLeaveTurnStrict(r);
        }).map(r => ({
          id: r.id, _kind: 'leave', _raw: r, req_type: 'leave', _label: r.leave_type,
          requester: r.user_name, status: r.status,
          title: `${r.user_name} · ${r.start_date}${(r.end_date && r.end_date !== r.start_date) ? '~' + r.end_date : ''} (${r.days}일)`,
        }));
        const merged = [...approvals, ...leaves];
        merged.sort((a, b) => (this.isMyApprovalInbox(b) ? 1 : 0) - (this.isMyApprovalInbox(a) ? 1 : 0));
        this.myPendingApprovals = merged.slice(0, 12);
      });
      await Promise.all([issuesTask, pendingTask]).catch(() => {});
    },

    // ── 비용 관리 탭 ──
    expenseRows: [],
    expenseTotal: 0,
    expenseCount: 0,
    expenseLoading: false,
    expenseLoaded: false,
    expenseSaving: false,
    expenseFilterYear: String(new Date().getFullYear()),
    expenseFilterMonth: String(new Date().getMonth() + 1),
    expenseViewMode: 'month',  // 'month', 'year', 'all'
    expenseForm: { show: false, date: '', manager: '', company: '', description: '', bank: '', account: '', amount: '', depositor: '', note: '' },

    async loadExpenses() {
      this.expenseLoading = true;
      try {
        const params = new URLSearchParams();
        if (this.expenseViewMode !== 'all') params.set('year', this.expenseFilterYear);
        if (this.expenseViewMode === 'month') params.set('month', this.expenseFilterMonth);
        const res = await _authFetch(API_BASE + '/api/expenses?' + params.toString());
        if (res.ok) {
          const d = await res.json();
          this.expenseRows = d.rows || [];
          this.expenseTotal = d.total || 0;
          this.expenseCount = d.count || 0;
          this.expenseLoaded = true;
        } else if (!this.isLegacy()) {
          this.showToast('warning', '비용 데이터를 불러오지 못했습니다 — 잠시 후 새로고침 해주세요');
        }
      } catch { if (!this.isLegacy()) this.showToast('error', '비용 데이터 불러오기 실패 — 네트워크를 확인하세요'); }
      this.expenseLoading = false;
    },

    setExpenseView(mode) {
      if (mode === 'all') {
        // 전체 모드: 최근 200건만 표시됨을 알림
        if (!confirm('전체 조회는 최근 200건만 표시됩니다.\n연도별 조회를 권장합니다. 계속하시겠습니까?')) return;
      }
      this.expenseViewMode = mode;
      this.loadExpenses();
    },

    async addExpense() {
      const f = this.expenseForm;
      if (!f.description || !f.amount) { this.showToast('info', '내용과 금액은 필수입니다.'); return; }
      this.expenseSaving = true;
      try {
        const now = new Date();
        const res = await _authFetch(API_BASE + '/api/expenses', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            year: String(now.getFullYear()),
            date: f.date || ((now.getMonth() + 1) + '/' + now.getDate()),
            manager: f.manager, company: f.company, description: f.description,
            bank: f.bank, account: f.account, amount: f.amount,
            depositor: f.depositor, status: '',
            manager2: '', note: f.note,
          }),
        });
        if (res.ok) {
          this.expenseForm = { show: false, date: '', manager: '', company: '', description: '', bank: '', account: '', amount: '', depositor: '', note: '' };
          this.showToast('success', '비용이 추가되었습니다. 구글 시트에도 반영됩니다.');
          await this.loadExpenses();
        } else {
          const err = await res.json().catch(() => ({}));
          this.showToast('error', '추가 실패: ' + (err.error || '알 수 없는 오류'));
        }
      } catch (e) { this.showToast('error', '추가 실패: ' + e.message); }
      this.expenseSaving = false;
    },

    async updateExpenseField(row, field, value) {
      try {
        const res = await fetch(location.origin + '/api/expenses/' + row.rowNum, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (localStorage.getItem('_dashboard_auth_token') || '') },
          body: JSON.stringify({ [field]: value }),
        });
        if (res.ok) { row[field] = value; }
        else { this.showToast('error', '수정 실패'); }
      } catch (e) { this.showToast('error', '수정 오류: ' + e.message); }
    },

    async updateExpenseStatus(row, newStatus) {
      await this.updateExpenseField(row, 'status', newStatus);
    },

    async deleteExpense(row) {
      if (!confirm('이 비용을 삭제하시겠습니까?\n' + (row.description || ''))) return;
      try {
        const res = await _authFetch(API_BASE + '/api/expenses/' + row.rowNum, { method: 'DELETE' });
        if (res.ok) { await this.loadExpenses(); }
      } catch { /* 무시 */ }
    },

    formatKRW(n) {
      return '₩' + (n || 0).toLocaleString('ko-KR');
    },

    // ── 근태/연차 탭 ──
    hrTab: 'attendance',  // 'attendance', 'leave', 'calendar'
    hrUser: '',  // 현재 사용자
    hrAttendance: [],
    hrAttendanceDate: new Date().toISOString().slice(0, 10),
    hrAttendanceLoading: false,
    hrMonthly: [],
    hrMonthlyRecords: [],
    hrMonthlyHalfdayWarnings: [],
    hrMonthlyCoverageWarnings: [],
    hrMonthlyLoading: false,
    hrMonthlyError: '',
    _hrMonthlyRequestSeq: 0,
    hrMonthlyYear: String(new Date().getFullYear()),
    hrMonthlyMonth: String(new Date().getMonth() + 1),
    hrMonthlyDetailPage: 1,
    hrMonthlyDetailPageScope: '',
    get hrMonthlyIsAnnual() { return this.hrMonthlyMonth === 'all'; },
    get hrMonthlySummaryTitle() { return this.hrMonthlyIsAnnual ? '연간 요약' : '월간 요약'; },
    get hrMonthlyDetailTitle() { return this.hrMonthlyIsAnnual ? '연간 출퇴근 상세' : '월간 출퇴근 상세'; },
    get hrMonthlyVisible() {
      const rows = this.visibleProfiles(this.hrMonthly);
      const user = (this.hrUser || '').trim();
      return user ? rows.filter(row => row.user_name === user) : rows;
    },
    get hrMonthlyRecordsVisible() {
      const rows = this.visibleProfiles(this.hrMonthlyRecords);
      const user = (this.hrUser || '').trim();
      return user ? rows.filter(row => row.user_name === user) : rows;
    },
    get hrMonthlyDetailScope() {
      return JSON.stringify([this.hrMonthlyYear, this.hrMonthlyMonth, (this.hrUser || '').trim()]);
    },
    get hrMonthlyDetailPageCount() {
      return this.hrMonthlyIsAnnual ? Math.max(1, Math.ceil(this.hrMonthlyRecordsVisible.length / 100)) : 1;
    },
    get hrMonthlyDetailCurrentPage() {
      if (this.hrMonthlyDetailPageScope !== this.hrMonthlyDetailScope) return 1;
      return Math.min(this.hrMonthlyDetailPageCount, Math.max(1, this.hrMonthlyDetailPage));
    },
    get hrMonthlyRecordsPage() {
      const rows = this.hrMonthlyRecordsVisible;
      if (!this.hrMonthlyIsAnnual) return rows;
      const start = (this.hrMonthlyDetailCurrentPage - 1) * 100;
      return rows.slice(start, start + 100);
    },
    get hrMonthlyDetailRange() {
      const total = this.hrMonthlyRecordsVisible.length;
      if (!total) return '0 / 0건';
      const start = (this.hrMonthlyDetailCurrentPage - 1) * 100;
      return (start + 1) + '–' + Math.min(start + 100, total) + ' / ' + total + '건';
    },
    setHrMonthlyDetailPage(page) {
      this.hrMonthlyDetailPageScope = this.hrMonthlyDetailScope;
      this.hrMonthlyDetailPage = Math.min(this.hrMonthlyDetailPageCount, Math.max(1, page));
    },
    resetHrMonthlyDetailPage() { this.setHrMonthlyDetailPage(1); },
    get hrMonthlyHalfdayWarningsVisible() {
      const rows = this.visibleProfiles(this.hrMonthlyHalfdayWarnings);
      const user = (this.hrUser || '').trim();
      return user ? rows.filter(row => row.user_name === user) : rows;
    },
    get hrMonthlyCoverageWarningsVisible() {
      const user = (this.hrUser || '').trim();
      return this.hrMonthlyCoverageWarnings.filter(row => !row.user_name ||
        (this.isVisibleProfile(row) && (!user || row.user_name === user)));
    },
    hasHrMonthlyCoverageWarning(codes) {
      return this.hrMonthlyCoverageWarningsVisible.some(row => codes.includes(row.code));
    },
    attendanceTime(value) { return value ? value.slice(11, 16) || '—' : '—'; },
    hasApprovedHalfDay(row) { return Array.isArray(row.approved_half_days) && row.approved_half_days.length > 0; },
    canEditAttendance(row) { return !!row && row.id != null && (this.isAdmin() || this.isPm()); },
    async refreshAttendanceViews() {
      const requests = [this.loadAttendance(), this.loadMonthly(), this.loadMyTodayAttendance()];
      if (this.isAdmin()) requests.push(this.loadExecutiveSummary());
      await Promise.all(requests);
    },
    hrLeaveBalance: [],
    // 연차 미적용 대상은 설치자가 지정한다. 승인 이력에는 적용하지 않는다.
    get _LEAVE_HIDDEN() { return this.teamConfig.excludedLeaveBalanceNames || []; },
    get hrLeaveBalanceVisible() {
      return (this.hrLeaveBalance || []).filter(b => !this._LEAVE_HIDDEN.includes(b.user_name) && this.isVisibleProfile(b));
    },
    hrLeaveRequests: [],
    hrLeaveLoading: false,
    hrLeaveForm: { show: false, leave_type: '연차', start_date: '', end_date: '', days: 1, reason: '', approver: '' },
    hrApprovers: [],
    hrLoaded: false,

    async loadAttendance() {
      this.hrAttendanceLoading = true;
      try {
        const res = await _authFetch(API_BASE + '/api/attendance?date=' + this.hrAttendanceDate);
        if (res.ok) this.hrAttendance = (await res.json()).rows || [];
      } catch {}
      this.hrAttendanceLoading = false;
    },

    // ── 직원(member/PM) 전용 상단 카드: 본인 오늘 출퇴근 + 오늘 회의 ──
    myTodayAttendance: null,
    todayMeetings: [],
    async loadMyTodayAttendance() {
      const name = (this.currentUser && this.currentUser.name) || '';
      if (!name) return;
      const today = new Date().toISOString().slice(0, 10);
      try {
        const res = await _authFetch(API_BASE + '/api/attendance?date=' + today);
        if (res.ok) {
          const rows = (await res.json()).rows || [];
          this.myTodayAttendance = rows.find(r => r.user_name === name) || null;
        }
      } catch {}
    },
    async loadTodayMeetings() {
      const today = new Date().toISOString().slice(0, 10);
      try {
        const res = await _authFetch(API_BASE + '/api/calendar/events?start=' + today + '&end=' + today);
        if (!res.ok) return;
        let evs = (await res.json()).events || [];
        // 오늘에 걸치는 이벤트만 (Google 종일 end-exclusive 보정)
        evs = evs.filter(e => {
          const s = (e.startDate || '').slice(0, 10);
          let en = (e.endDate || e.startDate || '').slice(0, 10);
          if (e.allDay !== false && en > s) {
            const d = new Date(en + 'T00:00:00'); d.setDate(d.getDate() - 1);
            en = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
          }
          return today >= s && today <= en;
        });
        // 직원(member/pm)은 본인 일정만 — title 또는 attendees에 본인 이름 포함
        const me = (this.currentUser && this.currentUser.name) || '';
        if (me && (this.isMember() || this.isPm()) && !this.isAdmin() && !this.isLegacy()) {
          evs = evs.filter(e => {
            if ((e.title || '').includes(me)) return true;
            const atts = e.attendees || [];
            return Array.isArray(atts) && atts.some(a => (typeof a === 'string' ? a : (a && a.name) || '').includes(me));
          });
        }
        this.todayMeetings = evs.sort((a, b) => (a.startTime || '00:00').localeCompare(b.startTime || '00:00'));
      } catch {}
    },

    async clockIn() {
      const name = (this.currentUser && this.currentUser.name) || '';
      if (!name) { this.showToast('warning', '개인 로그인이 필요합니다.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/clock-in', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: '{}',
        });
        const d = await res.json();
        if (res.ok) { this.showToast('error', name + ' 출근 완료! (' + d.time + ' ' + d.status + ')'); await this.refreshAttendanceViews(); }
        else this.showToast('error', d.error || '출근 실패');
      } catch (e) { this.showToast('error', '출근 실패: ' + e.message); }
    },

    async clockOut() {
      const name = (this.currentUser && this.currentUser.name) || '';
      if (!name) { this.showToast('warning', '개인 로그인이 필요합니다.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/clock-out', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: '{}',
        });
        const d = await res.json();
        if (res.ok) { this.showToast('success', name + ' 퇴근 완료! (' + d.time + ')'); await this.refreshAttendanceViews(); }
        else this.showToast('error', d.error || '퇴근 실패');
      } catch (e) { this.showToast('error', '퇴근 실패: ' + e.message); }
    },

    async editAttendance(row, field) {
      if (!this.canEditAttendance(row)) return;
      const current = field === 'clock_in' ? (row.clock_in || '').slice(11,16) : (row.clock_out || '').slice(11,16);
      const newTime = prompt(field === 'clock_in' ? '출근 시간 수정 (HH:MM)' : '퇴근 시간 수정 (HH:MM)', current);
      if (!newTime || newTime === current) return;
      const dateStr = row.work_date;
      const newDateTime = dateStr + 'T' + newTime + ':00';
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/' + row.id, {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ [field]: newDateTime }),
        });
        if (res.ok) await this.refreshAttendanceViews();
      } catch {}
    },

    async editAttendanceStatus(row, newStatus) {
      if (!this.canEditAttendance(row) || this.hasApprovedHalfDay(row)) return;
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/' + row.id, {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ status: newStatus }),
        });
        if (res.ok) await this.refreshAttendanceViews();
      } catch {}
    },

    async deleteAttendance(row) {
      if (!this.canEditAttendance(row)) return;
      if (!confirm(row.user_name + '의 ' + row.work_date + ' 출퇴근 기록을 삭제하시겠습니까?')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/' + row.id, { method: 'DELETE' });
        if (res.ok) await this.refreshAttendanceViews();
      } catch {}
    },

    async loadMonthly() {
      const seq = ++this._hrMonthlyRequestSeq;
      const year = this.hrMonthlyYear;
      const month = this.hrMonthlyMonth;
      if (this.hrMonthlyDetailPageScope !== this.hrMonthlyDetailScope) this.resetHrMonthlyDetailPage();
      const isCurrent = () => seq === this._hrMonthlyRequestSeq && year === this.hrMonthlyYear && month === this.hrMonthlyMonth;
      this.hrMonthlyLoading = true;
      this.hrMonthlyError = '';
      try {
        const res = await _authFetch(API_BASE + '/api/attendance/monthly?year=' + year + '&month=' + month);
        if (!res.ok) throw new Error('월간 조회 실패');
        const data = await res.json();
        if (!isCurrent()) return;
        // 응답 원본을 보존하고 사용자·프로필 필터는 표시 getter에서만 적용한다.
        this.hrMonthly = data.rows || [];
        this.hrMonthlyRecords = data.records || [];
        this.hrMonthlyHalfdayWarnings = data.halfday_warnings || [];
        this.hrMonthlyCoverageWarnings = data.coverage_warnings || [];
        this.setHrMonthlyDetailPage(this.hrMonthlyDetailCurrentPage);
      } catch {
        if (isCurrent()) {
          this.hrMonthlyError = '월간 근태를 불러오지 못했습니다. 잠시 후 다시 시도해주세요.';
          this.showToast('warning', this.hrMonthlyError);
        }
      } finally {
        if (seq === this._hrMonthlyRequestSeq) this.hrMonthlyLoading = false;
      }
    },

    async loadLeave() {
      this.hrLeaveLoading = true;
      try {
        const [bRes, rRes, aRes] = await Promise.all([
          _authFetch(API_BASE + '/api/leave/balance'),
          _authFetch(API_BASE + '/api/leave/requests'),
          _authFetch(API_BASE + '/api/users/approvers'),
          this.loadApprovalSettings(),  // 연차 결재선(1차 PM·2차 대표) 표시용
        ]);
        let _failed = false;
        if (bRes.ok) this.hrLeaveBalance = (await bRes.json()).rows || []; else _failed = true;
        if (rRes.ok) this.hrLeaveRequests = (await rRes.json()).rows || []; else _failed = true;
        if (aRes.ok) this.hrApprovers = (await aRes.json()).approvers || [];
        // 조용한 실패 방지 — 불러오기 실패 시 사용자에게 안내 (공용 세션 제외)
        if (_failed && !this.isLegacy()) this.showToast('warning', '연차 데이터를 불러오지 못했습니다 — 잠시 후 새로고침 해주세요');
      } catch { if (!this.isLegacy()) this.showToast('error', '연차 데이터 불러오기 실패 — 네트워크를 확인하세요'); }
      this.hrLeaveLoading = false;
    },

    // 반차 = 오전/오후반차 + 무급 오전/오후반차(잔여 차감 없음) — 0.5일 고정, 종료일 = 시작일
    _isLeaveHalfDay(t) { return ['오전반차', '오후반차', '무급오전반차', '무급오후반차'].includes(t); },
    onLeaveTypeChange() {
      const f = this.hrLeaveForm;
      if (this._isLeaveHalfDay(f.leave_type)) {
        if (f.start_date) f.end_date = f.start_date;
        f.days = 0.5;
      } else {
        this.recalcLeaveDays();
      }
    },
    onLeaveStartChange() {
      const f = this.hrLeaveForm;
      if (!f.start_date) return;
      if (this._isLeaveHalfDay(f.leave_type)) {
        f.end_date = f.start_date;
        f.days = 0.5;
        return;
      }
      if (!f.end_date || f.end_date < f.start_date) f.end_date = f.start_date;
      this.recalcLeaveDays();
    },
    onLeaveEndChange() {
      const f = this.hrLeaveForm;
      if (!f.end_date) return;
      if (this._isLeaveHalfDay(f.leave_type)) {
        f.start_date = f.end_date;
        f.days = 0.5;
        return;
      }
      if (!f.start_date || f.start_date > f.end_date) f.start_date = f.end_date;
      this.recalcLeaveDays();
    },
    recalcLeaveDays() {
      const f = this.hrLeaveForm;
      if (this._isLeaveHalfDay(f.leave_type)) { f.days = 0.5; return; }
      // 풀 종류 — 날짜 비어있어도 일단 기본값 1일 (날짜 둘 다 들어오면 정확히 계산)
      if (!f.start_date || !f.end_date) { f.days = 1; return; }
      const s = new Date(f.start_date + 'T00:00:00');
      const e = new Date(f.end_date + 'T00:00:00');
      const diff = Math.floor((e - s) / 86400000) + 1;
      f.days = diff > 0 ? diff : 1;
    },

    async submitLeave() {
      const f = this.hrLeaveForm;
      if (!this.currentUser || !this.currentUser.name) { this.showToast('warning', '개인 로그인이 필요합니다.'); return; }
      if (!f.start_date || !f.end_date) { this.showToast('info', '시작일, 종료일은 필수입니다.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/leave/requests', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({
            leave_type: f.leave_type, start_date: f.start_date, end_date: f.end_date,
            days: f.days, reason: f.reason,
          }),
        });
        if (res.ok) {
          this.hrLeaveForm = { show: false, leave_type: '연차', start_date: '', end_date: '', days: 1, reason: '', approver: '' };
          this.showToast('success', '연차 신청 완료');
          await Promise.all([this.loadLeave(), this.refreshAttendanceViews()]);
        } else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '신청 실패'); }
      } catch (e) { this.showToast('error', '신청 실패: ' + e.message); }
    },

    async approveLeave(id, status) {
      if (!this.isAdmin() && !this.isPm()) { this.showToast('info', '관리자/PM만 처리 가능합니다.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/leave/requests/' + id, {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ status }),
        });
        if (res.ok) await Promise.all([this.loadLeave(), this.refreshAttendanceViews()]);
        else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '처리 실패'); }
      } catch {}
    },

    // ── 연차 2단계 결재 (1차 PM → 2차 대표) ──
    leaveStageOf(req) {
      if (!req) return 'done';
      const ps = req.pm_status || '대기';
      const as = req.admin_status || '대기';
      if (ps === '반려' || as === '반려') return 'rejected';
      if (ps === '대기') return 'pm';
      if (ps === '승인' && as === '대기') return 'admin';
      return 'done';
    },
    isMyLeaveInbox(req) {
      const me = this.currentUser && this.currentUser.name;
      if (!me || !req) return false;
      const stage = this.leaveStageOf(req);
      if (stage === 'pm') return req.pm_approver === me || this.isAdmin();  // 대표는 PM 단계 대리 승인 가능
      if (stage === 'admin') return req.admin_approver === me;
      return false;
    },
    // 내 홈 "내 결재"용 — 대리 제외, 본인이 실제 그 단계 결재자일 때만
    isMyLeaveTurnStrict(req) {
      const me = this.currentUser && this.currentUser.name;
      if (!me || !req) return false;
      const stage = this.leaveStageOf(req);
      if (stage === 'pm') return req.pm_approver === me;
      if (stage === 'admin') return req.admin_approver === me;
      return false;
    },
    leaveApproverPair() {
      const p = (this.approvalTypeApprovers && this.approvalTypeApprovers.leave) || {};
      const defaults = this.teamConfig.approvalDefaults || {};
      return { pm: p.pm || defaults.primary || '', admin: p.admin || defaults.reference || '' };
    },

    async cancelLeave(id) {
      if (!confirm('이 연차를 삭제할까요?\n(잔여일수가 복구되고 연동된 캘린더 일정도 삭제됩니다)')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/leave/requests/' + id, { method: 'DELETE' });
        if (res.ok) await Promise.all([this.loadLeave(), this.refreshAttendanceViews()]);
        else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '취소 실패'); }
      } catch {}
    },

    async editLeaveBalance(b) {
      const cur = b.total_days;
      const input = prompt(`${b.user_name} 연차 총일수 (현재 ${cur}일, 사용 ${b.used_days}일)`, String(cur));
      if (input === null) return;
      const total = parseFloat(input);
      if (!isFinite(total) || total < 0) { this.showToast('warning', '0 이상의 숫자를 입력하세요.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/leave/balance', {
          method: 'PUT', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ user: b.user_name, year: b.year, total_days: total }),
        });
        if (res.ok) await this.loadLeave();
        else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '수정 실패'); }
      } catch (e) { this.showToast('error', '수정 실패: ' + e.message); }
    },

    // ── 캘린더 (월간 그리드) ──
    calEvents: [],
    calLoading: false,
    calMonth: (() => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0'); })(),
    calEventForm: { show: false, title: '', start_date: '', end_date: '', all_day: true, start_time: '09:00', end_time: '18:00', description: '' },

    get calMonthLabel() {
      const [y, m] = this.calMonth.split('-').map(Number);
      return y + '년 ' + m + '월';
    },

    calShiftMonth(delta) {
      const [y, m] = this.calMonth.split('-').map(Number);
      const d = new Date(y, m - 1 + delta, 1);
      this.calMonth = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0');
      this.loadCalendar();
    },

    calGoToday() {
      const d = new Date();
      this.calMonth = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0');
      this.loadCalendar();
    },

    // 월간 셀 그리드 (앞뒤 패딩 포함 정확히 42개, 일요일 시작)
    get calGrid() {
      const [y, m] = this.calMonth.split('-').map(Number);
      const first = new Date(y, m - 1, 1);
      // 일요일 시작 (JS getDay: 0=일, 1=월 ... 6=토 → 그대로 사용)
      const padBefore = first.getDay();
      const gridStart = new Date(y, m - 1, 1 - padBefore);
      const todayStr = new Date().toISOString().slice(0,10);
      const cells = [];
      for (let i = 0; i < 42; i++) {
        const d = new Date(gridStart);
        d.setDate(gridStart.getDate() + i);
        const dateStr = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
        cells.push({
          date: dateStr,
          day: d.getDate(),
          inMonth: d.getMonth() === m - 1,
          isToday: dateStr === todayStr,
          isSunday: d.getDay() === 0,
          isSaturday: d.getDay() === 6,
          events: this._eventsForDate(dateStr),
        });
      }
      return cells;
    },

    _eventsForDate(dateStr) {
      const out = [];
      const filterUser = (this.hrUser || '').trim();
      // hr 캘린더는 연차류 이벤트만 표시 — 두 가지 포맷 지원:
      //   1) "[연차] 사용자명"            (현재 _create_leave_event 포맷)
      //   2) "사용자명 연차 1일"          (구버전/수동 등록 포맷)
      const RE_BRACKET = /^\[(연차|무급오전반차|무급오후반차|오전반차|오후반차|반차|공가|경조|무급|결근)\]\s*(.*?)\s*$/;
      const RE_FREE = /^(\S+?)\s+(무급오전반차|무급오후반차|오전반차|오후반차|반차|연차|공가|경조|무급|결근)(?:\s+[\d.]+\s*일)?\s*$/;
      for (const ev of this.calEvents) {
        const start = (ev.startDate || '').slice(0,10);
        let end = (ev.endDate || ev.startDate || '').slice(0,10);
        const allDay = ev.allDay !== false;
        // 구글 종일(all-day) 이벤트는 종료일이 exclusive(다음날) → inclusive로 보정
        if (allDay && end > start) {
          const d = new Date(end + 'T00:00:00');
          d.setDate(d.getDate() - 1);
          end = d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
        }
        if (end < start) end = start;
        if (dateStr < start || dateStr > end) continue;
        const title = ev.title || '';
        // 연차류만 통과 — 회의/일반 일정은 hr 캘린더에서 숨김
        let leaveType = '', leaveUser = '';
        const m1 = title.match(RE_BRACKET);
        if (m1) {
          leaveType = m1[1]; leaveUser = m1[2];
        } else {
          const m2 = title.match(RE_FREE);
          if (m2) { leaveUser = m2[1]; leaveType = m2[2]; }
          else continue;
        }
        // 사용자 선택 시 그 사용자 연차만 표시
        if (filterUser && leaveUser !== filterUser) continue;
        out.push({
          id: ev.id,
          title,
          isLeave: true,
          isHalf: /반차/.test(leaveType),
          isStart: dateStr === start,
          isMultiDay: start !== end,
          endDate: end,
          allDay,
          startTime: ev.startTime || '',
          description: ev.description || '',
        });
      }
      // 정렬: 종일 휴가(연차/공가/경조/무급/결근) → 반차
      out.sort((a, b) => (a.isHalf ? 1 : 0) - (b.isHalf ? 1 : 0));
      // 공휴일 자동 prepend — holidaysMap 직접 참조 (Alpine reactivity 의존성 추적 보장)
      const hn = (this.holidaysMap && this.holidaysMap[dateStr]) || '';
      if (hn) {
        out.unshift({
          id: 'holiday-' + dateStr,
          title: '🇰🇷 ' + hn,
          isHoliday: true, isLeave: false, isHalf: false,
          isStart: true, isMultiDay: false, endDate: dateStr,
          allDay: true, startTime: '', description: '',
        });
      }
      return out;
    },

    async loadCalendar() {
      this.calLoading = true;
      try {
        const [y, m] = this.calMonth.split('-').map(Number);
        // 그리드 범위 = 월 1일 7일 전 ~ 말일 7일 후 (그리드 셀에 안 빠지게)
        const start = new Date(y, m - 1, -6);
        const end = new Date(y, m, 7);
        const fmt = d => d.getFullYear() + '-' + String(d.getMonth()+1).padStart(2,'0') + '-' + String(d.getDate()).padStart(2,'0');
        const res = await _authFetch(API_BASE + '/api/calendar/events?start=' + fmt(start) + '&end=' + fmt(end));
        if (res.ok) this.calEvents = (await res.json()).events || [];
      } catch {}
      this.calLoading = false;
    },

    calOpenEventForm(dateStr) {
      this.calEventForm = { show: true, title: '', start_date: dateStr, end_date: dateStr, all_day: true, start_time: '09:00', end_time: '18:00', description: '' };
    },

    async addCalEvent() {
      const f = this.calEventForm;
      if (!f.title || !f.start_date) { this.showToast('info', '제목과 시작일은 필수입니다.'); return; }
      try {
        const res = await _authFetch(API_BASE + '/api/calendar/events', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({ ...f, end_date: f.end_date || f.start_date }),
        });
        if (res.ok) {
          this.calEventForm = { show: false, title: '', start_date: '', end_date: '', all_day: true, start_time: '09:00', end_time: '18:00', description: '' };
          await this.loadCalendar();
        } else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '추가 실패'); }
      } catch (e) { this.showToast('error', '추가 실패: ' + e.message); }
    },

    async deleteCalEvent(id) {
      if (!confirm('이 일정을 삭제하시겠습니까?')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/calendar/events/' + id, { method: 'DELETE' });
        if (res.ok) await this.loadCalendar();
      } catch {}
    },

    async loadHrData() {
      await Promise.all([this.loadAttendance(), this.loadMonthly(), this.loadLeave(), this.loadCalendar()]);
      this.hrLoaded = true;
    },

    // ── 회의록 탭 ──
    notesList: [],
    notesLoading: false,
    notesLoaded: false,
    notesFilterType: '',
    notesFilterWeek: '',
    notesSelectedId: null,
    notesForm: {
      show: false, id: '', title: '', meeting_type: '주간회의', week_label: '',
      content: '', author: '', attachments: [],
    },
    notesUploading: false,
    notesCalEvents: [],        // 회의록 탭 전용 캘린더 일정
    notesCalLoading: false,
    notesRecorderOpen: false,  // 녹음 iframe 토글

    // ── 공유 메모 ──
    sharedMemos: [],
    sharedMemosLoaded: false,
    memoForm: { show: false, id: null, title: '', content: '' },
    memoHistoryModal: { show: false, memoId: null, history: [] },

    async loadSharedMemos() {
      try {
        const res = await _authFetch(location.origin + '/api/memos');
        if (res.ok) { this.sharedMemos = (await res.json()).memos || []; this.sharedMemosLoaded = true; }
      } catch (e) { console.error('메모 로드 실패:', e); }
    },
    openMemoForm(memo) {
      if (memo) {
        this.memoForm = { show: true, id: memo.id, title: memo.title, content: memo.content };
      } else {
        this.memoForm = { show: true, id: null, title: '', content: '' };
      }
    },
    async saveMemo() {
      try {
        const f = this.memoForm;
        if (!f.title && !f.content) { this.showToast('warning', '제목 또는 내용을 입력하세요'); return; }
        const author = (this.currentUser && this.currentUser.name) ? this.currentUser.name : '관리자';
        const body = { title: f.title || '제목 없음', content: f.content || '', author: author, edited_by: author };
        const base = location.origin;
        const url = f.id ? (base + '/api/memos/' + f.id) : (base + '/api/memos');
        const method = f.id ? 'PUT' : 'POST';
        const authToken = localStorage.getItem('_dashboard_auth_token') || '';
        const res = await fetch(url, { method: method, headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + authToken }, body: JSON.stringify(body) });
        if (res.ok) { this.memoForm.show = false; await this.loadSharedMemos(); }
        else { const err = await res.text(); this.showToast('error', '메모 저장 실패: ' + err); }
      } catch (e) { this.showToast('error', '메모 저장 오류: ' + (e.message || e)); }
    },
    async deleteMemo(id) {
      if (!confirm('이 메모를 삭제하시겠습니까?')) return;
      try {
        const res = await fetch(location.origin + '/api/memos/' + id, {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('_dashboard_auth_token') || '') },
        });
        if (res.ok) { await this.loadSharedMemos(); }
        else { this.showToast('error', '삭제 실패: ' + res.status); }
      } catch (e) { this.showToast('error', '삭제 오류: ' + (e.message || e)); }
    },
    async toggleMemoPin(memo) {
      try {
        await _authFetch(location.origin + '/api/memos/' + memo.id, {
          method: 'PUT', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ pinned: !memo.pinned, edited_by: (this.currentUser && this.currentUser.name) || '관리자' }),
        });
        await this.loadSharedMemos();
      } catch {}
    },
    async showMemoHistory(memoId) {
      try {
        const res = await _authFetch(location.origin + '/api/memos/' + memoId + '/history');
        if (res.ok) {
          const d = await res.json();
          this.memoHistoryModal = { show: true, memoId, history: d.history || [] };
        }
      } catch {}
    },
    calEventModal: { show: false, title: '', date: new Date().toISOString().slice(0,10), start_time: '', end_time: '', all_day: true, description: '', attendees: [] },
    calEventSaving: false,
    openCalEventModal() {
      this.calEventModal = {
        show: true, title: '', date: new Date().toISOString().slice(0,10),
        start_time: '10:00', end_time: '11:00', all_day: false, description: '', attendees: [],
      };
    },
    toggleCalAttendee(name) {
      const idx = this.calEventModal.attendees.indexOf(name);
      if (idx >= 0) { this.calEventModal.attendees.splice(idx, 1); }
      else { this.calEventModal.attendees.push(name); }
    },
    async submitCalEvent() {
      const m = this.calEventModal;
      if (!m.title) { this.showToast('warning', '일정 제목을 입력하세요'); return; }
      if (!m.date) { this.showToast('warning', '날짜를 선택하세요'); return; }
      this.calEventSaving = true;
      try {
        const res = await fetch(location.origin + '/api/calendar/events', {
          method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + (localStorage.getItem('_dashboard_auth_token') || '') },
          body: JSON.stringify({
            title: m.title, start_date: m.date, end_date: m.date,
            all_day: m.all_day,
            start_time: m.all_day ? '09:00' : m.start_time,
            end_time: m.all_day ? '18:00' : m.end_time,
            description: (m.attendees.length > 0 ? '참석자: ' + m.attendees.join(', ') + '\n' : '') + (m.description || ''),
            attendees: m.attendees,
          }),
        });
        if (res.ok) {
          this.calEventModal.show = false;
          await this.loadNotesCalendar();
        } else {
          const d = await res.json().catch(() => ({}));
          this.showToast('error', d.error || '일정 추가 실패');
        }
      } catch (e) { this.showToast('error', '일정 추가 실패: ' + e.message); }
      finally { this.calEventSaving = false; }
    },
    async deleteCalEvent(id) {
      if (!confirm('이 일정을 삭제하시겠습니까?')) return;
      try {
        const res = await fetch(location.origin + '/api/calendar/events/' + id, {
          method: 'DELETE',
          headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('_dashboard_auth_token') || '') },
        });
        if (res.ok) await this.loadNotesCalendar();
        else this.showToast('error', '삭제 실패: ' + res.status);
      } catch (e) { this.showToast('error', '삭제 오류: ' + e.message); }
    },

    async loadNotesCalendar() {
      this.notesCalLoading = true;
      try {
        // 이번 달 1일부터 3개월 후까지 (월간 캘린더 과거 일정도 포함)
        const now = new Date();
        const start = new Date(now.getFullYear(), now.getMonth(), 1).toISOString().slice(0, 10);
        const end = new Date(now.getFullYear(), now.getMonth() + 3, 0).toISOString().slice(0, 10);
        const res = await _authFetch(location.origin + '/api/calendar/events?start=' + start + '&end=' + end);
        if (res.ok) {
          this.notesCalEvents = (await res.json()).events || [];
        }
      } catch {} finally { this.notesCalLoading = false; }
    },

    async loadNotes() {
      this.notesLoading = true;
      try {
        const params = new URLSearchParams();
        if (this.notesFilterType) params.set('meeting_type', this.notesFilterType);
        if (this.notesFilterWeek) params.set('week', this.notesFilterWeek);
        const res = await _authFetch(API_BASE + '/api/meeting-notes?' + params.toString());
        if (res.ok) { this.notesList = (await res.json()).rows || []; this.notesLoaded = true; }
      } catch {}
      this.notesLoading = false;
    },

    get notesWeekOptions() {
      const weeks = [...new Set(this.notesList.map(n => n.week_label).filter(Boolean))];
      weeks.sort().reverse();
      return weeks;
    },

    openNoteForm(note) {
      if (note) {
        this.notesForm = { show: true, id: note.id, title: note.title, meeting_type: note.meeting_type, week_label: note.week_label,
          content: note.content, author: note.author, attachments: note.attachments || [],
          meeting_date: note.meeting_date || '', start_time: note.start_time || '', end_time: note.end_time || '', add_to_calendar: false };
      } else {
        const cur = this.getCurrentWeekInfo ? this.getCurrentWeekInfo() : { month: new Date().getMonth()+1, week: 1 };
        this.notesForm = { show: true, id: '', title: '', meeting_type: '주간회의', week_label: cur.month + '월 ' + cur.week + '주차',
          content: '', author: '', attachments: [],
          meeting_date: new Date().toISOString().slice(0,10), start_time: '', end_time: '', add_to_calendar: true };
      }
    },

    // 캘린더 일정에서 회의록 작성 시작
    openNoteFromCalEvent(ev) {
      const cur = this.getCurrentWeekInfo ? this.getCurrentWeekInfo() : { month: new Date().getMonth()+1, week: 1 };
      this.notesForm = { show: true, id: '', title: ev.title, meeting_type: '기타',
        week_label: cur.month + '월 ' + cur.week + '주차', content: '',
        author: '', attachments: [],
        meeting_date: ev.startDate, start_time: ev.startTime || '', end_time: ev.endTime || '', add_to_calendar: false };
    },

    async saveNote() {
      const f = this.notesForm;
      if (!f.title) { this.showToast('info', '제목은 필수입니다.'); return; }
      try {
        const url = f.id ? API_BASE + '/api/meeting-notes/' + f.id : API_BASE + '/api/meeting-notes';
        const method = f.id ? 'PUT' : 'POST';
        const res = await _authFetch(url, {
          method, headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ title: f.title, meeting_type: f.meeting_type, week_label: f.week_label, content: f.content, author: f.author, attachments: f.attachments }),
        });
        if (res.ok) {
          this.notesForm.show = false;
          await this.loadNotes();
        } else { const d = await res.json().catch(()=>({})); this.showToast('error', d.error || '저장 실패'); }
      } catch (e) { this.showToast('error', '저장 실패: ' + e.message); }
    },

    async deleteNote(id) {
      if (!confirm('이 회의록을 삭제하시겠습니까?')) return;
      try {
        const res = await _authFetch(API_BASE + '/api/meeting-notes/' + id, { method: 'DELETE' });
        if (res.ok) { this.notesSelectedId = null; await this.loadNotes(); }
      } catch {}
    },

    async uploadNoteFile(event) {
      const file = event.target.files[0];
      if (!file) return;
      this.notesUploading = true;
      try {
        const fd = new FormData();
        fd.append('file', file);
        const res = await _authFetch(API_BASE + '/api/upload', { method: 'POST', body: fd });
        if (res.ok) {
          const d = await res.json();
          this.notesForm.attachments.push({ name: d.name || file.name, url: d.url, isImage: d.isImage || false });
        }
      } catch {}
      this.notesUploading = false;
      event.target.value = '';
    },

    removeNoteAttachment(idx) {
      this.notesForm.attachments.splice(idx, 1);
    },

    isTextFile(name) {
      const ext = (name || '').split('.').pop().toLowerCase();
      return ['md', 'txt', 'csv', 'html', 'json', 'xml', 'yaml', 'yml', 'log'].includes(ext);
    },

    isHtmlFile(name) {
      return (name || '').toLowerCase().endsWith('.html');
    },

    fileProxyUrl(url) {
      // Supabase URL에서 파일명 추출 → 프록시 경로로 변환
      const prefix = '/storage/v1/object/public/uploads/';
      const idx = url.indexOf(prefix);
      if (idx !== -1) {
        const fname = url.substring(idx + prefix.length);
        return API_BASE + '/api/file-proxy/' + fname;
      }
      return url;
    },

    get selectedNote() {
      return this.notesList.find(n => n.id === this.notesSelectedId) || null;
    },

    // 팀 프로젝트 탭 상태
    selectedPriorityId: null,
    tpLinkDropdown: false,

    // 선택된 우선순위 객체
    get selectedPriority() {
      if (!this.selectedPriorityId) return null;
      return (this.data.teamPriorities || []).find(p => p.id === this.selectedPriorityId) || null;
    },

    // 전체 프로젝트 (completedResults + activeProjects 통합, ID 중복 제거)
    get _allProjects() {
      const map = new Map();
      (this.data.completedResults?.items || []).forEach(r => map.set(r.id, r));
      (this.data.activeProjects?.projects || []).forEach(p => { if (!map.has(p.id)) map.set(p.id, p); });
      return map;
    },

    // 선택된 우선순위에 매핑된 프로젝트 목록
    get selectedPriorityProjects() {
      const sp = this.selectedPriority;
      if (!sp) return [];
      const ids = sp.projectIds || [];
      const all = this._allProjects;
      return ids.map(id => all.get(id)).filter(Boolean);
    },

    // 미연결 프로젝트 (어떤 우선순위에도 연결 안 된 것, 완료 제외)
    get unlinkedProjects() {
      const allLinked = new Set();
      (this.data.teamPriorities || []).forEach(p => (p.projectIds || []).forEach(id => allLinked.add(id)));
      return [...this._allProjects.values()].filter(r => !allLinked.has(r.id) && r.status !== 'done');
    },

    // 우선순위 미지정 프로젝트 (어떤 우선순위에도 연결 안 된 것, 모든 상태 포함)
    get unassignedPriorityProjects() {
      const allLinked = new Set();
      (this.data.teamPriorities || []).forEach(p => (p.projectIds || []).forEach(id => allLinked.add(id)));
      return [...this._allProjects.values()].filter(r => !allLinked.has(r.id));
    },

    // 팀 업무 현황 팝업
    showTeamStatusPopup: false,

    // 팝업 내 필터 (제외 방식 — 복합 설정 가능)
    tpFilter: {
      excludeActive: false,
      excludeHold: false,
      excludeDone: false,
      excludeWaiting: false,
      owner: 'all',
    },

    // 공통 필터 로직
    _applyTpFilter(list) {
      const f = this.tpFilter;
      return list.filter(p => {
        const s = p.status || 'active';
        if (f.excludeActive && s === 'active') return false;
        if (f.excludeHold && s === 'hold') return false;
        if (f.excludeDone && s === 'done') return false;
        if (f.excludeWaiting && s === 'waiting') return false;
        if (f.owner !== 'all') {
          if (f.owner === 'none' && p.owner && p.owner.trim()) return false;
          if (f.owner !== 'none' && p.owner !== f.owner) return false;
        }
        return true;
      });
    },

    // 필터 적용된 미지정 프로젝트
    get filteredUnassignedProjects() {
      return this._applyTpFilter(this.unassignedPriorityProjects);
    },

    // 필터 적용된 우선순위별 프로젝트
    get filteredPriorityProjects() {
      return this._applyTpFilter(this.selectedPriorityProjects);
    },

    // 우선순위 추가
    addTeamPriority() {
      if (!this.data.teamPriorities) this.data.teamPriorities = [];
      const rank = this.data.teamPriorities.length + 1;
      const np = { id: uid(), rank, label: rank + '순위', description: '', projectIds: [] };
      this.data.teamPriorities.push(np);
      this.selectedPriorityId = np.id;
      this.save();
    },

    // 우선순위 삭제
    removeTeamPriority(id) {
      if (!confirm('이 순위를 삭제하시겠습니까?')) return;
      this._markDeleted(id);
      this.data.teamPriorities = this.data.teamPriorities.filter(p => p.id !== id);
      this.data.teamPriorities.forEach((p, i) => { p.rank = i + 1; p.label = (i + 1) + '순위'; });
      if (this.selectedPriorityId === id) this.selectedPriorityId = this.data.teamPriorities[0]?.id || null;
      this.save();
    },

    // 우선순위 순서 변경
    moveTeamPriority(id, dir) {
      const arr = this.data.teamPriorities;
      const idx = arr.findIndex(p => p.id === id);
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= arr.length) return;
      [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
      arr.forEach((p, i) => { p.rank = i + 1; p.label = (i + 1) + '순위'; });
      this.save();
    },

    // 프로젝트 연결 (priorityId 지정 시 해당 우선순위에 직접 연결)
    linkProjectToPriority(projectId, priorityId) {
      const sp = priorityId
        ? (this.data.teamPriorities || []).find(p => p.id === priorityId)
        : this.selectedPriority;
      if (!sp || !projectId) return;
      if (!sp.projectIds) sp.projectIds = [];
      if (!sp.projectIds.includes(projectId)) {
        sp.projectIds.push(projectId);
        this.save();
      }
      this.tpLinkDropdown = false;
    },

    // 프로젝트 연결 해제
    unlinkProjectFromPriority(projectId) {
      const sp = this.selectedPriority;
      if (!sp) return;
      sp.projectIds = (sp.projectIds || []).filter(id => id !== projectId);
      this.save();
    },

    // 일일 회의 탭 상태
    dailyExpandedOwner: null,
    dailySelectedProject: null,
    dailyNewComment: '',
    dailyCommentsLoading: false,
    _commentsCache: {},  // 서버에서 로드한 댓글 캐시 (report.json과 독립)

    // 일일 회의 — Linear 연동 데이터
    dailyCompletedYesterday: {},  // { assignee: [{identifier, title, project}] }
    dailyPriorityIssues: [],      // [{rank, label, projectName, byAssignee}]
    dailyDataLoading: false,
    dailyDataLoaded: false,

    // Initiative 트리 데이터
    initData: { initiatives: [], unassigned: [], noDueDate: [], backlog: [] },
    initExpanded: {},    // { [id]: true/false } 펼침 상태
    initDueDateEdit: null, // 현재 편집 중인 이슈/프로젝트 id
    initDueDateValue: '',

    // 일일 회의 — Linear 데이터 로드
    async loadDailyData() {
      if (this.dailyDataLoading) return;
      this.dailyDataLoading = true;
      try {
        const [cyRes, piRes, initRes] = await Promise.all([
          _authFetch(API_BASE + '/api/linear/completed-yesterday'),
          _authFetch(API_BASE + '/api/linear/priority-issues'),
          _authFetch(API_BASE + '/api/linear/initiatives'),
        ]);
        if (cyRes.ok) this.dailyCompletedYesterday = (await cyRes.json()).byAssignee || {};
        if (piRes.ok) this.dailyPriorityIssues = (await piRes.json()).priorities || [];
        if (initRes.ok) this.initData = await initRes.json();
        this.dailyDataLoaded = true;
      } catch { /* 무시 */ }
      this.dailyDataLoading = false;
    },

    // Initiative 트리 펼침/접힘
    toggleInit(id) { this.initExpanded[id] = !this.initExpanded[id]; },
    isInitExpanded(id) { return !!this.initExpanded[id]; },
    coreAllExpanded: false,
    expandAllCore() {
      this.coreAllExpanded = true;
      const expand = (nodes, prefix) => {
        for (const n of nodes) {
          this.initExpanded[prefix + n.id] = true;
          expand(n.children || [], prefix);
        }
      };
      expand(this.coreTree, 'core-');
    },
    collapseAllCore() {
      this.coreAllExpanded = false;
      const collapse = (nodes, prefix) => {
        for (const n of nodes) {
          this.initExpanded[prefix + n.id] = false;
          collapse(n.children || [], prefix);
        }
      };
      collapse(this.coreTree, 'core-');
    },

    // 마감일 D-day 계산
    dueDayTag(dateStr) {
      if (!dateStr) return null;
      const today = new Date(); today.setHours(0,0,0,0);
      const due = new Date(dateStr); due.setHours(0,0,0,0);
      const diff = Math.round((due - today) / 86400000);
      if (diff < 0) return { text: 'D+' + Math.abs(diff) + ' 지연', cls: 'bg-red-100 text-red-700' };
      if (diff === 0) return { text: '오늘 마감', cls: 'bg-orange-100 text-orange-700' };
      if (diff <= 3) return { text: 'D-' + diff, cls: 'bg-amber-100 text-amber-700' };
      return { text: dateStr.slice(5), cls: 'bg-slate-100 text-slate-500' };
    },

    // 마감일 수정
    startDueDateEdit(id, currentDate) {
      this.initDueDateEdit = id;
      this.initDueDateValue = currentDate || '';
    },
    async saveDueDate(issueId) {
      try {
        await _authFetch(API_BASE + '/api/linear/issue/' + issueId, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ dueDate: this.initDueDateValue || null }),
        });
        this.initDueDateEdit = null;
        await this.loadDailyData();
      } catch { this.showToast('error', '마감일 수정 실패'); }
    },

    // 담당자별 Initiative 트리 필터링
    initForOwner(owner) {
      const filterIssues = (issues) => issues.filter(i => i.assignee === owner);
      const filterProject = (p) => {
        const issues = filterIssues(p.issues || []);
        if (issues.length === 0) return null;
        const done = issues.filter(i => i.stateType === 'completed').length;
        return { ...p, issues, total: issues.length, done, progress: issues.length > 0 ? Math.round(done / issues.length * 100) : 0 };
      };
      const result = { initiatives: [], unassigned: [], noDueDate: [], backlog: [] };
      for (const init of this.initData.initiatives || []) {
        const projects = (init.projects || []).map(filterProject).filter(Boolean);
        if (projects.length > 0) {
          const ti = projects.reduce((s, p) => s + p.total, 0);
          const di = projects.reduce((s, p) => s + p.done, 0);
          result.initiatives.push({ ...init, projects, totalIssues: ti, doneIssues: di, progress: ti > 0 ? Math.round(di / ti * 100) : 0 });
        }
      }
      for (const p of this.initData.unassigned || []) {
        const fp = filterProject(p);
        if (fp) result.unassigned.push(fp);
      }
      result.noDueDate = (this.initData.noDueDate || []).filter(i => i.assignee === owner);
      result.backlog = (this.initData.backlog || []).filter(i => i.assignee === owner);
      return result;
    },

    // 담당자 전체 상태 배지
    dailyOwnerStatus(owner) {
      const groups = this.dailyOwnerByStatus(owner);
      if ((groups.active || []).length > 0) return { label: '진행', cls: 'bg-blue-100 text-blue-700 border-blue-200' };
      if ((groups.waiting || []).length > 0) return { label: '대기', cls: 'bg-orange-100 text-orange-700 border-orange-200' };
      if ((groups.hold || []).length > 0) return { label: '보류', cls: 'bg-amber-100 text-amber-700 border-amber-200' };
      if ((groups.done || []).length > 0) return { label: '완료', cls: 'bg-emerald-100 text-emerald-700 border-emerald-200' };
      return { label: '없음', cls: 'bg-slate-100 text-slate-500 border-slate-200' };
    },

    // 기타 메모 저장
    saveDailyMemo(owner) {
      if (!this.data.dailyMemos) this.data.dailyMemos = {};
      this.save();
    },

    // 일일 회의 필터 상태
    dailyFilter: {
      excludeDone: true,
      excludeHold: false,
      excludeWaiting: false,
      completedAfter: '',   // 완료일 이후만 (YYYY-MM-DD 또는 M/DD 등)
      completedBefore: '',  // 완료일 이전만
    },

    // 상태별 전체 개수 (필터 전)
    dailyStatusCounts() {
      const counts = { active: 0, done: 0, hold: 0, waiting: 0 };
      const count = (items) => {
        (items || []).forEach(p => {
          if (!p.owner || !p.owner.trim()) return;
          const s = p.status || 'active';
          if (counts[s] !== undefined) counts[s]++;
        });
      };
      count(this.data.activeProjects?.projects);
      count(this.data.completedResults?.items);
      return counts;
    },

    // 일일 회의 - 이름별 프로젝트 목록 (필터 적용)
    // activeProjects + completedResults 모두 연동
    get dailyOwners() {
      const ownerMap = {};
      const f = this.dailyFilter;
      const addItems = (items, source) => {
        (items || []).forEach(p => {
          if (!p.owner || !p.owner.trim()) return;
          const status = p.status || 'active';
          // 상태 필터
          if (f.excludeDone && status === 'done') return;
          if (f.excludeHold && status === 'hold') return;
          if (f.excludeWaiting && status === 'waiting') return;
          // 완료일 필터
          if (f.completedAfter && p.completedDate) {
            if (p.completedDate < f.completedAfter) return;
          }
          if (f.completedBefore && p.completedDate) {
            if (p.completedDate > f.completedBefore) return;
          }
          if (!ownerMap[p.owner]) ownerMap[p.owner] = [];
          ownerMap[p.owner].push({ ...p, _source: source });
        });
      };
      addItems(this.data.activeProjects?.projects, 'project');
      addItems(this.data.completedResults?.items, 'result');
      // 각 담당자의 프로젝트를 완료일순 정렬
      Object.keys(ownerMap).forEach(owner => {
        ownerMap[owner].sort((a, b) => {
          const aDate = a.completedDate || '';
          const bDate = b.completedDate || '';
          if (!aDate && !bDate) return 0;
          if (!aDate) return 1;
          if (!bDate) return -1;
          return aDate.localeCompare(bDate);
        });
      });
      return ownerMap;
    },

    get dailyOwnerNames() {
      // 빈 담당자 카드 제외 대상은 설치자가 지정한다. 실제 할당 이슈는 보존한다.
      const exclude = new Set(this.teamConfig.dailyOwnerExcludeNames || []);
      const owners = new Set(Object.keys(this.dailyOwners));
      (this.teamConfig.members || []).forEach(m => { if (!exclude.has(m)) owners.add(m); });
      // 대표는 실제 할당 이슈 있을 때만 표시
      exclude.forEach(name => { if (!this.dailyOwners[name]) owners.delete(name); });
      return [...owners].sort();
    },

    // 필터 후 총 프로젝트 수
    get dailyTotalCount() {
      return Object.values(this.dailyOwners).reduce((sum, arr) => sum + arr.length, 0);
    },

    // 상태별 섹션 순서: 진행 → 대기 → 완료 → 보류
    _dailyStatusSections: [
      { key: 'active', label: '진행', colorBg: 'bg-blue-50', colorBorder: 'border-blue-200', colorText: 'text-blue-600', colorBadge: 'bg-blue-100 text-blue-700' },
      { key: 'waiting', label: '대기', colorBg: 'bg-orange-50', colorBorder: 'border-orange-200', colorText: 'text-orange-600', colorBadge: 'bg-orange-100 text-orange-700' },
      { key: 'done', label: '완료', colorBg: 'bg-emerald-50', colorBorder: 'border-emerald-200', colorText: 'text-emerald-600', colorBadge: 'bg-emerald-100 text-emerald-700' },
      { key: 'hold', label: '보류', colorBg: 'bg-amber-50', colorBorder: 'border-amber-200', colorText: 'text-amber-600', colorBadge: 'bg-amber-100 text-amber-700' },
    ],

    // 담당자의 프로젝트를 상태별로 그룹화
    dailyOwnerByStatus(owner) {
      const projects = this.dailyOwners[owner] || [];
      const groups = {};
      projects.forEach(p => {
        const s = p.status || 'active';
        if (!groups[s]) groups[s] = [];
        groups[s].push(p);
      });
      return groups;
    },

    // 해당 담당자에 존재하는 상태 섹션만 반환 (순서 유지)
    dailyOwnerStatusKeys(owner) {
      const groups = this.dailyOwnerByStatus(owner);
      return this._dailyStatusSections.filter(s => (groups[s.key] || []).length > 0);
    },

    // 상태 섹션 스타일 가져오기
    dailyStatusStyle(statusKey) {
      return this._dailyStatusSections.find(s => s.key === statusKey) || this._dailyStatusSections[0];
    },

    // 프로젝트의 마감일 가져오기 (일정 중 마지막 날짜)
    _getProjectDeadline(project) {
      const schedules = project.schedules || [];
      if (schedules.length === 0) return '';
      const dates = schedules.map(s => s.date).filter(d => d);
      if (dates.length === 0) return '';
      return dates.sort().pop();
    },

    // 일일 회의 - 담당자 드롭다운 토글
    toggleDailyOwner(owner) {
      if (this.dailyExpandedOwner === owner) {
        this.dailyExpandedOwner = null;
      } else {
        this.dailyExpandedOwner = owner;
        this.loadComments(owner);
      }
    },

    // 댓글 로드 (서버 API에서 가져와서 data.dailyComments에 병합)
    async loadComments(key) {
      this.dailyCommentsLoading = true;
      try {
        const res = await _authFetch(API_BASE +'/api/comments/' + encodeURIComponent(key));
        if (res.ok) {
          const data = await res.json();
          this._commentsCache = { ...this._commentsCache, [key]: data.comments || [] };
        }
      } catch { /* 무시 */ }
      this.dailyCommentsLoading = false;
    },

    // 댓글 추가 (서버 API로 저장 → 다중 사용자 동시 입력 지원)
    async addComment(key) {
      const text = this.dailyNewComment.trim();
      if (!text) return;
      try {
        const res = await _authFetch(API_BASE +'/api/comments/' + encodeURIComponent(key), {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text }),
        });
        if (res.ok) {
          this.dailyNewComment = '';
          await this.loadComments(key);
        }
      } catch { /* 무시 */ }
    },

    // 댓글 삭제
    async removeComment(key, commentId) {
      try {
        await _authFetch(API_BASE +'/api/comments/' + encodeURIComponent(key) + '/' + commentId, { method: 'DELETE' });
        await this.loadComments(key);
      } catch { /* 무시 */ }
    },

    // 일일 회의 - 현황 보기 팝업
    dailyDetailProject: null,
    openDailyDetail(proj) {
      // activeProjects 또는 completedResults에서 원본 찾기
      const ap = (this.data.activeProjects?.projects || []).find(p => p.id === proj.id);
      const cr = (this.data.completedResults?.items || []).find(r => r.id === proj.id);
      this.dailyDetailProject = ap || cr || proj;
    },
    closeDailyDetail() {
      this.dailyDetailProject = null;
    },

    // 일일 회의 프로젝트 삭제
    removeDailyProject(proj) {
      if (!confirm('이 프로젝트를 삭제하시겠습니까?\n' + (proj.title || ''))) return;
      // activeProjects에서 찾아 삭제
      const ap = this.data.activeProjects?.projects || [];
      const apIdx = ap.findIndex(p => p.id === proj.id);
      if (apIdx !== -1) { ap.splice(apIdx, 1); this.save(); return; }
      // completedResults에서 찾아 삭제
      const cr = this.data.completedResults?.items || [];
      const crIdx = cr.findIndex(r => r.id === proj.id);
      if (crIdx !== -1) { cr.splice(crIdx, 1); this.save(); }
    },

    // 댓글 시간 포맷
    formatCommentTime(ts) {
      if (!ts) return '';
      const d = new Date(ts);
      const mm = String(d.getMonth() + 1).padStart(2, '0');
      const dd = String(d.getDate()).padStart(2, '0');
      const hh = String(d.getHours()).padStart(2, '0');
      const mi = String(d.getMinutes()).padStart(2, '0');
      return `${mm}/${dd} ${hh}:${mi}`;
    },

    // 댓글 주차별 보기 토글
    commentWeekView: false,

    // 댓글을 주차별로 그룹핑 (월요일 기준)
    groupCommentsByWeek(owner) {
      const comments = (this._commentsCache || {})[owner] || [];
      if (comments.length === 0) return [];

      // 월요일 기준 주차 시작일 계산
      const getWeekStart = (dateStr) => {
        const d = new Date(dateStr);
        const day = d.getDay();
        const diff = d.getDate() - day + (day === 0 ? -6 : 1); // 월요일 기준
        const monday = new Date(d);
        monday.setDate(diff);
        monday.setHours(0, 0, 0, 0);
        return monday;
      };

      const groups = {};
      comments.forEach(c => {
        const weekStart = getWeekStart(c.createdAt);
        const key = weekStart.toISOString().split('T')[0];
        if (!groups[key]) groups[key] = { weekStart: key, comments: [] };
        groups[key].comments.push(c);
      });

      // 최신 주차 순으로 정렬
      return Object.values(groups).sort((a, b) => b.weekStart.localeCompare(a.weekStart));
    },

    // 주차 라벨 포맷 (3/24 ~ 3/30)
    formatWeekLabel(weekStart) {
      const start = new Date(weekStart + 'T00:00:00');
      const end = new Date(start);
      end.setDate(end.getDate() + 6);
      const sm = start.getMonth() + 1;
      const sd = start.getDate();
      const em = end.getMonth() + 1;
      const ed = end.getDate();
      return `${sm}/${sd} ~ ${em}/${ed}`;
    },

    get reportPeriod() {
      const now = new Date();
      const mon = new Date(now); mon.setDate(now.getDate() - now.getDay() + 1);
      const fri = new Date(mon); fri.setDate(mon.getDate() + 4);
      const f = (d) => `${d.getMonth() + 1}/${d.getDate()}`;
      return `${now.getFullYear()}년 ${now.getMonth() + 1}월 ${Math.ceil(now.getDate() / 7)}주차 (${f(mon)} ~ ${f(fri)})`;
    },

    // ── 저장/내보내기 ──
    save() {
      // Linear 동기화 중에는 저장 차단 (동기화 결과 덮어쓰기 방지)
      if (window._linearSyncLock) return;
      if (window._syncingFromServer) return; // 서버 동기화 중 재저장 방지
      this.data._lastModified = new Date().toISOString();
      localStorage.setItem(STORAGE_KEY, JSON.stringify(this.data));
      this.lastUpdated = new Date().toLocaleTimeString('ko-KR');
      // 서버에 비동기 저장
      _saveToServer('/api/report', this.data, this);
    },

    // 항목 삭제 id를 전역 _deletedIds에 기록 → 동시 편집 병합 시 삭제 항목이 되살아나지 않도록 함
    _markDeleted(id) {
      if (!id) return;
      if (!Array.isArray(this.data._deletedIds)) this.data._deletedIds = [];
      this.data._deletedIds.push(id);
      if (this.data._deletedIds.length > 500) {
        this.data._deletedIds = this.data._deletedIds.slice(-500);
      }
    },

    resetAll() {
      if (confirm('모든 데이터를 초기화합니다. 계속하시겠습니까?')) {
        this.data = getDefault();
        this.save();
      }
    },

    exportJson() { return JSON.stringify(this.data, null, 2); },
    copyJson() { navigator.clipboard.writeText(this.exportJson()); this.showToast('success', 'JSON이 클립보드에 복사되었습니다.'); },

    // ══════════════════════════════
    // 드래그 앤 드롭 - 섹션 순서 변경
    // ══════════════════════════════
    dragStart(type, idx) {
      this.dragState = { type, fromIdx: idx };
    },

    dragOver(e) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
    },

    dropOn(type, toIdx) {
      const { type: dragType, fromIdx } = this.dragState;
      if (dragType !== type || fromIdx === toIdx || fromIdx === -1) return;

      let arr;
      if (type === 'section') arr = this.data.metricSections;
      else if (type === 'meeting') arr = this.data.meetingAgenda.items;
      else return;

      const item = arr.splice(fromIdx, 1)[0];
      arr.splice(toIdx, 0, item);

      // 섹션 번호 재정렬
      if (type === 'section') {
        arr.forEach((s, i) => s.number = String(i + 1).padStart(2, '0'));
      }

      this.dragState = { type: null, fromIdx: -1 };
      this.save();
    },

    dragEnd() {
      this.dragState = { type: null, fromIdx: -1 };
    },

    // 버튼으로 순서 변경 (드래그 대체)
    moveSection(idx, dir) {
      const arr = this.data.metricSections;
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= arr.length) return;
      [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
      arr.forEach((s, i) => s.number = String(i + 1).padStart(2, '0'));
      this.save();
    },

    moveMeeting(idx, dir) {
      const arr = this.data.meetingAgenda.items;
      const newIdx = idx + dir;
      if (newIdx < 0 || newIdx >= arr.length) return;
      [arr[idx], arr[newIdx]] = [arr[newIdx], arr[idx]];
      this.save();
    },

    // ══════════════════════════════
    // 지표 메트릭 CRUD
    // ══════════════════════════════
    openMetric(sectionId, idx) {
      const sec = this.data.metricSections.find(s => s.id === sectionId);
      this.form = idx === -1
        ? { label: '', value: '', prevValue: '', trend: 0, status: 'green', subLabel: '', detail: '', alert: false }
        : { ...sec.metrics[idx] };
      this.modal = { show: true, type: 'metric', sectionId, itemIdx: idx };
    },
    saveMetric() {
      const sec = this.data.metricSections.find(s => s.id === this.modal.sectionId);
      if (!sec) return;
      const item = { ...this.form, id: this.form.id || uid() };
      if (this.modal.itemIdx === -1) sec.metrics.push(item);
      else sec.metrics.splice(this.modal.itemIdx, 1, item);
      this.modal.show = false;
      this.save();
    },
    removeMetric(sectionId, idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      const sec = this.data.metricSections.find(s => s.id === sectionId);
      this._markDeleted(sec?.metrics[idx]?.id);
      sec.metrics.splice(idx, 1);
      this.save();
    },

    // ══════════════════════════════
    // 회의 안건 CRUD
    // ══════════════════════════════
    MEETING_TEMPLATE: `## 목적\n\n\n## 배경\n\n\n## 논의할 내용\n-\n\n## 결정이 필요한 사항\n-`,

    // 회의 안건 탭 및 검색
    meetingTab: 'current',   // 'current' | 'past' | 'aiSummary'
    pastMeetingsMounted: false,
    aiSummaryMounted: false,
    pastMeetingSearch: '',
    pastWeekFilter: '',        // 주차 필터 (빈 문자열 = 전체)
    pastWeekCollapsed: {},     // 주차별 접기 상태

    // 현재 안건 필터
    meetingFilterImportance: JSON.parse(localStorage.getItem('mf_importance') || '""'),
    // 페이지 로드 시 항상 '대기중'으로 시작 — 완료 안건이 기본으로 숨겨지도록 함
    meetingFilterDiscussed: 'pending',
    meetingFilterPinned: JSON.parse(localStorage.getItem('mf_pinned') || '""'),
    meetingFilterOpen: false,
    meetingFilterWeek: '',  // 주차 필터

    // 안건 탭 주차 옵션 (전체 안건에서 중복 제거, 최신순)
    getCurrentWeekOptions() {
      const items = this.data.meetingAgenda?.items || [];
      const weeks = [...new Set(items.map(i => i.discussionWeek).filter(Boolean))];
      weeks.sort((a, b) => {
        const [, aM, aW] = a.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        const [, bM, bW] = b.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        return (parseInt(bM) - parseInt(aM)) || (parseInt(bW) - parseInt(aW));
      });
      return weeks;
    },

    // 필터 적용 여부 확인
    hasMeetingFilter() {
      return this.meetingFilterImportance || this.meetingFilterDiscussed || this.meetingFilterPinned || this.meetingFilterWeek;
    },
    // 필터 localStorage 저장
    saveMeetingFilters() {
      localStorage.setItem('mf_importance', JSON.stringify(this.meetingFilterImportance));
      localStorage.setItem('mf_discussed', JSON.stringify(this.meetingFilterDiscussed));
      localStorage.setItem('mf_pinned', JSON.stringify(this.meetingFilterPinned));
    },
    // 필터 초기화
    clearMeetingFilters() {
      this.meetingFilterImportance = '';
      this.meetingFilterDiscussed = '';
      this.meetingFilterPinned = '';
      this.meetingFilterWeek = '';
      this.saveMeetingFilters();
    },
    // 안건이 필터 조건에 맞는지 확인
    passMeetingFilter(item) {
      if (this.meetingFilterImportance && item.importance !== this.meetingFilterImportance) return false;
      if (this.meetingFilterDiscussed === 'done' && !item.discussed) return false;
      if (this.meetingFilterDiscussed === 'pending' && item.discussed) return false;
      if (this.meetingFilterPinned === 'pinned' && !item.pinned) return false;
      if (this.meetingFilterPinned === 'unpinned' && item.pinned) return false;
      if (this.meetingFilterWeek && item.discussionWeek !== this.meetingFilterWeek) return false;
      return true;
    },

    // AI 요약 상태
    showOriginalAgenda: '',      // 원문 보기 팝업 (프로젝트명)
    aiSummaryWeek: '',           // 선택된 주차
    aiSummaryLoading: false,     // 로딩 중
    aiSummaryError: '',          // 에러 메시지
    aiPromptEditing: false,      // 프롬프트 편집 모달
    aiPromptDraft: '',           // 편집 중인 프롬프트
    aiSummaryKeyOk: true,        // 서버 환경변수에서 Gemini 키 관리
    // 현재 월/주차 정보
    getCurrentWeekInfo() {
      const now = new Date();
      const month = now.getMonth() + 1;
      const week = Math.ceil(now.getDate() / 7);
      return { month, week };
    },
    // 주차가 지났는지 판별
    isPassedWeek(weekStr) {
      if (!weekStr) return false;
      const match = weekStr.match(/(\d+)월\s*(\d+)주차/);
      if (!match) return false;
      const itemMonth = parseInt(match[1]);
      const itemWeek = parseInt(match[2]);
      const cur = this.getCurrentWeekInfo();
      if (itemMonth < cur.month) return true;
      if (itemMonth === cur.month && itemWeek < cur.week) return true;
      return false;
    },
    // 현재 안건 (주차 미경과 + 날짜 미설정)
    getCurrentMeetings() {
      return (this.data.meetingAgenda?.items || []).filter((item, idx) => {
        item._origIdx = idx;
        // 상단 고정된 안건은 기간이 지나도 이번 주 안건에 표시
        return item.pinned || !this.isPassedWeek(item.discussionWeek);
      });
    },
    // 지난 안건 (주차 경과)
    getPastMeetings() {
      const keyword = this.pastMeetingSearch.trim().toLowerCase();
      return (this.data.meetingAgenda?.items || []).filter((item, idx) => {
        item._origIdx = idx;
        // 상단 고정된 안건은 이번 주 안건에 표시되므로 지난 안건에서 제외
        if (item.pinned) return false;
        if (!this.isPassedWeek(item.discussionWeek)) return false;
        if (!keyword) return true;
        return (item.project || '').toLowerCase().includes(keyword) ||
               (item.author || '').toLowerCase().includes(keyword) ||
               (item.detail || '').toLowerCase().includes(keyword) ||
               (item.discussionWeek || '').toLowerCase().includes(keyword) ||
               (item.discussedNote || '').toLowerCase().includes(keyword);
      });
    },
    // 지난 안건 수
    getPastMeetingCount() {
      // 상단 고정된 안건은 지난 안건 수에서 제외
      return (this.data.meetingAgenda?.items || []).filter(item => !item.pinned && this.isPassedWeek(item.discussionWeek)).length;
    },

    // 지난 안건 주차 목록 (최신순)
    getPastWeekOptions() {
      const items = (this.data.meetingAgenda?.items || [])
        .filter(item => !item.pinned && this.isPassedWeek(item.discussionWeek));
      const weeks = [...new Set(items.map(i => i.discussionWeek).filter(Boolean))];
      weeks.sort((a, b) => {
        const [, aM, aW] = a.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        const [, bM, bW] = b.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        return (parseInt(bM) - parseInt(aM)) || (parseInt(bW) - parseInt(aW));
      });
      return weeks;
    },
    // 지난 안건을 주차별 그룹으로 변환
    getGroupedPastMeetings() {
      let items = this.getPastMeetings();
      if (this.pastWeekFilter) {
        items = items.filter(i => i.discussionWeek === this.pastWeekFilter);
      }
      const groups = {};
      items.forEach(item => {
        const week = item.discussionWeek || '날짜 미설정';
        if (!groups[week]) groups[week] = [];
        groups[week].push(item);
      });
      const sortedKeys = Object.keys(groups).sort((a, b) => {
        const [, aM, aW] = a.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        const [, bM, bW] = b.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        return (parseInt(bM) - parseInt(aM)) || (parseInt(bW) - parseInt(aW));
      });
      return sortedKeys.map(week => ({ week, items: groups[week] }));
    },
    // 주차 그룹 접기/펼치기
    togglePastWeekCollapse(week) {
      this.pastWeekCollapsed[week] = !this.pastWeekCollapsed[week];
    },

    // ══════════════════════════════
    // AI 요약 기능
    // ══════════════════════════════

    // 모든 주차 목록 (현재 + 지난 안건 포함, 최신순)
    getAllWeekOptions() {
      const items = this.data.meetingAgenda?.items || [];
      const weeks = [...new Set(items.map(i => i.discussionWeek).filter(Boolean))];
      weeks.sort((a, b) => {
        const [, aM, aW] = a.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        const [, bM, bW] = b.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        return (parseInt(bM) - parseInt(aM)) || (parseInt(bW) - parseInt(aW));
      });
      return weeks;
    },

    // 선택된 주차의 안건 목록
    getWeekItems(week) {
      if (!week) return [];
      return (this.data.meetingAgenda?.items || []).filter(i => i.discussionWeek === week);
    },

    // 현재 주차의 AI 요약 데이터
    getAiSummary(week) {
      if (!this.data.aiSummaries) this.data.aiSummaries = {};
      return this.data.aiSummaries[week || this.aiSummaryWeek] || null;
    },

    // AI 요약 생성
    async generateAiSummary() {
      const week = this.aiSummaryWeek;
      if (!week) { this.aiSummaryError = '주차를 선택하세요'; return; }

      const items = this.getWeekItems(week);
      if (items.length === 0) { this.aiSummaryError = '해당 주차에 안건이 없습니다'; return; }

      // API 키는 서버 환경변수에서 관리

      this.aiSummaryLoading = true;
      this.aiSummaryError = '';

      try {
        // 프롬프트 설정 로드 + 변수 치환
        const rawPrompt = this.data.aiPromptConfig?.current || '';
        const prompt = rawPrompt ? this.resolvePromptVariables(rawPrompt) : '';

        // 안건 데이터 매핑 (HTML 첨부파일 내용 포함)
        const itemPayloads = [];
        for (const i of items) {
          const payload = {
            project: i.project,
            author: i.author,
            importance: i.importance,
            detail: i.detail,
            ceoComment: i.ceoComment,
            discussedNote: i.discussedNote,
          };
          // HTML 첨부파일 내용 추출하여 포함
          if (i.images && i.images.length > 0) {
            const htmlDocs = [];
            for (const img of i.images) {
              if (img.url && (img.name?.endsWith('.html') || img.name?.endsWith('.htm'))) {
                try {
                  const resp = await fetch(img.url);
                  if (resp.ok) {
                    const htmlText = await resp.text();
                    const parser = new DOMParser();
                    const doc = parser.parseFromString(htmlText, 'text/html');
                    doc.querySelectorAll('script, style, noscript').forEach(el => el.remove());
                    const bodyText = doc.body?.innerText || doc.body?.textContent || '';
                    if (bodyText.trim()) htmlDocs.push(`[첨부: ${img.name}]\n${bodyText}`);
                  }
                } catch { /* 무시 */ }
              }
            }
            if (htmlDocs.length > 0) payload.attachedHtml = htmlDocs.join('\n\n');
          }
          itemPayloads.push(payload);
        }

        const res = await _authFetch(API_BASE +'/api/ai/summarize', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            items: itemPayloads,
            prompt: prompt || undefined,
          }),
        });

        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          throw new Error(err.error || `서버 오류 (${res.status})`);
        }

        const result = await res.json();

        // 원본 안건의 첨부파일 매핑 (프로젝트명 → 이미지 배열)
        const attachMap = {};
        items.forEach(i => {
          if (i.images && i.images.length > 0) {
            attachMap[i.project] = i.images;
          }
        });

        // AI 요약 항목에 첨부파일 연결
        const summaryItems = result.summaryItems.map(si => {
          si.images = attachMap[si.project] || [];
          return si;
        });

        // 요약 데이터 저장
        if (!this.data.aiSummaries) this.data.aiSummaries = {};
        this.data.aiSummaries[week] = {
          generatedAt: result.generatedAt,
          model: result.model,
          items: summaryItems,
          rawText: result.rawText,
        };
        this.save();

      } catch (e) {
        this.aiSummaryError = e.message;
      } finally {
        this.aiSummaryLoading = false;
      }
    },

    // 저장된 AI 요약 주차 목록 (최신순)
    getSavedAiSummaryWeeks() {
      const summaries = this.data.aiSummaries || {};
      const weeks = Object.keys(summaries).filter(k => summaries[k]?.items?.length > 0);
      weeks.sort((a, b) => {
        const [, aM, aW] = a.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        const [, bM, bW] = b.match(/(\d+)월\s*(\d+)주차/) || [, 0, 0];
        return (parseInt(bM) - parseInt(aM)) || (parseInt(bW) - parseInt(aW));
      });
      return weeks.map(week => ({
        week,
        itemCount: summaries[week].items.length,
        date: new Date(summaries[week].generatedAt).toLocaleDateString('ko-KR'),
      }));
    },

    // AI 요약 항목 수정 (인라인 편집)
    updateAiSummaryItem(week, idx, field, value) {
      if (!this.data.aiSummaries?.[week]?.items?.[idx]) return;
      this.data.aiSummaries[week].items[idx][field] = value;
      this.save();
    },

    // AI 요약 삭제
    deleteAiSummary(week) {
      if (!confirm(`${week} AI 요약을 삭제하시겠습니까?`)) return;
      delete this.data.aiSummaries[week];
      this.save();
    },

    // Gemini API 키는 서버 환경변수(GEMINI_API_KEY)에서 관리

    // ── 프롬프트 관리 ──

    // 기본 프롬프트 (서버 DEFAULT_AI_PROMPT와 동일)
    AI_DEFAULT_PROMPT: `당신은 스타트업 대표에게 보고하는 전문 비즈니스 분석가입니다.
주간 회의 안건들을 분석하여 대표가 빠르게 전체 현황을 파악할 수 있도록 구조화된 요약을 작성합니다.

## 출력 형식

각 안건에 대해 아래 JSON 배열을 반환하세요. 반드시 순수 JSON만 출력하고, 마크다운 코드블록이나 다른 텍스트는 포함하지 마세요.

[
  {
    "project": "안건 제목",
    "author": "작성자",
    "category": "decision 또는 info",
    "situation": ["현황 핵심 포인트 1", "현황 핵심 포인트 2", "현황 핵심 포인트 3"],
    "businessImpact": "비즈니스에 영향을 끼치는 부분 상세 설명",
    "ceoDecision": "대표님 의사결정이 필요한 사항 (없으면 '없음')"
  }
]

## category 분류 기준 (매우 중요 — 반드시 아래 규칙을 엄격히 따를 것)

### "decision"으로 분류해야 하는 경우 (하나라도 해당하면 decision):
- 예산·비용·투자 관련 판단이 필요한 경우 (예산 승인, 비용 증가, 투자 결정)
- 전략·방향성 변경이 포함된 경우 (A안 vs B안 선택, 기능 우선순위 변경, 타겟 변경)
- 인력·리소스 배치 결정이 필요한 경우 (담당자 배정, 외주 여부, 채용)
- 일정·마일스톤 변경이 언급된 경우 (출시일 변경, 스프린트 조정)
- 파트너십·계약·외부 협력 관련 결정이 필요한 경우
- 안건 본문에 "결정이 필요한 사항", "승인 요청", "선택", "제안" 등의 표현이 있는 경우
- 현재 문제가 있고 해결 방향에 대한 대표의 판단이 필요한 경우
- 새로운 시도·실험·기능 도입을 제안하는 경우

### "info"로 분류하는 경우:
- 위 기준에 전혀 해당하지 않고, 순수하게 진행 현황 보고·수치 공유·완료 보고인 경우에만

### 핵심 원칙:
- 판단이 조금이라도 애매하면 반드시 "decision"으로 분류한다
- 안건 내용에 문제점·리스크·변경사항이 언급되어 있으면 대표가 알고 판단해야 할 사안이므로 "decision"이다
- 단순 보고처럼 보여도 숨겨진 의사결정 포인트가 있는지 반드시 확인한다

## 작성 원칙
- 각 안건의 핵심 상황을 논리순으로 정리 (원인 → 현상 → 대응/제안)
- 비즈니스 임팩트를 구체적 수치나 영향 범위로 명시
- decision 안건은 반드시 Q. 형태로 구체적 질문 제시 (선택지가 있으면 선택지도 제시)
- info 안건의 ceoDecision은 "없음"으로 설정
- 한국어로 작성, 간결하고 명확하게`,

    // AI 요약 카테고리 판정 (category 필드 없는 기존 데이터도 처리)
    _isDecisionItem(item) {
      // 명시적 category가 있으면 그대로 사용
      if (item.category === 'decision') return true;
      if (item.category === 'info') return false;
      // category 없는 기존 데이터: ceoDecision 내용이 있으면 decision으로 분류
      return item.ceoDecision && item.ceoDecision.trim() !== '' && item.ceoDecision.trim() !== '없음';
    },

    // AI 요약 카테고리별 필터
    getAiDecisionItems(week) {
      const summary = this.getAiSummary(week);
      if (!summary?.items) return [];
      return summary.items.filter(i => this._isDecisionItem(i));
    },
    getAiInfoItems(week) {
      const summary = this.getAiSummary(week);
      if (!summary?.items) return [];
      return summary.items.filter(i => !this._isDecisionItem(i));
    },

    // 원문 안건 찾기 (프로젝트명으로 매칭)
    getOriginalAgendaItem(projectName) {
      if (!projectName) return null;
      const items = this.data.meetingAgenda?.items || [];
      return items.find(i => i.project === projectName) || null;
    },

    // ceoDecision 텍스트를 Q. 단위로 분리하여 배열 반환
    formatDecisionQuestions(text) {
      if (!text) return [];
      // "Q." 앞에서 분할 (첫 번째 Q. 전 텍스트는 버림)
      const parts = text.split(/(?=Q\.\s)/).filter(p => p.trim().startsWith('Q.'));
      if (parts.length === 0) return [text]; // Q.가 없으면 원문 그대로
      return parts.map(p => {
        // Q. 질문과 (결정 시 팀 적용 방안: ...) 부분 분리
        const match = p.match(/^(Q\..+?)(\s*\(결정 시.+?\))\s*$/s);
        if (match) return { question: match[1].trim(), guidance: match[2].trim() };
        return { question: p.trim(), guidance: '' };
      });
    },

    // 프롬프트 문서 업로드 중 상태
    aiDocUploading: false,

    // 프롬프트 편집 열기
    openPromptEditor() {
      if (!this.data.aiPromptConfig) {
        this.data.aiPromptConfig = { current: '', versions: [], documents: [] };
      }
      if (!this.data.aiPromptConfig.documents) this.data.aiPromptConfig.documents = [];
      this.aiPromptDraft = this.data.aiPromptConfig.current || this.AI_DEFAULT_PROMPT;
      this.aiPromptEditing = true;
    },

    // 프롬프트 저장 (버전 히스토리 추가)
    savePrompt() {
      if (!this.data.aiPromptConfig) {
        this.data.aiPromptConfig = { current: '', versions: [] };
      }
      const prev = this.data.aiPromptConfig.current;
      const next = this.aiPromptDraft.trim();

      // 변경사항이 있을 때만 버전 추가
      if (prev && prev !== next) {
        this.data.aiPromptConfig.versions.push({
          id: uid(),
          prompt: prev,
          savedAt: new Date().toISOString(),
          label: `v${this.data.aiPromptConfig.versions.length + 1}`,
        });
        // 최대 20개 버전 유지
        if (this.data.aiPromptConfig.versions.length > 20) {
          this.data.aiPromptConfig.versions.shift();
        }
      }

      this.data.aiPromptConfig.current = next;
      this.aiPromptEditing = false;
      this.save();
    },

    // 프롬프트 기본값 복원
    resetPromptToDefault() {
      if (!confirm('프롬프트를 기본값으로 복원하시겠습니까?')) return;
      this.aiPromptDraft = this.AI_DEFAULT_PROMPT;
    },

    // 이전 버전 프롬프트 복원
    restorePromptVersion(version) {
      if (!confirm(`"${version.label}" 버전으로 복원하시겠습니까?`)) return;
      this.aiPromptDraft = version.prompt;
    },

    // 프롬프트 버전 삭제
    deletePromptVersion(idx) {
      this.data.aiPromptConfig.versions.splice(idx, 1);
      this.save();
    },

    // ── 프롬프트 첨부문서 관리 ──

    // 문서 업로드 (텍스트 추출하여 저장)
    async uploadPromptDoc(event) {
      const files = event.target.files;
      if (!files || files.length === 0) return;
      if (!this.data.aiPromptConfig.documents) this.data.aiPromptConfig.documents = [];

      this.aiDocUploading = true;
      for (const file of files) {
        try {
          let content = '';
          const ext = file.name.split('.').pop().toLowerCase();

          if (['txt', 'md', 'csv', 'json', 'html', 'htm'].includes(ext)) {
            // 텍스트 계열 파일 → 직접 읽기
            content = await file.text();
            // HTML 파일인 경우 본문 텍스트 추출
            if (['html', 'htm'].includes(ext)) {
              const parser = new DOMParser();
              const doc = parser.parseFromString(content, 'text/html');
              // 스크립트/스타일 태그 제거
              doc.querySelectorAll('script, style, noscript').forEach(el => el.remove());
              content = doc.body?.innerText || doc.body?.textContent || content;
            }
          } else {
            content = `[바이너리 파일 — 텍스트 추출 불가: ${file.name}]`;
          }

          // 변수명 생성: 파일명에서 확장자 제거 + 공백→언더스코어
          const varName = file.name.replace(/\.[^.]+$/, '').replace(/[\s\-]+/g, '_').replace(/[^a-zA-Z0-9가-힣_]/g, '');

          this.data.aiPromptConfig.documents.push({
            id: uid(),
            fileName: file.name,
            varName,
            content,
            charCount: content.length,
            uploadedAt: new Date().toISOString(),
          });
        } catch (e) {
          console.error('문서 업로드 실패:', file.name, e);
        }
      }
      this.aiDocUploading = false;
      this.save();
      // 입력 초기화
      event.target.value = '';
    },

    // 첨부문서 삭제
    removePromptDoc(idx) {
      this.data.aiPromptConfig.documents.splice(idx, 1);
      this.save();
    },

    // 프롬프트 내 {{변수명}} 치환 → 문서 내용으로 교체
    resolvePromptVariables(promptText) {
      const docs = this.data.aiPromptConfig?.documents || [];
      let resolved = promptText;
      docs.forEach(doc => {
        // {{변수명}} 패턴 치환
        const pattern = new RegExp(`\\{\\{${doc.varName}\\}\\}`, 'g');
        resolved = resolved.replace(pattern, doc.content);
      });
      return resolved;
    },

    // 프롬프트에 변수 삽입 헬퍼
    insertPromptVar(varName) {
      this.aiPromptDraft += `{{${varName}}}`;
    },

    // 논의 완료 팝업 상태
    discussPopup: { show: false, mIdx: -1, note: '' },
    openDiscussPopup(mIdx) {
      const item = this.data.meetingAgenda.items[mIdx];
      this.discussPopup = { show: true, mIdx, note: item.discussedNote || '' };
    },
    confirmDiscuss() {
      const item = this.data.meetingAgenda.items[this.discussPopup.mIdx];
      item.discussed = true;
      item.discussedNote = this.discussPopup.note;
      this.discussPopup.show = false;
      this.save();
    },
    undoDiscuss(mIdx) {
      this.data.meetingAgenda.items[mIdx].discussed = false;
      this.data.meetingAgenda.items[mIdx].discussedNote = '';
      this.save();
    },
    // 논의 날짜 주차 옵션 생성
    getDiscussionWeekOptions() {
      const now = new Date();
      const options = [];
      for (let offset = -1; offset <= 1; offset++) {
        const d = new Date(now.getFullYear(), now.getMonth() + offset, 1);
        const month = d.getMonth() + 1;
        for (let w = 1; w <= 5; w++) {
          const weekStart = new Date(d.getFullYear(), d.getMonth(), (w - 1) * 7 + 1);
          if (weekStart.getMonth() !== d.getMonth()) break;
          options.push(`${month}월 ${w}주차`);
        }
      }
      return options;
    },
    // 중요도 배지 클래스
    importanceClass(level) {
      return { high: 'bg-red-100 text-red-700 border-red-200', medium: 'bg-orange-100 text-orange-600 border-orange-200', low: 'bg-slate-100 text-slate-500 border-slate-200' }[level] || 'bg-slate-50 text-slate-400 border-slate-200';
    },
    importanceLabel(level) {
      return { high: '매우 중요', medium: '중간', low: '낮음' }[level] || '미설정';
    },

    // 다음 회의 주차 자동 계산 (매주 월요일 회의 기준)
    // 오늘이 월요일이면 이번 주, 화~일이면 다음 월요일 주차
    getNextMeetingWeek() {
      const now = new Date();
      const day = now.getDay(); // 0=일, 1=월, ...
      // 월요일이 아닌 경우 다음 월요일로 이동
      const target = new Date(now);
      if (day !== 1) {
        const daysUntilMon = day === 0 ? 1 : (8 - day);
        target.setDate(target.getDate() + daysUntilMon);
      }
      const month = target.getMonth() + 1;
      const week = Math.ceil(target.getDate() / 7);
      return `${month}월 ${week}주차`;
    },

    openMeeting(idx) {
      if (idx === -1) {
        this.form = { project: '', author: '', required: false, detail: this.MEETING_TEMPLATE, ceoComment: '', images: [], importance: '', discussed: false, discussedNote: '', pinned: false, discussionWeek: this.getNextMeetingWeek() };
      } else {
        const item = this.data.meetingAgenda.items[idx];
        this.form = { ...item, detail: item.detail || '', ceoComment: item.ceoComment || '', images: item.images || [], author: item.author || '', importance: item.importance || '', discussed: item.discussed || false, discussedNote: item.discussedNote || '', pinned: item.pinned || false, discussionWeek: item.discussionWeek || '' };
      }
      this.modal = { show: true, type: 'meeting', sectionId: '', itemIdx: idx };
    },
    saveMeeting() {
      const item = { ...this.form, id: this.form.id || uid() };
      if (this.modal.itemIdx === -1) this.data.meetingAgenda.items.push(item);
      else this.data.meetingAgenda.items.splice(this.modal.itemIdx, 1, item);
      this.modal.show = false;
      this.save();
    },
    removeMeeting(idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      this._markDeleted(this.data.meetingAgenda.items[idx]?.id);
      this.data.meetingAgenda.items.splice(idx, 1);
      this.save();
    },

    // 이미지 업로드 상태
    imageUploading: false,

    // 파일 업로드 (이미지 + 문서, 파일 선택 또는 드롭)
    async uploadMeetingImage(event) {
      const files = event.target?.files || event.dataTransfer?.files;
      if (!files || files.length === 0) return;
      this.imageUploading = true;
      for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        try {
          const res = await _authFetch(API_BASE +'/api/upload', { method: 'POST', body: fd });
          if (!res.ok) continue;
          const data = await res.json();
          if (!this.form.images) this.form.images = [];
          // isImage 플래그 포함 (서버 응답 기반, 없으면 true 폴백)
          this.form.images.push({ id: uid(), url: data.url, name: data.name || file.name, isImage: data.isImage !== false });
        } catch { /* 무시 */ }
      }
      this.imageUploading = false;
      // 파일 input 초기화
      if (event.target?.value) event.target.value = '';
    },

    // 이미지 삭제
    async removeMeetingImage(idx) {
      const img = this.form.images[idx];
      if (!img) return;
      // 서버에서 파일 삭제
      const filename = img.url.split('/').pop();
      try {
        await _authFetch(API_BASE +'/api/upload/' + encodeURIComponent(filename), { method: 'DELETE' });
      } catch { /* 무시 */ }
      this.form.images.splice(idx, 1);
    },

    // 이미지 드래그앤드롭 핸들러
    handleImageDrop(event) {
      event.preventDefault();
      this.uploadMeetingImage(event);
    },

    // ══════════════════════════════
    // 방향성 CRUD
    // ══════════════════════════════
    directionExpanded: {},  // 펼침 상태 (id → boolean)

    openDirection(idx) {
      if (idx === -1) {
        this.form = { title: '', detail: '', priority: (this.data.directions?.items?.length || 0) + 1, linkedProjects: [], linkedMeetings: [], reason: '' };
      } else {
        const d = this.data.directions.items[idx];
        this.form = { ...d, linkedProjects: [...(d.linkedProjects || [])], linkedMeetings: [...(d.linkedMeetings || [])], reason: d.reason || '' };
      }
      this.modal = { show: true, type: 'direction', sectionId: '', itemIdx: idx };
    },
    saveDirection() {
      const item = { ...this.form, id: this.form.id || uid(), linkedProjects: [...(this.form.linkedProjects || [])], linkedMeetings: [...(this.form.linkedMeetings || [])], reason: this.form.reason || '' };
      if (!this.data.directions) this.data.directions = { title: '방향성', subtitle: '우선순위별 핵심 방향', items: [] };
      if (this.modal.itemIdx === -1) this.data.directions.items.push(item);
      else this.data.directions.items.splice(this.modal.itemIdx, 1, item);
      this.modal.show = false;
      this.save();
    },
    removeDirection(idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      this._markDeleted(this.data.directions.items[idx]?.id);
      this.data.directions.items.splice(idx, 1);
      this.save();
    },
    toggleDirectionExpand(id) {
      this.directionExpanded[id] = !this.directionExpanded[id];
    },
    // 방향성에 연동된 프로젝트 목록 가져오기
    getLinkedProjects(linkedIds) {
      return (this.data.completedResults?.items || []).filter(r => (linkedIds || []).includes(r.id));
    },
    // 방향성에 연동된 회의 안건 목록 가져오기
    getLinkedMeetings(linkedIds) {
      return (this.data.meetingAgenda?.items || []).filter(m => (linkedIds || []).includes(m.id));
    },
    // 회의 안건의 연동된 방향성 ID 가져오기
    getMeetingDirectionId(meetingId) {
      const dirs = this.data.directions?.items || [];
      for (const d of dirs) {
        if ((d.linkedMeetings || []).includes(meetingId)) return d.id;
      }
      return '';
    },
    // 회의 안건 테이블에서 방향성 연동 변경
    changeMeetingDirection(meetingId, newDirId) {
      const dirs = this.data.directions?.items || [];
      // 기존 연동 해제
      dirs.forEach(d => {
        const idx = (d.linkedMeetings || []).indexOf(meetingId);
        if (idx !== -1) d.linkedMeetings.splice(idx, 1);
      });
      // 새 방향성 연동
      if (newDirId) {
        const target = dirs.find(d => d.id === newDirId);
        if (target) {
          if (!target.linkedMeetings) target.linkedMeetings = [];
          target.linkedMeetings.push(meetingId);
        }
      }
      this.save();
    },
    // 방향성 우선순위 배지 색상
    directionPriorityClass(p) {
      return { 1: 'bg-red-100 text-red-700 border-red-200', 2: 'bg-orange-100 text-orange-700 border-orange-200', 3: 'bg-blue-100 text-blue-700 border-blue-200', 4: 'bg-slate-100 text-slate-600 border-slate-200' }[p] || 'bg-slate-100 text-slate-600 border-slate-200';
    },

    // ══════════════════════════════
    // 완료 결과 CRUD (우선순위 정렬 포함)
    // ══════════════════════════════
    resultPriorityFilter: 'all',  // 'all', 1, 2, 3, 4, 'none'
    // completedDate 파싱 (M/DD, M.DD, YYYY-MM-DD 등 다양한 형식 지원)
    _parseCompletedDate(cd) {
      if (!cd) return null;
      // YYYY-MM-DD
      if (/^\d{4}-\d{2}-\d{2}$/.test(cd)) return new Date(cd + 'T00:00:00');
      // M/DD 또는 M.DD
      const parts = cd.match(/(\d+)[\/.](\d+)/);
      if (parts) {
        const year = new Date().getFullYear();
        return new Date(year, parseInt(parts[1]) - 1, parseInt(parts[2]));
      }
      return null;
    },

    // 소요 기간 계산 (생성일 기준)
    calcDuration(item) {
      if (!item) return '';
      // createdDate (YYYY-MM-DD) 우선, 없으면 createdAt (ISO)
      const src = item.createdDate || (item.createdAt ? new Date(item.createdAt).toISOString().split('T')[0] : '');
      if (!src) return '';
      const created = new Date(src + 'T00:00:00');
      if (isNaN(created)) return '';
      const status = item.status || 'active';
      if (status === 'done' && item.completedDate) {
        const end = this._parseCompletedDate(item.completedDate);
        if (end) {
          const days = Math.ceil((end - created) / (1000 * 60 * 60 * 24));
          return days >= 0 ? days + '일 소요' : '';
        }
        return '';
      }
      // 진행 중: 생성일 ~ 오늘
      const today = new Date();
      today.setHours(0, 0, 0, 0);
      created.setHours(0, 0, 0, 0);
      const days = Math.ceil((today - created) / (1000 * 60 * 60 * 24)) + 1;
      return days + '일째';
    },

    openResult(idx) {
      if (idx === -1) {
        const today = new Date().toISOString().split('T')[0];
        this.form = { title: '', owner: '', completedDate: '', targetMetric: '', actualMetric: '', achieved: false, note: '', status: 'active', schedules: [], createdAt: new Date().toISOString(), createdDate: today };
      } else {
        const r = this.data.completedResults.items[idx];
        // 마이그레이션: 누락 필드 보완
        if (!r.status) r.status = 'active';
        if (!r.schedules) r.schedules = [];
        if (!r.createdAt) r.createdAt = new Date().toISOString();
        // createdDate = YYYY-MM-DD (date input용)
        const cd = r.createdAt ? new Date(r.createdAt).toISOString().split('T')[0] : new Date().toISOString().split('T')[0];
        this.form = { ...r, schedules: r.schedules.map(s => ({ ...s })), createdDate: r.createdDate || cd };
      }
      this.modal = { show: true, type: 'result', sectionId: '', itemIdx: idx };
    },
    saveResult() {
      const item = { ...this.form, id: this.form.id || uid(), schedules: this.form.schedules.map(s => ({ ...s })) };
      // createdDate → createdAt 동기화
      if (item.createdDate) {
        item.createdAt = new Date(item.createdDate + 'T00:00:00').toISOString();
      }
      if (!item.createdAt) item.createdAt = new Date().toISOString();
      if (this.modal.itemIdx === -1) this.data.completedResults.items.push(item);
      else this.data.completedResults.items.splice(this.modal.itemIdx, 1, item);
      this.modal.show = false;
      this.save();
    },
    removeResult(idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      this._markDeleted(this.data.completedResults.items[idx]?.id);
      this.data.completedResults.items.splice(idx, 1);
      this.save();
    },
    // 일정 추가/삭제
    addResultSchedule() {
      if (!this.form.schedules) this.form.schedules = [];
      this.form.schedules.push({ id: uid(), title: '', date: '' });
    },
    removeResultSchedule(idx) {
      this.form.schedules.splice(idx, 1);
    },
    // 프로젝트의 방향성 우선순위 가져오기
    getResultPriority(resultId) {
      const dirs = this.data.directions?.items || [];
      for (const d of dirs) {
        if ((d.linkedProjects || []).includes(resultId)) return d.priority;
      }
      return 999;  // 미연동 → 최하위
    },
    // 필터링된 결과 (상태 + 우선순위 필터)
    get filteredResults() {
      let list = (this.data.completedResults.items || []).filter(r => (r.status || 'active') === this.resultFilter);
      if (this.resultPriorityFilter !== 'all') {
        if (this.resultPriorityFilter === 'none') {
          list = list.filter(r => this.getResultPriority(r.id) >= 999);
        } else {
          const pf = Number(this.resultPriorityFilter);
          list = list.filter(r => this.getResultPriority(r.id) === pf);
        }
      }
      return list;
    },
    // 상태별 카운트
    resultCountByStatus(status) {
      return (this.data.completedResults.items || []).filter(r => (r.status || 'active') === status).length;
    },
    // ID 기반 열기/삭제 (필터링 후 원본 인덱스 필요)
    openResultById(id) {
      const idx = this.data.completedResults.items.findIndex(r => r.id === id);
      if (idx !== -1) this.openResult(idx);
    },
    removeResultById(id) {
      const idx = this.data.completedResults.items.findIndex(r => r.id === id);
      if (idx !== -1) this.removeResult(idx);
    },
    // 테이블에서 상태 직접 변경
    changeResultStatus(id, newStatus) {
      const item = this.data.completedResults.items.find(r => r.id === id);
      if (item) { item.status = newStatus; this.save(); }
    },
    // 테이블에서 우선순위 직접 변경 (방향성 연동 이동)
    changeResultPriority(resultId, newPriority) {
      const dirs = this.data.directions?.items || [];
      // 기존 연동 해제
      dirs.forEach(d => {
        const idx = (d.linkedProjects || []).indexOf(resultId);
        if (idx !== -1) d.linkedProjects.splice(idx, 1);
      });
      // 새 우선순위 연동 (0 = 해제만)
      if (newPriority > 0) {
        const target = dirs.find(d => d.priority === newPriority);
        if (target) {
          if (!target.linkedProjects) target.linkedProjects = [];
          target.linkedProjects.push(resultId);
        }
      }
      this.save();
    },
    // 사용 가능한 우선순위 목록 (방향성에 정의된 것만)
    getAvailablePriorities() {
      return (this.data.directions?.items || [])
        .map(d => d.priority)
        .sort((a, b) => a - b);
    },

    // ══════════════════════════════
    // 프로젝트 CRUD (섹션→담당자→질문 연동)
    // ══════════════════════════════
    projectValidationError: '',

    // 프로젝트의 질문 목록 조회 (카드 표시용)
    _getProjectQuestions(p) {
      const sec = this.teamConfig.sections.find(s => s.section === p.section);
      if (!sec) return [];
      const role = sec.roles.find(r => r.owner === p.owner && r.theme === p.theme);
      return role ? role.questions : [];
    },

    isProjectComplete(p) {
      if ((p.status || 'active') === 'done') return true;
      if (!p.section || !p.owner) return false;
      const qs = this._getProjectQuestions(p);
      if (qs.length === 0) return !!(p.section && p.owner);
      const answers = p.answers || [];
      return qs.every((_, i) => answers[i] && answers[i].trim());
    },

    projectMissingQs(p) {
      const missing = [];
      if (!p.section) missing.push('섹션');
      if (!p.owner) missing.push('담당자');
      const qs = this._getProjectQuestions(p);
      const answers = p.answers || [];
      qs.forEach((_, i) => { if (!answers[i] || !answers[i].trim()) missing.push('Q' + (i + 1)); });
      return missing;
    },

    openProject(idx) {
      this.projectValidationError = '';
      if (idx === -1) {
        this.form = { title: '', section: '', owner: '', theme: '', category: '', stage: '', stagePercent: 0, answers: [], schedules: [], checklist: [], attachments: [], status: 'active', targetMetric: '', completedDate: '', note: '' };
      } else {
        const p = { ...this.data.activeProjects.projects[idx] };
        // 마이그레이션: q1/q2/q3 → answers 배열
        if (!p.answers) {
          p.answers = [p.q1 || '', p.q2 || '', p.q3 || ''];
          delete p.q1; delete p.q2; delete p.q3;
        }
        if (!p.section) p.section = '';
        if (!p.theme) p.theme = '';
        // 마이그레이션: 새 필드 초기화
        if (!p.schedules) p.schedules = [];
        if (!p.checklist) p.checklist = [];
        if (!p.attachments) p.attachments = [];
        if (!p.status) p.status = 'active';
        this.form = p;
      }
      this.modal = { show: true, type: 'project', sectionId: '', itemIdx: idx };
    },

    saveProject() {
      if (!this.form.owner) { this.projectValidationError = '담당자를 선택해주세요.'; return; }
      if (!this.form.targetMetric || !this.form.targetMetric.trim()) { this.projectValidationError = '"확인해야 할 데이터"를 입력해주세요.'; return; }

      // 필수 기준 답변 검증
      const qs = this.currentQuestions;
      if (qs.length > 0) {
        const missing = [];
        qs.forEach((q, i) => {
          if (!this.form.answers[i] || !this.form.answers[i].trim()) missing.push('Q' + (i + 1));
        });
        if (missing.length > 0) {
          this.projectValidationError = '필수 답변 미입력: ' + missing.join(', ');
          return;
        }
      }

      this.projectValidationError = '';
      const item = { ...this.form, id: this.form.id || uid(), stagePercent: Number(this.form.stagePercent) || 0,
        schedules: this.form.schedules || [], checklist: this.form.checklist || [],
        attachments: this.form.attachments || [], status: this.form.status || 'active' };
      delete item.q1; delete item.q2; delete item.q3;
      if (this.modal.itemIdx === -1) this.data.activeProjects.projects.push(item);
      else this.data.activeProjects.projects.splice(this.modal.itemIdx, 1, item);
      this.modal.show = false;
      this.save();
    },

    removeProject(idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      this._markDeleted(this.data.activeProjects.projects[idx]?.id);
      this.data.activeProjects.projects.splice(idx, 1);
      this.save();
    },

    // ── 프로젝트 일정 관리 ──
    addSchedule() {
      if (!this.form.schedules) this.form.schedules = [];
      const n = this.form.schedules.length + 1;
      this.form.schedules.push({ id: uid(), label: n + '차', date: '', note: '' });
    },
    removeSchedule(idx) {
      this.form.schedules.splice(idx, 1);
    },

    // ── 프로젝트 체크리스트 ──
    addCheckItem(text) {
      if (!this.form.checklist) this.form.checklist = [];
      if (!text || !text.trim()) return;
      this.form.checklist.push({ id: uid(), text: text.trim(), checked: false });
      this.newCheckText = '';
    },
    toggleCheckItem(idx) {
      this.form.checklist[idx].checked = !this.form.checklist[idx].checked;
    },
    removeCheckItem(idx) {
      this.form.checklist.splice(idx, 1);
    },

    // ── 프로젝트 첨부파일 ──
    async uploadProjectAttachment(event) {
      const files = event.target?.files || event.dataTransfer?.files;
      if (!files || files.length === 0) return;
      this.projectAttachUploading = true;
      for (const file of files) {
        const fd = new FormData();
        fd.append('file', file);
        try {
          const res = await _authFetch(API_BASE +'/api/upload', { method: 'POST', body: fd });
          if (!res.ok) continue;
          const data = await res.json();
          if (!this.form.attachments) this.form.attachments = [];
          this.form.attachments.push({ id: uid(), url: data.url, name: data.name || file.name, isImage: data.isImage !== false });
        } catch { /* 무시 */ }
      }
      this.projectAttachUploading = false;
      if (event.target?.value) event.target.value = '';
    },
    async removeProjectAttachment(idx) {
      const att = this.form.attachments[idx];
      if (!att) return;
      const filename = att.url.split('/').pop();
      try {
        await _authFetch(API_BASE +'/api/upload/' + encodeURIComponent(filename), { method: 'DELETE' });
      } catch { /* 무시 */ }
      this.form.attachments.splice(idx, 1);
    },
    handleProjectAttachDrop(event) {
      event.preventDefault();
      this.uploadProjectAttachment(event);
    },

    // ── 프로젝트 필터링 ──
    get filteredProjects() {
      return (this.data.activeProjects.projects || []).filter(p => (p.status || 'active') === this.projectFilter);
    },
    projectCountByStatus(status) {
      return (this.data.activeProjects.projects || []).filter(p => (p.status || 'active') === status).length;
    },
    openProjectById(id) {
      const idx = this.data.activeProjects.projects.findIndex(p => p.id === id);
      if (idx !== -1) this.openProject(idx);
    },
    removeProjectById(id) {
      const idx = this.data.activeProjects.projects.findIndex(p => p.id === id);
      if (idx !== -1) this.removeProject(idx);
    },

    // ══════════════════════════════
    // 칸반 이슈 CRUD
    // ══════════════════════════════
    openIssue(colId, idx) {
      const col = this.data.kanbanColumns.find(c => c.id === colId);
      this.form = idx === -1
        ? { title: '', identifier: '', priority: 3, assignee: '', labelsStr: '', project: '', dueDate: '', description: '' }
        : { ...col.issues[idx], labelsStr: (col.issues[idx].labels || []).join(', ') };
      this.modal = { show: true, type: 'issue', sectionId: colId, itemIdx: idx };
    },
    saveIssue() {
      const col = this.data.kanbanColumns.find(c => c.id === this.modal.sectionId);
      if (!col) return;
      const f = { ...this.form };
      f.labels = (f.labelsStr || '').split(',').map(s => s.trim()).filter(Boolean);
      delete f.labelsStr;
      f.priority = Number(f.priority) || 0;
      f.id = f.id || uid();
      if (this.modal.itemIdx === -1) col.issues.push(f);
      else col.issues.splice(this.modal.itemIdx, 1, f);
      this.modal.show = false;
      this.save();
    },
    removeIssue(colId, idx) {
      if (!confirm('삭제하시겠습니까?')) return;
      const col = this.data.kanbanColumns.find(c => c.id === colId);
      this._markDeleted(col?.issues[idx]?.id);
      col.issues.splice(idx, 1);
      this.save();
    },

    // ══════════════════════════════
    // 섹션 추가/삭제
    // ══════════════════════════════
    addMetricSection() {
      const num = String(this.data.metricSections.length + 1).padStart(2, '0');
      this.data.metricSections.push({ id: uid(), number: num, title: '새 섹션', subtitle: '부제목', metrics: [] });
      this.save();
    },
    removeMetricSection(idx) {
      if (!confirm('이 섹션을 삭제하시겠습니까?')) return;
      this._markDeleted(this.data.metricSections[idx]?.id);
      this.data.metricSections.splice(idx, 1);
      this.data.metricSections.forEach((s, i) => s.number = String(i + 1).padStart(2, '0'));
      this.save();
    },

    // ── 모달 저장 디스패처 ──
    saveModal() {
      ({ metric: () => this.saveMetric(), meeting: () => this.saveMeeting(), result: () => this.saveResult(), project: () => this.saveProject(), issue: () => this.saveIssue(), todo: () => this.saveTodo(), direction: () => this.saveDirection() })[this.modal.type]?.();
    },

    // ── 유틸리티 ──
    priorityClass(p) { return { 1:'bg-red-100 text-red-600', 2:'bg-orange-100 text-orange-600', 3:'bg-amber-100 text-amber-700', 4:'bg-slate-100 text-slate-500' }[p] || ''; },
    priorityLabel(p) { return { 1:'긴급', 2:'높음', 3:'중간', 4:'낮음' }[p] || ''; },
    statusDot(s) { return { green:'bg-emerald-500', yellow:'bg-amber-400', red:'bg-red-500 blink-warning' }[s] || 'bg-slate-300'; },
    statusBorder(s) { return { green:'border-slate-200 hover:shadow-md', yellow:'border-amber-300 bg-amber-50/30', red:'border-red-300 bg-red-50/30' }[s] || 'border-slate-200'; },
    statusText(s) { return { green:'text-slate-900', yellow:'text-amber-600', red:'text-red-600' }[s] || 'text-slate-900'; },
    refreshData() { this.loading = true; setTimeout(() => { this.loading = false; this.lastUpdated = new Date().toLocaleTimeString('ko-KR'); }, 500); },

    // ══════════════════════════════
    // HTML 내보내기 (전체 페이지)
    // ══════════════════════════════
    async exportPageHtml() {
      const d = this.data;
      const period = this.reportPeriod;
      const esc = (s) => String(s || '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
      const nl2br = (s) => esc(s).replace(/\n/g, '<br>');
      const absUrl = (u) => { if (!u) return ''; return u.startsWith('http') ? u : location.origin + u; };

      // 파일을 base64 Data URI로 변환 (HTML에 내장하여 서버 없이도 동작)
      const toDataUri = async (url) => {
        try {
          const res = await fetch(absUrl(url));
          if (!res.ok) return absUrl(url);
          const blob = await res.blob();
          return new Promise((resolve) => {
            const reader = new FileReader();
            reader.onloadend = () => resolve(reader.result);
            reader.onerror = () => resolve(absUrl(url));
            reader.readAsDataURL(blob);
          });
        } catch { return absUrl(url); }
      };

      // 현재 필터 기준 안건의 첨부파일만 base64 변환
      const fileCache = new Map();
      const meetingItemsForCache = this.meetingTab === 'past'
        ? this.getPastMeetings()
        : this.getCurrentMeetings();
      const allImages = meetingItemsForCache.flatMap(item => item.images || []);
      await Promise.all(allImages.map(async (img) => {
        if (img.url && !fileCache.has(img.url)) {
          fileCache.set(img.url, await toDataUri(img.url));
        }
      }));
      const fileUri = (url) => fileCache.get(url) || absUrl(url);

      // 마크다운 간이 변환
      const md = (s) => esc(s)
        .replace(/^## (.+)$/gm, '<h4 class="md-h">$1</h4>')
        .replace(/^- (.+)$/gm, '<li>$1</li>')
        .replace(/\n/g, '<br>');

      // ── 지표 섹션 ──
      let metricsHtml = '';
      d.metricSections.forEach(sec => {
        let cards = '';
        sec.metrics.forEach(m => {
          const st = m.status || 'green';
          const trendVal = Number(m.trend) || 0;
          const trendDir = trendVal > 0 ? '↑ +' : trendVal < 0 ? '↓ ' : '→ ';
          const trendCls = trendVal > 0 ? 'trend-up' : trendVal < 0 ? 'trend-down' : 'trend-flat';
          cards += `
            <div class="metric-card metric-${st}">
              <div class="metric-header">
                <span class="metric-label">${esc(m.label)}</span>
                <span class="status-dot dot-${st}"></span>
              </div>
              <div class="metric-value">${esc(m.value)}</div>
              ${m.subLabel ? `<div class="metric-sub">${esc(m.subLabel)}</div>` : ''}
              ${m.prevValue ? `<div class="metric-trend"><span class="${trendCls}">${trendDir}${Math.abs(trendVal)}%</span> <span class="metric-vs">전주 대비</span></div>` : ''}
              ${m.detail ? `<div class="metric-detail">${esc(m.detail)}</div>` : ''}
              ${m.prevValue ? `<div class="metric-prev">비교: ${esc(m.prevValue)}</div>` : ''}
            </div>`;
        });
        metricsHtml += `
          <section class="report-section">
            <div class="section-header">
              <span class="section-num blue">${esc(sec.number)}</span>
              <h2 class="section-title">${esc(sec.title)}</h2>
              <span class="section-sub">${esc(sec.subtitle)}</span>
            </div>
            <div class="metric-grid">${cards}</div>
          </section>`;
      });

      // ── 회의 안건 (현재 필터 상태 반영) ──
      const meetingItems = this.meetingTab === 'past'
        ? this.getPastMeetings()
        : this.getCurrentMeetings();
      let meetingHtml = '';
      if (meetingItems.length > 0) {
        // 테이블 요약
        let tableRows = '';
        meetingItems.forEach((item, i) => {
          tableRows += `
            <tr>
              <td class="td-num">${i + 1}</td>
              <td class="td-project">${esc(item.project) || '—'}</td>
              <td>${esc(item.topic) || '—'}</td>
              <td class="td-center">${item.author ? `<span class="badge badge-indigo">${esc(item.author)}</span>` : '—'}</td>
              <td class="td-center">${item.detail && item.detail.trim() ? '<span class="badge badge-blue">작성</span>' : '—'}${item.images && item.images.length > 0 ? ` <span class="badge badge-slate">📎${item.images.length}</span>` : ''}</td>
              <td class="td-center">${item.ceoComment && item.ceoComment.trim() ? '<span class="badge badge-violet">있음</span>' : '—'}</td>
              <td class="td-center">${item.required ? '<span class="badge badge-red">필수</span>' : ''}</td>
            </tr>`;
        });

        // 각 안건 상세
        let details = '';
        meetingItems.forEach((item, i) => {
          let imagesHtml = '';
          if (item.images && item.images.length > 0) {
            imagesHtml = '<div class="mtg-images">';
            item.images.forEach(img => {
              if (img.isImage !== false) {
                imagesHtml += `<img src="${fileUri(img.url)}" alt="${esc(img.name)}" class="mtg-img">`;
              } else {
                imagesHtml += `<a href="${fileUri(img.url)}" download="${esc(img.name)}" class="file-link">📎 ${esc(img.name)}</a>`;
              }
            });
            imagesHtml += '</div>';
          }
          details += `
            <div class="mtg-detail-card">
              <div class="mtg-detail-header">
                <span class="mtg-num">${i + 1}</span>
                <div class="mtg-title-group">
                  <span class="mtg-project">${esc(item.project) || '(프로젝트명)'}</span>
                  <span class="mtg-topic">${esc(item.topic) || '(주제)'}</span>
                </div>
                <div class="mtg-badges">
                  ${item.author ? `<span class="badge badge-indigo">${esc(item.author)}</span>` : ''}
                  ${item.required ? '<span class="badge badge-red">필수 열람</span>' : ''}
                </div>
              </div>
              ${item.detail && item.detail.trim() ? `<div class="mtg-content">${md(item.detail)}</div>` : ''}
              ${imagesHtml}
              ${item.ceoComment && item.ceoComment.trim() ? `<div class="ceo-comment"><div class="ceo-label">대표님 코멘트</div><div class="ceo-text">${nl2br(item.ceoComment)}</div></div>` : ''}
            </div>`;
        });

        meetingHtml = `
          <section class="report-section">
            <div class="section-header">
              <span class="section-num violet">MTG</span>
              <h2 class="section-title">${esc(d.meetingAgenda.title)}${this.meetingTab === 'past' ? ' (지난 안건)' : ' (이번 주)'}</h2>
              <span class="section-sub">${esc(d.meetingAgenda.subtitle)}</span>
            </div>
            <div class="card">
              <table class="tbl">
                <thead><tr>
                  <th class="th-num">#</th><th>프로젝트명</th><th>주제</th><th class="th-center">작성자</th><th class="th-center">상세</th><th class="th-center">코멘트</th><th class="th-center">열람</th>
                </tr></thead>
                <tbody>${tableRows}</tbody>
              </table>
            </div>
            <div class="mtg-details-wrap">${details}</div>
          </section>`;
      }

      // ── 완료 결과 (현재 필터 상태 반영) ──
      const filteredResultItems = this.filteredResults;
      let resultsHtml = '';
      if (filteredResultItems.length > 0) {
        const statusBadge = (st) => {
          const map = { active: ['진행중', 'tag-blue'], hold: ['보류', 'tag-yellow'], done: ['완료', 'tag-green'], waiting: ['대기', 'tag-gray'] };
          const [label, cls] = map[st] || map.active;
          return `<span class="status-badge ${cls}">${label}</span>`;
        };
        let rows = '';
        filteredResultItems.forEach(r => {
          const st = r.status || 'active';
          rows += `
            <tr>
              <td class="td-project">${esc(r.title)}</td>
              <td>${statusBadge(st)}</td>
              <td>${esc(r.owner)}</td>
              <td>${esc(r.focusPeriod || r.deployDate)}</td>
              <td>${esc(r.targetMetric)}</td>
              <td class="${r.achieved ? 'text-green' : 'text-red'} fw-600">${esc(r.completedDate || r.actualMetric)}</td>
              <td class="td-center"><span class="achieved-badge ${r.achieved ? 'achieved-yes' : 'achieved-no'}">${r.achieved ? '✓' : '✗'}</span></td>
              ${r.note ? `</tr><tr><td colspan="7" class="td-note">${nl2br(r.note)}</td>` : ''}
            </tr>`;
          // 일정 행 추가
          if (r.schedules && r.schedules.length > 0) {
            let schedRows = '';
            r.schedules.forEach(s => {
              schedRows += `<tr><td>${esc(s.title)}</td><td>${esc(s.date)}</td></tr>`;
            });
            rows += `<tr><td colspan="7" class="td-note"><table class="tbl tbl-sm"><thead><tr><th>일정</th><th>날짜</th></tr></thead><tbody>${schedRows}</tbody></table></td></tr>`;
          }
        });
        resultsHtml = `
          <section class="report-section">
            <div class="section-header">
              <span class="section-num blue">${String(d.metricSections.length + 1).padStart(2, '0')}</span>
              <h2 class="section-title">${esc(d.completedResults.title)}</h2>
              <span class="section-sub">${esc(d.completedResults.subtitle)}</span>
            </div>
            <div class="card">
              <table class="tbl">
                <thead><tr><th>프로젝트</th><th>상태</th><th>담당자</th><th>집중 기간</th><th>목표</th><th>완료일</th><th class="th-center">달성</th></tr></thead>
                <tbody>${rows}</tbody>
              </table>
            </div>
          </section>`;
      }

      // ── 진행 중인 프로젝트 (현재 필터 상태 반영) ──
      const filteredProjectItems = this.filteredProjects;
      let projectsHtml = '';
      if (filteredProjectItems.length > 0) {
        let cards = '';
        filteredProjectItems.forEach(p => {
          const pct = Number(p.stagePercent) || 0;
          const st = p.status || 'active';
          const statusLabel = st === 'hold' ? '보류' : st === 'done' ? '완료' : '진행중';
          const statusColor = st === 'hold' ? 'tag-yellow' : st === 'done' ? 'tag-green' : 'tag-blue';
          const qs = this._getProjectQuestions(p);
          let qHtml = '';
          qs.forEach((q, qi) => {
            const a = (p.answers && p.answers[qi]) || '';
            qHtml += `
              <div class="qa-item">
                <div class="qa-q"><span class="qa-num">Q${qi + 1}</span> ${esc(q)}</div>
                <div class="qa-a">${a ? nl2br(a) : '<span class="empty">미입력</span>'}</div>
              </div>`;
          });
          // 일정 테이블
          let schedHtml = '';
          if (p.schedules && p.schedules.length > 0) {
            let schedRows = '';
            p.schedules.forEach(s => {
              schedRows += `<tr><td>${esc(s.label)}</td><td>${esc(s.date)}</td><td>${esc(s.note)}</td></tr>`;
            });
            schedHtml = `<div class="sched-wrap"><table class="tbl tbl-sm"><thead><tr><th>일정</th><th>날짜</th><th>비고</th></tr></thead><tbody>${schedRows}</tbody></table></div>`;
          }
          // 체크리스트
          let checkHtml = '';
          if (p.checklist && p.checklist.length > 0) {
            let items = '';
            p.checklist.forEach(c => {
              items += `<div class="check-item">${c.checked ? '☑' : '☐'} ${esc(c.text)}</div>`;
            });
            checkHtml = `<div class="check-wrap">${items}</div>`;
          }
          // 첨부파일
          let attachHtml = '';
          if (p.attachments && p.attachments.length > 0) {
            let links = '';
            p.attachments.forEach(a => {
              links += `<a href="${esc(a.url)}" class="attach-link" target="_blank">${esc(a.name)}</a> `;
            });
            attachHtml = `<div class="attach-wrap">${links}</div>`;
          }
          cards += `
            <div class="project-card">
              <div class="project-title">${esc(p.title) || '(제목 없음)'} <span class="tag ${statusColor}">${statusLabel}</span></div>
              <div class="project-tags">
                ${p.section ? `<span class="tag tag-slate">${esc(p.section)}</span>` : ''}
                ${p.owner ? `<span class="tag tag-indigo">${esc(p.owner)}</span>` : ''}
                ${p.category ? `<span class="tag tag-purple">${esc(p.category)}</span>` : ''}
              </div>
              <div class="progress-wrap">
                <div class="progress-info"><span>${esc(p.stage) || ''}</span><span>${pct}%</span></div>
                <div class="progress-bar"><div class="progress-fill" style="width:${pct}%"></div></div>
              </div>
              ${qHtml ? `<div class="qa-list">${qHtml}</div>` : ''}
              ${schedHtml}${checkHtml}${attachHtml}
            </div>`;
        });
        projectsHtml = `
          <section class="report-section">
            <div class="section-header">
              <span class="section-num blue">${String(d.metricSections.length + 2).padStart(2, '0')}</span>
              <h2 class="section-title">${esc(d.activeProjects.title)}</h2>
              <span class="section-sub">${esc(d.activeProjects.subtitle)}</span>
            </div>
            <div class="project-grid">${cards}</div>
          </section>`;
      }

      // ── 최종 HTML ──
      const now = new Date();
      const dateStr = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;

      const css = `
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Noto Sans KR','Malgun Gothic',sans-serif;background:#f1f5f9;color:#1e293b;line-height:1.6;-webkit-font-smoothing:antialiased;}
.wrap{max-width:960px;margin:0 auto;padding:0 24px 48px;}

/* 헤더 */
.page-header{background:#fff;border-bottom:1px solid #e2e8f0;padding:28px 0;margin-bottom:8px;}
.page-header .inner{max-width:960px;margin:0 auto;padding:0 24px;display:flex;justify-content:space-between;align-items:center;}
.page-header h1{font-size:20px;font-weight:800;color:#0f172a;letter-spacing:-0.3px;}
.page-header .period{font-size:13px;color:#94a3b8;margin-top:3px;}
.page-header .date-badge{font-size:11px;color:#64748b;background:#f1f5f9;padding:4px 12px;border-radius:8px;}

/* 섹션 */
.report-section{margin-top:32px;}
.section-header{display:flex;align-items:center;gap:8px;margin-bottom:14px;}
.section-num{font-size:11px;font-weight:800;text-transform:uppercase;letter-spacing:1.5px;}
.section-num.blue{color:#2563eb;}
.section-num.violet{color:#7c3aed;}
.section-title{font-size:14px;font-weight:700;color:#334155;}
.section-sub{font-size:11px;color:#94a3b8;}

/* 지표 카드 */
.metric-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:12px;}
.metric-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px 16px;box-shadow:0 1px 3px rgba(0,0,0,0.04);transition:box-shadow .2s;}
.metric-green{border-color:#d1fae5;}
.metric-yellow{background:#fffbeb;border-color:#fde68a;}
.metric-red{background:#fef2f2;border-color:#fecaca;}
.metric-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;}
.metric-label{font-size:12px;color:#94a3b8;font-weight:500;}
.status-dot{width:10px;height:10px;border-radius:50%;}
.dot-green{background:#10b981;}
.dot-yellow{background:#f59e0b;}
.dot-red{background:#ef4444;}
.metric-value{font-size:26px;font-weight:800;color:#0f172a;letter-spacing:-0.5px;line-height:1.2;}
.metric-sub{font-size:11px;color:#94a3b8;margin-top:3px;}
.metric-trend{font-size:12px;margin-top:6px;display:flex;align-items:center;gap:4px;}
.trend-up{color:#059669;font-weight:600;}
.trend-down{color:#ef4444;font-weight:600;}
.trend-flat{color:#94a3b8;}
.metric-vs{font-size:10px;color:#94a3b8;}
.metric-detail{font-size:10px;color:#94a3b8;margin-top:8px;line-height:1.5;}
.metric-prev{font-size:10px;color:#cbd5e1;margin-top:4px;}

/* 카드 & 테이블 */
.card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.04);}
.tbl{width:100%;border-collapse:collapse;font-size:13px;}
.tbl thead{background:#f8fafc;}
.tbl th{text-align:left;padding:11px 14px;font-size:11px;color:#64748b;font-weight:600;text-transform:uppercase;letter-spacing:0.3px;border-bottom:1px solid #e2e8f0;}
.tbl td{padding:11px 14px;border-bottom:1px solid #f1f5f9;color:#475569;}
.th-num{width:40px;text-align:center;}
.th-center,.td-center{text-align:center;}
.td-num{text-align:center;font-weight:600;color:#94a3b8;}
.td-project{font-weight:600;color:#1e293b;}
.td-note{font-size:12px;color:#64748b;background:#f8fafc;padding:8px 14px !important;line-height:1.6;}
.text-green{color:#059669;}
.text-red{color:#ef4444;}
.fw-600{font-weight:600;}

/* 배지 */
.badge{display:inline-block;font-size:11px;padding:2px 9px;border-radius:999px;font-weight:500;line-height:1.6;}
.badge-blue{background:#dbeafe;color:#2563eb;}
.badge-indigo{background:#e0e7ff;color:#4f46e5;}
.badge-violet{background:#ede9fe;color:#7c3aed;}
.badge-red{background:#fee2e2;color:#dc2626;font-weight:600;}
.badge-slate{background:#f1f5f9;color:#64748b;}
.badge-green{background:#dcfce7;color:#16a34a;}
.achieved-badge{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:50%;font-size:13px;font-weight:700;}
.achieved-yes{background:#dcfce7;color:#059669;}
.achieved-no{background:#fee2e2;color:#ef4444;}

/* 회의 안건 상세 */
.mtg-details-wrap{margin-top:20px;display:flex;flex-direction:column;gap:16px;}
.mtg-detail-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.04);}
.mtg-detail-header{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid #f1f5f9;flex-wrap:wrap;}
.mtg-num{display:inline-flex;align-items:center;justify-content:center;width:28px;height:28px;border-radius:10px;background:#f5f3ff;color:#7c3aed;font-size:12px;font-weight:700;flex-shrink:0;}
.mtg-title-group{flex:1;min-width:0;}
.mtg-project{font-size:15px;font-weight:700;color:#0f172a;display:block;}
.mtg-topic{font-size:13px;color:#64748b;display:block;margin-top:2px;}
.mtg-badges{display:flex;gap:6px;flex-shrink:0;}
.mtg-content{padding:18px 22px;font-size:13px;line-height:1.8;color:#334155;background:#fafbfc;border-bottom:1px solid #f1f5f9;}
.mtg-content .md-h{font-size:14px;font-weight:700;color:#1e293b;margin:16px 0 6px;padding-bottom:4px;border-bottom:1px solid #e2e8f0;}
.mtg-content .md-h:first-child{margin-top:0;}
.mtg-content li{margin-left:18px;padding:2px 0;color:#475569;}
.mtg-images{padding:16px 22px;display:flex;gap:10px;flex-wrap:wrap;border-bottom:1px solid #f1f5f9;}
.mtg-img{max-width:220px;max-height:160px;border-radius:10px;border:1px solid #e2e8f0;object-fit:cover;}
.file-link{display:inline-flex;align-items:center;font-size:12px;color:#4f46e5;background:#f1f5f9;padding:8px 14px;border-radius:8px;text-decoration:none;gap:4px;}
.ceo-comment{margin:0;padding:16px 22px;background:#faf5ff;border-top:none;}
.ceo-label{font-size:11px;font-weight:700;color:#7c3aed;margin-bottom:6px;text-transform:uppercase;letter-spacing:0.5px;}
.ceo-text{font-size:13px;color:#4c1d95;line-height:1.7;}

/* 프로젝트 카드 */
.project-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;}
.project-card{background:#fff;border:1px solid #e2e8f0;border-radius:14px;padding:18px;box-shadow:0 1px 3px rgba(0,0,0,0.04);}
.project-title{font-size:15px;font-weight:700;color:#0f172a;margin-bottom:8px;}
.project-tags{display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px;}
.tag{font-size:10px;padding:3px 10px;border-radius:999px;font-weight:500;}
.tag-slate{background:#f1f5f9;color:#475569;}
.tag-indigo{background:#e0e7ff;color:#4f46e5;}
.tag-purple{background:#f3e8ff;color:#9333ea;}
.tag-yellow{background:#fef3c7;color:#b45309;}
.tag-green{background:#dcfce7;color:#16a34a;}
.tag-blue{background:#dbeafe;color:#2563eb;}
.tag-gray{background:#f1f5f9;color:#64748b;}
.status-badge{display:inline-block;padding:2px 8px;border-radius:9999px;font-size:11px;font-weight:600;}
.tbl-sm{font-size:12px;margin-top:8px;}
.tbl-sm th,.tbl-sm td{padding:6px 10px;}
.sched-wrap{margin-top:10px;}
.check-wrap{margin-top:8px;font-size:12px;color:#475569;line-height:1.8;}
.attach-wrap{margin-top:8px;font-size:12px;}
.attach-link{color:#2563eb;text-decoration:underline;margin-right:8px;}
.progress-wrap{margin-bottom:14px;}
.progress-info{display:flex;justify-content:space-between;font-size:11px;color:#94a3b8;margin-bottom:5px;}
.progress-bar{background:#e2e8f0;border-radius:999px;height:6px;overflow:hidden;}
.progress-fill{background:linear-gradient(90deg,#6366f1,#818cf8);height:100%;border-radius:999px;transition:width .3s;}
.qa-list{border-top:1px solid #f1f5f9;padding-top:12px;}
.qa-item{margin-bottom:10px;}
.qa-q{font-size:11px;color:#64748b;margin-bottom:3px;line-height:1.5;}
.qa-num{display:inline-block;font-size:10px;font-weight:700;color:#6366f1;background:#eef2ff;padding:1px 6px;border-radius:4px;margin-right:4px;}
.qa-a{font-size:12px;color:#334155;background:#f8fafc;padding:8px 12px;border-radius:8px;line-height:1.6;}
.qa-a .empty{color:#cbd5e1;}

/* 푸터 */
.page-footer{text-align:center;padding:28px 24px;font-size:11px;color:#94a3b8;margin-top:48px;border-top:1px solid #e2e8f0;}

/* 인쇄 */
@media print{
  body{background:#fff;-webkit-print-color-adjust:exact;print-color-adjust:exact;}
  .page-header{position:static;}
  .report-section,.mtg-detail-card,.project-card,.metric-card{break-inside:avoid;}
}`;

      const html = `<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>주간 업무 보고 — ${esc(period)}</title>
<style>${css}</style>
</head>
<body>
<div class="page-header">
  <div class="inner">
    <div>
      <h1>주간 업무 보고</h1>
      <div class="period">${esc(period)}</div>
    </div>
    <span class="date-badge">${dateStr}</span>
  </div>
</div>
<div class="wrap">
${metricsHtml}
${meetingHtml}
${resultsHtml}
${projectsHtml}
</div>
<div class="page-footer">주간 업무 보고 대시보드 · 생성일 ${dateStr}</div>
</body></html>`;

      const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `주간업무보고_${dateStr}.html`;
      a.click();
      URL.revokeObjectURL(url);
    },

    // ══════════════════════════════
    // 서버 동기화
    // ══════════════════════════════

    // 서버에서 리포트 데이터 비동기 로드
    async loadFromServer() {
      try {
        const res = await _authFetch(API_BASE +'/api/report');
        if (res.status === 204) return null;
        if (!res.ok) return null;
        return await res.json();
      } catch { return null; }
    },

    // 서버에서 팀 설정 비동기 로드
    async loadTeamConfigFromServer() {
      try {
        const res = await _authFetch(API_BASE +'/api/team-config');
        if (res.status === 204) return null;
        if (!res.ok) return null;
        const data = await res.json();
        // 서버에서 받은 직후 company 6개 키 보장 (구버전 데이터/빈 객체 대비)
        return _ensureCompany(data);
      } catch { return null; }
    },

    // 초기화: 서버 데이터를 항상 우선 사용
    async init() {
      // 글로벌 에러 캡처 (한 번만 등록 — 로그인 전에도 동작)
      this._initGlobalErrorCapture();
      // 인증 토큰이 없으면 서버 로드 건너뜀 (로그인 전)
      if (!_getAuthToken()) return;
      // 공휴일 로드 — 비차단(백그라운드). 캐시 있으면 즉시, 없으면 ~수초 뒤 자동 색칠(반응형).
      // 이전엔 await로 ~2.8초 동안 전체 로드를 막았음 → 핵심 데이터 시작을 앞당김.
      this.loadHolidaysIfNeeded();

      // 상단 카드는 리포트·팀 설정·할일의 완료를 기다릴 필요가 없다.
      if (this.currentUser && this.currentUser.role === 'admin') {
        this.activeTab = 'report';
      }
      // 경영 KPI 대시보드 로드 (admin 전용 — 내부에서 가드)
      this.loadExecutiveSummary();
      // 직원(member/PM, 비legacy) — 주간 리포트 상단 카드용 데이터 (본인 출퇴근 + 오늘 회의)
      if (this.currentUser && (this.currentUser.role === 'member' || this.currentUser.role === 'pm') && this.currentUser.user_id !== 'legacy') {
        this.loadMyTodayAttendance();
        this.loadTodayMeetings();
      }
      this.loadExecStatus();
      // 결재·내 홈은 개인 로그인 필요 — 공용(legacy) 세션은 호출 안 함 (불필요 403 방지)
      // loadMyHome은 자체적으로 결재·연차를 직접 조회하므로 loadApprovals와 독립 실행
      // (loadApprovals reject 시에도 내 결재가 비지 않도록 체이닝 분리)
      if (!this.isLegacy()) {
        this.loadApprovals();
        this.loadMyHome();
      }

      // 각 결과는 도착 즉시 반영한다. 할일은 다른 화면의 로드를 막지 않는다.
      const reportTask = this.loadFromServer().then(serverReport => {
        if (serverReport) {
          this.data = serverReport;
          localStorage.setItem(STORAGE_KEY, JSON.stringify(serverReport));
          this.lastUpdated = new Date().toLocaleTimeString('ko-KR');
        } else if (this.data && this.data._lastModified) {
          _saveToServer('/api/report', this.data);
        }
      });
      const teamTask = this.loadTeamConfigFromServer().then(serverTeam => {
        if (serverTeam) {
          this.teamConfig = serverTeam;
          this.syncExecProfiles();
          localStorage.setItem(TEAM_CONFIG_KEY, JSON.stringify(serverTeam));
        } else if (this.teamConfig && this.teamConfig._version) {
          _saveToServer('/api/team-config', this.teamConfig);
        }
      });
      this.loadTodos();
      await Promise.all([reportTask, teamTask]);

      // 리포트·팀 설정을 사용하는 하단 기능은 기존 순서를 유지한다.
      this.loadCoreTree();
      this.loadProjectHierarchy();
      this.loadMemberReports();
      // 주간 체크리스트는 블록의 x-init="loadChecklist()"가 독립 로드(init 의존 제거)
      // 전사 지표 — 날짜만 설정(가벼움). 실제 조회는 아래 지연 블록에서 (워커 경쟁 방지)
      this.setMetricsRange('1w');

      // 3) 무거운 백그라운드 작업은 초기 렌더 이후로 지연 + 시차 (워커 2개 동시 점유 방지)
      //    - 지표 자동 집계(외부 API): 서버 15분 캐시라 대부분 즉시
      setTimeout(() => { try { this.fetchMetrics(); } catch (e) {} }, 4000);
      //    - Linear 동기화: 마지막 동기화 15분 경과 시에만 (주기적), 지표와 시차
      setTimeout(() => {
        try {
          const last = +(localStorage.getItem('_coreTreeLastSync') || 0);
          if (Date.now() - last > 15 * 60 * 1000) {
            localStorage.setItem('_coreTreeLastSync', String(Date.now()));
            this.syncCoreTreeFromLinear(true);
          }
        } catch (e) { this.syncCoreTreeFromLinear(true); }
      }, 10000);

      // 5) 필터 상태 변경 감지 → localStorage 자동 저장
      this.$watch('meetingFilterImportance', () => this.saveMeetingFilters());
      this.$watch('meetingFilterDiscussed', () => this.saveMeetingFilters());
      this.$watch('meetingFilterPinned', () => this.saveMeetingFilters());

      // 6) 30초마다 서버 데이터 자동 동기화
      const self = this;
      setInterval(() => {
        if (!document.hidden && !window._syncingFromServer && !window._linearSyncLock) {
          _syncFromServer(self);
        }
      }, 30000);

      // 7) 인앱 알림 — 초기 로드 + 60초 폴링 (legacy 제외)
      if (this.currentUser && !this.isLegacy() && this.currentUser.name) {
        this.loadNotifications();
        this.notif._timer = setInterval(() => {
          if (!document.hidden) this.loadNotifications();
        }, 60000);
      }
    },
  };

  // 서버 PUT 헬퍼 (비동기, UI 블로킹 없음, 인증 포함, 안건 병합 동기화)
  function _saveToServer(endpoint, data, instance) {
    _authFetch(API_BASE + endpoint, {
      method: 'PUT',
      headers: _authHeaders(),
      body: JSON.stringify(data),
    }).then(async resp => {
      if (resp.status === 409 && instance && endpoint === '/api/report') {
        // 충돌 발생 → 서버 최신 데이터 자동 로드
        console.log('[sync] 충돌 감지 — 서버 최신 데이터 자동 로드');
        _syncFromServer(instance);
        return;
      }
      // 서버가 동시 편집을 감지해 항목을 병합했으면, 병합 결과로 로컬 목록을 맞춤
      if (resp.ok && instance && endpoint === '/api/report') {
        try {
          const result = await resp.json();
          if (result && result.merged && result.report) {
            // 리스트형 섹션만 선택 반영 → 편집 중인 다른 입력값은 보존
            const r = result.report, d = instance.data;
            if (r.meetingAgenda && d.meetingAgenda) d.meetingAgenda.items = r.meetingAgenda.items || [];
            if (r.directions && d.directions) d.directions.items = r.directions.items || [];
            if (r.completedResults && d.completedResults) d.completedResults.items = r.completedResults.items || [];
            if (r.activeProjects && d.activeProjects) d.activeProjects.projects = r.activeProjects.projects || [];
            if (r.teamPriorities) d.teamPriorities = r.teamPriorities;
            if (r.metricSections) d.metricSections = r.metricSections;
            if (r.kanbanColumns) d.kanbanColumns = r.kanbanColumns;
            if (r._deletedIds) d._deletedIds = r._deletedIds;
            localStorage.setItem(STORAGE_KEY, JSON.stringify(d));
            console.log('[sync] 동시 편집 감지 — 서버 병합 결과로 동기화');
          }
        } catch {}
      }
    }).catch(() => {});
  }

  // 서버에서 최신 데이터 가져와서 반영
  async function _syncFromServer(instance) {
    try {
      window._syncingFromServer = true;
      const res = await _authFetch(API_BASE + '/api/report');
      if (res.ok) {
        const serverData = await res.json();
        const serverMod = serverData._lastModified || '';
        const localMod = instance.data._lastModified || '';
        if (serverMod > localMod) {
          instance.data = serverData;
          localStorage.setItem(STORAGE_KEY, JSON.stringify(serverData));
          instance.lastUpdated = new Date().toLocaleTimeString('ko-KR') + ' (동기화)';
          console.log('[sync] 서버 데이터로 업데이트 완료');
        }
      }
    } catch {} finally {
      window._syncingFromServer = false;
    }
  }

  return instance;
}
