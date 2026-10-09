"""
라이엇 친구 접속 알리미
- 내 PC의 라이엇 클라이언트 로컬 API에서 친구 상태를 읽어
  지정한 친구의 접속 상태가 바뀌면 디스코드 웹훅으로 알려줍니다.
- 라이엇 클라이언트가 켜져 있고 로그인된 상태여야 동작합니다.

사용법
  pip install requests
  python riot_friend_notifier.py          # 실행 (처음엔 config.json 생성 후 종료)
  python riot_friend_notifier.py --dump   # 현재 보이는 친구 상태 원본 출력 (점검용)
"""
import base64
import json
import os
import sys
import time
from pathlib import Path

import requests
import urllib3

urllib3.disable_warnings()  # 로컬 API는 자체 서명 인증서를 사용함

CONFIG_PATH = Path(__file__).with_name("config.json")
LOCKFILE = Path(os.environ.get("LOCALAPPDATA", "")) / "Riot Games" / "Riot Client" / "Config" / "lockfile"

DEFAULT_CONFIG = {
    "friends": ["닉네임#태그"],          # 태그 없이 닉네임만 적어도 됨
    "discord_webhook_url": "",           # 디스코드 채널 설정 > 연동 > 웹후크에서 복사
    "interval_seconds": 10,
    "ignore_mobile": True,               # 라이엇 모바일 앱 접속은 온라인으로 치지 않음
}

PRODUCT_NAMES = {
    "valorant": "발로란트",
    "league_of_legends": "롤",
    "keystone": "라이엇 클라이언트",
    "riot_client": "라이엇 클라이언트",
    "bacon": "레전드 오브 룬테라",
}

# 롤 queueId (TFT도 product는 league_of_legends로 옴)
LOL_QUEUES = {
    "420": "솔로랭크",
    "440": "자유랭크",
    "400": "일반",
    "430": "일반",
    "480": "신속 대전",
    "490": "빠른 대전",
    "450": "칼바람",
    "900": "우르프",
    "1700": "아레나",
    "1090": "TFT 일반",
    "1100": "TFT 랭크",
    "1130": "TFT 초고속",
    "1160": "TFT 더블업",
}

VALORANT_QUEUES = {
    "competitive": "경쟁전",
    "unrated": "일반전",
    "swiftplay": "신속 플레이",
    "spikerush": "스파이크 돌격",
    "deathmatch": "데스매치",
    "ggteam": "단계적 확대",
    "hurm": "팀 데스매치",
    "premier": "프리미어",
}


def load_config():
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"설정 파일을 만들었습니다: {CONFIG_PATH}")
        print("friends와 discord_webhook_url을 채운 뒤 다시 실행하세요.")
        sys.exit(0)
    return {**DEFAULT_CONFIG, **json.loads(CONFIG_PATH.read_text(encoding="utf-8"))}


def fetch_presences():
    """라이엇 클라이언트가 꺼져 있으면 None, 켜져 있으면 presence 목록을 돌려줍니다."""
    try:
        # lockfile 형식: 이름:PID:포트:비밀번호:프로토콜 (클라이언트를 켤 때마다 바뀜)
        _, _, port, password, _ = LOCKFILE.read_text().strip().split(":")
        r = requests.get(
            f"https://127.0.0.1:{port}/chat/v4/presences",
            auth=("riot", password),
            verify=False,
            timeout=5,
        )
        r.raise_for_status()
        return r.json().get("presences", [])
    except (OSError, ValueError, requests.RequestException):
        return None


def matches(presence, target):
    name = (presence.get("game_name") or "").lower()
    tag = (presence.get("game_tag") or "").lower()
    target = target.strip().lower()
    if "#" in target:
        t_name, t_tag = target.rsplit("#", 1)
        return name == t_name and tag == t_tag
    return name == target


