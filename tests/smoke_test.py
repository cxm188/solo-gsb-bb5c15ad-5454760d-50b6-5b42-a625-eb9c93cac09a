"""端到端冒烟测试：不依赖第三方库，直接调用 mime.py。"""
import base64
import io
import sys
import zipfile

sys.path.insert(0, ".")
from app import diff, mime

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  {detail}")


def roundtrip(raw_b, **kw):
    """raw_b 为 bytes；按 UTF-8 解码成 JSON 文本后传入（模拟浏览器）。"""
    r = mime.parse_message(raw_b.decode("utf-8"), kw.get("ct"), kw.get("boundary"))
    assert r["ok"], r
    enc = mime.encode_tree(r["tree"], include_headers=kw.get("include_headers", True))
    assert enc["ok"], enc["issues"]
    return r, enc, base64.b64decode(enc["payloadBase64"])


# 1. 基本 multipart/form-data，含 CJK 与 quoted boundary --------------------
print("1) 基本 form-data 往返")
raw = (
    b'Content-Type: multipart/form-data; boundary="----B"\r\n'
    b'\r\n'
    b'------B\r\n'
    b'Content-Disposition: form-data; name="u"\r\n'
    b'\r\n'
    b'alice\r\n'
    b'------B\r\n'
    b'Content-Disposition: form-data; name="bio"\r\n'
    b'\r\n'
    + "你好".encode() + b'\r\n'
    b'------B--'
)
r, enc, out = roundtrip(raw)
check("顶层是 multipart", r["tree"]["kind"] == "multipart")
check("2 个子部分", len(r["tree"]["parts"]) == 2)
check("boundary 去引号生效", r["effectiveBoundary"] == "----B")
check("part name 解析", r["tree"]["parts"][0]["name"] == "u")
check("CJK 往返", "你好".encode() in out)
check("无 error", r["counts"]["error"] == 0, r["issues"])
check("重新编码包含关闭定界符", b"------B--" in out)
# 参数引号会被规范化（合法 token 去引号），故比较规范化后的形式
canonical_raw = raw.replace(b'boundary="----B"', b"boundary=----B")
check("字节级往返一致（参数引号规范化）", out == canonical_raw,
      f"\n{out!r}\n!=\n{canonical_raw!r}")

# 2. 嵌套 multipart（含 preamble / epilogue） -------------------------------
print("2) 嵌套 multipart")
raw2 = (
    b'Content-Type: multipart/mixed; boundary=OUTER\r\n\r\n'
    b'preamble text\r\n'
    b'--OUTER\r\n'
    b'Content-Type: text/plain\r\n\r\n'
    b'outer body\r\n'
    b'--OUTER\r\n'
    b'Content-Type: multipart/alternative; boundary="INNER42"\r\n\r\n'
    b'nested preamble\r\n'
    b'--INNER42\r\nContent-Type: text/plain\r\n\r\nplain\r\n'
    b'--INNER42\r\nContent-Type: text/html\r\n\r\n<b>html</b>\r\n'
    b'--INNER42--\r\n'
    b'--OUTER\r\nContent-Type: text/csv\r\n\r\na,b\r\n1,2\r\n'
    b'--OUTER--\r\n'
    b'epi'
)
r2, enc2, out2 = roundtrip(raw2)
t = r2["tree"]
check("3 个外层部分", len(t["parts"]) == 3, [(p["kind"], p.get("mediaType")) for p in t["parts"]])
nested = t["parts"][1]
check("第 2 部分识别为嵌套 multipart", nested["kind"] == "multipart"
      and nested["mediaType"] == "multipart/alternative")
check("嵌套 2 部分", len(nested["parts"]) == 2)
check("preamble 保留", "preamble text" in t["preamble"])
check("嵌套 preamble 保留", "nested preamble" in nested["preamble"])
check("epilogue 保留", t["epilogue"] == "epi", repr(t["epilogue"]))
check("嵌套往返一致", out2 == raw2.replace(b'boundary="INNER42"', b"boundary=INNER42"),
      f"\n{out2!r}\n!=\n{raw2!r}")
check("嵌套无 error", r2["counts"]["error"] == 0,
      [i for i in r2["issues"] if i["level"] == "error"])

# 3. LF 规范化 + 缺关闭定界符 + 畸形 header ---------------------------------
print("3) 错误报文诊断")
raw3 = (
    b'Content-Type: multipart/form-data; boundary=X\n'
    b'\n'
    b'--X\nContent-Disposition: form-data; name="ok"\n\ngood\n'
    b'--X\nBroken Header Line no colon\n\nrest'
)
r3 = mime.parse_message(raw3.decode(), None, None)
codes = [i["code"] for i in r3["issues"]]
check("检测到 LF 规范化", "LF_NORMALIZED" in codes, codes)
# 首行不是合法字段 => 该部分无有效头部块
check("畸形头部报错", "NO_HEADER_BODY_SEPARATOR" in codes, codes)
check("缺少关闭定界符报错", "MISSING_CLOSE_DELIMITER" in codes, codes)

# 3b. 首行合法、次行畸形 -> BAD_HEADER_LINE
raw3b = (
    b"Content-Type: multipart/mixed; boundary=Y\r\n\r\n"
    b"--Y\r\nGood-Header: yes\r\nbroken line without colon\r\n\r\nbody\r\n--Y--"
)
r3b = mime.parse_message(raw3b.decode(), None, None)
check("首行合法时次行畸形报 BAD_HEADER_LINE",
      any(i["code"] == "BAD_HEADER_LINE" for i in r3b["issues"]),
      [i["code"] for i in r3b["issues"]])
