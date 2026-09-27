"""CID 引用检查：HTML 中的 ``cid:`` 引用与 multipart/related 内 Content-ID 的匹配。

全部基于**当前树**（即可能已被前端编辑过的 ``/api/parse`` 结果）：

* 类型与 Content-ID 一律从节点**当前头部**读取（Content-Type / Content-ID），
  不依赖解析时缓存的 ``mediaType`` 字段；
* HTML 正文从正文编辑通道还原字节（``body`` + ``bodyEncoding``，与重新编码
  共用同一套还原逻辑）；只有还原结果是**合法 UTF-8** 的 ``text/html`` 叶子
  才会被解析，其他 HTML 叶子报“无法检查”；
* 仅从 ``src`` / ``href`` 属性中提取 ``cid:`` 引用（标准库 HTMLParser 纯文本
  解析，不执行脚本、不加载任何资源）；
* 每个 HTML 叶子的匹配作用域是**最近的 multipart/related 祖先**；收集候选
  叶子时排除嵌套的 multipart/related 子树（它们构成自己的作用域）；
  没有 related 祖先时引用报“无作用域”；
* 规范化：Content-ID 去首尾空白与外层一对尖括号；引用去掉 ``cid:`` 前缀
  （方案名不区分大小写）后百分号解码一次；标识符**区分大小写**；
* 每条引用报告：来源路径、原始引用、规范化标识符、作用域路径、候选路径，
  以及唯一匹配 / 缺失 / 重复歧义（歧义时列出全部候选，不默认选取）。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import unquote

from .mime import _encode_text, find_header, parse_content_type

# src / href 之外的属性不视为资源引用
_REF_ATTRS = ("src", "href")


# --------------------------------------------------------------------------- #
# 规范化
# --------------------------------------------------------------------------- #

def normalize_content_id(value: Optional[str]) -> str:
    """Content-ID 头值 -> 规范化标识符：去首尾空白，再去外层一对尖括号。"""
    v = (value or "").strip()
    if len(v) >= 2 and v.startswith("<") and v.endswith(">"):
        v = v[1:-1].strip()
    return v


def is_cid_reference(value: Optional[str]) -> bool:
    """属性值是否为 ``cid:`` 引用（方案名不区分大小写，允许首尾空白）。"""
    return (value or "").strip()[:4].lower() == "cid:"


def normalize_reference(reference: Optional[str]) -> str:
    """``cid:`` 引用 -> 规范化标识符。

    去掉 ``cid:`` 前缀后**百分号解码一次**（``%2540`` -> ``%40``，不会继续
    解码成 ``@``）；非法的 ``%`` 转义按字面保留。
    """
    v = (reference or "").strip()
    if v[:4].lower() == "cid:":
        v = v[4:]
    return unquote(v, encoding="utf-8", errors="replace")


# --------------------------------------------------------------------------- #
# 当前头部 -> 类型 / Content-ID
# --------------------------------------------------------------------------- #

def node_media_type(node: dict) -> Optional[str]:
    """按节点**当前** Content-Type 头判断媒体类型；头部缺失/畸形时回退解析缓存。"""
    ct = find_header(node.get("headers") or [], "Content-Type")
    if ct is not None:
        mt, _params = parse_content_type(ct.get("value"), [])
        if mt:
            return mt
    cached = (node.get("mediaType") or "").strip().lower()
    return cached or None


def leaf_content_id(node: dict) -> Optional[str]:
    """叶子的规范化 Content-ID；无该头部或规范化后为空时返回 None。"""
    h = find_header(node.get("headers") or [], "Content-ID")
    if h is None:
        return None
    return normalize_content_id(h.get("value")) or None


# --------------------------------------------------------------------------- #
# HTML 中 cid: 引用提取（纯文本解析，不执行/加载资源）
# --------------------------------------------------------------------------- #

class _CidRefParser(HTMLParser):
    """只读取开始标签的 src/href 属性；不执行脚本、不发起任何加载。"""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.refs: list[dict] = []

    def handle_starttag(self, tag, attrs):
        self._scan(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._scan(tag, attrs)

    def _scan(self, tag, attrs):
        for name, value in attrs:
            if value is None:
                continue
            lname = name.lower()
            if lname in _REF_ATTRS and is_cid_reference(value):
                self.refs.append({
                    "tag": tag,
                    "attribute": lname,
                    "reference": value.strip(),
                })


def extract_cid_refs(html_text: str) -> list[dict]:
    """从 HTML 文本提取 ``cid:`` 引用，返回 [{tag, attribute, reference}]。"""
    parser = _CidRefParser()
    parser.feed(html_text)
    parser.close()
    return parser.refs


# --------------------------------------------------------------------------- #
# 作用域与候选
# --------------------------------------------------------------------------- #

def _scope_leaves(related_node: dict) -> list[dict]:
    """multipart/related 作用域内的候选叶子（先序）。

    嵌套的 multipart/related 子树整体排除（它们是自己的作用域）；
    其他类型的嵌套 multipart 继续深入。
    """
    leaves: list[dict] = []

    def walk(node):
        for child in node.get("parts") or []:
            if not isinstance(child, dict):
                continue
            if child.get("kind") == "multipart":
                if node_media_type(child) == "multipart/related":
                    continue  # 嵌套 related 子树排除
                walk(child)
            else:
                leaves.append(child)

    walk(related_node)
    return leaves


def check_cid_references(tree: dict) -> dict:
    """对当前树执行 CID 引用检查，返回可 JSON 序列化的报告。"""
    entries: list[dict] = []
    html_parts: list[dict] = []
    scopes: list[dict] = []
    scope_by_id: dict[int, dict] = {}

    def scope_for(mp_node: dict) -> dict:
        """multipart/related 节点的作用域描述（含 Content-ID 索引），按需构建。"""
        key = id(mp_node)
        scope = scope_by_id.get(key)
        if scope is None:
            content_ids: list[dict] = []
            index: dict[str, list[dict]] = {}
            for leaf in _scope_leaves(mp_node):
                cid = leaf_content_id(leaf)
                if cid is None:
                    continue
                item = {"path": leaf.get("path") or "", "contentId": cid}
                content_ids.append(item)
                index.setdefault(cid, []).append(item)
            for item in content_ids:
                # 同一作用域内标识符重复：仅标记，匹配时不默认选取
                if len(index[item["contentId"]]) > 1:
                    item["duplicate"] = True
            scope = {
                "path": mp_node.get("path") or "",
                "contentIds": content_ids,
                "_index": index,
            }
            scope_by_id[key] = scope
            scopes.append(scope)
        return scope

    def walk(node, related_stack):
        if not isinstance(node, dict):
            return
        if node.get("kind") == "multipart":
            stack = related_stack
            if node_media_type(node) == "multipart/related":
                stack = related_stack + [node]
            for child in node.get("parts") or []:
                walk(child, stack)
            return
        # 叶子：仅 text/html 参与；类型按当前头部判断
        if node_media_type(node) != "text/html":
            return
        path = node.get("path") or ""
        # 从正文编辑通道还原字节（与重新编码相同的还原逻辑）
        data = _encode_text(node.get("body", ""),
                            node.get("bodyEncoding", "utf-8"))
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            html_parts.append({
                "path": path,
                "checkable": False,
                "reason": "正文还原字节不是合法 UTF-8（编辑通道为 base64），无法检查",
            })
            return
        try:
            refs = extract_cid_refs(text)
        except Exception as exc:  # HTMLParser 实际不会抛错，防御性兜底
            html_parts.append({
                "path": path,
                "checkable": False,
                "reason": f"HTML 解析失败：{exc}",
            })
            return
        html_parts.append({"path": path, "checkable": True,
                           "refCount": len(refs)})
        scope = scope_for(related_stack[-1]) if related_stack else None
        for ref in refs:
            identifier = normalize_reference(ref["reference"])
            entry = {
                "sourcePath": path,
                "tag": ref["tag"],
                "attribute": ref["attribute"],
                "reference": ref["reference"],
                "identifier": identifier,
                "scopePath": scope["path"] if scope else None,
                "candidates": [],
                "status": "no-scope",
            }
            if scope is not None:
                matches = scope["_index"].get(identifier, [])
                entry["candidates"] = [dict(m) for m in matches]
                if len(matches) == 1:
                    entry["status"] = "ok"
                elif not matches:
                    entry["status"] = "missing"
                else:
                    # 重复歧义：列出全部候选，不默认选取
                    entry["status"] = "ambiguous"
            entries.append(entry)

    walk(tree if isinstance(tree, dict) else {}, [])

    summary = {"ok": 0, "missing": 0, "ambiguous": 0, "noScope": 0}
    for e in entries:
        key = "noScope" if e["status"] == "no-scope" else e["status"]
        summary[key] = summary.get(key, 0) + 1
    uncheckable = sum(1 for h in html_parts if not h["checkable"])

    return {
        "ok": True,
        "htmlCount": len(html_parts),
        "uncheckableCount": uncheckable,
        "refCount": len(entries),
        "summary": summary,
        "entries": entries,
        "htmlParts": html_parts,
        "scopes": [
            {"path": s["path"], "contentIds": s["contentIds"]} for s in scopes
        ],
    }
