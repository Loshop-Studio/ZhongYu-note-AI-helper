#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
云笔记【写入】模块 —— 把一页图片作为新笔记上传回中育云。

⚠️ 这是本项目第一个会**写数据**的模块：调用成功会在用户账号里创建一本新笔记。
   默认带 `--dry-run`（只换 STS 凭证、不上传），需要显式加 `--go` 才真正写。

流程（1:1 复刻 `src/utils/oss.ts` + `src/api/pdfNote.ts`）：

    1. 取 userId（JWT 的 sub，与资源里的 userId 一致，实测 690）
    2. POST ObjectStorage/GenerateTokenV2Async 换 STS 临时凭证
         sign = MD5("{userId}+note_v2+res+2++0+{nonce}+{ts}").upper()
    3. OSS V1 签名 PUT 上传模板 bin 与页面图
         Authorization: OSS {ak}:{base64(HMAC-SHA1(sk, stringToSign))}
         + x-oss-security-token
    4. POST Resources/AddOrUpdate 写资源清单（每页 9 条）
    5. POST Notes/AddOrUpdate 建笔记（type=12）

关键认知：**云笔记存的不是 PDF，而是「每页一张图 + 8 条固定模板资源」**。
所以这里直接把渲染好的 PNG 当页面图上传，不经过 PDF 那一层。
"""

import argparse
import base64
import datetime
import hashlib
import hmac
import json
import os
import random
import sys
import urllib.error
import urllib.request
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zy_client  # noqa: E402

# 注意：reply_render 依赖 Pillow，而本模块运行在 `.venv`（只装了 pycryptodome）。
# 所以渲染器改为**用到时才导入** —— 正常情况下图片由 Python 3.14 那边渲染好，
# 本模块只负责把现成的字节传上去，不需要 PIL。

TEMPLATE_UUID = "a888b5fb-e65d-4611-a3af-1f80a0fb6ced"
# 「页面总览图」在模板里的相对路径 —— 这个位置放的就是"整页长什么样"。
# 官方笔记里它由 App 渲染生成（1920×2184 PNG），我们预先渲染好直接放进去。
SCREENSHOT_REL = TEMPLATE_UUID + "/screenshot.png"

# ⚠️ 正文图必须用这个**固定文件名**（不要改成随机 UUID）：
#   模板里的 `file.bin`（页面清单）**硬编码了这个名字**，换了名字 App 就找不到正文图，
#   编辑时会报「获取页面数据错误」。2026-10-01 实测：用固定名 = 实验E，**可正常编辑**。
IMG_FILENAME = ("B466246B6F67160E63431159941CD9A9"
                "screenCaptureb59d24b6-00fa-4f53-bc4f-1df255a5101a.webp")


# ------------------------------------------------- 本地渲染图：用完自动进回收站
#
# 用户 2026-10-01 指定：回复时在本地生成的页面图（都放在工作区 `_out/` 下）
# 属于临时产物，上传成功后**自动移入回收站**，不要越攒越多。
# ⚠️ 只对 `_out/` 目录里的文件生效 —— 别处的文件一律不动（避免误伤用户的文件）。
OUT_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "_out")


def _is_in_out_dir(path):
    """path 是否在工作区 `_out/` 目录内（含子目录）。"""
    try:
        p = os.path.abspath(path)
        return os.path.commonpath([p, OUT_DIR]) == os.path.abspath(OUT_DIR)
    except Exception:
        return False


def recycle_paths(paths):
    """
    把本地文件移入**回收站**（不直接删除），失败静默忽略。仅 Windows 有效。

    走 shell32.SHFileOperationW（wFunc=FO_DELETE + FOF_ALLOWUNDO）。
    注意：wFunc 必须是 0x3，0x10 是 FOF_NOCONFIRMATION 的标志位，绝不能与它混用。
    """
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    from ctypes import wintypes

    FO_DELETE, FOF_ALLOWUNDO = 0x3, 0x40
    FOF_NOCONFIRMATION, FOF_SILENT, FOF_NOERRORUI = 0x10, 0x4, 0x400

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [
            ("hwnd", wintypes.HWND),
            ("wFunc", ctypes.c_uint),
            ("pFrom", ctypes.c_wchar_p),
            ("pTo", ctypes.c_wchar_p),
            ("fFlags", ctypes.c_ushort),
            ("fAnyOperationsAborted", wintypes.BOOL),
            ("hNameMappings", ctypes.c_void_p),
            ("lpszProgressTitle", ctypes.c_wchar_p),
        ]

    shell32 = ctypes.windll.shell32
    shell32.SHFileOperationW.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]

    done = []
    for p in paths:
        if not p:
            continue
        p = os.path.abspath(p)
        if not os.path.exists(p):
            continue
        op = SHFILEOPSTRUCTW()
        op.wFunc = FO_DELETE
        op.pFrom = ctypes.c_wchar_p(p.rstrip("\\/") + "\0")
        op.fFlags = (FOF_ALLOWUNDO | FOF_NOCONFIRMATION | FOF_SILENT | FOF_NOERRORUI)
        op.fAnyOperationsAborted = False
        shell32.SHFileOperationW(ctypes.byref(op))
        done.append(p)
    return done
if getattr(sys, "frozen", False):
    # ⚠️ PyInstaller 打包后：__file__ 指向临时解包目录（_MEIPASS），
    #    往上两级会跑到 %TEMP%，模板根本不在那儿 —— 必须从 _MEIPASS 取。
    #    打包命令要配 `--add-data "ZhongYuToolBox_Rev;ZhongYuToolBox_Rev"`。
    TEMPLATE_BASE = os.path.join(
        getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__))),
        "ZhongYuToolBox_Rev", "public", "example")
else:
    TEMPLATE_BASE = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "ZhongYuToolBox_Rev", "public", "example")

# 每页 8 条固定模板资源（相对路径 / 固定 md5 / resourceType）
TEMPLATE_RESOURCES = [
    ("page_router.bin", "C6FFAEB070ADBEC6B886BE63587CB0F8", 1),
    (TEMPLATE_UUID + "/059848e4-1971-47fb-9e47-517266cdef05_matrix.bin",
     "5D03C5A75809ED20D24C18388BB8AB63", 1),
    (TEMPLATE_UUID + "/a2b4fb47-3623-45be-9fe9-57fc62e66651_file.bin",
     "5924B6262213683E6A4A2AFD3E4A270B", 1),
    (TEMPLATE_UUID + "/e339e39b-64d9-4de0-bfaa-dace2a3f8e7d_command.bin",
     "FEC4C90827E797E54126BB996BF0AF05", 1),
    (TEMPLATE_UUID + "/header.bin", "A929A287A521818CA4E56A9E643866AE", 1),
    (TEMPLATE_UUID + "/router.bin", "053971527BD9F3D4E3F9B2A1A4D2023F", 1),
    (TEMPLATE_UUID + "/screenshot.png", "538BC7AC54289E9EAA758C50A006AE59", 2),
    (TEMPLATE_UUID + "/snapshot.bin", "9A26C2CA8A7C8731602497EA578C994F", 1),
]

# 注：`pdfNote.ts` 的 md5 表**不准**（模板 screenshot 真实 md5 是 2454A8F5…，
# 不是它写的 538BC7AC…），所以本模块的 md5 一律用**实际内容值**；
# 但它的正文图**固定文件名**是对的，必须照用（见上面 IMG_FILENAME）。


# ------------------------------------------------- 为什么**不上传** page_mdb（画布数据库）
#
# ⚠️⚠️ 一段弯路的结论（2026-10-01，别再走回头路）：
#   · 曾以为「9 条资源缺 page_mdb → 所以打不开编辑」，于是从官方笔记借 `data.mdb`
#     做等长字节替换后一起上传。**这是错的**：借来的画布库和内容对不上，
#     会让笔记点进去直接变空白，比不传还糟。
#   · 真相：`page_mdb` / `snapshot.bin` / `screenshot.png` 都是 **App 首次打开该页时
#     自己生成**的；上传只需给「8 模板 + 1 正文图」共 9 条，App 会自己补画布。
#   · 真正导致「能预览、一进编辑就报错」的元凶是**模板文件被 git 的 CRLF 污染**
#     （`page_router.bin` / `file.bin` 里的 \n 被 core.autocrlf 换成 \r\n）。
#     已在 `load_templates()` 做 LF 归一化，并在仓库加了 `.gitattributes` 防复发。


# ------------------------------------------------------------------ 基础工具


def get_user_id(cli):
    """userId 取自 JWT 的 sub（实测 690，与资源条目里的 userId 一致）。"""
    tok = cli.token or ""
    try:
        payload = tok.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        pl = json.loads(base64.urlsafe_b64decode(payload))
        return str(pl.get("sub") or "")
    except Exception:
        return ""


def generate_nonce():
    """复刻 generateNonce —— 就是标准 UUID v4。"""
    return str(uuid.uuid4())


def generate_custom_file_id(prefix="h", length=32):
    """
    复刻 generateCustomFileId：`h` + 32 位 **0-9a-z 三十六进制**随机串。

    ⚠️ 这里踩过坑：最初用 `uuid4().hex` 生成，那是**纯十六进制（0-9a-f）**，
    永远不含 g-z。而官方实现刻意重试到出现 g-z 为止，可见服务端/客户端
    对 ID 的字符集是有期待的（实测官方 fileId 形如
    `h08yneq62udrp6ikiopr1dfnlgsqg2h0u`，含 y/n/q/k/o/l/g）。
    """
    all_chars = "0123456789abcdefghijklmnopqrstuvwxyz"
    while True:
        body = "".join(random.choice(all_chars) for _ in range(length))
        if any("g" <= c <= "z" for c in body):
            return prefix + body


def md5_upper(s):
    return hashlib.md5(s.encode("utf-8")).hexdigest().upper()


def build_note_name(title=None, full_name=None, now=None):
    """
    生成云笔记名：`AI回复-{标题}-{月日}-{时分}`（用户 2026-09-30 指定）。

    为什么要带标题：光靠时间戳（旧格式 `AI回复-0930-2158`），
    事后翻列表根本想不起那条回复在讲什么。
    为什么还要带时间：同一个标题可能回复很多次，得能区分。

    例：`AI回复-问候-0930-2216`

    · 给了 full_name 就原样返回（留个后门）
    · title 为空则退化成只有时间：`AI回复-0930-2216`
    """
    if full_name:
        return full_name
    now = now or datetime.datetime.now()
    stamp = now.strftime("%m%d-%H%M")
    if not title:
        return "AI回复-%s" % stamp
    bad = '\\/:*?"<>|\r\n\t'
    t = "".join("_" if c in bad else c for c in str(title)).strip()
    t = t[:24].rstrip(". ") or "回复"
    return "AI回复-%s-%s" % (t, stamp)


# ------------------------------------------------------------------ STS 凭证


def generate_sts_token(cli, user_id, fc="note_v2", fr="res", ft=2, fe="", fo="0",
                       nonce=None, ts=None):
    """
    换 STS 临时凭证。复刻 oss.ts 的 generateStsToken。

    注意 sign 与 body 的不一致（照抄官方，别"修"）：
      · sign 里的 fc 是**字符串** "note_v2"
      · body 里的 fc 是**数字** 1（V_MAP["note_v2"] = 1）
    """
    nonce = nonce or generate_nonce()
    ts = ts or int(datetime.datetime.now().timestamp() * 1000)

    raw = "%s+%s+%s+%s+%s+%s+%s+%s" % (user_id, fc, fr, ft, fe, fo, nonce, ts)
    sign = md5_upper(raw)

    V_MAP = {"note_v2": 1}
    G_MAP = {"res": 1}
    body = {
        "fc": V_MAP.get(fc, 1),
        "fr": G_MAP.get(fr, 1),
        "ft": ft,
        "fe": fe,
        "fo": fo,
        "nonce": nonce,
        "ts": ts,
        "sign": sign,
    }
    status, text = cli._request(
        cli.api_base + "/api/services/app/ObjectStorage/GenerateTokenV2Async",
        method="POST", body=body,
        headers={"Accept": "application/json",
                 "Authorization": "Bearer " + (cli.token or ""),
                 "Content-Type": "application/json"},
        timeout=60)
    if status != 200:
        raise zy_client.ZhongYuError("换 STS 失败 HTTP %s：%s" % (status, text[:300]))
    data = json.loads(text)
    if not data.get("result"):
        raise zy_client.ZhongYuError("换 STS 失败：" + text[:300])
    return data["result"], nonce


# ------------------------------------------------------------------ OSS 上传


def oss_put(sts, remote_file, data, content_type="application/octet-stream",
            timeout=180, debug=False):
    """
    用 OSS V1 签名直传一个对象。

    stringToSign =
        VERB \\n Content-MD5 \\n Content-Type \\n Date \\n
        CanonicalizedOSSHeaders \\n CanonicalizedResource

    这里**刻意不发任何多余的 x-oss-* 头**（除了必须的 security-token），
    因为所有 x-oss-* 头都会被服务端算进签名 —— 多发一个就得同步签一个，
    少一事不如少一事。
    """
    ak = sts["accessKeyId"]
    sk = sts["accessKeySecret"]
    token = sts["securityToken"]
    bucket = sts["bucket"]
    endpoint = (sts.get("endpoint") or
                "https://%s.oss-cn-hangzhou.aliyuncs.com" % bucket).rstrip("/")

    url = endpoint + "/" + remote_file
    date = datetime.datetime.utcnow().strftime("%a, %d %b %Y %H:%M:%S GMT")

    string_to_sign = "\n".join([
        "PUT",
        "",                      # Content-MD5 空
        content_type,
        date,
        "x-oss-security-token:" + token,
        "/%s/%s" % (bucket, remote_file),
    ])
    sig = base64.b64encode(
        hmac.new(sk.encode("utf-8"), string_to_sign.encode("utf-8"),
                 hashlib.sha1).digest()).decode()

    if debug:
        sys.stderr.write("[oss] PUT %s\n%s\n" % (url, string_to_sign))

    req = urllib.request.Request(url, data=data, method="PUT", headers={
        "Date": date,
        "Content-Type": content_type,
        "Content-Length": str(len(data)),
        "x-oss-security-token": token,
        "Authorization": "OSS %s:%s" % (ak, sig),
    })
    try:
        with zy_client._DIRECT_OPENER.open(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        raise zy_client.ZhongYuError(
            "OSS 上传失败 HTTP %s：%s\n对象：%s" % (e.code, body[:400], remote_file))


# ------------------------------------------------------------------ 写数据


def _post_raw(cli, path, raw_body, timeout=90):
    """POST 一段**已经是密文**的原始字符串（AddOrUpdate 要求 body 就是密文）。

    2026-10-18：加了 401 自动重登 —— 现在 token 会缓存 2 小时，
    过期后第一发必然是 401，不重试的话上传会直接失败。
    """
    for attempt in (0, 1):
        req = urllib.request.Request(
            cli.api_base + path, data=raw_body.encode("utf-8"), method="POST",
            headers={"Authorization": "Bearer " + (cli.token or ""),
                     "Content-Type": "application/json; charset=UTF-8"})
        try:
            with zy_client._DIRECT_OPENER.open(req, timeout=timeout) as r:
                return r.status, r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 401 and attempt == 0:
                cli.login()
                zy_client.save_cached_token(cli.user_name, cli.api_base, cli.token)
                continue
            return e.code, e.read().decode("utf-8", "replace")


def save_resource_list(cli, resource_list):
    raw = cli.enc(json.dumps(resource_list, ensure_ascii=False))
    status, text = _post_raw(cli, "/CloudNotes/api/Resources/AddOrUpdate", raw)
    try:
        data = json.loads(text)
    except Exception:
        raise zy_client.ZhongYuError("保存资源返回非 JSON HTTP %s：%s" % (status, text[:300]))
    if data.get("code") != 0:
        raise zy_client.ZhongYuError("保存资源失败：" + text[:400])
    return data


def save_note(cli, user_id, file_id, file_name, oss_root, parent_id="0"):
    today = datetime.date.today().strftime("%Y%m%d")
    file_url = "%snote_v2/res/%s/%s/%s/" % (oss_root, user_id, today, file_id)
    payload = {
        "fileId": file_id,
        "fileName": file_name,
        "parentId": parent_id,
        "type": "12",
        "fileUrl": file_url,
    }
    raw = cli.enc(json.dumps(payload, ensure_ascii=False))
    status, text = _post_raw(cli, "/CloudNotes/api/Notes/AddOrUpdate", raw)
    try:
        data = json.loads(text)
    except Exception:
        raise zy_client.ZhongYuError("保存笔记返回非 JSON HTTP %s：%s" % (status, text[:300]))
    if data.get("code") != 0:
        raise zy_client.ZhongYuError("保存笔记失败：" + text[:400])
    return data


def create_folder(cli, name, parent_id="0"):
    """在 parent 下新建一个文件夹（type=0），返回新文件夹的 fileId。

    ⚠️ 这是**写操作**：成功会在用户账号里真的建出一个文件夹。
       只在「按名字找不到目标文件夹」时由 control_bot 调用；
       失败就直接把异常抛给上层（上层的处理是跳过本轮，不会乱读别的目录）。
    """
    fid = str(uuid.uuid4())
    payload = {
        "fileId": fid,
        "fileName": name,
        "parentId": str(parent_id or "0"),
        "type": "0",
    }
    raw = cli.enc(json.dumps(payload, ensure_ascii=False))
    status, text = _post_raw(cli, "/CloudNotes/api/Notes/AddOrUpdate", raw)
    try:
        data = json.loads(text)
    except Exception:
        raise zy_client.ZhongYuError(
            "建文件夹返回非 JSON HTTP %s：%s" % (status, text[:300]))
    if data.get("code") != 0:
        raise zy_client.ZhongYuError("建文件夹失败：" + text[:400])
    return fid


def rename_note(cli, file_id, new_name):
    """
    给已有笔记改名（`Notes/AddOrUpdate` 覆盖 fileName）。

    ⚠️ 必须把原有的 `parentId` / `fileUrl` **一起回传** ——
    这个接口是"整套覆盖"，漏字段可能把笔记改成空壳。
    """
    cur = next((n for n in cli.get_all_notes() if n.get("fileId") == file_id), None)
    if not cur:
        raise zy_client.ZhongYuError("找不到笔记：" + file_id)
    payload = {
        "fileId": file_id,
        "fileName": new_name,
        "parentId": cur.get("parentId") or "0",
        "type": "12",
        "fileUrl": cur.get("fileUrl") or "",
    }
    raw = cli.enc(json.dumps(payload, ensure_ascii=False))
    status, text = _post_raw(cli, "/CloudNotes/api/Notes/AddOrUpdate", raw)
    try:
        data = json.loads(text)
    except Exception:
        raise zy_client.ZhongYuError("改名返回非 JSON HTTP %s：%s" % (status, text[:300]))
    if data.get("code") != 0:
        raise zy_client.ZhongYuError("改名失败：" + text[:400])
    return cur.get("fileName")


# ------------------------------------------------------------------ 主流程


def load_templates():
    """读本地模板文件（来自 ZhongYuToolBox_Rev/public/example/）。"""
    out = {}
    for rel, _, _ in TEMPLATE_RESOURCES:
        p = os.path.join(TEMPLATE_BASE, rel.replace("/", os.sep))
        if not os.path.exists(p):
            raise zy_client.ZhongYuError("模板文件缺失：" + p)
        with open(p, "rb") as f:
            data = f.read()
        # ⚠️ 防御性归一化：模板里的路由表（`page_router.bin` / `file.bin`）**对行尾敏感**，
        # git 的 core.autocrlf=true 会把 \n 换成 \r\n，App 解析时直接报
        # 「获取页面数据错误」。2026-10-01 踩过（只差 2 字节，极隐蔽）。
        # 对本身不含 CRLF 的模板是空操作，安全。
        if rel.endswith(".bin"):
            data = data.replace(b"\r\n", b"\n")
        out[rel] = data
    return out


def upload_note_page(cli, image_path, note_name, parent_id="0", progress=print,
                     body_image_path=None, keep_source=False):
    """
    把一张页面图作为一本新笔记上传。

    image_path      —— 「页面总览图」，会成为 screenshot.png，也就是**打开笔记看到的那一页**
    body_image_path —— 可选，「页内图片」素材；不传则复用总览图

    返回 dict：fileId / fileName / fileUrl / 已上传对象数
    """
    user_id = get_user_id(cli)
    if not user_id:
        raise zy_client.ZhongYuError("拿不到 userId（JWT 里没有 sub）")

    today = datetime.date.today().strftime("%Y%m%d")
    custom_file_id = generate_custom_file_id()
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(image_path, "rb") as f:
        page_shot = f.read()
    if body_image_path and os.path.exists(body_image_path):
        with open(body_image_path, "rb") as f:
            body_bytes = f.read()
    else:
        body_bytes = page_shot

    progress("1/5 换 STS 凭证 …")
    sts, nonce = generate_sts_token(cli, user_id, "note_v2", nonce=custom_file_id)
    progress("     桶=%s  region=%s  endpoint=%s"
             % (sts.get("bucket"), sts.get("region"), sts.get("endpoint")))

    bucket = sts["bucket"]
    region = sts.get("region") or "oss-cn-hangzhou"
    # 用服务端返回的 endpoint **原样**拼接。
    # 实测官方笔记里存的是 `http://...`，别自作主张升级成 https ——
    # 客户端拿 fileUrl / ossImageUrl 去取图，scheme 保持一致最稳。
    oss_root = (sts.get("endpoint")
                or "https://%s.%s.aliyuncs.com" % (bucket, region)).rstrip("/") + "/"

    progress("2/5 上传模板文件 …")
    templates = load_templates()
    page_hash = str(int(datetime.datetime.now().timestamp() * 1000))

    # ⭐⭐ 关键一步：把「页面总览图」screenshot.png 换成**我方渲染好的整页图**，
    # 而不是模板自带的灰色占位图。
    #
    # 实测官方正常笔记的 screenshot.png 就是 App 渲染出的整页画面：
    #   · 尺寸 **1920 × 2184**（与我们的回复图完全一致！）
    #   · 格式 **PNG**
    # 而模板里那张写着"同步的PDF笔记需要打开每一页以生成缩略图"——它是**占位图**，
    # 用户上传后必须逐页打开、由 App 渲染替换，期间看到的就是那张灰底提示图。
    #
    # 我们本来就已经把整页渲染好了，直接放进去即可：打开即见内容，无需等待。
    templates[SCREENSHOT_REL] = page_shot

    uploaded = 0
    for rel, blob in templates.items():
        remote = "note_v2/res/%s/%s/%s/%s/%s" % (user_id, today, custom_file_id,
                                                 page_hash, rel)
        oss_put(sts, remote, blob)
        uploaded += 1

    progress("3/5 上传页面图 …")
    # 正文图（页内图片素材）。默认与总览图同一张；
    # 若单独给了 --body-image（通常是 JPEG），则用它 —— 实测官方这个位置
    # 放的是 JPEG 内容配 .webp 文件名。
    img_bytes = body_bytes
    # 固定文件名（见 IMG_FILENAME 的说明）：不能改成随机 UUID，
    # 否则模板 `file.bin` 里硬编码的引用对不上，App 会找不到正文图。
    img_filename = IMG_FILENAME
    img_md5 = hashlib.md5(img_bytes).hexdigest().upper()
    img_remote = "note_v2/res/%s/%s/%s/%s/%s" % (user_id, today, custom_file_id,
                                                 page_hash, img_filename)
    ctype = "image/jpeg" if img_bytes[:2] == b"\xff\xd8" else "image/png"
    oss_put(sts, img_remote, img_bytes, content_type=ctype)
    uploaded += 1

    # ⚠️ **不上传 page_mdb**（画布数据库）——App 首次打开该页会自己生成。
    # 提前塞一个「借来的」会和内容对不上、把笔记搞成空白（2026-10-01 实测）。
    base_key = "note_v2/res/%s/%s/%s/%s" % (user_id, today, custom_file_id, page_hash)

    progress("4/5 写资源清单 …")
    page_base = ("/storage/emulated/0/Android/data/com.friday.cloudsnote/userNote/"
                 "%s/note/%s/%s" % (user_id, custom_file_id, page_hash))
    base_oss = "%snote_v2/res/%s/%s/%s/%s" % (oss_root, user_id, today,
                                              custom_file_id, page_hash)
    resource_list = []
    for rel, md5v, rtype in TEMPLATE_RESOURCES:
        blob = templates[rel]
        resource_list.append({
            "id": "%s/%s" % (page_base, rel),
            "fileId": custom_file_id,
            "pageName": page_base,
            "pageIndex": 0,
            # 用**实际内容的 md5**，而不是 pdfNote.ts 里那份硬编码表
            # （实测其中 3 条的硬编码值与本地文件并不一致；服务端反正也会重算，
            #  但传真值更不容易让客户端误判"文件损坏"）
            "md5": hashlib.md5(blob).hexdigest().upper(),
            "resourceType": rtype,
            "ossImageUrl": "%s/%s" % (base_oss, rel),
            "createTimeStamp": stamp,
            "updateTimeStamp": stamp,
            "toBeUploaded": False,
            "wasDeleted": False,
        })
    resource_list.append({
        "id": "%s/res/image/%s" % (page_base, img_filename),
        "fileId": custom_file_id,
        "pageName": page_base,
        "pageIndex": 0,
        "md5": img_md5,
        "resourceType": 0,
        "ossImageUrl": "%s/%s/%s" % ("%snote_v2/res/%s/%s/%s" % (
            oss_root, user_id, today, custom_file_id), page_hash, img_filename),
        "createTimeStamp": stamp,
        "updateTimeStamp": stamp,
        "toBeUploaded": False,
        "wasDeleted": False,
    })
    # 共 9 条 = 8 模板 + 1 正文图。**不含 page_mdb**（App 打开时会自己补）。
    save_resource_list(cli, resource_list)

    progress("5/5 建笔记 …")
    save_note(cli, user_id, custom_file_id, note_name, oss_root, parent_id)

    # 本地渲染图用完自动进回收站（只处理 `_out/` 下的，见文件上方说明）
    recycled = []
    if not keep_source:
        recycled = recycle_paths(
            [p for p in (image_path, body_image_path) if p and _is_in_out_dir(p)])
        if recycled:
            progress("     本地渲染图已移入回收站：%s"
                     % "、".join(os.path.basename(x) for x in recycled))

    return {
        "fileId": custom_file_id,
        "fileName": note_name,
        "fileUrl": "%snote_v2/res/%s/%s/%s/" % (oss_root, user_id, today, custom_file_id),
        "uploadedObjects": uploaded,
        "pageImage": "%s/%s" % (base_oss, img_filename),
        "screenshot": "%s/%s" % (base_oss, SCREENSHOT_REL),
        "recycledLocal": recycled,
    }


def _main():
    ap = argparse.ArgumentParser(description="把一页图片上传成中育云笔记（写操作）")
    ap.add_argument("--image", help="页面图路径（默认用 reply_render 现场渲染）")
    ap.add_argument("--body-image",
                    help="页内图片素材（建议 JPEG）；不传则复用 --image")
    ap.add_argument("--name", default=None,
                    help="完整笔记名（给了就原样用，覆盖 --title 规则）")
    ap.add_argument("--title", default=None,
                    help="回复标题。命名规则：AI回复-{标题}-{月日}-{时分}，"
                         "例如「AI回复-问候-0930-2216」")
    ap.add_argument("--parent", default="0", help="父文件夹 fileId，默认放根目录")
    ap.add_argument("--dry-run", action="store_true", help="只换 STS，不上传（安全）")
    ap.add_argument("--keep-source", action="store_true",
                    help="保留本地渲染图（默认上传成功后自动移入回收站）")
    ap.add_argument("--rename", metavar="FILE_ID",
                    help="只给已有笔记改名（配合 --title），不做上传")
    ap.add_argument("--go", action="store_true", help="真的写进云笔记")
    ap.add_argument("--debug", action="store_true", help="打印签名原文")
    args = ap.parse_args()

    cli = zy_client.build_client()
    uid = get_user_id(cli)
    print("已登录：%s   userId=%s   服务器=%s" % (cli.whoami() or "?", uid, cli.api_base))

    if args.rename:
        new_name = build_note_name(args.title, args.name)
        old_name = rename_note(cli, args.rename, new_name)
        print("\n改名完成：\n  旧名  %s\n  新名  %s" % (old_name, new_name))
        return

    if args.dry_run or not args.go:
        print("\n[DRY-RUN] 只验证 STS 凭证获取，不上传任何东西。")
        sts, nonce = generate_sts_token(cli, uid, "note_v2")
        print("  凭证获取成功：")
        print("    bucket        = %s" % sts.get("bucket"))
        print("    region        = %s" % sts.get("region"))
        print("    endpoint      = %s" % sts.get("endpoint"))
        print("    accessKeyId   = %s…" % str(sts.get("accessKeyId"))[:8])
        print("    securityToken = %s…" % str(sts.get("securityToken"))[:24])
        if not args.go:
            print("\n没有加 --go，到此为止（未写任何数据）。")
        return

    # 准备页面图
    image = args.image
    if not image:
        # 默认渲染到工作区 `_out/`（临时产物目录，上传成功后会进回收站），
        # 不再往桌面写文件。
        os.makedirs(OUT_DIR, exist_ok=True)
        image = os.path.join(OUT_DIR, "AI回复.png")
        import reply_render              # 需要 Pillow，见文件头的说明
        reply_render.render_reply(reply_render.SAMPLE_TITLE,
                                  reply_render.SAMPLE_BODY, image)
    if not os.path.exists(image):
        raise zy_client.ZhongYuError("找不到页面图：" + image)

    # 命名规则（用户 2026-09-30 指定）：AI回复-{标题}-{月日}-{时分}
    #   标题：一眼看出这条回复在讲什么（2~6 字，从内容里提炼）
    #   时间：区分「同一个标题的多次回复」
    # 例：AI回复-问候-0930-2216
    name = build_note_name(args.title, args.name)
    print("\n准备上传：%s\n笔记名：%s" % (image, name))
    print("-" * 60)
    r = upload_note_page(cli, image, name, parent_id=args.parent, progress=print,
                         body_image_path=args.body_image,
                         keep_source=args.keep_source)
    print("-" * 60)
    print("上传完成：")
    for k, v in r.items():
        print("  %-16s %s" % (k, v))


if __name__ == "__main__":
    try:
        _main()
    except zy_client.ZhongYuError as e:
        print("[出错] " + str(e))
        sys.exit(1)