check("仍尽力恢复 2 个部分", len(r3["tree"]["parts"]) == 2)
enc3 = mime.encode_tree(r3["tree"], include_headers=True)
check("重新编码总是生成关闭定界符",
      b"--X--" in base64.b64decode(enc3["payloadBase64"]))

# 4. boundary 输入框覆盖 + body-only ----------------------------------------
print("4) boundary 覆盖与无头部 body")
body_only = b"--CUSTOM\r\nContent-Type: text/plain\r\n\r\nv\r\n--CUSTOM--"
r4 = mime.parse_message(body_only.decode(), None, "CUSTOM")
check("无顶层头部按 body 解析", r4["hasTopHeaders"] is False)
check("输入框 boundary 生效", r4["effectiveBoundary"] == "CUSTOM")
check("无 error", r4["counts"]["error"] == 0, r4["issues"])
enc4 = mime.encode_tree(r4["tree"], include_headers=False)
out4 = base64.b64decode(enc4["payloadBase64"])
check("仅输出 body 时不含顶层头部", not out4.startswith(b"Content-Type"), out4[:60])
check("body-only 往返一致", out4 == body_only, f"\n{out4!r}\n!=\n{body_only!r}")

# 5. quoted 参数带转义 ------------------------------------------------------
print("5) quoted-string 参数解析")
issues5 = []
mt, p = mime.parse_content_type(
    'multipart/form-data; boundary="ABC.123_-"; charset="utf\\"-8"; x=tok',
    issues5)
check("quoted boundary 保留", p.get("boundary") == "ABC.123_-", p)
check("反斜杠转义引号", p.get("charset") == 'utf"-8', p)
check("普通 token 参数", p.get("x") == "tok")
check("无解析 issue", issues5 == [], issues5)

# 6. 边界碰撞：正文中的 "--Z" 不在行首或无 CRLF 后缀 ------------------------
print("6) 正文中包含类似 boundary 的文本")
body6 = (
    b"--Z\r\nContent-Type: text/plain\r\n\r\nfake --Zline\r\n--Z\r\n"
    b"Content-Type: text/plain\r\n\r\nsecond\r\n--Z--"
)
r6 = mime.parse_message(body6.decode(), "multipart/mixed", "Z")
check("碰撞文本未被误判为定界符", len(r6["tree"]["parts"]) == 2,
      [i["message"] for i in r6["issues"]])
check("第一部分正文完整", r6["tree"]["parts"][0]["body"] == "fake --Zline")
check("碰撞无 error", r6["counts"]["error"] == 0, r6["issues"])

# 7. 修改后再编码 -----------------------------------------------------------
print("7) 编辑后重新编码")
r7 = mime.parse_message(raw.decode(), None, None)
r7["tree"]["parts"][0]["body"] = "bob"
r7["tree"]["parts"][0]["bodyEdited"] = True
r7["tree"]["boundary"] = "NEW-B"
enc7 = mime.encode_tree(r7["tree"], include_headers=True)
out7 = base64.b64decode(enc7["payloadBase64"])
check("使用新 boundary", b"--NEW-B\r\n" in out7 and b"--NEW-B--" in out7)
check("Content-Type 头 boundary 同步", b"boundary=NEW-B" in out7, out7[:120])
check("正文更新", b"\r\nbob\r\n--NEW-B" in out7)
check("旧 boundary 不再出现", b"----B" not in out7)

# 8. 非 UTF-8 二进制正文逐字节保持 ------------------------------------------
print("8) 非 UTF-8 正文逐字节保持")
binary = bytes(range(1, 256))
raw8 = (b"Content-Type: multipart/mixed; boundary=BIN\r\n\r\n"
        b"--BIN\r\n\r\n" + binary + b"\r\n--BIN--")
r8 = mime.parse_message(None, None, None, raw_base64=base64.b64encode(raw8).decode())
check("二进制正文标记 base64", r8["tree"]["parts"][0]["bodyEncoding"] == "base64")
# 无头部部分：分隔定界符行后的前导 CRLF 是正文的一部分
check("base64 载荷正确", base64.b64decode(r8["tree"]["parts"][0]["body"]) == b"\r\n" + binary)
enc8 = mime.encode_tree(r8["tree"], True)
out8 = base64.b64decode(enc8["payloadBase64"])
check("二进制字节级往返", out8 == raw8, "二进制损坏")

# 9. 异常输入不崩溃 ---------------------------------------------------------
print("9) 异常输入不崩溃")
r9 = mime.parse_message("garbage without delimiters", "multipart/mixed", "NOPE")
check("找不到定界符报错且不崩", any(i["code"] == "NO_DELIMITER" for i in r9["issues"]))
r10 = mime.parse_message("", None, None)
check("空输入返回结构", "tree" in r10 or r10.get("ok") is False)

# 10. body 无头部、有 preamble 的 body-only ---------------------------------
print("10) 仅 body 且带 preamble")
raw10 = b"hi this is preamble\r\n--P\r\n\r\nbody\r\n--P--\r\ntail"
r10p = mime.parse_message(raw10.decode(), "multipart/mixed", "P")
check("preamble 识别", r10p["tree"]["preamble"] == "hi this is preamble",
      repr(r10p["tree"]["preamble"]))
