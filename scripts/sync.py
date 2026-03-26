"""고객 파이프라인 데이터 동기화 스크립트

Gmail API + Slack API로 원시 데이터를 수집하고,
Claude API로 지능적 파싱 후 data.json을 업데이트한다.
"""

import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import anthropic
import requests
from google.oauth2 import service_account
from googleapiclient.discovery import build

# ─── 설정 ───────────────────────────────────────
DATA_FILE = Path(__file__).parent.parent / "data.json"
GMAIL_USER = os.environ["GMAIL_IMPERSONATE"]
GMAIL_QUERIES = [
    "from:biz@searchright.net OR to:biz@searchright.net",
    "from:smyang@searchright.net OR to:smyang@searchright.net",
]
SLACK_CHANNELS = ["biz-general", "biz-list-contact"]


def load_current_data():
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {"lastSync": "2000-01-01", "data": []}


def get_gmail_service():
    sa_info = json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT"])
    creds = service_account.Credentials.from_service_account_info(
        sa_info,
        scopes=["https://www.googleapis.com/auth/gmail.readonly"],
        subject=GMAIL_USER,
    )
    return build("gmail", "v1", credentials=creds)


def fetch_gmail_messages(service, query, after_date):
    full_query = f"{query} after:{after_date.replace('-', '/')}"
    messages = []
    page_token = None

    while True:
        result = service.users().messages().list(
            userId="me", q=full_query, maxResults=100, pageToken=page_token
        ).execute()

        for msg_meta in result.get("messages", []):
            msg = service.users().messages().get(
                userId="me", id=msg_meta["id"], format="metadata",
                metadataHeaders=["From", "To", "Subject", "Date"],
            ).execute()

            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            messages.append({
                "from": headers.get("From", ""),
                "to": headers.get("To", ""),
                "subject": headers.get("Subject", ""),
                "date": headers.get("Date", ""),
                "snippet": msg.get("snippet", ""),
            })

        page_token = result.get("nextPageToken")
        if not page_token:
            break

    return messages


def fetch_slack_messages(query):
    token = os.environ["SLACK_USER_TOKEN"]
    headers = {"Authorization": f"Bearer {token}"}
    messages = []

    resp = requests.get(
        "https://slack.com/api/search.messages",
        headers=headers,
        params={"query": query, "count": 100, "sort": "timestamp"},
    )
    data = resp.json()

    if data.get("ok"):
        for match in data.get("messages", {}).get("matches", []):
            messages.append({
                "channel": match.get("channel", {}).get("name", ""),
                "user": match.get("username", ""),
                "text": match.get("text", "")[:500],
                "date": match.get("ts", ""),
            })

    return messages


def sync():
    current = load_current_data()
    last_date = max(
        (d["date"] for d in current["data"]),
        default="2000-01-01",
    )

    print(f"마지막 데이터: {last_date}")
    print(f"현재 고객 수: {len(current['data'])}")

    # ─── Gmail 수집 ─────────────────────────────
    print("\n[1/3] Gmail 검색 중...")
    gmail = get_gmail_service()
    all_emails = []
    for query in GMAIL_QUERIES:
        emails = fetch_gmail_messages(gmail, query, last_date)
        all_emails.extend(emails)
        print(f"  {query}: {len(emails)}건")

    # 중복 제거 (subject + date 기준)
    seen = set()
    unique_emails = []
    for e in all_emails:
        key = (e["subject"], e["date"])
        if key not in seen:
            seen.add(key)
            unique_emails.append(e)

    print(f"  총 고유 이메일: {len(unique_emails)}건")

    # ─── Slack 수집 ─────────────────────────────
    print("\n[2/3] Slack 검색 중...")
    all_slack = []

    # 고객사별 검색
    for client in current["data"]:
        msgs = fetch_slack_messages(client["name"])
        if msgs:
            all_slack.extend(msgs)
            print(f"  {client['name']}: {len(msgs)}건")

    # 채널별 검색
    for ch in SLACK_CHANNELS:
        msgs = fetch_slack_messages(f"in:#{ch} after:{last_date}")
        if msgs:
            all_slack.extend(msgs)
            print(f"  #{ch}: {len(msgs)}건")

    print(f"  총 Slack 메시지: {len(all_slack)}건")

    # ─── Claude API로 데이터 파싱 ───────────────
    print("\n[3/3] Claude API로 데이터 분석 중...")

    client_list = json.dumps(current["data"], ensure_ascii=False, indent=2)
    email_data = json.dumps(unique_emails[:200], ensure_ascii=False, indent=2)
    slack_data = json.dumps(all_slack[:200], ensure_ascii=False, indent=2)
    today = datetime.now().strftime("%Y-%m-%d")

    prompt = f"""당신은 서치라이트AI의 고객 파이프라인 데이터를 관리하는 전문가입니다.

아래 Gmail과 Slack 원시 데이터를 분석하여 고객 파이프라인 data.json을 업데이트해주세요.

## 현재 고객 데이터 ({len(current['data'])}개)
{client_list}

## 새로 수집된 Gmail 데이터 ({len(unique_emails)}건)
{email_data}

## 새로 수집된 Slack 데이터 ({len(all_slack)}건)
{slack_data}

## 업데이트 규칙
1. 기존 고객의 status, date, note를 최신 정보로 업데이트
2. 새로운 고객/리드 발견 시 추가
3. date는 YYYY-MM-DD 형식
4. type은 "고객" 또는 "리드"
5. owner는 기존 담당자 유지, 새 건은 이메일/슬랙에서 판단
6. note에 출처 표시 [Gmail] [Slack]
7. lastSync는 "{today}"로 갱신
8. 변경사항이 없는 고객은 그대로 유지

## 출력 형식
반드시 아래 JSON 형식만 출력하세요. 다른 텍스트는 포함하지 마세요.
{{
  "lastSync": "{today}",
  "data": [
    {{
      "name": "회사명",
      "status": "현재 상태",
      "owner": "담당자",
      "date": "YYYY-MM-DD",
      "note": "상세 내용 [출처]",
      "type": "고객 또는 리드"
    }}
  ]
}}"""

    api = anthropic.Anthropic()
    response = api.messages.create(
        model="claude-sonnet-4-20250514",
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    result_text = response.content[0].text.strip()

    # JSON 추출 (마크다운 코드블록 제거)
    if result_text.startswith("```"):
        lines = result_text.split("\n")
        result_text = "\n".join(lines[1:-1])

    try:
        updated = json.loads(result_text)
        DATA_FILE.write_text(
            json.dumps(updated, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"\n완료! {len(updated['data'])}개 고객사 데이터 업데이트됨")
    except json.JSONDecodeError as e:
        print(f"\nClaude 응답 파싱 실패: {e}")
        print(f"응답 앞부분: {result_text[:500]}")
        raise


if __name__ == "__main__":
    sync()
