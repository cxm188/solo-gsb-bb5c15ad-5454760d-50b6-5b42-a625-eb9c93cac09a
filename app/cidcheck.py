"""CID 引用检查：静态扫描编辑树中 HTML 叶子的 ``cid:`` 引用。

规则（全部只做静态分析，不执行、不加载任何资源）：

* 仅检查 ``Content-Type: text/html`` 且**正文可还原为合法 UTF-8** 的叶子；
  其他含 "html" 的媒体类型（如 ``application/xhtml+xml``）或非合法 UTF-8 的
  text/html 一律报“无法检查”并说明原因；非 HTML 叶子忽略；
* 类型与 ``Content-ID`` 一律按节点**当前头部**判断（编辑后即时生效）；
  正文从叶子的“正文编辑通道”（``body`` + ``bodyEncoding``）还原字节；
* 用标准库 ``html.parser`` 仅做分词，提取各标签 ``src`` / ``href`` 属性中
  以 ``cid:``（大小写不敏感）开头的引用，记录标签/属性/行号；
* 引用去掉 ``cid:`` 前缀后**百分号解码一次**（``urllib.parse.unquote``），
  标识符区分大小写；
* ``Content-ID`` 去首尾空白并剥去外层尖括号（``<id>`` -> ``id``）；
* 每个 HTML 只在**最近的 ``multipart/related`` 祖先**作用域内匹配叶子：
  嵌套 ``multipart/related`` 子树中的叶子归内层作用域，不作为候选；
  HTML 没有 related 祖先时，其引用报“无作用域”；
* 候选按树的先序排列：恰好 1 个为唯一匹配；0 个为缺失；多个为重复歧义，
  列出全部候选路径但不默认选取任何一个。
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Optional
from urllib.parse import unquote

from .mime import _encode_text, find_header, parse_content_type

REF_ATTRS = ("src", "href")


# --------------------------------------------------------------------------- #
# 标识符规范化
# --------------------------------------------------------------------------- #

def normalize_content_id(value: Optional[str]) -> str:
    """Content-ID：去首尾空白，剥去一对外层尖括号（``<x>`` -> ``x``）。"""
    v = (value or "").strip()
    if len(v) >= 2 and v[0] == "<" and v[-1] == ">":
        v = v[1:-1].strip()
    return v


def decode_cid_ref(value: str) -> str:
    """引用值：去掉 ``cid:`` 前缀后百分号解码一次（区分大小写）。

    调用方保证 ``value`` 以 ``cid:`` 开头（大小写不敏感）。
    """
    return unquote(value[4:])


# --------------------------------------------------------------------------- #
# HTML 分词（只提取 src/href 中的 cid: 引用，绝不加载或执行任何内容）
# --------------------------------------------------------------------------- #

class _CidHTMLParser(HTMLParser):
    def __init__(self):
        # convert_charrefs=False：不改变属性文本；本解析器只做分词
        super().__init__(convert_charrefs=False)
        self.refs: list[dict] = []

    def _scan(self, tag: str, attrs):
        line, _col = self.getpos()
        for name, value in attrs:
            if not name or value is None:
                continue
            if name.lower() not in REF_ATTRS:
                continue
            if value[:4].lower() != "cid:":
                continue
            self.refs.append({
                "tag": tag.lower(),
                "attr": name.lower(),
                "line": line,
                "raw": value,
                "cid": decode_cid_ref(value),
            })

    def handle_starttag(self, tag, attrs):
        self._scan(tag, attrs)

    def handle_startendtag(self, tag, attrs):
        self._scan(tag, attrs)


def extract_cid_refs(html_text: str) -> list[dict]:
    """从 HTML 文本中按文档顺序提取 src/href 的 cid: 引用。"""
    parser = _CidHTMLParser()
    try:
        parser.feed(html_text)
        parser.close()
    except Exception:
        # HTMLParser 对畸形输入通常容错；即使抛错也返回已提取的部分
        pass
    return parser.refs


# --------------------------------------------------------------------------- #
# 树遍历：记录每个叶子最近的 multipart/related 祖先
# --------------------------------------------------------------------------- #

def node_media_type(node: dict) -> Optional[str]:
    """按节点当前 Content-Type 头部判断媒体类型；无头部时回退树中记录值。"""
    h = find_header(node.get("headers") or [], "Content-Type")
    if h is not None and (h.get("value") or "").strip():
        throwaway: list[dict] = []
        mt, _params = parse_content_type(h["value"], throwaway)
        if mt:
            return mt
    mt = (node.get("mediaType") or "").lower()
    return mt or None


def _node_content_id(node: dict) -> Optional[str]:
    h = find_header(node.get("headers") or [], "Content-ID")
    if h is None:
        return None
    return normalize_content_id(h.get("value")) or None


def _collect_leaves(node: dict, related_stack: list, out: list[dict]):
    """先序遍历，记录每个叶子及其最近的 multipart/related 祖先。

    栈中只压入 multipart/related 容器，因此“最近 related 祖先”天然排除了
    嵌套 related 子树（其中的叶子归内层 related 作用域）。
    """
    if not isinstance(node, dict):
        return
    if node.get("kind") == "multipart":
        stack = related_stack
        if node_media_type(node) == "multipart/related":
            stack = related_stack + [node]
        for child in node.get("parts") or []:
            _collect_leaves(child, stack, out)
        return
    out.append({
        "node": node,
        "path": node.get("path") or "",
        "mediaType": node_media_type(node),
        "contentId": _node_content_id(node),
        "scope": related_stack[-1] if related_stack else None,
    })


def _scope_info(scope: Optional[dict]) -> Optional[dict]:
    if scope is None:
        return None
    return {
        "path": scope.get("path") or "",
        "mediaType": node_media_type(scope) or "multipart/related",
    }


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

def check_cids(tree: dict) -> dict:
    """对当前（可能已编辑的）树执行 CID 引用检查。"""
    if not isinstance(tree, dict):
        return {"ok": False, "error": "报文树格式错误",
                "summary": {}, "scopes": [], "documents": []}

    leaves: list[dict] = []
    _collect_leaves(tree, [], leaves)

    # 作用域汇总（按先序首次出现顺序）
    scope_order: list[int] = []
    scope_meta: dict[int, dict] = {}
    for leaf in leaves:
        scope = leaf["scope"]
        if scope is None:
            continue
        key = id(scope)
        if key not in scope_meta:
            info = _scope_info(scope)
            scope_order.append(key)
            scope_meta[key] = {**info, "leafCount": 0}
        scope_meta[key]["leafCount"] += 1

    summary = {
        "htmlDocuments": 0,
        "checked": 0,
        "uncheckable": 0,
        "references": 0,
        "unique": 0,
        "missing": 0,
        "duplicate": 0,
        "noScope": 0,
    }
    documents: list[dict] = []

    for leaf in leaves:
        mt = leaf["mediaType"]
        html_like = bool(mt and "html" in mt)
        if mt != "text/html" and not html_like:
            continue

        summary["htmlDocuments"] += 1
        node = leaf["node"]
        entry = {
            "path": leaf["path"],
            "mediaType": mt or "application/octet-stream",
            "contentId": leaf["contentId"],
            "scope": _scope_info(leaf["scope"]),
            "status": "checked",
            "reason": None,
            "references": [],
        }

        if mt != "text/html":
            entry["status"] = "uncheckable"
            entry["reason"] = (
                f"仅支持 text/html 的 CID 引用检查；媒体类型为 {mt}，无法检查"
            )
            summary["uncheckable"] += 1
            documents.append(entry)
            continue

        data = _encode_text(node.get("body", ""),
                            node.get("bodyEncoding", "utf-8"))
        try:
            html_text = data.decode("utf-8")
        except UnicodeDecodeError:
            entry["status"] = "uncheckable"
            enc = node.get("bodyEncoding", "utf-8")
            chan = "base64 编辑通道" if enc == "base64" else f"{enc} 通道"
            entry["reason"] = (
                "正文不是合法 UTF-8（以" + chan + "承载，可能是二进制或其他字符集），"
                "无法按 text/html 检查 cid: 引用"
            )
            summary["uncheckable"] += 1
            documents.append(entry)
            continue

        summary["checked"] += 1
        refs = extract_cid_refs(html_text)
        scope = leaf["scope"]
        for ref in refs:
            summary["references"] += 1
            item = {**ref, "status": None, "candidates": [], "match": None}
            if scope is None:
                item["status"] = "no-scope"
                summary["noScope"] += 1
            else:
                # 同一最近 related 祖先下的叶子；标识符区分大小写
                candidates = [
                    lf for lf in leaves
                    if lf["scope"] is scope
                    and lf["contentId"] is not None
                    and lf["contentId"] == ref["cid"]
                ]
                item["candidates"] = [lf["path"] for lf in candidates]
                if len(candidates) == 1:
                    item["status"] = "unique"
                    item["match"] = candidates[0]["path"]
                    summary["unique"] += 1
                elif len(candidates) == 0:
                    item["status"] = "missing"
                    summary["missing"] += 1
                else:
                    # 重复歧义：列出全部候选，不默认选取
                    item["status"] = "duplicate"
                    summary["duplicate"] += 1
            entry["references"].append(item)
        documents.append(entry)

    scopes = [scope_meta[k] for k in scope_order]
    return {"ok": True, "summary": summary,
            "scopes": scopes, "documents": documents}