check("epilogue 识别", r10p["tree"]["epilogue"] == "tail", repr(r10p["tree"]["epilogue"]))
enc10 = mime.encode_tree(r10p["tree"], include_headers=False)
out10 = base64.b64decode(enc10["payloadBase64"])
check("body-only 带序言往返", out10 == raw10, f"\n{out10!r}\n!=\n{raw10!r}")

# 11. 样本对比：媒体类型 / 头部 / 正文 / boundary / 序言 / 诊断 --------------
print("11) 样本对比 diff_results")
rawA = (
    b'Content-Type: multipart/mixed; boundary=AA\r\n\r\n'
    b'pre\r\n'
    b'--AA\r\nContent-Type: text/plain\r\n\r\nhello\r\n'
    b'--AA\r\nContent-Type: text/html\r\nX-Extra: 1\r\n\r\n<b>hi</b>\r\n'
    b'--AA--\r\nepi'
)
rawB = (
    b'Content-Type: multipart/mixed; boundary=BB\r\n\r\n'
    b'pre2\r\n'
    b'--BB\r\nContent-Type: text/plain\r\n\r\nhello\r\n'
    b'--BB\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<b>hi2</b>\r\n'
    b'--BB\r\nContent-Type: application/json\r\n\r\n{}\r\n'
    b'--BB--\r\nepi'
)
sa = mime.parse_message(rawA.decode(), None, None)
sb = mime.parse_message(rawB.decode(), None, None)
d = diff.diff_results({"tree": sa["tree"], "issues": sa["issues"]},
                      {"tree": sb["tree"], "issues": sb["issues"]})
check("diff ok", d["ok"] is True)
by_path = {e["path"]: e for e in d["entries"]}
check("新增 1 个部分", d["summary"]["added"] == 1, d["summary"])
check("无删除", d["summary"]["removed"] == 0, d["summary"])
check("parts[2] 标记为新增", by_path["parts[2]"]["change"] == "added")
check("parts[0] 完全一致", by_path["parts[0]"]["change"] == "unchanged")
root_fields = {f["field"] for f in by_path["root"]["fields"]}
check("root 检出 boundary 变化", "boundary" in root_fields, root_fields)
check("root 检出序言变化", "preamble" in root_fields, root_fields)
check("root 未误报尾注变化", "epilogue" not in root_fields, root_fields)
p1_fields = {f["field"]: f for f in by_path["parts[1]"]["fields"]}
check("parts[1] 检出正文变化", "body" in p1_fields, list(p1_fields))
check("parts[1] 检出头部变化", "headers" in p1_fields, list(p1_fields))
check("头部新增 Content-Type 行",
      any("charset" in l for l in p1_fields["headers"]["added"]),
      p1_fields["headers"])
check("头部删除 X-Extra 行",
      any(l.startswith("X-Extra") for l in p1_fields["headers"]["removed"]),
      p1_fields["headers"])
check("正文预览带长度", p1_fields["body"]["b"]["length"] == len("<b>hi2</b>"))

# 11b. 反向对比：新增变删除；诊断代码差异 ------------------------------------
d2 = diff.diff_results({"tree": sb["tree"], "issues": sb["issues"]},
                       {"tree": sa["tree"], "issues": sa["issues"]})
by_path2 = {e["path"]: e for e in d2["entries"]}
check("反向 parts[2] 标记为删除", by_path2["parts[2]"]["change"] == "removed")
check("反向 summary 删除 1", d2["summary"]["removed"] == 1, d2["summary"])

rawC = (b'Content-Type: multipart/mixed; boundary=AA\r\n\r\n'
        b'--AA\r\nContent-Type: text/plain\r\n\r\nhello\r\n'
        b'--AA\r\nContent-Type: text/html\r\nX-Extra: 1\r\n\r\n<b>hi</b>\r\n'
        b'--AA--')
sc = mime.parse_message(rawC.decode(), None, None)
check("rawC 有序言诊断", any(i["code"] == "PREAMBLE" for i in sa["issues"]))
d3 = diff.diff_results({"tree": sa["tree"], "issues": sa["issues"]},
                       {"tree": sc["tree"], "issues": sc["issues"]})
by_path3 = {e["path"]: e for e in d3["entries"]}
root_issue_field = next((f for f in by_path3["root"]["fields"]
                         if f["field"] == "issues"), None)
check("root 检出诊断代码差异", root_issue_field is not None,
      by_path3["root"]["fields"])
check("PREAMBLE 仅存在于 A", root_issue_field
      and "PREAMBLE" in root_issue_field["removed"], root_issue_field)

# 11c. 嵌套子树整体删除折叠为一条；全局诊断 -----------------------------------
rawD = (
    b'Content-Type: multipart/mixed; boundary=O\r\n\r\n'
    b'--O\r\nContent-Type: text/plain\r\n\r\nkeep\r\n'
    b'--O\r\nContent-Type: multipart/alternative; boundary=I\r\n\r\n'
    b'--I\r\nContent-Type: text/plain\r\n\r\np\r\n'
    b'--I\r\nContent-Type: text/html\r\n\r\nh\r\n'
    b'--I--\r\n'
    b'--O--'
)
rawE = (b'Content-Type: multipart/mixed; boundary=O\r\n\r\n'
        b'--O\r\nContent-Type: text/plain\r\n\r\nkeep\r\n'
        b'--O--')
