#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
中育云笔记客户端（只读）

以 下载云笔记.py 为基础，三处关键差异：

  1. HTTP 走标准库 urllib，并【显式禁用代理】。
     urllib 默认会读取 HTTP_PROXY / HTTPS_PROXY 环境变量，把请求交给代理；
     明明能直连的服务器会因此返回 502，看着像"必须先挂代理"。这里用
     ProxyHandler({}) 强制直连，不吃环境变量的亏。

  2. 资源列表返回【完整原始字段】。
     保留 md5 / updateTimeStamp / pageIndex 等，供上层判断"内容有没有变"，
     而不只是数页数。

  3. 密钥按【当天日期】动态生成，并带跨天自动重建。
     原脚本在 __init__ 里一次性建好 cipher；常驻进程跨过午夜后密钥就错了。

只读：本模块不调用任何 AddOrUpdate / 上传 / 写入接口。
"""

import argparse
import base64
import datetime
import json
import os
import sys
import urllib.error
import urllib.request

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# 2026-10-17：不再硬编码服务地址。开源项目不该绑定某个特定服务，
# 由用户在设置界面填写，存在凭据文件里。
DEFAULT_API_BASE = ""
OSS_FALLBACK = "http://friday-note.oss-cn-hangzhou.aliyuncs.com/"

# 强制直连：显式禁用一切代理（http/https/ftp...）
_DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class ZhongYuError(RuntimeError):
    """本模块统一抛出的业务异常。"""


def creds_path() -> str:
    """凭据文件位置：%LOCALAPPDATA%\\ZhongYuToolBox\\zy_credentials.json"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ZhongYuToolBox", "zy_credentials.json")


