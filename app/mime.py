"""MIME multipart 解析与重新编码（遵循 RFC 2045/2046/2616）。

全部以字节为单位处理：
* CRLF 行结束符；
* Content-Type 参数中的 quoted-string（带反斜杠转义）；
* 递归嵌套 multipart；
* 重新编码时按各部分原 boundary 规则生成 body part / 关闭定界符。

任何不严谨但仍可继续解析的情况记录为 issue（error / warning），
而不是直接抛出，以便在前端逐条展示“解析错误”。
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass, field
from typing import Optional

CRLF = b"\r\n"
DASHDASH = b"--"

TOKEN_RE = re.compile(rb"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# RFC 2046 bcharsnospace：A-Za-z0-9 与 '()+_,-./:=?，长度 1..70
BOUNDARY_BCHARS_RE = re.compile(rb"\A[0-9A-Za-z'()+_,\-./:=?]{1,70}\Z")


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #

def _to_bytes(raw) -> tuple[Optional[bytes], Optional[str]]:
    """把输入还原为原始字节。

    来自 JSON 的 str 已由 FastAPI 按 UTF-8 解码，这里按 UTF-8 重新编码，
    与浏览器 fetch 发出的线上字节保持一致。
    """
    if raw is None:
        return b"", None
    if isinstance(raw, (bytes, bytearray)):
        return bytes(raw), None
    if isinstance(raw, str):
        return raw.encode("utf-8", errors="replace"), None
    return None, f"无法识别的二进制数据类型：{type(raw).__name__}"


def normalize_newlines(data: bytes):
    """把孤立 LF/CR 规范化为 CRLF，返回 (data, changed)。"""
    out = bytearray()
    changed = False
    i = 0
    n = len(data)
    while i < n:
        b = data[i]
        if b == 0x0D:  # CR
            if i + 1 < n and data[i + 1] == 0x0A:
                out += CRLF
                i += 2
            else:
                out += CRLF
                i += 1
                changed = True
        elif b == 0x0A:  # 孤立 LF
            out += CRLF
            i += 1
            changed = True
        else:
            out.append(b)
            i += 1
    return bytes(out), changed


def _issue(issues, level: str, code: str, message: str, offset=None):
    item = {"level": level, "code": code, "message": message}
    if offset is not None:
        item["offset"] = offset
    issues.append(item)


# --------------------------------------------------------------------------- #
# 头部块 / Content-Type
# --------------------------------------------------------------------------- #

def parse_header_block(block: bytes):
    """解析 CRLF 分隔的头部块（含 unfolding），返回 (headers, issues)。

    headers: [{name, value, raw}]
    """
    issues: list[dict] = []
    headers: list[dict] = []

    # RFC 5322 unfolding： CRLF + WSP -> 单个空格
    unfolded = re.sub(rb"\r\n([ \t])", b" ", block)

    for line in unfolded.split(CRLF):
        if not line:
            continue
        if line[:1] in (b" ", b"\t"):
            # unfolding 之后仍以空白开头：畸形延续行
            _issue(issues, "warning", "MALFORMED_FOLD",
                   "无法归属任何头部字段的折叠行已作为头部值保留", None)
            raw = line.decode("utf-8", errors="replace")
            headers.append({"name": "", "value": raw, "raw": raw})
            continue
        if b":" not in line:
            _issue(issues, "error", "BAD_HEADER_LINE",
                   f"头部行缺少冒号：{line[:60]!r}")
            continue
        name_b, _, value_b = line.partition(b":")
        name = name_b.decode("ascii", errors="replace")
        value = value_b.strip(b" \t").decode("utf-8", errors="replace")
        if not TOKEN_RE.fullmatch(name_b):
            _issue(issues, "error", "BAD_HEADER_NAME",
                   f"字段名不符合 token 规则：{name!r}")
        headers.append({"name": name, "value": value, "raw": name + ": " + value})
    return headers, issues


def parse_content_type(header_value: Optional[str], issues: list[dict],
                       context="Content-Type"):
    """解析 `type/subtype; key=value; key="quoted"`。

    返回 (media_type|None, params dict[str,str])。
    """
    params: dict[str, str] = {}
    if not header_value:
        return None, params

    data = header_value.encode("utf-8", errors="replace")
    # 媒体类型在第一个分号之前
    if b";" in data:
        mt_b, rest = data.split(b";", 1)
    else:
        mt_b, rest = data, b""
    media_type = mt_b.strip().decode("ascii", errors="replace").lower()
    if "/" not in media_type:
        _issue(issues, "error", "BAD_MEDIA_TYPE",
               f"{context} 的媒体类型缺少 '/'：{media_type!r}")
        media_type = None
    elif not media_type.replace("/", "").replace(".", "").replace("+", "").replace("-", "").isalnum():
        _issue(issues, "warning", "BAD_MEDIA_TYPE",
               f"{context} 的媒体类型含可疑字符：{media_type!r}")

    # 参数分词：支持 quoted-string 与反斜杠转义
    i, n = 0, len(rest)
    while i < n:
        while i < n and rest[i] in b" \t":
            i += 1
        m = TOKEN_RE.match(rest, i)
        if not m:
            # 跳过游离字符
            ch = rest[i:i + 1]
            _issue(issues, "error", "BAD_PARAM_SYNTAX",
                   f"{context} 参数位置出现非法字符 {ch!r}（偏移 {i}）")
            i += 1
            continue
        name = m.group(0).decode("ascii").lower()
        i = m.end()
        while i < n and rest[i] in b" \t":
            i += 1
        if i >= n or rest[i] != 0x3B and rest[i] != 0x3D:
            if i < n and rest[i] == 0x3B:
                i += 1
            _issue(issues, "error", "BAD_PARAM_SYNTAX",
                   f"{context} 参数 {name!r} 缺少 '='")
            continue
        if rest[i] == 0x3B:
            _issue(issues, "error", "BAD_PARAM_SYNTAX",
                   f"{context} 参数 {name!r} 缺少值")
            i += 1
            continue
        i += 1  # 跳过 '='
        while i < n and rest[i] in b" \t":
            i += 1
        value = ""
        if i < n and rest[i] == 0x22:  # 引号
            i += 1
            buf = bytearray()
            closed = False
            while i < n:
                c = rest[i]
                if c == 0x5C and i + 1 < n:  # 反斜杠转义
                    buf.append(rest[i + 1])
                    i += 2
                elif c == 0x22:
                    closed = True
                    i += 1
                    break
                elif c in (0x0D, 0x0A):
                    _issue(issues, "error", "BAD_QUOTED_STRING",
                           f"{context} 参数 {name!r} 的引号字符串未闭合即遇到换行")
                    break
                else:
                    buf.append(c)
                    i += 1
            if not closed:
                _issue(issues, "error", "BAD_QUOTED_STRING",
                       f"{context} 参数 {name!r} 的引号字符串缺少结束引号，已按已闭合解析")
            value = buf.decode("utf-8", errors="replace")
        else:
            m2 = TOKEN_RE.match(rest, i)
            if not m2:
                ch = rest[i:i + 1]
                _issue(issues, "error", "BAD_PARAM_SYNTAX",
                       f"{context} 参数 {name!r} 的值非法（未加引号？）：{ch!r}")
                # 跳到下一个分号
                semi = rest.find(b";", i)
                i = n if semi < 0 else semi + 1
                continue
            value = m2.group(0).decode("ascii", errors="replace")
            i = m2.end()
        while i < n and rest[i] in b" \t":
            i += 1
        if i < n and rest[i] == 0x3B:
            i += 1
        elif i < n:
            _issue(issues, "warning", "BAD_PARAM_SYNTAX",
                   f"{context} 参数 {name!r} 之后缺少分号，已继续解析")
            # 尽量恢复：寻找下一个分号
            semi = rest.find(b";", i)
            i = n if semi < 0 else semi + 1

        if name == "boundary":
            bval = value.encode("utf-8", errors="replace")
            if b" " in bval:
                _issue(issues, "warning", "BOUNDARY_SUSPICIOUS",
                       "boundary 参数含有空格；RFC 2046 建议 bcharsnospace 不含空格，"
                       "部分实现可能拒绝该报文")
            if not BOUNDARY_BCHARS_RE.fullmatch(bval):
                _issue(issues, "warning", "BOUNDARY_NONCOMPLIANT",
                       "boundary 超过 70 个字符或包含 RFC 2046 允许字符集之外的字符，"
                       "兼容性可能存在问题")
        params[name] = value
    return media_type, params


def build_content_type(media_type: str, params: dict[str, str]) -> str:
    parts = [media_type]
    for k, v in params.items():
        v_b = v.encode("utf-8", errors="replace")
        if v == "" or not TOKEN_RE.fullmatch(v_b):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            parts.append(f'{k}="{escaped}"')
        else:
            parts.append(f"{k}={v}")
    return "; ".join(parts)


def find_header(headers: list[dict], name: str) -> Optional[dict]:
    lname = name.lower()
    for h in headers:
        if h["name"].lower() == lname:
            return h
    return None


# --------------------------------------------------------------------------- #
# Content-Transfer-Encoding（RFC 2045 §6）
# --------------------------------------------------------------------------- #

IDENTITY_TRANSFER_ENCODINGS = {"7bit", "8bit", "binary"}
KNOWN_TRANSFER_ENCODINGS = IDENTITY_TRANSFER_ENCODINGS | {
    "base64", "quoted-printable",
}
_B64_CHARS = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/=")
_HEX_CHARS = frozenset(b"0123456789abcdefABCDEF")
# RFC 2045：base64 / quoted-printable 编码行最大 76 字符（不含 CRLF）
MAX_ENCODED_LINE = 76


def parse_transfer_encoding(headers: list[dict], issues: list[dict], path: str):
    """读取 Content-Transfer-Encoding 头。

    返回 ``(cte, declared)``：缺省头时为 ``("7bit", "7bit")``。
    按 RFC 2045 该头只能是单个 token、不允许参数；畸形/未知值记录诊断，
    返回一个 ``x-unknown`` 之类的透传标记，正文按原始字节处理。
    """
    h = find_header(headers, "Content-Transfer-Encoding")
    if h is None:
        return "7bit", "7bit"
    value = (h.get("value") or "").strip()
    m = TOKEN_RE.match(value.encode("ascii", "replace"))
    token = m.group(0).decode("ascii").lower() if m else ""
    rest = value[m.end():].strip() if m else value.strip()
    if not token:
        _issue(issues, "warning", "BAD_CTE_SYNTAX",
               f"{path}：Content-Transfer-Encoding 值 {value!r} 不是合法 token，"
               "已按未知编码原样透传（原始载荷保留）")
        return "x-unknown", "x-unknown"
    if rest:
        _issue(issues, "warning", "BAD_CTE_SYNTAX",
               f"{path}：Content-Transfer-Encoding 只允许单个 token，"
               f"已忽略多余内容 {rest!r}")
    if token not in KNOWN_TRANSFER_ENCODINGS:
        _issue(issues, "warning", "UNKNOWN_TRANSFER_ENCODING",
               f"{path}：不支持的 Content-Transfer-Encoding {token!r}，"
               "正文按原始字节透传，不做解码/重新编码")
    return token, token


def _check_identity_cte(data: bytes, cte: str, issues: list[dict], path: str):
    """校验 7bit/8bit 正文约束；binary 不做任何限制。"""
    if cte not in IDENTITY_TRANSFER_ENCODINGS:
        return
    has_ctl = any(b < 0x20 and b not in (0x09, 0x0A, 0x0D) for b in data)
    if cte == "7bit":
        if any(b > 127 for b in data):
            _issue(issues, "warning", "CTE_NOT_7BIT",
                   f"{path}：声明为 7bit 但正文含非 ASCII 字节（>0x7F），"
                   "严格实现可能拒绝；可切换为 base64 或 quoted-printable")
        elif has_ctl:
            _issue(issues, "warning", "CTE_NOT_7BIT",
                   f"{path}：声明为 7bit 但正文含 NUL 等 C0 控制字节，"
                   "不满足 RFC 2045 的 7bit 规则；可切换为 binary/base64")
    elif cte == "8bit" and has_ctl:
        _issue(issues, "warning", "CTE_BINARY_IN_8BIT",
               f"{path}：声明为 8bit 但正文含 NUL 等 C0 控制字节（更接近 binary）")
    if cte in ("7bit", "8bit"):
        for i, b in enumerate(data):
            bare = (b == 0x0A and (i == 0 or data[i - 1] != 0x0D)) or \
                   (b == 0x0D and (i + 1 >= len(data) or data[i + 1] != 0x0A))
            if bare:
                _issue(issues, "warning", "CTE_BARE_NEWLINE",
                       f"{path}：声明为 {cte} 的正文含孤立 CR/LF；"
                       "RFC 2045 要求行结束为 CRLF")
                break


def _decode_base64_wire(data: bytes, issues: list[dict], path: str):
    """宽容解码 base64 传输正文，返回 ``(decoded, malformed)``。

    剔除非法空白/字符、容错 padding；任何畸形都产生带节点路径的诊断。
    原始传输字节由调用方保留在 rawBodyBase64，未编辑即逐字节还原。
    """
    start = len(issues)
    illegal = sorted({b for b in data
                      if b not in _B64_CHARS and b not in (0x20, 0x09, 0x0D, 0x0A)})
    if illegal:
        preview = " ".join(f"0x{b:02X}" for b in illegal[:8])
        _issue(issues, "warning", "BAD_BASE64",
               f"{path}：base64 正文含非法字符（{preview}），已剔除后尽力解码；"
               "原始传输字节保留在该节点，未编辑即逐字节还原")
    for line_no, line in enumerate(data.split(CRLF), 1):
        token = line.strip(b" \t")
        if len(token) > MAX_ENCODED_LINE:
            _issue(issues, "warning", "CTE_LONG_LINE",
                   f"{path}：base64 第 {line_no} 行 {len(token)} 字符，"
                   f"超过 RFC 2045 的 {MAX_ENCODED_LINE} 字符折行限制")
            break
    cleaned = bytes(b for b in data if b in _B64_CHARS)
    core = cleaned.rstrip(b"=")
    pad = len(cleaned) - len(core)
    if b"=" in core:
        _issue(issues, "warning", "BAD_BASE64",
               f"{path}：base64 的 '=' 只允许出现在末尾填充位，已尽力解码")
    if pad > 2:
        _issue(issues, "warning", "BAD_BASE64",
               f"{path}：base64 填充位 '=' 最多 2 个（发现 {pad} 个），已尽力解码")
    decoded: Optional[bytes] = None
    try:
        decoded = base64.b64decode(cleaned, validate=True)
    except (binascii.Error, ValueError):
        padded = core + b"=" * (-len(core) % 4)
        try:
            decoded = base64.b64decode(padded, validate=True)
            _issue(issues, "warning", "BAD_BASE64",
                   f"{path}：base64 缺少/错误 padding，已补齐后解码")
        except (binascii.Error, ValueError):
            try:
                decoded = base64.b64decode(cleaned, validate=False)
            except (binascii.Error, ValueError):
                decoded = data.strip(b" \t\r\n")
            _issue(issues, "warning", "BAD_BASE64",
                   f"{path}：base64 数据无法完整解码，已保留可恢复部分；"
                   "原始传输字节未丢失")
    malformed = any(it["code"] == "BAD_BASE64" for it in issues[start:])
    return decoded, malformed


def _decode_quoted_printable_wire(data: bytes, issues: list[dict], path: str):
    """宽容解码 quoted-printable（RFC 2045 §6.7），返回 ``(decoded, malformed)``。"""
    start = len(issues)
    out = bytearray()
    n = len(data)
    pos = 0
    line_no = 0
    bare_reported = False
    long_reported = False

    while True:
        line_no += 1
        nl = data.find(b"\n", pos)
        segment = data[pos:nl] if nl >= 0 else data[pos:n]
        if segment.endswith(b"\r"):
            segment = segment[:-1]
        elif nl >= 0 and not bare_reported:
            _issue(issues, "warning", "CTE_BARE_NEWLINE",
                   f"{path}：quoted-printable 正文含孤立 LF；"
                   "RFC 2045 要求 CRLF，已按 CRLF 解释")
            bare_reported = True
        if b"\r" in segment:
            segment = segment.replace(b"\r", b"")
            if not bare_reported:
                _issue(issues, "warning", "CTE_BARE_NEWLINE",
                       f"{path}：quoted-printable 正文含孤立 CR；已按 CRLF 解释")
                bare_reported = True
        if len(segment) > MAX_ENCODED_LINE and not long_reported:
            _issue(issues, "warning", "CTE_LONG_LINE",
                   f"{path}：quoted-printable 第 {line_no} 行 {len(segment)} 字符，"
                   f"超过 RFC 2045 的 {MAX_ENCODED_LINE} 字符折行限制")
            long_reported = True
        soft_break = segment.endswith(b"=")
        if soft_break:
            segment = segment[:-1]
        if (not soft_break and segment and segment[-1] in (0x20, 0x09)):
            _issue(issues, "warning", "CTE_TRAILING_WSP",
                   f"{path}：quoted-printable 硬换行前的空格/制表符应编码为 =20/=09，"
                   "严格解码时可能丢失")
        i = 0
        while i < len(segment):
            b = segment[i]
            if b == 0x3D:
                hx = segment[i + 1:i + 3]
                # 行尾的 '=4'：第三个十六进制字符落在软折行之外，
                # 按 RFC 2045 属畸形转义；'=' 与已取到的字符按字面保留
                if len(hx) == 2 and all(c in _HEX_CHARS for c in hx):
                    out.append(int(hx, 16))
                    i += 3
                    continue
                _issue(issues, "warning", "BAD_QUOTED_PRINTABLE",
                       f"{path}：quoted-printable 非法转义 "
                       f"{bytes(segment[i:i + 3])!r}，已按字面保留；"
                       "原始传输字节未丢失")
                out += segment[i:i + 1 + len(hx)]
                i += 1 + len(hx)
                continue
            out.append(b)
            i += 1
        if nl < 0:
            break
        pos = nl + 1
        if not soft_break:
            out += CRLF
    malformed = any(it["code"] == "BAD_QUOTED_PRINTABLE" for it in issues[start:])
    return bytes(out), malformed


def decode_transfer_body(headers: list[dict], body: bytes,
                         issues: list[dict], path: str) -> dict:
    """叶子部分：按声明的 CTE 解码传输正文，再转换为可编辑表示。

    返回字段：
      body / bodyEncoding        解码后内容的编辑器表示（utf-8 或 base64 通道）；
      transferEncoding           头部声明的 CTE（规范化小写，缺省 7bit）；
      transferEncodingDeclared   解析时声明的 CTE（未编辑快速路径判据）；
      decodeState                raw / decoded / malformed / passthrough；
      rawBodyBase64              未做 CTE 解码的原始传输体（保证载荷不丢、
                                 未编辑节点字节稳定）。
    """
    cte, _declared = parse_transfer_encoding(headers, issues, path)
    state = "raw"
    if cte in IDENTITY_TRANSFER_ENCODINGS:
        _check_identity_cte(body, cte, issues, path)
        decoded = body
    elif cte == "base64":
        decoded, malformed = _decode_base64_wire(body, issues, path)
        state = "malformed" if malformed else "decoded"
    elif cte == "quoted-printable":
        decoded, malformed = _decode_quoted_printable_wire(body, issues, path)
        state = "malformed" if malformed else "decoded"
    else:
        decoded = body
        state = "passthrough"

    text, edit_enc, bin_warn = _decode_body_for_edit(decoded)
    if bin_warn:
        _issue(issues, "info", "BINARY_BODY", f"{path}：{bin_warn}")
    return {
        "body": text,
        "bodyEncoding": edit_enc,
        "transferEncoding": cte,
        "transferEncodingDeclared": cte,
        "decodeState": state,
        "rawBodyBase64": base64.b64encode(body).decode("ascii"),
    }


def _leaf_node_fields(headers: list[dict], body: bytes,
                      issues: list[dict], path: str) -> dict:
    """叶子节点的 CTE 解码结果 + Content-Disposition 的 name/filename。"""
    fields = decode_transfer_body(headers, body, issues, path)
    cd = find_header(headers, "Content-Disposition")
    name = None
    filename = None
    if cd is not None:
        cd_params = _parse_secondary_params(cd["value"])
        name = cd_params.get("name")
        filename = cd_params.get("filename")
    fields["name"] = name
    fields["filename"] = filename
    return fields


def _check_multipart_cte(headers: list[dict], issues: list[dict], path: str):
    """RFC 2045：multipart 实体只能使用 7bit/8bit/binary。"""
    cte, _ = parse_transfer_encoding(headers, issues, path)
    if cte not in IDENTITY_TRANSFER_ENCODINGS:
        _issue(issues, "warning", "BAD_CTE_ON_MULTIPART",
               f"{path}：multipart 实体声明了 Content-Transfer-Encoding: {cte}，"
               "RFC 2045 要求 multipart 只能使用 7bit/8bit/binary；已忽略该编码")


# --------------------------------------------------------------------------- #
# multipart 切分（RFC 2046 §5.1.1）
# --------------------------------------------------------------------------- #

def split_multipart(body: bytes, boundary: bytes, issues: list[dict],
                    path: str = "multipart"):
    """按 CRLF "--boundary" 切分正文（RFC 2046 §5.1.1）。

    每个识别到的定界符记录为 (start, content_start, line_end, closing)：
    * start           定界符 "--boundary" 的起始偏移；
    * content_start   普通定界符行 CRLF 之后的偏移（正文起点）；关闭时为 -1；
    * line_end        定界符行结束偏移（含行尾 CRLF）；
    * closing         是否为关闭定界符 "--boundary--"。
    """
    delim = DASHDASH + boundary
    delims: list[tuple[int, int, int, bool]] = []
    pos = 0
    while True:
        idx = body.find(delim, pos)
        if idx < 0:
            break
        # RFC 2046：定界符只能出现在行首（preamble 前必须有 CRLF；
        # 第一个定界符若恰好在正文起始也宽容接受）
        at_line_start = idx == 0 or body[idx - 2:idx] == CRLF
        if not at_line_start:
            pos = idx + 1
            continue
        # 命中时 idx 指向 "--boundary" 起始；后续从 CRLF 之后继续
        j = idx + len(delim)
        while j < len(body) and body[j] in (0x20, 0x09):  # transport padding
            j += 1
        closing = body[j:j + 2] == DASHDASH
        if closing:
            k = j + 2
            while k < len(body) and body[k] in (0x20, 0x09):
                k += 1
            if k == len(body):
                delims.append((idx, -1, k, True))
                break
            if body[k:k + 2] == CRLF:
                delims.append((idx, -1, k + 2, True))
                break
            _issue(issues, "error", "BAD_DELIMITER",
                   f"{path}：关闭定界符 \"--{boundary.decode('latin-1', 'replace')}--\" "
                   f"之后偏移 {k} 处存在非法字符 0x{body[k]:02X}，已忽略该匹配")
            pos = idx + 1
            continue
        # 普通定界符行：空白之后必须是 CRLF
        if body[j:j + 2] == CRLF:
            delims.append((idx, j + 2, j + 2, False))
            pos = j + 2
            continue
        _issue(issues, "error", "BAD_DELIMITER",
               f"{path}：偏移 {idx} 处出现类似定界符的文本，但其后既无 \"--\" 也无 CRLF，"
               "已忽略（可能是正文中的碰撞文本）")
        pos = idx + 1

    result = {"preamble": b"", "parts": [], "epilogue": b"",
              "matched": False, "closed": False, "leadingCrlf": False}
    if not delims:
        _issue(issues, "error", "NO_DELIMITER",
               f"{path}：正文中找不到任何 \"--{boundary.decode('latin-1', 'replace')}\" 定界符")
        return result
    result["matched"] = True

    first = delims[0]
    preamble = body[:first[0]]
    if preamble.endswith(CRLF):
        # 该 CRLF 属于首定界符行；剥离后若仍有内容则为序言
        preamble_content = preamble[:-2]
        result["leadingCrlf"] = True
    else:
        preamble_content = preamble
        if preamble.strip(b" \t\r\n"):
            _issue(issues, "warning", "BAD_PREAMBLE",
                   f"{path}：首个定界符之前缺少 CRLF，前置文本可能无法被严格解析器识别")
        elif preamble:
            _issue(issues, "warning", "LEADING_WHITESPACE",
                   f"{path}：首个定界符前存在无 CRLF 的空白，已忽略")
        _issue(issues, "info", "NO_LEADING_CRLF",
               f"{path}：首个定界符前没有 CRLF（HTTP multipart 常见，"
               "但不符合 RFC 2046 形式语法）；重新编码时保留原样")
    if preamble_content.strip(b" \t\r\n"):
        preview = preamble_content[:80].decode("utf-8", errors="replace")
        _issue(issues, "info", "PREAMBLE",
               f"{path}：存在序言（preamble），MIME 处理器应忽略：{preview!r}")
    result["preamble"] = preamble_content

    opens = [d for d in delims if not d[3]]
    for k, (s, cstart, lend, closing) in enumerate(opens):
        nxt = delims[k + 1] if k + 1 < len(delims) else None
        end = nxt[0] if nxt is not None else len(body)
        seg = body[cstart:end]
        # 定界符行自带的前导 CRLF 属于定界符行，需要从正文末尾剥离
        if seg.endswith(CRLF):
            seg = seg[:-2]
        else:
            stripped = seg.rstrip(b" \t")
            if stripped.endswith(CRLF):
                _issue(issues, "warning", "TRAILING_PADDING",
                       f"{path}：第 {k + 1} 部分之后存在 transport padding，已忽略空白")
                seg = stripped[:-2]
            else:
                _issue(issues, "error", "TRUNCATED_PART",
                       f"{path}：第 {k + 1} 部分之后缺少结束该部分的 CRLF，报文可能被截断；"
                       "已尽力恢复正文")
        result["parts"].append(seg)

    last = delims[-1]
    result["closed"] = last[3]
    result["epilogue"] = body[last[2]:]
    if result["epilogue"].strip(b" \t\r\n"):
        preview = result["epilogue"].strip()[:80].decode("utf-8", errors="replace")
        _issue(issues, "info", "EPILOGUE",
               f"{path}：关闭定界符之后存在尾注（epilogue）：{preview!r}")

    if not result["parts"]:
        _issue(issues, "error", "NO_PARTS", f"{path}：multipart 不包含任何部分")
    if not last[3]:
        _issue(issues, "error", "MISSING_CLOSE_DELIMITER",
               f"{path}：缺少关闭定界符 \"--{boundary.decode('latin-1', 'replace')}--\"，"
               "最后一部分之后的内容可能被截断")
    return result


# --------------------------------------------------------------------------- #
# 解析为节点树
# --------------------------------------------------------------------------- #

def _decode_body_for_edit(data: bytes):
    """把字节转为可在 JSON 文本框中编辑的形式。

    返回 (text, encoding, warn)。判定“可文本编辑”要求同时满足：
    1. 合法 UTF-8；2. 不含 NUL 等 C0 控制字符（\\r\\n\\t 除外）；
    3. 不使用替换字符 U+FFFD（避免把二进制误当文本）。
    其余一律以 base64 呈现，未修改即可逐字节还原。
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = None
    if text is not None and "�" not in text and all(
            b >= 0x20 or b in (0x09, 0x0A, 0x0D) for b in data):
        return text, "utf-8", None
    return (base64.b64encode(data).decode("ascii"), "base64",
            "正文不是合法 UTF-8 文本，已按 base64 呈现；未修改即可逐字节还原")