sd_ = mime.parse_message(rawD.decode(), None, None)
se = mime.parse_message(rawE.decode(), None, None)
d4 = diff.diff_results({"tree": sd_["tree"], "issues": sd_["issues"]},
                       {"tree": se["tree"], "issues": se["issues"]})
removed_entries = [e for e in d4["entries"] if e["change"] == "removed"]
check("嵌套子树删除折叠为 1 条", len(removed_entries) == 1,
      [(e["path"], e["change"]) for e in d4["entries"]])
check("删除摘要带后代数量",
      removed_entries[0]["summary"]["descendantParts"] == 2,
      removed_entries[0]["summary"])

rawF = rawE.replace(b"\r\n", b"\n")  # 全 LF -> 触发全局 LF_NORMALIZED
sf = mime.parse_message(rawF.decode("utf-8"), None, None)
d5 = diff.diff_results({"tree": se["tree"], "issues": se["issues"]},
                       {"tree": sf["tree"], "issues": sf["issues"]})
global_entries = [e for e in d5["entries"] if e["path"] == ""]
check("全局诊断差异单列一条", len(global_entries) == 1,
      [(e["path"], e["change"]) for e in d5["entries"]])
check("LF_NORMALIZED 为新增诊断",
      "LF_NORMALIZED" in global_entries[0]["fields"][0]["added"],
      global_entries[0]["fields"])

# 11d. 完全相同 -> 全部 unchanged ---------------------------------------------
d6 = diff.diff_results({"tree": sa["tree"], "issues": sa["issues"]},
                       {"tree": sa["tree"], "issues": sa["issues"]})
check("自比无差异", d6["summary"]["modified"] == 0
      and d6["summary"]["added"] == 0 and d6["summary"]["removed"] == 0,
      d6["summary"])
check("自比全部 unchanged", d6["summary"]["unchanged"] == len(d6["entries"]))

def _last_part_body(wire, boundary):
    head = wire.rpartition(b"\r\n--" + boundary)[0]
    return head.rsplit(b"\r\n\r\n", 1)[1]


# 12. Content-Transfer-Encoding：base64 叶子 ---------------------------------
print("12) base64 CTE 解析/未编辑字节稳定/切换")
payload = bytes(range(0, 200)) + b"\r\ntail\r\n"
_b64_wire12 = b"\r\n".join(  # RFC 2045 折行：每行 76 字符
    base64.b64encode(payload)[i:i + 76]
    for i in range(0, len(base64.b64encode(payload)), 76))
raw12 = (
    b'Content-Type: multipart/mixed; boundary=B64\r\n\r\n'
    b'--B64\r\n'
    b'Content-Type: application/octet-stream\r\n'
    b'Content-Transfer-Encoding: base64\r\n\r\n'
    + _b64_wire12 + b'\r\n'
    b'--B64--'
)
r12 = mime.parse_message(None, None, None,
                         raw_base64=base64.b64encode(raw12).decode())
p12 = r12["tree"]["parts"][0]
check("识别 CTE base64", p12["transferEncoding"] == "base64", p12)
check("解码状态 decoded", p12["decodeState"] == "decoded", p12["decodeState"])
check("声明 CTE 保留", p12["transferEncodingDeclared"] == "base64")
check("原始传输体保留", base64.b64decode(p12["rawBodyBase64"]) == _b64_wire12,
      "rawBodyBase64 丢失")
check("base64 正文解码为原始字节（编辑通道 base64）",
      base64.b64decode(p12["body"]) == payload, p12["bodyEncoding"])
check("base64 节点无诊断",
      not [i for i in r12["issues"] if i["level"] == "warning"],
      [i["code"] for i in r12["issues"]])
enc12 = mime.encode_tree(r12["tree"], True)
out12 = base64.b64decode(enc12["payloadBase64"])
check("未编辑 base64 节点字节稳定", out12 == raw12,
      f"\n{out12!r}\n!=\n{raw12!r}")

# 切换到 7bit：按头部声明回写 7bit 原文（二进制内容触发 7bit 约束警告）
p12["transferEncoding"] = "7bit"
p12["bodyEdited"] = True
enc12b = mime.encode_tree(r12["tree"], True)
out12b = base64.b64decode(enc12b["payloadBase64"])
check("切换后 CTE 头改写为 7bit", b"Content-Transfer-Encoding: 7bit" in out12b)
check("7bit 约束诊断", any(i["code"] == "CTE_NOT_7BIT" for i in enc12b["issues"]),
      [i["code"] for i in enc12b["issues"]])

# 13. Content-Transfer-Encoding：quoted-printable ----------------------------
print("13) quoted-printable CTE")
qp_wire = (
    b'Content-Type: multipart/mixed; boundary=QP\r\n\r\n'
    b'--QP\r\nContent-Type: text/plain; charset=utf-8\r\n'
    b'Content-Transfer-Encoding: quoted-printable\r\n\r\n'
    b'=E4=BD=A0=E5=A5=BD=EF=BC=8CCaf=C3=A9=20=3D=2042\r\n'
    b'long line =20=3D=20end\r\n'
    b'--QP--'
)
r13 = mime.parse_message(None, None, None,
                         raw_base64=base64.b64encode(qp_wire).decode())
