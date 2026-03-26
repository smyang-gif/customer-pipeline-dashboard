#!/usr/bin/env python3
"""고객 파이프라인 대시보드 — 로컬 서버

사용법:
    python3 server.py          # http://localhost:3000 에서 대시보드 실행
    python3 server.py 8080     # 포트 지정
"""

import http.server
import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
BASE_DIR = Path(__file__).parent
DATA_FILE = BASE_DIR / "data.json"


class DashboardHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(BASE_DIR), **kwargs)

    def do_GET(self):
        path = urlparse(self.path).path

        if path == "/api/data":
            self._serve_json()
        elif path == "/" or path == "":
            self.path = "/index.html"
            super().do_GET()
        else:
            super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path

        if path == "/api/refresh":
            self._handle_refresh()
        elif path == "/api/save":
            self._handle_save()
        else:
            self.send_error(404)

    def _serve_json(self):
        try:
            data = DATA_FILE.read_text(encoding="utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data.encode("utf-8"))
        except FileNotFoundError:
            self.send_error(404, "data.json not found")

    def _handle_save(self):
        """클라이언트에서 수정한 데이터를 data.json에 저장"""
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            payload = json.loads(body)
            DATA_FILE.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            self._json_response({"ok": True})
        except (json.JSONDecodeError, IOError) as e:
            self._json_response({"ok": False, "error": str(e)}, status=400)

    def _handle_refresh(self):
        """Claude CLI를 호출하여 Gmail/Slack에서 최신 데이터를 가져온다"""
        try:
            current = json.loads(DATA_FILE.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError):
            current = {"lastSync": "2000-01-01", "data": []}

        latest_date = max(
            (d["date"] for d in current["data"]),
            default="2000-01-01",
        )

        prompt = f"""고객 파이프라인 대시보드를 새로고침해줘.

데이터 파일: {DATA_FILE}

마지막 동기화 날짜: {latest_date}
이 날짜 이후의 Gmail과 Slack 데이터를 검색해서 data.json을 업데이트해줘.

검색 전략 (중요 — 누락 방지):
1. Gmail 전체 스캔: "from:biz@searchright.net OR to:biz@searchright.net after:{latest_date.replace('-','/')}" maxResults:100, nextPageToken으로 페이지네이션 완료
2. Gmail 보조 스캔: "from:smyang@searchright.net OR to:smyang@searchright.net after:{latest_date.replace('-','/')}" maxResults:100
3. Slack 회사별 검색: 기존 고객사 이름으로 개별 검색 (AND 로직 주의 — 회사명 단독 검색)
4. Slack 채널 스캔: #biz-general, #biz-list-contact after:{latest_date}
5. 기존 고객의 상태/날짜 업데이트 + 신규 고객 추가
6. lastSync 날짜도 오늘로 갱신

주의사항:
- Gmail 검색 시 반드시 nextPageToken 따라가며 전체 결과 수집
- Slack 검색 시 회사명만 단독으로 검색 (복합 키워드 AND 조합 금지)
- 새 회사 발견 시 개별 Gmail/Slack 검색으로 상세 정보 수집
- 결과를 {DATA_FILE} 에 JSON 형식으로 저장 (기존 형식 유지)

현재 고객 목록 ({len(current['data'])}개):
{chr(10).join('- ' + d['name'] for d in current['data'])}
"""

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

        # SSE: 진행 상황을 실시간으로 전달
        def send_event(event, data):
            msg = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
            self.wfile.write(msg.encode("utf-8"))
            self.wfile.flush()

        send_event("status", {"message": "Claude CLI 호출 중..."})

        try:
            result = subprocess.run(
                ["claude", "-p", prompt, "--output-format", "text"],
                capture_output=True,
                text=True,
                timeout=300,
                cwd=str(BASE_DIR),
            )

            if result.returncode == 0:
                # Claude가 data.json을 직접 수정했으므로 다시 읽기
                try:
                    updated = json.loads(DATA_FILE.read_text(encoding="utf-8"))
                    send_event("complete", {
                        "message": f"동기화 완료 — {len(updated['data'])}개 고객사",
                        "count": len(updated["data"]),
                    })
                except (FileNotFoundError, json.JSONDecodeError):
                    send_event("complete", {
                        "message": "동기화 완료 (데이터 파일 확인 필요)",
                    })
            else:
                send_event("error", {
                    "message": f"Claude CLI 오류: {result.stderr[:500]}",
                })
        except FileNotFoundError:
            send_event("error", {
                "message": "Claude CLI를 찾을 수 없습니다. 'npm install -g @anthropic-ai/claude-code'로 설치하세요.",
            })
        except subprocess.TimeoutExpired:
            send_event("error", {
                "message": "Claude CLI 응답 시간 초과 (5분). 다시 시도해주세요.",
            })

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        print(f"[Dashboard] {args[0]}")


if __name__ == "__main__":
    server = http.server.HTTPServer(("0.0.0.0", PORT), DashboardHandler)
    print(f"\n  고객 파이프라인 대시보드")
    print(f"  http://localhost:{PORT}\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n서버 종료")
        server.server_close()
