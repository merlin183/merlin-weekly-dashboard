// 주간 업무 보고 대시보드 — 코어 유틸 (인증/fetch/첨부).
// ⚠️ app.js 보다 먼저 로드되어야 함 (전역 함수/상수 — app.js·index.html 인라인 Alpine 식이 참조).

// 프로필 표시 정책: 기본은 모두 표시. 설치자 설정은 화면 목록에만 적용하며 원본·이력은 보존한다.
window.dashboardProfileVisibility = (() => {
  function profileName(person) {
    if (typeof person === 'string') return person.trim();
    if (!person || typeof person !== 'object') return '';
    return String(person.name || person.user_name || person.owner || '').trim();
  }
  function isVisibleProfile(person, policy = {}) {
    const hiddenNames = Array.isArray(policy?.hiddenNames) ? policy.hiddenNames : [];
    return !hiddenNames.some(name => profileName(name) === profileName(person));
  }
  function visibleProfiles(people, policy = {}) {
    return Array.isArray(people) ? people.filter(person => isVisibleProfile(person, policy)) : [];
  }
  function memberOptions(members, selectedName = '', policy = {}) {
    const options = visibleProfiles(members, policy);
    // 기존 거래의 담당자 표시는 보존한다. 새 선택 목록에만 숨김 정책을 적용한다.
    if (selectedName && !options.includes(selectedName)) options.push(selectedName);
    return options;
  }
  return Object.freeze({ isVisibleProfile, visibleProfiles, memberOptions });
})();

// ── 인증 헬퍼 (전역) ──
const AUTH_TOKEN_KEY = '_dashboard_auth_token';

function _getAuthToken() {
  return localStorage.getItem(AUTH_TOKEN_KEY) || '';
}

function _authHeaders(extra = {}) {
  const token = _getAuthToken();
  return { 'Content-Type': 'application/json', ...(token ? { 'Authorization': 'Bearer ' + token } : {}), ...extra };
}

// 인증 포함 fetch 래퍼
async function _authFetch(url, options = {}) {
  const token = _getAuthToken();
  if (!token) {
    // 토큰 없으면 요청 자체를 차단 (무한 리로드 방지)
    throw new Error('인증 필요');
  }
  if (!options.headers) options.headers = {};
  options.headers['Authorization'] = 'Bearer ' + token;
  const resp = await fetch(url, options);
  if (resp.status === 401) {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    location.reload();
    throw new Error('인증 만료');
  }
  return resp;
}

// 인증된 프록시의 다운로드 정책을 유지한다. 활성 문서는 앱 origin의 Blob 탭으로 열지 않는다.
async function openAttachment(url, event) {
  if (event) event.preventDefault();
  if (!url) return false;
  const proxyUrl = '/api/proxy-file?url=' + encodeURIComponent(url);
  try {
    const response = await _authFetch(proxyUrl);
    if (!response.ok) throw new Error('파일 로드 실패');
    const type = (response.headers.get('content-type') || '').split(';')[0].trim().toLowerCase();
    const disposition = response.headers.get('content-disposition') || '';
    const previewTypes = new Set([
      'image/png', 'image/jpeg', 'image/gif', 'image/webp', 'image/avif', 'image/bmp',
      'image/x-icon', 'image/vnd.microsoft.icon', 'application/pdf', 'text/plain',
    ]);
    const download = /^\s*attachment\b/i.test(disposition) || !previewTypes.has(type);
    const blob = await response.blob();
    const blobUrl = URL.createObjectURL(new Blob([blob], { type: download ? 'application/octet-stream' : type }));
    try {
      if (download) {
        // filename* (RFC 5987)를 우선하며, 파일명은 경로·제어문자 없이 사용한다.
        const encoded = disposition.match(/filename\*\s*=\s*UTF-8''([^;]+)/i);
        const plain = disposition.match(/filename\s*=\s*(?:"([^"]*)"|([^;]+))/i);
        let name = plain ? (plain[1] || plain[2] || '').trim() : 'attachment';
        if (encoded) {
          try { name = decodeURIComponent(encoded[1].trim()); } catch { /* 일반 파일명 사용 */ }
        }
        name = name.replace(/[\u0000-\u001f\u007f/\\]/g, '_').replace(/^\.+/, '') || 'attachment';
        const link = document.createElement('a');
        link.href = blobUrl;
        link.download = name;
        document.body.appendChild(link);
        try { link.click(); } finally { link.remove(); }
      } else {
        window.open(blobUrl, '_blank', 'noopener,noreferrer');
      }
    } finally {
      // 새 탭·다운로드가 URL을 소비할 시간을 주고 메모리를 회수한다.
      setTimeout(() => URL.revokeObjectURL(blobUrl), 60000);
    }
    return true;
  } catch {
    if (typeof window.alert === 'function') window.alert('첨부파일을 불러오지 못했습니다. 로그인 상태와 파일 접근 권한을 확인해주세요.');
    return false;
  }
}

// 이전 호환용
function proxyUrl(url) { return url; }