def _block_first_line_is_header(header_block: bytes) -> bool:
    first_line = header_block.split(CRLF, 1)[0]
    if b":" not in first_line:
        return False
    name_b = first_line.split(b":", 1)[0]
    return bool(TOKEN_RE.fullmatch(name_b))


def _build_part(raw: bytes, issues: list[dict], path: str) -> dict:
    sep = raw.find(CRLF + CRLF)
    has_headers = False
    headers: list[dict] = []
    h_issues: list[dict] = []
    body = raw
    if sep >= 0 and _block_first_line_is_header(raw[:sep]):
        has_headers = True
        header_block = raw[:sep]
        body = raw[sep + 4:]
        headers, h_issues = parse_header_block(header_block)
    elif sep < 0:
        _issue(issues, "info", "HEADERLESS_PART",
               f"{path}：该部分没有头部块（仅正文）")
    else:
        _issue(issues, "warning", "NO_HEADER_BODY_SEPARATOR",
               f"{path}：空行之前不是合法头部字段，整块按正文处理")
    for it in h_issues:
        msg = f"{path}：{it['message']}"
        _issue(issues, it["level"], it["code"], msg)

    ct_header = find_header(headers, "Content-Type")
    node_issues: list[dict] = list(h_issues)

    is_multipart = False
    media_type = None
    params: dict[str, str] = {}
    boundary: Optional[bytes] = None
    if ct_header is not None:
        media_type, params = parse_content_type(ct_header["value"], node_issues,
                                                context=f"{path} Content-Type")
        for it in node_issues:
            _issue(issues, it["level"], it["code"], f"{path}：{it['message']}")
        is_multipart = bool(media_type and media_type.startswith("multipart/"))
        if "boundary" in params:
            boundary = params["boundary"].encode("utf-8", errors="replace")
        elif is_multipart:
            _issue(issues, "error", "MISSING_BOUNDARY",
                   f"{path}：{media_type} 缺少 boundary 参数，无法切分子部分")

    if is_multipart and boundary is not None:
        _check_multipart_cte(headers, issues, path)
        sp = split_multipart(body, boundary, issues, path=path)
        children = []
        for idx, p in enumerate(sp["parts"]):
            children.append(_build_part(p, issues, f"{path}.parts[{idx}]"))
        preamble_text, pre_enc, pre_warn = _decode_body_for_edit(sp["preamble"])
        epilogue_text, epi_enc, epi_warn = _decode_body_for_edit(sp["epilogue"])
        if pre_warn:
            _issue(issues, "info", "BINARY_PREAMBLE", f"{path}：{pre_warn}")
        if epi_warn:
            _issue(issues, "info", "BINARY_EPILOGUE", f"{path}：{epi_warn}")
        return {
            "kind": "multipart",
            "path": path,
            "mediaType": media_type,
            "boundary": boundary.decode("latin-1"),
            "headers": headers,
            "preamble": preamble_text,
            "preambleEncoding": pre_enc,
            "epilogue": epilogue_text,
            "epilogueEncoding": epi_enc,
            "leadingCrlf": sp["leadingCrlf"],
            "parts": children,
            "rawSize": len(raw),
        }

    leaf = _leaf_node_fields(headers, body, issues, path)
    return {
        "kind": "part",
        "path": path,
        "mediaType": media_type or "application/octet-stream",
        "headers": headers,
        "hasHeaders": has_headers,
        "rawSize": len(raw),
        **leaf,
    }


