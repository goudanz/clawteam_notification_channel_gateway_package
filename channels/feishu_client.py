import json
import time
from urllib import parse, request, error

BASE = "https://open.feishu.cn"


def http_json(method: str, url: str, body: dict | None = None, headers: dict | None = None, timeout: int = 30) -> dict:
    data = None
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json; charset=utf-8")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except error.HTTPError as e:
        raw = e.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"HTTP {e.code} {url}: {raw}")


class FeishuClient:
    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token = None
        self._expire_at = 0.0

    def tenant_access_token(self) -> str:
        if self._token and time.time() < self._expire_at - 60:
            return self._token
        url = f"{BASE}/open-apis/auth/v3/tenant_access_token/internal"
        r = http_json("POST", url, {"app_id": self.app_id, "app_secret": self.app_secret})
        if r.get("code") != 0:
            raise RuntimeError(f"get tenant_access_token failed: {r}")
        self._token = r["tenant_access_token"]
        self._expire_at = time.time() + int(r.get("expire", 7200))
        return self._token

    def get_bot_info(self) -> dict:
        token = self.tenant_access_token()
        url = f"{BASE}/open-apis/bot/v3/info"
        r = http_json("GET", url, headers={"Authorization": f"Bearer {token}"})
        if r.get("code") != 0:
            raise RuntimeError(f"get bot info failed: {r}")
        return r

    def send_text_to_chat(self, chat_id: str, text: str):
        token = self.tenant_access_token()
        q = parse.urlencode({"receive_id_type": "chat_id"})
        url = f"{BASE}/open-apis/im/v1/messages?{q}"
        payload = {
            "receive_id": chat_id,
            "msg_type": "text",
            "content": json.dumps({"text": text}, ensure_ascii=False),
        }
        r = http_json("POST", url, payload, headers={"Authorization": f"Bearer {token}"})
        if r.get("code") != 0:
            raise RuntimeError(f"send message failed: {r}")
        return r
