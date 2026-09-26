"""附件提取：从（可能已编辑的）解析树中发现附件叶子并打包 ZIP。

规则：
* 附件叶子：Content-Disposition 为 ``attachment``，或带任意形式的文件名参数
  （``filename`` / ``filename*`` / RFC 2231 分段 ``filename*0*`` 等）；
* 文件名优先级：合法的 RFC 2231 扩展值（含分段拼接）> 普通 ``filename``
  > 按节点路径命名；非法扩展值回退普通值；
* ZIP 内文件名为净化后结果：去除路径分隔符（``/``、``\\``）与控制字符，
  重名按树的先序遍历顺序加序号；
* ZIP 条目字节为 **CTE 解码后** 的字节：已编辑叶子取编辑后内容；
  未编辑叶子仅在 ``raw``/``decoded`` 状态可导出，``malformed``（畸形
  base64/quoted-printable）与 ``passthrough``（未知 CTE）状态禁导并给出原因。
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Optional

from .mime import _encode_text, find_header, parse_content_type

# filename*0 / filename*0* / filename*12* 形式的分段参数
_SEG_KEY_RE = re.compile(r"^(?P<base>.+)\*(?P<idx>\d+)(?P<star>\*?)$")
_HEX_RE = re.compile(r"^[0-9A-Fa-f]{2}$")

# ZIP 头与各主流文件系统的保守文件名长度
_MAX_NAME_LEN = 200
_FALLBACK_PREFIX = "attachment"


# --------------------------------------------------------------------------- #
# Content-Disposition / RFC 2231
# --------------------------------------------------------------------------- #

def parse_disposition(headers: list[dict]):
    """返回 ``(disposition_type|None, params)``。

    disposition_type 为 ``attachment`` / ``form-data`` / ``inline`` 等小写 token；
    参数按与 Content-Type 相同的 quoted-string/转义规则解析，参数名小写。
    """
    h = find_header(headers, "Content-Disposition")
    if h is None:
        return None, {}
    value = h.get("value") or ""
    throwaway: list[dict] = []
    _, params = parse_content_type("application/x-ignored; " + value, throwaway)
    head = value.split(";", 1)[0].strip().strip('"').lower()
    return head or None, params


def _percent_decode(text: str) -> Optional[bytes]:
    """RFC 2231/3986 百分号解码；遇非法 ``%`` 转义返回 None。

    未转义字节按 UTF-8 透传（attr-char 本就是 ASCII，非 ASCII 属宽容处理）。
    """
    out = bytearray()
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "%":
            hx = text[i + 1:i + 3]
            if not _HEX_RE.match(hx):
                return None
            out.append(int(hx, 16))
            i += 3
            continue
        out += c.encode("utf-8", errors="replace")
        i += 1
    return bytes(out)


def _decode_single_extended(value: str, key: str):
    """解码 ``filename*=charset'lang'%xx`` 形式的单值扩展参数。"""
    parts = value.split("'", 2)
    if len(parts) != 3:
        return None, f"{key} 不是合法的 charset'language'value 扩展值"
    charset = parts[0] or "utf-8"
    chunk = _percent_decode(parts[2])
    if chunk is None:
        return None, f"{key} 含非法的百分号转义"
    try:
        return chunk.decode(charset), None
    except LookupError:
        return None, f"{key} 指定了未知字符集 {charset!r}"
    except UnicodeDecodeError:
        return None, f"{key} 的字节无法按 {charset!r} 解码"


def _extended_value(params: dict[str, str], base: str = "filename"):
    """按 RFC 2231 组装扩展文件名，返回 ``(text|None, note|None)``。

    支持 ``base*`` 单值与 ``base*0*``/``base*1``/… 连续分段（分段可混合扩展
    与非扩展形式，首个扩展段携带 charset'language'）。任何非法情况都返回
    ``(None, 原因)``，由调用方回退普通值。
    """
    segments: dict[int, tuple[bool, str]] = {}
    for key, val in params.items():
        m = _SEG_KEY_RE.match(key)
        if m and m.group("base") == base:
            segments[int(m.group("idx"))] = (bool(m.group("star")), val)

    if segments:
        if 0 not in segments:
            return None, f"{base}* 的分段参数必须从 {base}*0 开始"
        max_idx = max(segments)
        if set(segments) != set(range(max_idx + 1)):
            missing = sorted(set(range(max_idx + 1)) - set(segments))
            return None, f"{base}* 分段不连续（缺少段 {missing[0]}）"
        raw = bytearray()
        charset: Optional[str] = None
        for idx in range(max_idx + 1):
            is_ext, val = segments[idx]
            if is_ext:
                # 仅序列中的第一个扩展段允许携带 charset'language' 前缀
                if charset is None and val.count("'") >= 2:
                    cs, _lang, pct = val.split("'", 2)
                    charset = cs or "utf-8"
                    chunk = _percent_decode(pct)
                elif charset is not None or "'" not in val:
                    chunk = _percent_decode(val)
                else:
                    return None, f"{base}*{idx}* 缺少 charset'language' 前缀"
                if chunk is None:
                    return None, f"{base}*{idx}* 含非法的百分号转义"
                raw += chunk
            else:
                # 非扩展段为头部文本：首个扩展段已指定 charset 时按该
                # charset 解释（RFC 2231 所有段共享同一字符集），否则按 UTF-8
                raw += val.encode(charset or "utf-8", errors="replace")
        try:
            return bytes(raw).decode(charset or "utf-8"), None
        except LookupError:
            return None, f"{base}* 指定了未知字符集 {charset!r}"
        except UnicodeDecodeError:
            enc = charset or "utf-8"
            return None, f"{base}* 的字节无法按 {enc!r} 解码"

    single = base + "*"
    if single in params:
        return _decode_single_extended(params[single], single)
    return None, None  # 不存在扩展参数


def resolve_filename(params: dict[str, str]):
    """决定叶子使用的文件名，返回 ``(filename|None, source, note)``。

    source: ``extended``（RFC 2231 扩展值）/ ``plain``（普通 filename）/
    ``path``（无文件名，调用方应按节点路径命名）。
    """
    if not params:
        return None, "path", None
    extended, ext_note = _extended_value(params, "filename")
    if extended:
        return extended, "extended", ext_note
    plain = params.get("filename")
    if plain:
        return plain, "plain", ext_note
    return None, "path", ext_note


def has_filename_param(params: dict[str, str]) -> bool:
    if params.get("filename"):
        return True
    for k in params:
        if k == "filename*":
            return True
        m = _SEG_KEY_RE.match(k)
        if m and m.group("base") == "filename":
            return True
    return False


# --------------------------------------------------------------------------- #
# 文件名净化与重名
# --------------------------------------------------------------------------- #

def _strip_unsafe(name: str) -> str:
    return "".join(
        c for c in name
        if c not in "/\\" and not (ord(c) < 0x20 or ord(c) == 0x7F)
    )


def _path_name(path: str) -> str:
    """无文件名时按节点路径命名（路径里只有字母/数字/./[]，仍做一次净化）。"""
    name = _strip_unsafe(path or _FALLBACK_PREFIX).strip()
    return name or _FALLBACK_PREFIX


def sanitize_filename(name: Optional[str], path: str) -> str:
    """清除路径分隔符与控制字符；结果为空或仅点号时按节点路径命名。"""
    cleaned = _strip_unsafe(name or "").strip()
    # Windows 不允许文件名以空格/点号结尾，去掉后可能整体为空
    cleaned = cleaned.rstrip(" .")
    if not cleaned or cleaned in (".", ".."):
        cleaned = _path_name(path)
    if len(cleaned) > _MAX_NAME_LEN:
        stem, dot, ext = cleaned.rpartition(".")
        if dot and len(ext) < 40:
            cleaned = stem[:_MAX_NAME_LEN - len(ext) - 1] + dot + ext
        else:
            cleaned = cleaned[:_MAX_NAME_LEN]
    return cleaned


def _dedupe_one(name: str, used: set[str]) -> str:
    """重名按调用顺序加序号：``a.txt`` -> ``a (2).txt``（大小写不敏感）。"""
    if name.lower() not in used:
        used.add(name.lower())
        return name
    stem, dot, ext = name.rpartition(".")
    base = stem if dot else name
    suffix = ("." + ext) if dot else ""
    n = 2
    while True:
        candidate = f"{base} ({n}){suffix}"
        if candidate.lower() not in used:
            used.add(candidate.lower())
            return candidate
        n += 1


# --------------------------------------------------------------------------- #
# 附件发现 / 导出字节
# --------------------------------------------------------------------------- #

def leaf_export_bytes(node: dict):
    """返回 ``(bytes|None, reason|None)``。

    * 已编辑叶子（``bodyEdited``）：取编辑器中 CTE 解码后的内容；
    * 未编辑且 ``decodeState`` 为 raw/decoded：由编辑通道还原解码字节；
    * 未编辑且 malformed/passthrough：禁导并给出原因。
    """
    edited = bool(node.get("bodyEdited"))
    state = node.get("decodeState", "raw")
    if edited or state in ("raw", "decoded"):
        return _encode_text(node.get("body", ""),
                            node.get("bodyEncoding", "utf-8")), None
    cte = node.get("transferEncoding") or "?"
    if state == "malformed":
        return None, (
            f"Content-Transfer-Encoding（{cte}）的传输正文畸形，"
            "未编辑时无法保证 CTE 解码字节正确；请在结构树中编辑该节点后再导出")
    return None, (
        f"未知的 Content-Transfer-Encoding（{cte}）无法进行传输解码；"
        "请在结构树中编辑该节点后再导出")


def _collect(tree: dict):
    """先序遍历，收集附件条目（附带节点引用，仅供后端内部使用）。"""
    entries: list[dict] = []

    def walk(node):
        if not isinstance(node, dict):
            return
        if node.get("kind") == "multipart":
            for child in node.get("parts") or []:
                walk(child)
            return
        headers = node.get("headers") or []
        disposition, params = parse_disposition(headers)
        if disposition != "attachment" and not has_filename_param(params):
            return
        filename, source, note = resolve_filename(params)
        data, reason = leaf_export_bytes(node)
        path = node.get("path") or ""
        entries.append({
            "path": path,
            "mediaType": node.get("mediaType") or "application/octet-stream",
            "disposition": disposition,
            "filename": filename,
            "filenameSource": source,
            "filenameNote": note,
            "transferEncoding": node.get("transferEncoding") or "7bit",
            "decodeState": node.get("decodeState", "raw"),
            "edited": bool(node.get("bodyEdited")),
            "exportable": data is not None,
            "reason": reason,
            "size": len(data) if data is not None else 0,
            "_node": node,
        })

    walk(tree if isinstance(tree, dict) else {})
    # 先序顺序下净化文件名并去重（先序即遍历顺序）
    used: set[str] = set()
    for e in entries:
        chosen = e["filename"] if e["filenameSource"] != "path" else None
        e["zipName"] = _dedupe_one(sanitize_filename(chosen, e["path"]), used)
    return entries


def _public_entry(e: dict) -> dict:
    return {k: v for k, v in e.items() if not k.startswith("_")}


def list_attachments(tree: dict) -> dict:
    entries = [_public_entry(e) for e in _collect(tree)]
    exportable = sum(1 for e in entries if e["exportable"])
    return {
        "ok": True,
        "count": len(entries),
        "exportableCount": exportable,
        "attachments": entries,
    }


def build_attachments_zip(tree: dict, paths: list[str]):
    """把勾选的附件按先序顺序打入 ZIP，返回 ``(zip_bytes|None, error_dict)``。"""
    entries = _collect(tree)
    by_path = {e["path"]: e for e in entries}
    selected = [p for p in (paths or []) if p in by_path]
    missing = [p for p in (paths or []) if p not in by_path]
    blocked = [p for p in selected if not by_path[p]["exportable"]]
    if not paths:
        return None, {"error": "未勾选任何附件", "code": "EMPTY_SELECTION"}
    if missing:
        return None, {"error": "勾选的部分附件在当前树中不存在（结构已变化？），请刷新列表",
                      "code": "STALE_PATHS", "paths": missing}
    if blocked:
        return None, {"error": "勾选的附件中有当前不可导出的节点，请刷新列表后重选",
                      "code": "NOT_EXPORTABLE", "paths": blocked}

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for e in entries:  # 已按先序排列
            if e["path"] not in paths:
                continue
            data, _reason = leaf_export_bytes(e["_node"])
            info = zipfile.ZipInfo(e["zipName"], date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # 文件名可能含非 ASCII（zipName 来自 UTF-8 文本）：置语言编码标志位
            info.flag_bits |= 0x0800
            zf.writestr(info, data or b"")
    return buf.getvalue(), None