def _parse_secondary_params(value: str) -> dict[str, str]:
    """仅解析 `; key=value` 参数（用于 Content-Disposition）。"""
    issues: list[dict] = []
    _, params = parse_content_type("application/x-ignored; " + value, issues)
    return params


# --------------------------------------------------------------------------- #
# 顶层解析
# --------------------------------------------------------------------------- #

def looks_like_header_block(data: bytes) -> bool:
    """启发式判断报文是否以顶层头部块开头。

    严格判据：首个分隔符（CRLF CRLF / LF LF）之前的*第一行*就必须是
    合法的 `token: value`，且整个头部块每一行都是合法头部或折叠行。
    这样 multipart body（第一行通常是 ``--boundary``）不会被误判。
    """
    head: Optional[bytes] = None
    for double in (CRLF + CRLF, b"\n\n"):
        idx = data.find(double)
        if idx >= 0:
            head = data[:idx]
            break
    if head is None:
        return False
    nl = CRLF if CRLF in head else b"\n"
    lines = head.split(nl)
    if not lines:
        return False

    def is_header_line(line: bytes) -> bool:
        if not line or line[:1] in (b" ", b"\t"):
            return True  # 折叠延续行，交给头部解析器报告
        if b":" not in line:
            return False
        name = line.split(b":", 1)[0]
        return bool(TOKEN_RE.fullmatch(name))

    # 第一行必须是合法头部（最关键，排除 --boundary 起头的 body）
    if not is_header_line(lines[0]):
        return False
    return all(is_header_line(ln) for ln in lines)