p13 = r13["tree"]["parts"][0]
check("识别 CTE quoted-printable", p13["transferEncoding"] == "quoted-printable")
check("QP 解码状态 decoded", p13["decodeState"] == "decoded")
check("QP 正文解码为 UTF-8 文本", p13["body"] == "你好，Café = 42\r\nlong line  = end",
      repr(p13["body"]))
enc13 = mime.encode_tree(r13["tree"], True)
out13 = base64.b64decode(enc13["payloadBase64"])
check("未编辑 QP 节点字节稳定", out13 == qp_wire,
      f"\n{out13!r}\n!=\n{qp_wire!r}")

# 编辑文本后切换到 base64：回写 base64 且遵守 76 字符折行
p13["body"] = "新内容 new body"
p13["bodyEdited"] = True
p13["transferEncoding"] = "base64"
enc13b = mime.encode_tree(r13["tree"], True)
out13b = base64.b64decode(enc13b["payloadBase64"])
check("切换后 CTE 头改写为 base64",
      b"Content-Transfer-Encoding: base64" in out13b)
body13b = _last_part_body(out13b, b"QP")
check("base64 正文解码为编辑后字节",
      base64.b64decode(body13b, validate=False) == "新内容 new body".encode("utf-8"),
      body13b)
check("base64 折行不超过 76 字符",
      all(len(ln) <= 76 for ln in body13b.split(b"\r\n")), body13b)

# 文本切换到 QP：非 ASCII 以 =XX 表示，超长软折行
p13["transferEncoding"] = "quoted-printable"
enc13c = mime.encode_tree(r13["tree"], True)
out13c = base64.b64decode(enc13c["payloadBase64"])
body13c = _last_part_body(out13c, b"QP")
check("QP 回写包含 =XX", b"=E6" in body13c, body13c)
check("QP 折行不超过 76 字符",
      all(len(ln) <= 76 for ln in body13c.split(b"\r\n")), body13c)

# 14. 畸形 base64 / QP：带路径诊断且不丢失原始载荷 ---------------------------
print("14) 畸形 CTE 诊断与载荷保留")
bad_wire = (
    b'Content-Type: multipart/mixed; boundary=BAD\r\n\r\n'
    b'--BAD\r\nContent-Transfer-Encoding: base64\r\n\r\n'
    b'TW@e!!not-base64===\r\n'
    b'--BAD--'
)
r14 = mime.parse_message(None, None, None,
                         raw_base64=base64.b64encode(bad_wire).decode())
p14 = r14["tree"]["parts"][0]
b64_issues = [i for i in r14["issues"] if i["code"] == "BAD_BASE64"]
check("畸形 base64 产生诊断", bool(b64_issues), [i["code"] for i in r14["issues"]])
check("畸形诊断带节点路径",
      any("parts[0]" in i["message"] for i in b64_issues), b64_issues)
check("解码状态 malformed", p14["decodeState"] == "malformed")
check("原始载荷未丢失",
      base64.b64decode(p14["rawBodyBase64"]) == b"TW@e!!not-base64===")
enc14 = mime.encode_tree(r14["tree"], True)
out14 = base64.b64decode(enc14["payloadBase64"])
check("畸形但未编辑：原始字节还原", out14 == bad_wire,
      f"\n{out14!r}\n!=\n{bad_wire!r}")

bad_qp = (
    b'Content-Type: multipart/mixed; boundary=BQ\r\n\r\n'
    b'--BQ\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
    b'ok=ZZbad=4\r\n'
    b'--BQ--'
)
r14b = mime.parse_message(None, None, None,
                          raw_base64=base64.b64encode(bad_qp).decode())
qp_issues = [i for i in r14b["issues"] if i["code"] == "BAD_QUOTED_PRINTABLE"]
check("畸形 QP 产生带路径诊断",
      bool(qp_issues) and any("parts[0]" in i["message"] for i in qp_issues),
      [i["code"] for i in r14b["issues"]])
check("QP 畸形节点状态 malformed",
      r14b["tree"]["parts"][0]["decodeState"] == "malformed")
check("QP 畸形载荷保留并尽力解码（非法 =ZZ 与行尾 =4 按字面保留）",
      r14b["tree"]["parts"][0]["body"] == "ok=ZZbad=4",
      r14b["tree"]["parts"][0]["body"])
enc14b = mime.encode_tree(r14b["tree"], True)
out14b = base64.b64decode(enc14b["payloadBase64"])
check("畸形 QP 未编辑字节还原", out14b == bad_qp,
      f"\n{out14b!r}\n!=\n{bad_qp!r}")

# 15. 7bit / 8bit / binary 语义与默认值 --------------------------------------
print("15) 7bit/8bit/binary 语义")
raw15 = (
    b'Content-Type: multipart/mixed; boundary=I7\r\n\r\n'
    b'--I7\r\nContent-Transfer-Encoding: 8bit\r\n\r\n'
    + "café 8bit".encode() + b'\r\n'
    b'--I7\r\nContent-Type: text/plain\r\n\r\n'
    b'plain 7bit default\r\n'
    b'--I7\r\nContent-Transfer-Encoding: BINARY\r\n\r\n'
    + bytes([0, 1, 255]) + b'\r\n'
    b'--I7--'
)
r15 = mime.parse_message(None, None, None,
                         raw_base64=base64.b64encode(raw15).decode())
