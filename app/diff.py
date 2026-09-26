"""样本对比：按树路径对齐比较两份解析结果（tree + issues）。

比较维度（与前端清单一一对应）：
* 节点增删（added / removed，整棵子树折叠为一条）；
* 媒体类型、boundary、序言/尾注（含编码）、正文（含编码）、头部逐行；
* 每个节点归属的诊断代码集合（按 issue 消息中的路径前缀分桶）。

所有文本值在报告中以预览形式给出（截断保护 + 完整长度），
避免巨型正文撑爆差异报告。
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

PREVIEW_LIMIT = 400

_PATH_INDEX_RE = re.compile(r"\[(\d+)\]")

# issue 消息中这些前缀归属于 root 节点（无 "root：" 前缀的顶层诊断）
_ROOT_PREFIXES = ("顶层头部", "顶层 Content-Type", "输入的 Content-Type")


def _path_key(path: str):
    """把 'parts[2].parts[10]' 转成 (2, 10)，root 为 ()，用于先序排序。"""
    if path == "root":
        return ()
    return tuple(int(x) for x in _PATH_INDEX_RE.findall(path))


def _ancestor_paths(path: str):
    """'parts[0].parts[1]' -> ['root', 'parts[0]']。"""
    if path == "root":
        return []
    segs = path.split(".parts")
    prefixes = ["root"]
    cur = segs[0]
    prefixes.append(cur)
    for s in segs[1:]:
        cur += ".parts" + s
        prefixes.append(cur)
    return prefixes[:-1]


def _preview(text, limit: int = PREVIEW_LIMIT):
    """统一字段值结构：{text, truncated, length}；None 表示该侧无此值。"""
    if text is None:
        return {"text": None, "truncated": False, "length": 0}
    text = str(text)
    if len(text) <= limit:
        return {"text": text, "truncated": False, "length": len(text)}
    return {"text": text[:limit] + "…", "truncated": True, "length": len(text)}


def _index_tree(root):
    """把树摊平为 path -> node；缺失 path 时按位置补齐（与前端 reindex 规则一致）。"""
    idx: dict[str, dict] = {}

    def walk(node, fallback):
        if not isinstance(node, dict):
            return
        path = node.get("path") or fallback
        idx[path] = node
        for i, child in enumerate(node.get("parts") or []):
            child_fb = f"parts[{i}]" if path == "root" else f"{path}.parts[{i}]"
            walk(child, child_fb)

    if isinstance(root, dict):
        walk(root, "root")
    return idx


def _node_brief(node: dict):
    """added/removed 节点的摘要（不再逐字段展开）。"""
    descendants = 0

    def count(nd):
        nonlocal descendants
        for c in nd.get("parts") or []:
            descendants += 1
            count(c)

    count(node)
    return {
        "mediaType": node.get("mediaType"),
        "rawSize": node.get("rawSize", 0),
        "descendantParts": descendants,
        "headers": len(node.get("headers") or []),
    }


def _header_lines(headers):
    return [f"{(h.get('name') or '').strip()}: {h.get('value') or ''}"
            for h in (headers or [])]


def _diff_headers(headers_a, headers_b):
    """头部按 'Name: value' 行做多重集合比较；内容一致仅顺序不同单独标记。"""
    la, lb = _header_lines(headers_a), _header_lines(headers_b)
    ca, cb = Counter(la), Counter(lb)
    removed = sorted((ca - cb).elements())
    added = sorted((cb - ca).elements())
    reordered = not added and not removed and la != lb
    if not added and not removed and not reordered:
        return None
    return {
        "field": "headers",
        "label": "头部",
        "added": added,
        "removed": removed,
        "reordered": reordered,
        "aCount": len(la),
        "bCount": len(lb),
    }


def _bucket_issues(issues, paths):
    """按消息前缀把 issue 归到节点路径；无法归属的进 ''（全局）。"""
    buckets: dict[str, set] = defaultdict(set)
    for it in issues or []:
        code = it.get("code") or "UNKNOWN"
        msg = it.get("message") or ""
        owner = ""
        best = -1
        for p in paths:
            if p and (msg.startswith(p + "：") or msg.startswith(p + ":")):
                if len(p) > best:
                    best = len(p)
                    owner = p
        if not owner and any(msg.startswith(px) for px in _ROOT_PREFIXES):
            owner = "root"
        buckets[owner].add(code)
    return buckets


def _scalar_field(field, label, a, b):
    if a == b:
        return None
    return {"field": field, "label": label, "a": _preview(a), "b": _preview(b)}


def _compare_nodes(path, na, nb, buckets_a, buckets_b):
    fields: list[dict] = []
    kind_a = na.get("kind", "part")
    kind_b = nb.get("kind", "part")
    kind_changed = kind_a != kind_b
    if kind_changed:
        fields.append({"field": "kind", "label": "节点类型",
                       "a": _preview(kind_a), "b": _preview(kind_b)})

    f = _scalar_field("mediaType", "媒体类型", na.get("mediaType"), nb.get("mediaType"))
    if f:
        fields.append(f)

    # 类型一致的专属字段；kind 不同（part <-> multipart）时跳过，避免无意义对照
    if not kind_changed:
        if kind_a == "multipart":
            for key, label in (
                ("boundary", "boundary"),
                ("preamble", "序言 preamble"),
                ("preambleEncoding", "序言编码"),
                ("epilogue", "尾注 epilogue"),
                ("epilogueEncoding", "尾注编码"),
                ("leadingCrlf", "首定界符前 CRLF"),
            ):
                f = _scalar_field(key, label, na.get(key), nb.get(key))
                if f:
                    fields.append(f)
        else:
            for key, label in (
                ("name", "name 参数"),
                ("filename", "filename 参数"),
                ("body", "正文"),
                ("bodyEncoding", "正文编辑通道"),
                ("transferEncoding", "Content-Transfer-Encoding"),
                ("decodeState", "传输解码状态"),
                ("hasHeaders", "含头部块"),
            ):
                f = _scalar_field(key, label, na.get(key), nb.get(key))
                if f:
                    fields.append(f)

    hf = _diff_headers(na.get("headers"), nb.get("headers"))
    if hf:
        fields.append(hf)

    codes_a = buckets_a.get(path, set())
    codes_b = buckets_b.get(path, set())
    i_added = sorted(codes_b - codes_a)
    i_removed = sorted(codes_a - codes_b)
    if i_added or i_removed:
        fields.append({"field": "issues", "label": "诊断代码",
                       "added": i_added, "removed": i_removed})
    return fields


def diff_results(sample_a, sample_b):
    """比较两份样本 {tree, issues}，返回 {ok, summary, entries}。

    entries 按树先序排列；path 为 '' 的条目是全局诊断变化。
    added/removed 的子树只列顶层节点（summary 里带后代数量）。
    """
    sample_a = sample_a or {}
    sample_b = sample_b or {}
    idx_a = _index_tree(sample_a.get("tree") or {})
    idx_b = _index_tree(sample_b.get("tree") or {})
    all_paths = set(idx_a) | set(idx_b)
    buckets_a = _bucket_issues(sample_a.get("issues"), all_paths)
    buckets_b = _bucket_issues(sample_b.get("issues"), all_paths)

    entries: list[dict] = []
    summary = {"added": 0, "removed": 0, "modified": 0, "unchanged": 0}

    # 全局诊断（无法归属任何节点的 issue，如 LF_NORMALIZED / NOT_MULTIPART）
    g_added = sorted(buckets_b.get("", set()) - buckets_a.get("", set()))
    g_removed = sorted(buckets_a.get("", set()) - buckets_b.get("", set()))
    if g_added or g_removed:
        entries.append({
            "path": "",
            "kind": "global",
            "change": "modified",
            "fields": [{
                "field": "issues",
                "label": "全局诊断代码",
                "added": g_added,
                "removed": g_removed,
            }],
        })
        summary["modified"] += 1

    covered: set[str] = set()  # 已整体标记 added/removed 的子树根
    for path in sorted(all_paths, key=lambda p: (_path_key(p), p)):
        if any(a in covered for a in _ancestor_paths(path)):
            continue
        na, nb = idx_a.get(path), idx_b.get(path)
        if na is not None and nb is None:
            entries.append({
                "path": path,
                "kind": na.get("kind", "part"),
                "change": "removed",
                "summary": _node_brief(na),
                "fields": [],
            })
            summary["removed"] += 1
            covered.add(path)
            continue
        if nb is not None and na is None:
            entries.append({
                "path": path,
                "kind": nb.get("kind", "part"),
                "change": "added",
                "summary": _node_brief(nb),
                "fields": [],
            })
            summary["added"] += 1
            covered.add(path)
            continue
        fields = _compare_nodes(path, na, nb, buckets_a, buckets_b)
        if fields:
            entries.append({
                "path": path,
                "kind": nb.get("kind", "part"),
                "change": "modified",
                "fields": fields,
            })
            summary["modified"] += 1
        else:
            entries.append({
                "path": path,
                "kind": nb.get("kind", "part"),
                "change": "unchanged",
                "fields": [],
            })
            summary["unchanged"] += 1

    return {
        "ok": True,
        "summary": summary,
        "entries": entries,
        "previewLimit": PREVIEW_LIMIT,
    }