def state_path() -> str:
    """快照文件位置（供增量比对用）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "ZhongYuToolBox", "zy_snapshot.json")


def load_credentials() -> dict:
    p = creds_path()
    if not os.path.exists(p):
        raise ZhongYuError(
            "找不到凭据文件：" + p +
            "\n请把 zy_credentials.example.json 复制到该位置并填好账号密码。"
        )
    with open(p, "r", encoding="utf-8") as f:
        creds = json.load(f)
    if not creds.get("userName") or not creds.get("password"):
        raise ZhongYuError("凭据文件里的 userName / password 还没填：" + p)
    # 服务地址：凭据文件优先，其次 config.json，再其次环境变量 ZY_API_BASE。
    # 这样即使凭据文件被重置，也不会因为少一个字段就整个罢工。
    if not creds.get("apiBase"):
        try:
            cfgp = os.path.join(os.path.dirname(p), "config.json")
            with open(cfgp, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("apiBase"):
                creds["apiBase"] = cfg["apiBase"]
        except Exception:
            pass
    if not creds.get("apiBase"):
        creds["apiBase"] = os.environ.get("ZY_API_BASE", "").strip()
    if not creds.get("apiBase"):
        raise ZhongYuError(
            "没有服务器地址（apiBase）。三处都没有：凭据文件、config.json、环境变量 ZY_API_BASE。\n"
            "请在设置界面里填写，或运行：set ZY_API_BASE=https://你的地址")
    return creds


# ------------------------------------------------------------------ token 缓存
# 2026-10-18：服务端发的 accessToken 是 JWT，实测有效期 **2 小时**。
# 以前每轮巡检都重新登录（1 分钟频率 = 1440 次/天），完全是浪费，也很容易撞风控。
# 现在把 token 存本地，2 小时内复用，登录次数降到 ~12 次/天。
# 过期的兜底：_authed_get() 遇到 401 会自动 login() 再试一次，不会卡死。

TOKEN_CACHE = os.path.join(os.path.dirname(creds_path()), "token_cache.json")


def jwt_exp(tok):
    """从 JWT 里取 exp（Unix 秒）；解析失败返回 0"""
    try:
        p = str(tok).split(".")[1]
        p += "=" * (-len(p) % 4)
        return int(json.loads(base64.urlsafe_b64decode(p)).get("exp") or 0)
    except Exception:
        return 0


def load_cached_token(user_name, api_base):
    """读缓存的 accessToken；账号或服务器不匹配、或者快过期了，就返回 None"""
    try:
        with open(TOKEN_CACHE, encoding="utf-8") as f:
            d = json.load(f)
        if (d.get("userName") != user_name) or (d.get("apiBase") != api_base):
            return None
        tok = d.get("token") or ""
        exp = int(d.get("exp") or jwt_exp(tok))
        # 留 10 分钟余量，别用着用着正好过期
        if tok and exp - int(datetime.datetime.now().timestamp()) > 600:
            return tok
    except Exception:
        pass
    return None


def save_cached_token(user_name, api_base, tok):
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE), exist_ok=True)
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"userName": user_name, "apiBase": api_base,
                       "token": tok, "exp": jwt_exp(tok),
                       "savedAt": datetime.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def aes_key(now=None) -> bytes:
    """
    复刻网页端 generateAesKey：按当天日期动态生成 16 位密钥。

    原式：head(1) + 按日期旋转后取 14 位 + 当日字符(1) = 16 位
    """
    e = ":F0wKU!Qg3}UkbW+w[:9|D3-5h=:T;7t#_GZ4#G;~ZNSq{8;}QIP>'{q.lje"
    t = now or datetime.datetime.now()
    y, m, d = t.year, t.month, t.day
    c = (y * m * d) % len(e)
    head = chr((33 + d * m * 33) % 94 + 33)
    tail = e[d + m]
    return (head + (e[c:] + e[:c])[:14] + tail).encode()


class ZhongYuClient:
    """中育云笔记只读客户端。"""

    def __init__(self, api_base: str = "", user_name: str = "",
                 password: str = ""):
        self.api_base = (api_base or DEFAULT_API_BASE or "").rstrip("/")
        if not self.api_base:
            raise ZhongYuError("还没配置服务器地址（apiBase），请运行设置界面填写")
        self.user_name = user_name
        self.password = password
        self.token = None
        self.real_name = ""
        self._cipher_day = None
        self._cipher = None

    # ------------------------------------------------------------------ 加解密

    def _get_cipher(self):
        """当天的 AES-ECB cipher，跨天自动重建。"""
        today = datetime.date.today()
        if self._cipher_day != today or self._cipher is None:
            self._cipher_day = today
            self._cipher = AES.new(aes_key(), AES.MODE_ECB)
        return self._cipher

    def enc(self, s: str) -> str:
        ct = self._get_cipher().encrypt(pad(s.encode("utf-8"), 16))
        return base64.b64encode(ct).decode()

    def dec(self, s: str) -> str:
        pt = self._get_cipher().decrypt(base64.b64decode(s))
        return unpad(pt, 16).decode("utf-8")

    # ------------------------------------------------------------------ HTTP

    def _request(self, url, method="GET", body=None, headers=None, timeout=30):
        h = {"User-Agent": _UA, "Accept": "application/json, text/plain, */*"}
        if headers:
            h.update(headers)
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            h["Content-Type"] = "application/json; charset=UTF-8"
        req = urllib.request.Request(url, data=data, headers=h, method=method)
        try:
            with _DIRECT_OPENER.open(req, timeout=timeout) as resp:
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace")
        except urllib.error.URLError as e:
            raise ZhongYuError("网络不可达（已强制直连、未走代理）：%s\n%s"
                               % (url, e.reason)) from e

    def _auth_headers(self):
        return {"Authorization": "Bearer " + (self.token or "")}

    def _authed_get(self, url, timeout=60):
        """带 401 自动重登一次的 GET。"""
        status, text = self._request(url, headers=self._auth_headers(), timeout=timeout)
        if status == 401:
            self.login()
            save_cached_token(self.user_name, self.api_base, self.token)   # 换新 token 也存一份
            status, text = self._request(url, headers=self._auth_headers(), timeout=timeout)
        if status != 200:
            raise ZhongYuError("请求失败 HTTP %s：%s" % (status, text[:200]))
        return text

    # ------------------------------------------------------------------ 业务

    def login(self):
        status, text = self._request(
            self.api_base + "/api/TokenAuth/Login",
            method="POST",
            body={"userName": self.user_name,
                  "password": self.password,
                  "clientType": 1},
            timeout=30,
        )
        try:
            data = json.loads(text)
        except Exception:
            raise ZhongYuError("登录返回不是 JSON（HTTP %s）：%s" % (status, text[:200]))
        if not data.get("result"):
            err = data.get("error") or {}
            raise ZhongYuError("登录失败：" + (err.get("message") or text[:200]))
        self.token = data["result"]["accessToken"]
        self.real_name = (
            data["result"].get("user", {}).get("name")
            or data["result"].get("realName") or ""
        )
        return data["result"]

    def whoami(self):
        """取一次真实姓名（可选，失败不影响主流程）。"""
        try:
            text = self._authed_get(
                self.api_base + "/api/services/app/User/GetInfoAsync", timeout=30)
            self.real_name = (json.loads(text).get("result") or {}).get("realName", "")
        except Exception:
            pass
        return self.real_name

    def get_all_notes(self):
        """全部云笔记。返回 noteList（原始字段）。"""
        text = self._authed_get(
            self.api_base + "/CloudNotes/api/Notes/GetAll", timeout=90)
        raw = json.loads(text).get("data")
        if raw is None:
            raise ZhongYuError("GetAll 未返回 data：" + text[:200])
        inner = json.loads(self.dec(raw))
        return inner.get("noteList", []) or []

    def get_resources(self, file_id: str):
        """
        某本笔记的【完整】资源列表。

        这里刻意不做任何字段裁剪——md5 / updateTimeStamp / resourceType 全留着，
        上层要靠它们判断"这页有没有被改过"，而不只是"页数有没有变"。
        """
        q = self.enc("fileId=" + file_id)
        text = self._authed_get(
            self.api_base + "/CloudNotes/api/Resources/GetByFileId?" + q, timeout=60)
        raw = json.loads(text).get("data")
        if raw is None:
            raise ZhongYuError("GetByFileId 未返回 data：" + text[:200])
        inner = json.loads(self.dec(raw))
        return inner.get("resourceList", []) or []


def build_client(creds: dict = None, do_login: bool = True) -> ZhongYuClient:
    """按凭据文件建客户端。

    do_login=True 时的策略（2026-10-18 改）：
      1. 本地有还没过期的 token（JWT 有效期 2 小时）→ 直接用，**不登录**
      2. 没有才登录，登录完把 token 存下来
    过期的兜底交给 _authed_get()：遇到 401 会自动重登一次。
    """
    creds = creds or load_credentials()
    cli = ZhongYuClient(creds.get("apiBase", DEFAULT_API_BASE),
                        creds.get("userName", ""), creds.get("password", ""))
    if do_login:
        tok = load_cached_token(cli.user_name, cli.api_base)
        if tok:
            cli.token = tok
        else:
            cli.login()
            save_cached_token(cli.user_name, cli.api_base, cli.token)
    return cli


def notes_of_interest(notes):
    """只保留云笔记本体（type 1/12），过滤掉文件夹等。"""
    return [n for n in notes if n.get("type") in (1, 12)]


_BAD_CHARS = '\\/:*?"<>|\r\n\t'


def safe_name(s) -> str:
    """把笔记名净化为合法的文件夹名（Windows 保留字符全部替换）。"""
    s = "".join("_" if c in _BAD_CHARS else c for c in str(s or "")).strip()
    s = s.rstrip(". ") or "未命名"
    return s[:80]


# ====================================================================== 自测


def _selftest():
    """离线自测：验证密钥长度、AES 往返、跨天重建。不需要账号和网络。"""
    k = aes_key()
    assert len(k) == 16, "密钥长度应为 16，实际 %d" % len(k)

    cli = ZhongYuClient()
    s = "笔记fileId=abc123&页数=3"
    assert cli.dec(cli.enc(s)) == s, "AES 往返失败"

    # 跨天：人为把 cipher 的日期改旧，下一次取用应触发重建
    cli._cipher_day = datetime.date(2000, 1, 1)
    cli._get_cipher()
    assert cli._cipher_day == datetime.date.today(), "跨天重建失败"

    # 不同日期密钥应当不同
    a = aes_key(datetime.datetime(2026, 9, 30))
    b = aes_key(datetime.datetime(2026, 10, 1))
    assert a != b, "不同日期密钥不应相同"

    # 同一日期密钥应当稳定
    assert aes_key(datetime.datetime(2026, 9, 30)) == a, "同日密钥应稳定"

    print("[自测通过] 密钥长度 16 / AES 往返正常 / 跨天自动重建 / 同日密钥稳定")
    print("          今天(%s)的密钥前 4 字节: %r" % (datetime.date.today(), k[:4]))


def _main():
    ap = argparse.ArgumentParser(description="中育云笔记只读客户端")
    ap.add_argument("--selftest", action="store_true", help="离线自测，不需要账号")
    ap.add_argument("--whoami", action="store_true", help="登录并打印身份")
    ap.add_argument("--notes", action="store_true", help="列出全部云笔记")
    ap.add_argument("--resources", metavar="FILE_ID", help="打印某本笔记的完整资源字段")
    ap.add_argument("--creds", help="凭据文件路径（默认 %%LOCALAPPDATA%%\\ZhongYuToolBox\\zy_credentials.json）")
    args = ap.parse_args()

    if args.selftest:
        _selftest()
        return

    if args.creds:
        os.environ["LOCALAPPDATA"] = os.path.dirname(os.path.dirname(args.creds))

    cli = build_client()
    print("已登录：%s（%s）" % (cli.whoami() or "未知", cli.api_base))

    if args.whoami:
        return

    if args.notes or args.resources:
        notes = notes_of_interest(cli.get_all_notes())
        print("共 %d 本云笔记" % len(notes))
        for i, n in enumerate(notes, 1):
            print("  %3d. %s  fileId=%s  type=%s  create=%s"
                  % (i, n.get("fileName"), n.get("fileId"),
                     n.get("type"), n.get("createTime")))

    if args.resources:
        res = cli.get_resources(args.resources)
        print("\n资源条数：%d" % len(res))
        if res:
            print("单条资源的全部字段（用于确认 md5 / 时间戳是否存在）：")
            print(json.dumps(res[0], ensure_ascii=False, indent=2))
            keys = set()
            for r in res:
                keys |= set(r.keys())
            print("\n所有条目出现过的字段全集：")
            print("  " + ", ".join(sorted(keys)))


if __name__ == "__main__":
    try:
        _main()
    except ZhongYuError as e:
        print("[出错] " + str(e))
        sys.exit(1)