p0, p1, p2 = r15["tree"]["parts"]
check("8bit CTE 识别", p0["transferEncoding"] == "8bit")
check("8bit 允许非 ASCII 且无诊断", p0["decodeState"] == "raw" and
      not [i for i in r15["issues"] if i["code"] == "CTE_NOT_7BIT"])
check("缺省 CTE 视为 7bit", p1["transferEncoding"] == "7bit"
      and p1["decodeState"] == "raw", p1.get("transferEncoding"))
check("BINARY 大小写规范化", p2["transferEncoding"] == "binary")
check("binary 容忍 NUL", p2["decodeState"] == "raw"
      and not [i for i in r15["issues"] if i["code"].startswith("CTE_")],
      [i["code"] for i in r15["issues"]])

# 7bit 声明但含非 ASCII -> 警告
raw15b = (b'Content-Type: multipart/mixed; boundary=I8\r\n\r\n'
          b'--I8\r\nContent-Transfer-Encoding: 7bit\r\n\r\n'
          + "héllo".encode() + b'\r\n--I8--')
r15b = mime.parse_message(None, None, None,
                          raw_base64=base64.b64encode(raw15b).decode())
check("7bit 含非 ASCII 产生 CTE_NOT_7BIT",
      any(i["code"] == "CTE_NOT_7BIT" for i in r15b["issues"]),
      [i["code"] for i in r15b["issues"]])

# 未知 CTE -> passthrough，未编辑字节稳定
raw15c = (b'Content-Type: multipart/mixed; boundary=I9\r\n\r\n'
          b'--I9\r\nContent-Transfer-Encoding: x-uuencode\r\n\r\n'
          b'whatever raw\r\n--I9--')
r15c = mime.parse_message(None, None, None,
                          raw_base64=base64.b64encode(raw15c).decode())
p15c = r15c["tree"]["parts"][0]
check("未知 CTE 标记 passthrough", p15c["decodeState"] == "passthrough"
      and p15c["transferEncoding"] == "x-uuencode")
check("未知 CTE 产生诊断",
      any(i["code"] == "UNKNOWN_TRANSFER_ENCODING" for i in r15c["issues"]))
enc15c = mime.encode_tree(r15c["tree"], True)
out15c = base64.b64decode(enc15c["payloadBase64"])
check("未知 CTE 未编辑字节稳定", out15c == raw15c,
      f"\n{out15c!r}\n!=\n{raw15c!r}")

# multipart 上声明 base64 -> 警告
raw15d = (b'Content-Type: multipart/mixed; boundary=M1\r\n\r\n'
          b'--M1\r\nContent-Type: multipart/mixed; boundary=M2\r\n'
          b'Content-Transfer-Encoding: base64\r\n\r\n'
          b'--M2\r\nContent-Type: text/plain\r\n\r\nx\r\n--M2--\r\n--M1--')
r15d = mime.parse_message(None, None, None,
                          raw_base64=base64.b64encode(raw15d).decode())
check("multipart 上声明 base64 报警告",
      any(i["code"] == "BAD_CTE_ON_MULTIPART" for i in r15d["issues"]),
      [i["code"] for i in r15d["issues"]])

# 16. base64 折行：超过 76 字符的编码行诊断；长文本重新编码自动折行 -----------
print("16) base64/QP 折行规则")
long_payload = bytes(range(256)) * 2
raw16 = (b'Content-Type: multipart/mixed; boundary=L1\r\n\r\n'
         b'--L1\r\nContent-Transfer-Encoding: base64\r\n\r\n'
         + base64.b64encode(long_payload) + b'\r\n--L1--')
r16 = mime.parse_message(None, None, None,
                         raw_base64=base64.b64encode(raw16).decode())
check("超长 base64 行报 CTE_LONG_LINE",
      any(i["code"] == "CTE_LONG_LINE" for i in r16["issues"]),
      [i["code"] for i in r16["issues"]])
p16 = r16["tree"]["parts"][0]
check("超长行仍可解码", base64.b64decode(p16["body"]) == long_payload)
# 未编辑：即使畸形（超长行是 warning 而非 malformed）原始字节仍还原
enc16 = mime.encode_tree(r16["tree"], True)
out16 = base64.b64decode(enc16["payloadBase64"])
check("超长行未编辑字节稳定", out16 == raw16)
# 编辑后重新编码：76 字符折行
p16["bodyEdited"] = True
enc16b = mime.encode_tree(r16["tree"], True)
out16b = base64.b64decode(enc16b["payloadBase64"])
body16 = _last_part_body(out16b, b"L1")
check("重新编码后每行 <=76 字符",
      all(len(ln) <= 76 for ln in body16.split(b"\r\n")),
      [len(ln) for ln in body16.split(b"\r\n")][:5])
check("折行 base64 往返字节一致",
      base64.b64decode(b"".join(body16.split(b"\r\n")), validate=False)
      == long_payload)

# 17. diff 检出 CTE 变化 ------------------------------------------------------
print("17) diff 检出 CTE 字段变化")
s17a = mime.parse_message(
    (b'Content-Type: multipart/mixed; boundary=D\r\n\r\n'
     b'--D\r\nContent-Transfer-Encoding: base64\r\n\r\n'
     + base64.b64encode(b"hello") + b'\r\n--D--').decode(), None, None)
