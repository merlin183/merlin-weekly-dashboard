#!/usr/bin/env python3
"""주간 업무 보고 대시보드 - 사내 로컬 네트워크 서버

사용법:
  python serve.py          # 기본 포트 8080
  python serve.py 3000     # 포트 지정

같은 네트워크의 다른 PC에서 접속:
  http://<이 PC의 IP>:8080
"""
import socket
import sys

import uvicorn


PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080


def get_local_ip():
    """사내 네트워크 IP 주소 가져오기"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(('8.8.8.8', 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return '127.0.0.1'


if __name__ == '__main__':
    local_ip = get_local_ip()

    print(f"\n  주간 업무 보고 대시보드 서버 시작")
    print(f"  ─────────────────────────────")
    print(f"  본인 접속:  http://localhost:{PORT}")
    print(f"  사내 공유:  http://{local_ip}:{PORT}")
    print(f"  API 문서:   http://localhost:{PORT}/docs")
    print(f"  ─────────────────────────────")
    print(f"  종료: Ctrl+C\n")

    uvicorn.run(
        "src.main:app",
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