def decode_private(presence):
    """presence의 private 필드(base64 JSON)를 dict로. 없거나 형식이 다르면 빈 dict."""
    try:
        data = json.loads(base64.b64decode(presence.get("private") or ""))
        if isinstance(data, str):  # 롤은 JSON 문자열을 한 번 더 감싸서 보냄
            data = json.loads(data)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def game_detail(presence):
    """게임 중이면 '솔로랭크 게임 중' 같은 설명, 아니면 None. 로비/큐/챔피언 선택은 무시."""
    product = presence.get("product")
    info = decode_private(presence)
    if product == "league_of_legends":
        if info.get("gameStatus") != "inGame":
            return None
        queue = LOL_QUEUES.get(str(info.get("queueId", ""))) or info.get("gameQueueType") or "사용자 설정"
        return f"{queue} 게임 중"
    if product == "valorant":
        match = info.get("matchPresenceData", info)  # 버전에 따라 한 단계 안에 들어 있음
        if match.get("sessionLoopState") != "INGAME":
            return None
        queue_id = match.get("queueId") or ""
        return f"{VALORANT_QUEUES.get(queue_id) or queue_id or '사용자 설정'} 게임 중"
    return None


def current_status(presences, friend, ignore_mobile):
    """친구가 접속 중인 게임 이름(게임 중이면 큐 종류 포함)들의 집합. 비어 있으면 오프라인."""
    products = set()
    for p in presences:
        if not matches(p, friend):
            continue
        state = (p.get("state") or "").lower()
        if state == "offline" or (ignore_mobile and state == "mobile"):
            continue
        product = p.get("product") or "unknown"
        name = PRODUCT_NAMES.get(product, product)
        detail = game_detail(p)
        products.add(f"{name} {detail}" if detail else name)
    return frozenset(products)


def notify(webhook_url, text):
    print(time.strftime("[%H:%M:%S]"), text)
    if not webhook_url:
        return
    try:
        requests.post(webhook_url, json={"content": text}, timeout=5)
    except requests.RequestException as e:
        print("  디스코드 전송 실패:", e)


def main():
    if "--dump" in sys.argv:
        presences = fetch_presences()
        if presences is None:
            print("라이엇 클라이언트에 연결할 수 없습니다. 켜져 있고 로그인돼 있는지 확인하세요.")
        else:
            for p in presences:
                print(f'{p.get("game_name")}#{p.get("game_tag")}  product={p.get("product")}  state={p.get("state")}')
        return

    cfg = load_config()
    friends = [f for f in cfg["friends"] if f.strip()]
    last = {f: None for f in friends}  # None = 아직 모름 (첫 조회 때는 알림 없이 기록만)
    client_up = None
    announced = False  # 실행 후 첫 조회 결과는 디스코드로도 한 번 보냄
    print(f"감시 시작: {', '.join(friends)}  ({cfg['interval_seconds']}초 간격, Ctrl+C로 종료)")

    while True:
        presences = fetch_presences()
        if presences is None:
            if client_up is not False:
                print("라이엇 클라이언트 대기 중...")
            client_up = False
            last = {f: None for f in friends}  # 다시 켜지면 기준 상태부터 새로 잡음
        else:
            client_up = True
            startup = []
            for f in friends:
                now = current_status(presences, f, cfg["ignore_mobile"])
                before = last[f]
                if before is None:
                    print(f"  {f}: {'온라인 (' + ', '.join(sorted(now)) + ')' if now else '오프라인'}")
                    if not announced:
                        startup.append(f"🟢 **{f}** 접속 중 ({', '.join(sorted(now))})" if now else f"⚫ **{f}** 오프라인")
                elif now != before:
                    if now and not before:
                        notify(cfg["discord_webhook_url"], f"🟢 **{f}** 접속! ({', '.join(sorted(now))})")
                    elif before and not now:
                        notify(cfg["discord_webhook_url"], f"⚫ **{f}** 오프라인")
                    else:
                        notify(cfg["discord_webhook_url"], f"🔄 **{f}** 상태 변경: {', '.join(sorted(now))}")
                last[f] = now
            if not announced:
                notify(cfg["discord_webhook_url"], "\n".join(startup))
                announced = True
        time.sleep(cfg["interval_seconds"])


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n종료합니다.")