s17b = mime.parse_message(
    (b'Content-Type: multipart/mixed; boundary=D\r\n\r\n'
     b'--D\r\nContent-Transfer-Encoding: quoted-printable\r\n\r\n'
     b'hel=ZZlo\r\n--D--').decode(), None, None)
d17 = diff.diff_results({"tree": s17a["tree"], "issues": s17a["issues"]},
                        {"tree": s17b["tree"], "issues": s17b["issues"]})
fields17 = {f["field"] for e in d17["entries"] if e["path"] == "parts[0]"
            for f in e.get("fields", [])}
check("diff 检出 transferEncoding 变化", "transferEncoding" in fields17, fields17)
check("diff 检出 decodeState 变化", "decodeState" in fields17, fields17)

# 18. 附件提取与 ZIP 打包 -----------------------------------------------
print("18) 附件提取 / RFC 2231 文件名 / ZIP 打包")
from app import attachments as att

_b18 = base64.b64encode(b"hello world")
raw18 = b"".join([
    b'Content-Type: multipart/mixed; boundary=ATT\r\n\r\n',
    # 0: attachment + 普通 filename，base64 CTE 解码字节
    b'--ATT\r\nContent-Disposition: attachment; filename="a.txt"\r\n'
    b'Content-Transfer-Encoding: base64\r\n\r\n' + _b18 + b'\r\n',
    # 1: RFC 2231 分段扩展文件名（filename*0* + filename*1*），优先于普通 filename
    b"--ATT\r\nContent-Disposition: attachment;\r\n"
    b" filename*0*=UTF-8''caf%c3;\r\n"
    b" filename*1*=%a9.csv; filename=\"fallback.csv\"\r\n\r\nx\r\n",
    # 2: form-data 但带 filename 参数 -> 也算附件；路径穿越字符被净化
    b'--ATT\r\nContent-Disposition: form-data; name="f"; filename="../../etc/passwd"\r\n'
    b'\r\nabc\r\n',
    # 3/4: 重名 -> 按遍历顺序加序号
    b'--ATT\r\nContent-Disposition: attachment; filename="dup.txt"\r\n\r\none\r\n',
    b'--ATT\r\nContent-Disposition: attachment; filename="dup.txt"\r\n\r\ntwo\r\n',
    # 5: 畸形 base64，未编辑 -> 禁导
    b'--ATT\r\nContent-Disposition: attachment; filename="bad.b64"\r\n'
    b'Content-Transfer-Encoding: base64\r\n\r\n@@@not-base64!!!\r\n',
    # 6: 未知 CTE，未编辑 -> 禁导
    b'--ATT\r\nContent-Disposition: attachment; filename="unk.bin"\r\n'
    b'Content-Transfer-Encoding: x-uuencode\r\n\r\nwhatever\r\n',
    # 7: 无文件名 -> 按节点路径命名
    b'--ATT\r\nContent-Disposition: attachment\r\n'
    b'Content-Transfer-Encoding: 8bit\r\n\r\n' + "无名".encode() + b'\r\n',
    # 8: 普通 form-data 字段（无 filename、非 attachment）-> 不算附件
    b'--ATT\r\nContent-Disposition: form-data; name="field"\r\n\r\nvalue\r\n',
    b'--ATT--',
])
r18 = mime.parse_message(None, None, None, raw_base64=base64.b64encode(raw18).decode())
check("解析 18 成功", r18["ok"], r18.get("error"))
la18 = att.list_attachments(r18["tree"])
a_list = la18["attachments"]
check("发现 8 个附件（普通 form-data 字段排除）", la18["count"] == 8,
      [(a["path"], a["filename"]) for a in a_list])
check("可导出 6 个", la18["exportableCount"] == 6, la18["exportableCount"])
by_path = {a["path"]: a for a in a_list}

check("普通 filename 解析", by_path["parts[0]"]["filename"] == "a.txt"
      and by_path["parts[0]"]["filenameSource"] == "plain")
check("RFC 2231 分段拼接且优先", by_path["parts[1]"]["filename"] == "café.csv"
      and by_path["parts[1]"]["filenameSource"] == "extended",
      by_path["parts[1]"])
check("CTE 解码后字节数", by_path["parts[0]"]["size"] == 11
      and by_path["parts[1]"]["size"] == 1)
check("路径分隔符与控制字符被净化（不残留 / 与反斜杠）",
      "/" not in by_path["parts[2]"]["zipName"]
      and "\\" not in by_path["parts[2]"]["zipName"]
      and "etcpasswd" in by_path["parts[2]"]["zipName"],
      by_path["parts[2]"]["zipName"])
check("重名加序号（先序）",
      by_path["parts[3]"]["zipName"] == "dup.txt"
      and by_path["parts[4]"]["zipName"] == "dup (2).txt",
      [a["zipName"] for a in a_list])
check("无文件名按节点路径命名",
      by_path["parts[7]"]["filenameSource"] == "path"
      and by_path["parts[7]"]["zipName"] == "parts[7]",
      by_path["parts[7]"])
check("畸形 CTE 未编辑禁导且有原因",
      by_path["parts[5]"]["exportable"] is False
      and "畸形" in by_path["parts[5]"]["reason"], by_path["parts[5]"])
