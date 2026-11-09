"""Generic editable checklist returned only when an installation has no saved checklist.

Saved sections, week entries and historical records always take precedence.
"""


def _section(identifier, title, items):
    return {"id": identifier, "title": title, "owner": "", "purpose": "",
            "items": [{"id": f"{identifier}i{index + 1}", "text": text}
                      for index, text in enumerate(items)]}


WEEKLY_CHECKLIST_SEED = {
    "sections": [
        _section("s1", "주간 목표", ["목표 진행 상황 확인", "다음 주 우선순위 확인"]),
        _section("s2", "서비스 운영", ["서비스 정상 동작 확인", "고객 문의 및 개선 사항 확인"]),
        _section("s3", "안정성 및 보안", ["백업 상태 확인", "계정 권한 및 보안 설정 확인"]),
    ],
    "weeks": {},
}