def parse_message(raw_text: Optional[str], content_type_input: Optional[str],
                  boundary_input: Optional[str],
                  raw_base64: Optional[str] = None) -> dict:
    issues: list[dict] = []

    if raw_base64:
        try:
            data = base64.b64decode(raw_base64, validate=False)
        except (binascii.Error, ValueError):
            return {"ok": False, "error": "rawBase64 不是合法的 base64", "issues": []}
    else:
        data, err = _to_bytes(raw_text if raw_text is not None else "")
        if err:
            return {"ok": False, "error": err, "issues": []}

    # BOM
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
        _issue(issues, "warning", "BOM_STRIPPED", "输入以 UTF-8 BOM 开头，已移除")

    # 行结束规范化仅对“文本”报文执行；含 NUL/控制字节的二进制报文原样保留，
    # 否则会破坏二进制部分（孤立 CR 会被误改成 CRLF）。
    is_text_like = all(b >= 0x20 or b in (0x09, 0x0A, 0x0D) for b in data[:8192])
    if is_text_like:
        data, changed = normalize_newlines(data)
        if changed:
            _issue(issues, "warning", "LF_NORMALIZED",
                   "输入包含孤立 LF/CR 行结束符，已规范化为 CRLF（RFC 2046 要求 CRLF）")
    else:
        changed = False
        _issue(issues, "info", "BINARY_INPUT",
               "输入包含控制字节，按二进制报文处理，不做 LF→CRLF 规范化")

    has_top_headers = looks_like_header_block(data)
    top_headers: list[dict] = []
    if has_top_headers:
        block, _, rest = data.partition(CRLF + CRLF)
        top_headers, h_issues = parse_header_block(block)
        for it in h_issues:
            _issue(issues, it["level"], it["code"], f"顶层头部：{it['message']}")
        body = rest
    else:
        body = data

    # 决定 Content-Type / boundary（用户输入优先）
    ct_issues: list[dict] = []
    header_ct = find_header(top_headers, "Content-Type")
    header_media, header_params = (None, {})
    if header_ct is not None:
        header_media, header_params = parse_content_type(
            header_ct["value"], ct_issues, context="顶层 Content-Type")
    for it in ct_issues:
        _issue(issues, it["level"], it["code"], it["message"])

    media_type = None
    params: dict[str, str] = {}
    ct_source = None
    if content_type_input and content_type_input.strip():
        media_type, params = parse_content_type(
            content_type_input.strip(), issues, context="输入的 Content-Type")
        ct_source = "input"
        if header_ct is not None:
            _m2, _p2 = (None, {})
            if media_type and header_media and media_type != header_media:
                _issue(issues, "warning", "CT_OVERRIDE",
                       f"输入的媒体类型 {media_type} 覆盖了报文头部中的 {header_media}")
            if "boundary" in params and "boundary" in header_params and \
                    params["boundary"] != header_params["boundary"]:
                _issue(issues, "warning", "BOUNDARY_OVERRIDE",
                       f"输入的 boundary 与报文头部不一致，已使用输入值 "
                       f"{params.get('boundary')!r}")
    elif header_ct is not None:
        media_type, params = header_media, header_params
        ct_source = "header"
    else:
        media_type = "multipart/mixed"
        params = {}
        _issue(issues, "warning", "NO_CONTENT_TYPE",
               "未提供 Content-Type，已假定为 multipart/mixed")

    # 独立 boundary 输入框
    effective_boundary: Optional[bytes] = None
    if boundary_input and boundary_input.strip():
        # 输入框允许带引号也允许不带
        bval = boundary_input.strip()
        if len(bval) >= 2 and bval[0] == '"' and bval[-1] == '"':
            bval = bval[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        effective_boundary = bval.encode("latin-1", "replace")
        if "boundary" in params and params["boundary"] != bval:
            _issue(issues, "warning", "BOUNDARY_OVERRIDE",
                   f"boundary 输入框覆盖了 Content-Type 参数：{params['boundary']!r} -> {bval!r}")
        params = dict(params)
        params["boundary"] = bval
    elif "boundary" in params:
        effective_boundary = params["boundary"].encode("utf-8", errors="replace")

    if not media_type or not media_type.startswith("multipart/"):
        _issue(issues, "warning", "NOT_MULTIPART",
                       f"Content-Type 为 {media_type}，并非 multipart；将整块作为单个部分展示")

    # 保证编辑树里有一个可用的顶层 Content-Type 头
    if not has_top_headers:
        top_headers = [{"name": "Content-Type",
                        "value": build_content_type(media_type, params),
                        "raw": ""}]
    elif ct_source == "input":
        if header_ct is None:
            top_headers.append({"name": "Content-Type",
                                "value": build_content_type(media_type, params),
                                "raw": ""})
        else:
            header_ct["value"] = build_content_type(media_type, params)

    # 建树
    if media_type and media_type.startswith("multipart/") and effective_boundary is not None:
        if not BOUNDARY_BCHARS_RE.fullmatch(effective_boundary):
            _issue(issues, "warning", "BOUNDARY_NONCOMPLIANT",
                   "生效的 boundary 超过 70 字符或含不允许的字符")
        _check_multipart_cte(top_headers, issues, "root")
        sp = split_multipart(body, effective_boundary, issues, path="root")
        children = [
            _build_part(p, issues, f"parts[{idx}]")
            for idx, p in enumerate(sp["parts"])
        ]
        preamble_text, pre_enc, pre_warn = _decode_body_for_edit(sp["preamble"])
        epilogue_text, epi_enc, epi_warn = _decode_body_for_edit(sp["epilogue"])
        if pre_warn:
            _issue(issues, "info", "BINARY_PREAMBLE", f"root：{pre_warn}")
        if epi_warn:
            _issue(issues, "info", "BINARY_EPILOGUE", f"root：{epi_warn}")
        tree = {
            "kind": "multipart",
            "path": "root",
            "mediaType": media_type,
            "boundary": effective_boundary.decode("latin-1"),
            "headers": top_headers,
            "preamble": preamble_text,
            "preambleEncoding": pre_enc,
            "epilogue": epilogue_text,
            "epilogueEncoding": epi_enc,
            "leadingCrlf": sp["leadingCrlf"],
            "parts": children,
            "rawSize": len(data),
            "delimiterMatched": sp["matched"],
            "delimiterClosed": sp["closed"],
        }
    else:
        if media_type and media_type.startswith("multipart/") and effective_boundary is None:
            _issue(issues, "error", "MISSING_BOUNDARY",
                   "multipart 报文缺少 boundary 参数，无法切分")
        leaf = _leaf_node_fields(top_headers, body, issues, "root")
        tree = {
            "kind": "part",
            "path": "root",
            "mediaType": media_type or "application/octet-stream",
            "headers": top_headers,
            "hasHeaders": bool(has_top_headers),
            "rawSize": len(data),
            **leaf,
        }

    counts = {"error": 0, "warning": 0, "info": 0}
    for it in issues:
        counts[it["level"]] = counts.get(it["level"], 0) + 1

    return {
        "ok": True,
        "tree": tree,
        "issues": issues,
        "counts": counts,
        "hasTopHeaders": has_top_headers,
        "effectiveBoundary": effective_boundary.decode("latin-1")
        if effective_boundary is not None else None,
        "mediaType": media_type,
        "normalizedCrlf": changed,
    }


# --------------------------------------------------------------------------- #
# 重新编码
# --------------------------------------------------------------------------- #

def _encode_text(s, encoding: str) -> bytes:
    """与 _decode_body_for_edit 对应，把编辑器文本还原为字节。"""
    if s is None:
        return b""
    if encoding == "base64":
        try:
            return base64.b64decode(s, validate=False)
        except (binascii.Error, ValueError):
            # 用户改动后可能不再是合法 base64，退回逐字节编码
            return s.encode("latin-1", errors="replace")
    return s.encode("utf-8", errors="replace")


def _encode_base64_wire(data: bytes) -> bytes:
    """RFC 2045 §6.8：base64，每行 76 字符，CRLF 折行（末行不带多余 CRLF）。"""
    if not data:
        return b""
    encoded = base64.b64encode(data)
    lines = [encoded[i:i + MAX_ENCODED_LINE]
             for i in range(0, len(encoded), MAX_ENCODED_LINE)]
    return CRLF.join(lines)


def _encode_quoted_printable_wire(data: bytes) -> bytes:
    """RFC 2045 §6.7 quoted-printable 编码：

    * 可打印 ASCII（除 '='）原样输出；
    * '='、非 ASCII 与 C0 控制字节编码为 =XX；
    * 编码行不超过 76 字符，超长以 '=' 软折行（CRLF）；
    * 硬换行前（及数据末尾）的空格/制表符也编码为 =20/=09；
    * 原始 CRLF 保留为硬换行，孤立 CR/LF 编码（不生成裸换行）。
    """
    # 1) 先切分 token：普通可打印字节、=XX 转义、CRLF 硬换行标记
    tokens: list = []
    i, n = 0, len(data)
    while i < n:
        b = data[i]
        if b == 0x0D and i + 1 < n and data[i + 1] == 0x0A:
            tokens.append(("nl", CRLF))
            i += 2
            continue
        if 33 <= b <= 126 and b != 0x3D:
            tokens.append(("ch", bytes([b])))
        elif b in (0x20, 0x09):
            # 后随硬换行或位于数据末尾的 WSP 必须编码（RFC 2045 rule 3）
            if i + 1 >= n:
                follows_nl = False
                at_end = True
            else:
                at_end = False
                follows_nl = (data[i + 1] == 0x0A or
                              (data[i + 1] == 0x0D and
                               (i + 2 >= n or data[i + 2] == 0x0A)))
            tokens.append(("ch", b"=%02X" % b) if (at_end or follows_nl)
                          else ("ch", bytes([b])))
        else:
            tokens.append(("ch", b"=%02X" % b))
        i += 1

    # 2) 组装逻辑行，超过 75 字符就先补 '=' 软折行
    out_lines: list[bytes] = []
    line = bytearray()
    for kind, tok in tokens:
        if kind == "nl":
            out_lines.append(bytes(line))
            line = bytearray()
            continue
        if line and len(line) + len(tok) > 75:
            line.append(0x3D)
            out_lines.append(bytes(line))
            line = bytearray()
        line += tok
    out_lines.append(bytes(line))
    return CRLF.join(out_lines)


def _sync_cte_header(headers: list[dict], cte: str, issues: list[dict], path: str):
    """让 Content-Transfer-Encoding 头部与节点选择的 CTE 保持一致。

    已有头部时就地改值（7bit/8bit/binary 也同步，保证选择生效）；
    没有头部且 CTE 为非透传编码（base64 / quoted-printable / 未知）时新增，
    默认的 7bit 不凭空添加头部。
    """
    h = find_header(headers, "Content-Transfer-Encoding")
    if h is not None:
        if (h.get("value") or "").strip().lower() != cte:
            h["value"] = cte
            h["name"] = "Content-Transfer-Encoding"
        return
    if cte not in IDENTITY_TRANSFER_ENCODINGS:
        headers.append({"name": "Content-Transfer-Encoding",
                        "value": cte, "raw": ""})
        _issue(issues, "info", "CTE_HEADER_ADDED",
               f"{path}：已添加 Content-Transfer-Encoding: {cte} 头部")


def _sync_boundary_header(headers: list[dict], media_type: str, boundary: str,
                          issues: list[dict], path: str):
    ct = find_header(headers, "Content-Type")
    params: dict[str, str] = {}
    mt = media_type
    if ct is not None:
        mt2, params2 = parse_content_type(ct["value"], issues,
                                          context=f"{path} Content-Type")
        mt = mt2 or media_type
        params = params2
    else:
        ct = {"name": "Content-Type", "value": "", "raw": ""}
        headers.insert(0, ct)
    if params.get("boundary") != boundary:
        if "boundary" in params:
            _issue(issues, "warning", "BOUNDARY_SYNCED",
                   f"{path}：已用编辑器中的 boundary 同步 Content-Type 参数 "
                   f"（{params.get('boundary')!r} -> {boundary!r}）")
        params["boundary"] = boundary
    ct["value"] = build_content_type(mt or "multipart/mixed", params)
    ct["name"] = "Content-Type"


def _validate_headers(headers: list[dict], issues: list[dict], path: str):
    seen = set()
    for h in headers:
        name = (h.get("name") or "").strip()
        value = h.get("value") or ""
        if not name:
            _issue(issues, "error", "EMPTY_HEADER_NAME", f"{path}：存在空字段名的头部")
            continue
        if not TOKEN_RE.fullmatch(name.encode("ascii", "replace")):
            _issue(issues, "error", "BAD_HEADER_NAME",
                   f"{path}：字段名 {name!r} 非法（RFC 要求 token）")
        if name.lower() in seen:
            _issue(issues, "info", "DUPLICATE_HEADER",
                   f"{path}：重复的头部字段 {name!r}（将全部保留）")
        seen.add(name.lower())
        try:
            value.encode("ascii")
        except UnicodeEncodeError:
            _issue(issues, "warning", "NON_ASCII_HEADER",
                   f"{path}：头部 {name!r} 含非 ASCII 字符，已按 UTF-8 编码"
                   "（严格 MIME 应使用 RFC 2047 编码字）")
        if b"\r" in value.encode("utf-8", "replace") or b"\n" in value.encode("utf-8", "replace"):
            _issue(issues, "error", "HEADER_FOLD",
                   f"{path}：头部 {name!r} 的值含有换行，请删除")


def _build_head(headers: list[dict]) -> bytes:
    head = bytearray()
    for h in headers:
        name = (h.get("name") or "").strip()
        if not name:
            continue
        head += name.encode("ascii", "replace") + b": " + \
                (h.get("value") or "").encode("utf-8", "replace") + CRLF
    return bytes(head)


def _encode_leaf_body(node: dict, cte: str, issues: list[dict], path: str) -> bytes:
    """把编辑器中的“已解码正文”按所选 CTE 编码回传输字节。"""
    decoded = _encode_text(node.get("body", ""),
                           node.get("bodyEncoding", "utf-8"))
    if cte == "base64":
        return _encode_base64_wire(decoded)
    if cte == "quoted-printable":
        return _encode_quoted_printable_wire(decoded)
    if cte in IDENTITY_TRANSFER_ENCODINGS:
        _check_identity_cte(decoded, cte, issues, path)
        return decoded
    # 未知 / 非标准 CTE：原样透传编辑器字节
    _issue(issues, "warning", "UNKNOWN_TRANSFER_ENCODING",
           f"{path}：未知的 Content-Transfer-Encoding {cte!r}，正文按原始字节透传")
    return decoded


def _assemble_node(node: dict, issues: list[dict], path: str):
    """组装单个 entity，返回 (head, body)：
    * head：不含结尾空行的头部块；
    * body：entity 正文。
        - part：裸内容（头部与正文之间的空行 CRLF 由 entity 边界负责）；
        - multipart：完整 multipart body，按 RFC 2046 以起始 CRLF 开头
          （preamble 或第一个定界符之前必须有 CRLF）。
    完整 entity = head + CRLF + body。
    """
    kind = node.get("kind", "part")
    headers = node.get("headers", [])
    _validate_headers(headers, issues, path)

    if kind == "multipart":
        boundary = node.get("boundary") or ""
        bval = boundary.encode("utf-8", errors="replace")
        if not boundary:
            _issue(issues, "error", "EMPTY_BOUNDARY", f"{path}：boundary 为空")
        elif not BOUNDARY_BCHARS_RE.fullmatch(bval):
            _issue(issues, "warning", "BOUNDARY_NONCOMPLIANT",
                   f"{path}：boundary {boundary!r} 不符合 RFC 2046 长度/字符限制")
        _sync_boundary_header(headers, node.get("mediaType") or "multipart/mixed",
                              boundary, issues, path)
        head = _build_head(headers)

        out = bytearray()
        preamble = _encode_text(node.get("preamble", ""),
                                node.get("preambleEncoding", "utf-8"))
        # 首定界符之前必须有 CRLF（RFC 2046）：
        # 序言非空时其结尾的 CRLF 即充当该换行；
        # 无序言时按 leadingCrlf（缺省 True）补一个。
        leading_crlf = node.get("leadingCrlf", True)
        if preamble:
            if not preamble.endswith(CRLF):
                preamble += CRLF
            out += preamble
        elif leading_crlf:
            out += CRLF
        delim = DASHDASH + bval
        parts = node.get("parts", [])
        if not parts:
            _issue(issues, "warning", "EMPTY_MULTIPART",
                   f"{path}：multipart 不包含任何部分")
        for idx, child in enumerate(parts):
            out += delim + CRLF
            chead, cbody = _assemble_node(child, issues, f"{path}.parts[{idx}]")
            entity = (chead + CRLF) if chead else b""
            entity += cbody
            # 叶子部分必须保证其内容不会在“行首”伪造本层定界符
            if child.get("kind") != "multipart" and (CRLF + delim) in entity:
                _issue(issues, "error", "BOUNDARY_COLLISION",
                       f"{path}.parts[{idx}]：正文在某行行首包含定界符 "
                       f"\"--{boundary}\"，重新编码后的报文会被提前截断；"
                       "请修改 boundary 或对该部分使用 Content-Transfer-Encoding")
            out += entity
            # CRLF 属于下一个 boundary line（RFC 2046 明确规定）
            out += CRLF
        out += delim + DASHDASH
        epilogue = _encode_text(node.get("epilogue", ""),
                                node.get("epilogueEncoding", "utf-8"))
        if epilogue:
            # 关闭定界符行以 CRLF 结束，随后是 epilogue 内容
            out += CRLF + epilogue.lstrip(b"\r\n")
        return head, bytes(out)

    # hasHeaders=False 的部分原本就没有头部块（也没有头/体空行），
    # 必须逐字节保留；用户一旦添加头部则恢复标准 head+CRLF+body 结构。
    cte = (node.get("transferEncoding") or "7bit").strip().lower() or "7bit"
    declared = (node.get("transferEncodingDeclared") or cte).strip().lower()
    if node.get("hasHeaders") is False and not headers:
        # 未编辑的无头部部分：直接回写解析时保留的原始传输体
        raw_b64 = node.get("rawBodyBase64")
        if not node.get("bodyEdited") and raw_b64:
            return b"", base64.b64decode(raw_b64)
        return b"", _encode_leaf_body(node, cte, issues, path)

    # 未编辑节点字节稳定：CTE 未切换且正文未改动时回写原始传输字节
    raw_b64 = node.get("rawBodyBase64")
    unedited = (not node.get("bodyEdited")) and raw_b64 and cte == declared
    if unedited:
        body = base64.b64decode(raw_b64)
    else:
        body = _encode_leaf_body(node, cte, issues, path)
    _sync_cte_header(headers, cte, issues, path)
    head = _build_head(headers)
    return head, body


def encode_tree(tree: dict, include_headers: bool) -> dict:
    issues: list[dict] = []
    if not isinstance(tree, dict):
        return {"ok": False, "error": "报文树格式错误", "issues": []}

    head, body = _assemble_node(tree, issues, "root")
    if include_headers:
        message = head + CRLF + body
    else:
        # 仅输出正文（multipart body 自带起始 CRLF，符合 RFC 2046）
        message = body
        if head:
            _issue(issues, "info", "HEADERS_DROPPED",
                   "已按选项省略顶层头部，仅输出 multipart body")

    # 自检：若以 multipart 根重新解析，定界符必须可被找到
    root_boundary = tree.get("boundary") if tree.get("kind") == "multipart" else None
    if root_boundary:
        token = DASHDASH + root_boundary.encode("latin-1", "replace")
        if message.find(token) < 0:
            _issue(issues, "error", "ENCODE_VERIFY_FAILED",
                   "自检验证失败：输出中找不到预期定界符")
        elif message.find(token + DASHDASH) < 0:
            _issue(issues, "warning", "ENCODE_NOT_CLOSED",
                   "自检：输出缺少关闭定界符")

    counts = {"error": 0, "warning": 0, "info": 0}
    for it in issues:
        counts[it["level"]] = counts.get(it["level"], 0) + 1

    return {
        "ok": counts["error"] == 0,
        "payloadBase64": base64.b64encode(message).decode("ascii"),
        "size": len(message),
        "issues": issues,
        "counts": counts,
        "lineEnding": "CRLF",
    }