check("未知 CTE 未编辑禁导且有原因",
      by_path["parts[6]"]["exportable"] is False
      and "未知" in by_path["parts[6]"]["reason"], by_path["parts[6]"])

# ZIP：勾选全部可导出项
sel = [a["path"] for a in a_list if a["exportable"]]
zip_b, err = att.build_attachments_zip(r18["tree"], sel)
check("ZIP 构建成功", err is None and zip_b is not None, err)
zf = zipfile.ZipFile(io.BytesIO(zip_b))
names = zf.namelist()
check("ZIP 含 6 个条目且名称唯一", sorted(names) == sorted(set(names))
      and len(names) == 6, names)
check("ZIP 条目为 CTE 解码后字节",
      zf.read("a.txt") == b"hello world"
      and zf.read([a["zipName"] for a in a_list if a["path"] == "parts[1]"][0]) == b"x",
      {n: zf.read(n) for n in names})
check("ZIP 含 8bit UTF-8 内容",
      zf.read("parts[7]") == "无名".encode(), zf.read("parts[7]"))

# 勾选禁导项 -> 报错
_bad, err_bad = att.build_attachments_zip(
    r18["tree"], ["parts[5]", "parts[6]"])
check("勾选禁导项被拒", err_bad and err_bad["code"] == "NOT_EXPORTABLE"
      and err_bad["paths"] == ["parts[5]", "parts[6]"], err_bad)
# 空选择 / 失效路径
_e_empty, err_empty = att.build_attachments_zip(r18["tree"], [])
check("空选择报错", err_empty and err_empty["code"] == "EMPTY_SELECTION")
_e_stale, err_stale = att.build_attachments_zip(r18["tree"], ["parts[99]"])
check("失效路径报错", err_stale and err_stale["code"] == "STALE_PATHS")

# 编辑后：畸形 / 未知 CTE 叶子变为可导出，字节取编辑后内容
t18 = r18["tree"]
t18["parts"][5]["body"] = "edited-bad"
t18["parts"][5]["bodyEdited"] = True
t18["parts"][5]["bodyEncoding"] = "utf-8"
t18["parts"][6]["body"] = base64.b64encode(b"edited-unk").decode()
t18["parts"][6]["bodyEdited"] = True
t18["parts"][6]["bodyEncoding"] = "base64"
la18b = att.list_attachments(t18)
bp = {a["path"]: a for a in la18b["attachments"]}
check("编辑后畸形节点可导出", bp["parts[5]"]["exportable"]
      and bp["parts[5]"]["edited"] and bp["parts[5]"]["size"] == 10,
      bp["parts[5]"])
check("编辑后未知 CTE 节点可导出（编辑通道 base64）",
      bp["parts[6]"]["exportable"] and bp["parts[6]"]["size"] == 10,
      bp["parts[6]"])
zip_c, _ = att.build_attachments_zip(t18, ["parts[5]", "parts[6]"])
zfc = zipfile.ZipFile(io.BytesIO(zip_c))
check("ZIP 取编辑后字节",
      zfc.read("bad.b64") == b"edited-bad"
      and zfc.read("unk.bin") == b"edited-unk",
      {n: zfc.read(n) for n in zfc.namelist()})

# RFC 2231：非法扩展值回退普通 filename
_h = [{"name": "Content-Disposition",
       "value": "attachment; filename=\"ok.txt\"; filename*=UTF-8''bad%zz%41"}]
_, _p = att.parse_disposition(_h)
_fn, _src, _note = att.resolve_filename(_p)
check("非法扩展值回退普通 filename", _fn == "ok.txt" and _src == "plain" and _note,
      (_fn, _src, _note))

# filename* 单值扩展（GBK 字符集）与控制字符净化
_h2 = [{"name": "Content-Disposition",
        "value": "attachment; filename*=gbk''%c4%e3%ba%c3"}]
_, _p2 = att.parse_disposition(_h2)
_fn2, _src2, _ = att.resolve_filename(_p2)
check("filename* 单值 + 非 UTF-8 字符集", _fn2 == "你好" and _src2 == "extended",
      (_fn2, _src2))
check("控制字符/路径分隔符净化",
      att.sanitize_filename("a\x00b/c\\d.txt", "parts[0]") == "abcd.txt",
      att.sanitize_filename("a\x00b/c\\d.txt", "parts[0]"))
check("净化后为空按路径命名",
      att.sanitize_filename("../\x01", "parts[3]") == "parts[3]",
      att.sanitize_filename("../\x01", "parts[3]"))

# 嵌套 multipart 内的附件也能递归列出
raw18n = (
    b'Content-Type: multipart/mixed; boundary=O\r\n\r\n'
    b'--O\r\nContent-Type: multipart/alternative; boundary=I\r\n\r\n'
    b'--I\r\nContent-Disposition: attachment; filename="deep.dat"\r\n\r\n'
    b'inside\r\n'
    b'--I--\r\n'
    b'--O--'
)
r18n = mime.parse_message(None, None, None, raw_base64=base64.b64encode(raw18n).decode())
la_n = att.list_attachments(r18n["tree"])
check("递归发现嵌套附件",
      [(a["path"], a["zipName"]) for a in la_n["attachments"]]
      == [("parts[0].parts[0]", "deep.dat")],
      [(a["path"], a["zipName"]) for a in la_n["attachments"]])

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)