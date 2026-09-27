/* MIME Multipart 检查器前端（无框架原生实现） */
"use strict";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let parseResult = null;       // 最近一次解析返回
let tree = null;              // 当前可编辑树（对象引用，控件直接改它）
let encodedBytes = null;      // Uint8Array 编码结果
const issueFilter = { error: true, warning: true, info: true };

// 附件提取：最近一次 /api/attachments 返回、勾选集合、用户主动取消集合、
// 上次列表中存在的路径与可导出路径（用于识别“新出现/变为可导出”的节点）
let attachments = [];
const attachChecked = new Set();
const attachUnchecked = new Set();
const attachSeen = new Set();
const attachSeenExportable = new Set();
let attachDebounce = null;

// CID 引用检查：面板激活后（解析成功即激活）树发生变化会防抖自动重查
let cidDebounce = null;
let cidActivated = false;

// 样本对比：A/B 两份解析结果快照（localStorage 持久化）与最近一次 diff 结果
const SAMPLES = { a: null, b: null };
let diffResult = null;
const diffFilter = { added: true, removed: true, modified: true, unchanged: false };
const CHANGE_LABEL = { added: "新增", removed: "删除", modified: "修改", unchanged: "未变化" };

// ---------------------------------------------------------------- samples --

// 说明：RFC 2046 要求首定界符前有 CRLF；顶层头部块后的空行已提供该 CRLF，
// 因此 body 直接以 "--boundary" 开始（HTTP multipart 的常见形态）。
const SAMPLE_BASIC =
  'Content-Type: multipart/form-data; boundary=----WebKitFormBoundaryABC\r\n' +
  '\r\n' +
  '------WebKitFormBoundaryABC\r\n' +
  'Content-Disposition: form-data; name="username"\r\n' +
  '\r\n' +
  'alice@example.com\r\n' +
  '------WebKitFormBoundaryABC\r\n' +
  'Content-Disposition: form-data; name="bio"\r\n' +
  '\r\n' +
  '你好，MIME！\r\n' +
  '------WebKitFormBoundaryABC\r\n' +
  'Content-Disposition: form-data; name="file"; filename="notes.txt"\r\n' +
  'Content-Type: text/plain\r\n' +
  '\r\n' +
  'line1\r\n' +
  'line2\r\n' +
  '------WebKitFormBoundaryABC--';

const SAMPLE_NESTED =
  'Content-Type: multipart/mixed; boundary="OUTER-001"\r\n' +
  'MIME-Version: 1.0\r\n' +
  '\r\n' +
  'This is a MIME-formatted message.  If you see this, your mailer is broken.\r\n' +
  '--OUTER-001\r\n' +
  'Content-Type: text/plain; charset="utf-8"\r\n' +
  'Content-Transfer-Encoding: 8bit\r\n' +
  '\r\n' +
  '外层正文：请查收附件。\r\n' +
  '--OUTER-001\r\n' +
  'Content-Type: multipart/alternative; boundary="INNER-42"\r\n' +
  '\r\n' +
  '--INNER-42\r\n' +
  'Content-Type: text/plain; charset=utf-8\r\n' +
  'Content-Transfer-Encoding: 8bit\r\n' +
  '\r\n' +
  'plain 版本\r\n' +
  '--INNER-42\r\n' +
  'Content-Type: text/html; charset=utf-8\r\n' +
  'Content-Transfer-Encoding: 8bit\r\n' +
  '\r\n' +
  '<p><b>HTML</b> 版本</p>\r\n' +
  '--INNER-42--\r\n' +
  '--OUTER-001\r\n' +
  'Content-Type: text/plain; charset="utf-8"\r\n' +
  'Content-Transfer-Encoding: quoted-printable\r\n' +
  '\r\n' +
  '=E6=8A=A5=E4=BB=B7=EF=BC=9ACaf=C3=A9=20=3D=20=C2=A5123.45=EF=BC=8C=E6=95=AC=\r\n' +
  '=E8=AF=B7=E6=9F=A5=E6=94=B6=E9=99=84=E4=BB=B6=E5=B9=B6=E5=9C=A8=E6=9C=AC=E5=\r\n' +
  '=91=A8=E5=86=85=E5=9B=9E=E5=A4=8D=E7=A1=AE=E8=AE=A4=EF=BC=8C=E8=B0=A2=E8=B0=\r\n' +
  '=A2=E5=90=88=E4=BD=9C=EF=BC=81\r\n' +
  '--=20=E7=BB=93=E5=B0=BE=20--\r\n' +
  '--OUTER-001\r\n' +
  'Content-Disposition: attachment;\r\n' +
  " filename*0*=UTF-8''pay%6C%6F%61%64;\r\n" +
  ' filename*1*=%2Ebin; filename="payload-fallback.bin"\r\n' +
  'Content-Type: application/octet-stream\r\n' +
  'Content-Transfer-Encoding: base64\r\n' +
  '\r\n' +
  'TUlNRQABAiBiaW5hcnkg//4gcGF5bG9hZA0KbGluZTINCg==\r\n' +
  '--OUTER-001\r\n' +
  'Content-Disposition: attachment; filename="report.csv"\r\n' +
  'Content-Type: text/csv; charset=utf-8\r\n' +
  'Content-Transfer-Encoding: 8bit\r\n' +
  '\r\n' +
  'id,name\r\n' +
  '1,张三\r\n' +
  '--OUTER-001--\r\n' +
  'epilogue text, must be ignored';

const SAMPLE_BAD =
  // 故意使用 LF、缺关闭定界符、畸形头部、畸形 base64
  'Content-Type: multipart/form-data; boundary="BROKEN"\n' +
  '\n' +
  '--BROKEN\n' +
  'Content-Disposition: form-data; name="ok"\n' +
  '\n' +
  'good part\n' +
  '--BROKEN\n' +
  'Content-Type: application/octet-stream\n' +
  'Content-Transfer-Encoding: base64\n' +
  '\n' +
  'TUlN@@invalid!!!not-enough-padding\n' +
  '--BROKEN\n' +
  'this line is not a valid header (no colon)\n' +
  'Content-Disposition: form-data; name="bad"\n' +
  '\n' +
  'second part\n' +
  '--BROKEN\n' +
  'Content-Type: multipart/mixed; boundary=CHILD\n' +
  '\n' +
  '--CHILD\n' +
  '\n' +
  'nested child\n' +
  '--CHILD--';

// ------------------------------------------------------------- utilities --

function humanSize(n) {
  if (n < 1024) return n + " B";
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + " KB";
  return (n / 1024 / 1024).toFixed(2) + " MB";
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

async function postJSON(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) {
    throw new Error((data && (data.detail || data.error)) || `HTTP ${res.status}`);
  }
  return data;
}

function bytesToB64(bytes) {
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin);
}

function b64ToBytes(b64) {
  const bin = atob(b64);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

// --------------------------------------------------------------- issues --

function renderIssues(listEl, issues) {
  listEl.innerHTML = "";
  const shown = issues.filter((i) => issueFilter[i.level] !== false);
  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "info";
    li.innerHTML = '<span class="tag">OK</span><span class="msg">当前筛选下没有诊断信息</span>';
    listEl.appendChild(li);
    return;
  }
  for (const it of shown) {
    const li = document.createElement("li");
    li.className = it.level;
    const label = it.level === "error" ? "错误" : it.level === "warning" ? "警告" : "提示";
    li.innerHTML =
      `<span class="tag">${label}</span>` +
      `<span class="code">${escapeHtml(it.code || "")}</span>` +
      `<span class="msg">${escapeHtml(it.message || "")}</span>`;
    listEl.appendChild(li);
  }
}

function setCounts(counts) {
  counts = counts || { error: 0, warning: 0, info: 0 };
  $("#cntError").textContent = counts.error || 0;
  $("#cntWarning").textContent = counts.warning || 0;
  $("#cntInfo").textContent = counts.info || 0;
}

// ------------------------------------------------------------ tree view --

function reindexPaths(node, path) {
  node.path = path;
  if (node.kind === "multipart") {
    node.parts.forEach((c, i) => reindexPaths(c, `${path === "root" ? "parts" : path + ".parts"}[${i}]`));
  }
}

// RFC 2045 Content-Transfer-Encoding 选项
const CTE_OPTIONS = [
  ["7bit", "7bit"],
  ["8bit", "8bit"],
  ["binary", "binary"],
  ["base64", "base64"],
  ["quoted-printable", "quoted-printable"],
];
const CTE_SET = new Set(CTE_OPTIONS.map((o) => o[0]));
const DECODE_STATE_LABEL = {
  raw: "原始字节（7bit/8bit/binary，未做传输解码）",
  decoded: "传输解码成功，下方为解码后正文",
  malformed: "传输解码异常：已尽力恢复，原始载荷保留（未编辑即字节还原）",
  passthrough: "未知编码：正文按原始字节透传",
};

function cteStateClass(node) {
  switch (node.decodeState) {
    case "decoded": return "cte-ok";
    case "malformed":
    case "passthrough": return "cte-warn";
    default: return "cte-neutral";
  }
}

function newPartNode(index) {
  return {
    kind: "part",
    path: `parts[${index}]`,
    mediaType: "text/plain",
    headers: [
      { name: "Content-Type", value: "text/plain", raw: "" },
      { name: "Content-Disposition", value: `form-data; name="part${index + 1}"`, raw: "" },
    ],
    hasHeaders: true,
    body: "",
    bodyEncoding: "utf-8",
    // 新增节点没有原始传输体，必须走重新编码路径
    transferEncoding: "7bit",
    transferEncodingDeclared: "7bit",
    decodeState: "raw",
    rawBodyBase64: "",
    bodyEdited: true,
    rawSize: 0,
  };
}

function headerRow(table, node, idx) {
  const tr = document.createElement("tr");
  tr.innerHTML =
    `<td><input type="text" spellcheck="false" placeholder="Header-Name"></td>` +
    `<td><input type="text" spellcheck="false" placeholder="value"></td>` +
    `<td class="del-cell"><button class="btn btn-sm btn-danger" type="button">删</button></td>`;
  const [nameIn, valIn] = $$("input", tr);
  nameIn.value = node.headers[idx].name || "";
  valIn.value = node.headers[idx].value || "";
  nameIn.addEventListener("input", () => {
    node.headers[idx].name = nameIn.value;
    scheduleAttachRefresh();
    scheduleCidRefresh();
  });
  valIn.addEventListener("input", () => {
    node.headers[idx].value = valIn.value;
    scheduleAttachRefresh();
    scheduleCidRefresh();
  });
  $("button", tr).addEventListener("click", () => {
    node.headers.splice(idx, 1);
    rerenderTree();
  });
  return tr;
}

function renderNode(node, depth) {
  const wrap = document.createElement("div");
  wrap.className = "node";
  const isMulti = node.kind === "multipart";

  const titleParts = [];
  if (node.name != null) titleParts.push(`name=<b>${escapeHtml(node.name)}</b>`);
  if (node.filename) titleParts.push(`filename=<b>${escapeHtml(node.filename)}</b>`);
  const titleExtra = titleParts.length ? ` · ${titleParts.join(" · ")}` : "";

  const bar = document.createElement("div");
  bar.className = "node-bar";
  const ctePill = isMulti ? "" :
    `<span class="cte-badge ${cteStateClass(node)}" title="${escapeHtml(DECODE_STATE_LABEL[node.decodeState] || "")}">` +
    `CTE: ${escapeHtml(node.transferEncoding || "7bit")}${node.bodyEdited ? " ✎" : ""}</span>`;
  bar.innerHTML =
    `<span class="caret">▼</span>` +
    `<span class="badge ${isMulti ? "multipart" : ""}">${escapeHtml(node.mediaType || (isMulti ? "multipart/*" : "application/octet-stream"))}</span>` +
    ctePill +
    `<span class="node-title">${escapeHtml(node.path)}${titleExtra}</span>` +
    `<span class="node-spacer"></span>` +
    `<span class="node-size">${humanSize(node.rawSize || 0)}</span>`;
  bar.addEventListener("click", (e) => {
    if (e.target.closest("button, input, a")) return;
    wrap.classList.toggle("collapsed");
  });
  wrap.appendChild(bar);

  const body = document.createElement("div");
  body.className = "node-body";

  // boundary 编辑（仅 multipart）
  if (isMulti) {
    const br = document.createElement("div");
    br.className = "inline-row";
    br.innerHTML =
      `<label>boundary</label>` +
      `<input type="text" spellcheck="false">` +
      `<span class="meta-pill">定界符 --${escapeHtml(node.boundary || "")} / --${escapeHtml(node.boundary || "")}--</span>`;
    const bIn = $("input", br);
    bIn.value = node.boundary || "";
    bIn.addEventListener("input", () => { node.boundary = bIn.value; });
    bIn.addEventListener("click", (e) => e.stopPropagation());
    body.appendChild(br);
  }

  // 头部表
  const hdrLabel = document.createElement("label");
  hdrLabel.textContent = "头部（按输入顺序保留）";
  body.appendChild(hdrLabel);
  const table = document.createElement("table");
  table.className = "headers-table";
  table.innerHTML = `<tr><th>字段名</th><th>值</th><th></th></tr>`;
  node.headers.forEach((_h, idx) => table.appendChild(headerRow(table, node, idx)));
  body.appendChild(table);
  const addHdr = document.createElement("button");
  addHdr.className = "btn btn-sm add-row";
  addHdr.type = "button";
  addHdr.textContent = "+ 添加头部";
  addHdr.addEventListener("click", () => {
    node.headers.push({ name: "", value: "", raw: "" });
    rerenderTree();
  });
  body.appendChild(addHdr);

  if (isMulti) {
    // preamble / epilogue
    for (const key of ["preamble", "epilogue"]) {
      const encKey = key + "Encoding";
      const isBin = node[encKey] === "base64";
      const lab = document.createElement("label");
      const title = key === "preamble"
        ? "序言 preamble（非 MIME 客户端可见，通常无意义）"
        : "尾注 epilogue（关闭定界符之后）";
      lab.innerHTML = `${title} <span class="meta-pill ${isBin ? "latin" : ""}">` +
        `${isBin ? "二进制 · base64（未修改即逐字节还原）" : node[encKey]}</span>`;
      body.appendChild(lab);
      const ta = document.createElement("textarea");
      ta.spellcheck = false;
      ta.value = node[key] || "";
      ta.addEventListener("input", () => { node[key] = ta.value; });
      body.appendChild(ta);
    }

    // 子部分
    const childWrap = document.createElement("div");
    childWrap.className = "sub-node-wrap";
    node.parts.forEach((child) => childWrap.appendChild(renderNode(child, depth + 1)));

    const addPart = document.createElement("button");
    addPart.className = "btn btn-sm add-row";
    addPart.type = "button";
    addPart.textContent = "+ 在末尾插入部分";
    addPart.addEventListener("click", () => {
      node.parts.push(newPartNode(node.parts.length));
      rerenderTree();
    });
    childWrap.appendChild(addPart);
    body.appendChild(childWrap);
  } else {
    // Content-Transfer-Encoding 切换行
    const cteRow = document.createElement("div");
    cteRow.className = "inline-row cte-row";
    cteRow.innerHTML =
      `<label>Content-Transfer-Encoding</label>` +
      `<select class="cte-select"></select>` +
      `<span class="meta-pill ${cteStateClass(node)}">${escapeHtml(DECODE_STATE_LABEL[node.decodeState] || "")}</span>`;
    const sel = $(".cte-select", cteRow);
    for (const [val, label] of CTE_OPTIONS) {
      const opt = document.createElement("option");
      opt.value = val;
      opt.textContent = label;
      sel.appendChild(opt);
    }
    // 解析时遇到非标准 CTE（如 x-uuencode）：作为额外选项保留，避免选择丢失
    const curCte = node.transferEncoding || "7bit";
    if (!CTE_SET.has(curCte)) {
      const opt = document.createElement("option");
      opt.value = curCte;
      opt.textContent = curCte + "（原样透传）";
      sel.appendChild(opt);
    }
    sel.value = curCte;
    sel.addEventListener("click", (e) => e.stopPropagation());
    sel.addEventListener("change", () => {
      // 切换 base64 / quoted-printable 等：后端按新 CTE 重新编码后下载
      node.transferEncoding = sel.value;
      node.bodyEdited = true;
      rerenderTree();
    });
    body.appendChild(cteRow);

    if (node.bodyEdited) {
      const edited = document.createElement("div");
      edited.className = "hint cte-edited";
      edited.textContent = "该节点已被编辑：重新编码时将按所选 CTE 与 MIME 折行规则生成新的传输正文。";
      body.appendChild(edited);
    }
    if (node.decodeState === "malformed" || node.decodeState === "passthrough") {
      const warn = document.createElement("div");
      warn.className = "hint cte-malformed";
      warn.textContent = node.decodeState === "malformed"
        ? "原始 CTE 编码畸形：上方诊断面板有带节点路径的详情。未编辑时下载仍逐字节还原原始载荷；编辑后按所选 CTE 重新编码。"
        : "未识别的 CTE：解析时按原始字节透传，未编辑即字节还原。";
      body.appendChild(warn);
    }

    const lab = document.createElement("label");
    const enc = node.bodyEncoding || "utf-8";
    const isBin = enc === "base64";
    const cteEncoded = curCte === "base64" || curCte === "quoted-printable";
    lab.innerHTML =
      (cteEncoded ? "解码后正文（编辑此内容；下载时按所选 CTE 回写）" : "正文") +
      ` <span class="meta-pill ${isBin ? "latin" : ""}">` +
      `${isBin ? "编辑器通道 · base64（二进制，未修改即逐字节还原）" : enc}</span>`;
    body.appendChild(lab);
    if (isBin) {
      const note = document.createElement("div");
      note.className = "hint";
      note.textContent = "该部分解码后仍不是合法 UTF-8，以 base64 通道展示。直接编辑会被当作 base64 解码；"
        + "若想改为纯文本，请把内容替换为文本即可（将自动按 latin-1 还原字节）。";
      body.appendChild(note);
    }
    const ta = document.createElement("textarea");
    ta.spellcheck = false;
    ta.value = node.body || "";
    ta.addEventListener("input", () => {
      node.body = ta.value;
      node.bodyEdited = true;
      scheduleAttachRefresh();
      scheduleCidRefresh();
    });
    body.appendChild(ta);
  }

  // 删除按钮（根节点不显示）
  if (node.path !== "root") {
    const delRow = document.createElement("div");
    delRow.className = "add-row";
    const del = document.createElement("button");
    del.className = "btn btn-sm btn-danger";
    del.type = "button";
    del.textContent = "删除该部分";
    del.addEventListener("click", () => {
      const parent = findParent(tree, node);
      if (parent) {
        parent.parts = parent.parts.filter((p) => p !== node);
        rerenderTree();
      }
    });
    delRow.appendChild(del);
    body.appendChild(delRow);
  }

  wrap.appendChild(body);
  return wrap;
}

function findParent(root, target) {
  if (root.kind !== "multipart") return null;
  for (const p of root.parts) {
    if (p === target) return root;
    const found = findParent(p, target);
    if (found) return found;
  }
  return null;
}

function rerenderTree() {
  const rootEl = $("#treeRoot");
  rootEl.innerHTML = "";
  reindexPaths(tree, "root");
  rootEl.appendChild(renderNode(tree, 0));
  scheduleAttachRefresh();
  scheduleCidRefresh();
}

// ---------------------------------------------------------------- actions --

function fillSample(ta, text) {
  $("#rawInput").value = text;
  const firstLine = text.split(/\r?\n/, 1)[0];
  if (/content-type\s*:/i.test(firstLine)) {
    const m = firstLine.match(/content-type\s*:\s*(.+)/i);
    if (m) $("#contentTypeInput").value = m[1].trim();
  } else {
    $("#contentTypeInput").value = "";
  }
  $("#boundaryInput").value = "";
}

async function doParse() {
  const raw = $("#rawInput").value;
  if (!raw.trim()) {
    alert("请先粘贴报文或载入示例");
    return;
  }
  $("#btnParse").disabled = true;
  try {
    const rawBytes = new TextEncoder().encode($("#rawInput").value);
    const data = await postJSON("/api/parse", {
      raw: $("#rawInput").value,
      rawBase64: bytesToB64(rawBytes),
      contentType: $("#contentTypeInput").value || null,
      boundary: $("#boundaryInput").value || null,
    });
    parseResult = data;
    tree = data.tree;
    setCounts(data.counts);
    attachUnchecked.clear();
    attachSeen.clear();
    attachSeenExportable.clear();
    $("#effectiveBoundary").textContent = data.effectiveBoundary ?? "—（无）";
    const metaBits = [];
    metaBits.push(`媒体类型 <b>${escapeHtml(data.mediaType || "—")}</b>`);
    metaBits.push(`原始大小 <b>${humanSize(tree.rawSize || 0)}</b>`);
    metaBits.push(`行结束 <b>${data.normalizedCrlf ? "已规范化为 CRLF" : "CRLF"}</b>`);
    metaBits.push(`顶层头部 <b>${data.hasTopHeaders ? "检测到" : "未检测到"}</b>`);
    $("#parseMeta").innerHTML = metaBits.join("　·　");

    $("#issuesCard").classList.remove("hidden");
    $("#treeCard").classList.remove("hidden");
    $("#outputCard").classList.add("hidden");
    renderIssues($("#issuesList"), data.issues);
    rerenderTree();
    loadAttachments({ resetSelection: true });
    runCidCheck();
  } catch (e) {
    alert("解析失败：" + e.message);
  } finally {
    $("#btnParse").disabled = false;
  }
}

async function doEncode(andDownload = true) {
  if (!tree) return;
  reindexPaths(tree, "root");
  $("#btnEncode").disabled = true;
  try {
    const data = await postJSON("/api/encode", {
      tree,
      includeHeaders: $("#includeHeaders").checked,
    });
    encodedBytes = b64ToBytes(data.payloadBase64);
    $("#outputCard").classList.remove("hidden");
    const ok = (data.counts?.error || 0) === 0;
    $("#encodeMeta").innerHTML =
      (ok ? "✅ 编码成功" : "⚠️ 编码完成但存在错误") +
      `　·　大小 <b>${humanSize(data.size)}</b>　·　行结束 <b>${data.lineEnding}</b>`;
    renderIssues($("#encodeIssues"), data.issues);
    const preview = bytesToText(encodedBytes);
    $("#outputPreview").value = preview;
    if (andDownload && ok) downloadBlob();
    if (!ok) {
      $("#outputCard").scrollIntoView({ behavior: "smooth" });
    }
  } catch (e) {
    alert("编码失败：" + e.message);
  } finally {
    $("#btnEncode").disabled = false;
  }
}

function bytesToText(bytes) {
  // 优先 UTF-8，失败按 latin-1
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(bytes);
  } catch (_) {
    let s = "";
    for (const b of bytes) s += String.fromCharCode(b);
    return s;
  }
}

function currentFilename() {
  let name = ($("#filenameInput").value || "message.eml").trim();
  name = name.replace(/[\\/]/g, "_");
  return name;
}

function downloadBlob() {
  if (!encodedBytes) return;
  const blob = new Blob([encodedBytes], { type: "application/octet-stream" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = currentFilename();
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

async function copyOutput() {
  if (!encodedBytes) return;
  const text = bytesToText(encodedBytes);
  try {
    await navigator.clipboard.writeText(text);
    $("#btnCopy").textContent = "已复制";
    setTimeout(() => ($("#btnCopy").textContent = "复制为文本"), 1200);
  } catch (_) {
    const ta = $("#outputPreview");
    ta.classList.remove("hidden");
    ta.select();
    document.execCommand("copy");
  }
}

// ------------------------------------------------------- attachments (ZIP) --

const FILENAME_SOURCE_LABEL = {
  extended: "RFC 2231 扩展值（filename*）",
  plain: "filename 参数",
  path: "按节点路径命名",
};
const ATTACH_DECODE_LABEL = {
  raw: "原始字节（7bit/8bit/binary）",
  decoded: "CTE 解码成功",
  malformed: "畸形 CTE，未编辑不可导出",
  passthrough: "未知 CTE，未编辑不可导出",
};

function scheduleAttachRefresh(delay = 400) {
  // 正文输入/头部编辑/CTE 切换等树变化后延迟刷新附件列表（不清空当前勾选）
  if (!tree) return;
  clearTimeout(attachDebounce);
  attachDebounce = setTimeout(loadAttachments, delay);
}

async function loadAttachments({ resetSelection = false } = {}) {
  if (!tree) {
    $("#attachmentsCard").classList.add("hidden");
    return;
  }
  reindexPaths(tree, "root");
  try {
    const data = await postJSON("/api/attachments", { tree });
    attachments = data.attachments || [];
    if (resetSelection) {
      attachChecked.clear();
      attachSeen.clear();
      attachSeenExportable.clear();
      for (const a of attachments) {
        attachSeen.add(a.path);
        if (a.exportable) {
          attachSeenExportable.add(a.path);
          attachChecked.add(a.path);
        }
      }
    } else {
      const valid = new Set(attachments.map((a) => a.path));
      // 结构变化后剔除失效路径（勾选与“主动取消”记忆都要清理）
      for (const p of [...attachChecked]) if (!valid.has(p)) attachChecked.delete(p);
      for (const p of [...attachUnchecked]) if (!valid.has(p)) attachUnchecked.delete(p);
      for (const a of attachments) {
        // 新出现的附件：默认勾选（除非用户在同一路径上主动取消过——
        // 该路径在旧结构中已被删除的场景）；此前禁导、编辑后变为可导出的
        // 节点也视为“新可导出”，自动勾选
        if (a.exportable && !attachSeenExportable.has(a.path)
            && !attachUnchecked.has(a.path)) {
          attachChecked.add(a.path);
        }
        if (a.exportable) attachSeenExportable.add(a.path);
        attachSeen.add(a.path);
      }
    }
    $("#attachmentsCard").classList.remove("hidden");
    renderAttachments(data);
  } catch (e) {
    $("#attachMeta").textContent = "附件列表加载失败：" + e.message;
  }
}

// 用户主动取消过的路径在解析新报文时清空（见 doParse）

function renderAttachments(data) {
  const list = $("#attachList");
  list.innerHTML = "";
  if (!attachments.length) {
    const li = document.createElement("li");
    li.className = "attach-empty";
    li.textContent = "该报文中没有附件叶子（Content-Disposition: attachment 或带 filename 参数的叶子）。";
    list.appendChild(li);
  } else {
    for (const a of attachments) list.appendChild(renderAttachmentRow(a));
  }
  $("#attachMeta").innerHTML =
    `共 <b>${data.count}</b> 个附件 · 可导出 <b>${data.exportableCount}</b> 个`;
  const selectable = attachments.filter((a) => a.exportable);
  $("#attachSelectAll").checked = selectable.length > 0
    && selectable.every((a) => attachChecked.has(a.path));
  $("#btnDownloadZip").disabled =
    !attachments.some((a) => a.exportable && attachChecked.has(a.path));
}

function renderAttachmentRow(a) {
  const li = document.createElement("li");
  li.className = "attach-entry" + (a.exportable ? "" : " blocked");

  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.className = "attach-check";
  cb.checked = !!attachChecked.has(a.path);
  cb.disabled = !a.exportable;
  cb.addEventListener("click", (e) => e.stopPropagation());
  cb.addEventListener("change", () => {
    if (cb.checked) { attachChecked.add(a.path); attachUnchecked.delete(a.path); }
    else { attachChecked.delete(a.path); attachUnchecked.add(a.path); }
    renderAttachments({ count: attachments.length,
      exportableCount: attachments.filter((x) => x.exportable).length });
  });
  li.appendChild(cb);

  const main = document.createElement("div");
  main.className = "attach-main";
  const disp = a.disposition
    ? `<span class="attach-disp">${escapeHtml(a.disposition)}</span>` : "";
  const warnCls = a.decodeState === "malformed" || a.decodeState === "passthrough"
    ? "warn" : (a.edited ? "edited" : "ok");
  main.innerHTML =
    `<div class="attach-line1">` +
    `<span class="attach-name" title="ZIP 内文件名：${escapeHtml(a.zipName)}">${escapeHtml(a.zipName)}</span>` +
    `<span class="attach-size">${humanSize(a.size)}</span>` +
    `</div>` +
    `<div class="attach-line2">` +
    `<span class="attach-path">${escapeHtml(a.path)}</span>` +
    disp +
    `<span class="attach-badge">${escapeHtml(a.mediaType)}</span>` +
    `<span class="attach-src" title="文件名来源">${escapeHtml(FILENAME_SOURCE_LABEL[a.filenameSource] || a.filenameSource)}</span>` +
    `<span class="attach-cte ${warnCls}" title="Content-Transfer-Encoding / 解码状态">` +
    `CTE: ${escapeHtml(a.transferEncoding)} · ${escapeHtml(ATTACH_DECODE_LABEL[a.decodeState] || a.decodeState)}` +
    (a.edited ? " · 已编辑" : "") + `</span>` +
    `</div>`;
  if (a.filenameNote && a.filenameSource !== "extended") {
    const note = document.createElement("div");
    note.className = "attach-note";
    note.textContent = "扩展文件名（filename*）非法，已回退"
      + (a.filenameSource === "plain" ? "普通 filename" : "按节点路径命名")
      + "：" + a.filenameNote;
    main.appendChild(note);
  }
  if (!a.exportable && a.reason) {
    const reason = document.createElement("div");
    reason.className = "attach-reason";
    reason.textContent = "⛔ " + a.reason;
    main.appendChild(reason);
  }
  li.appendChild(main);
  return li;
}

async function downloadAttachmentsZip() {
  if (!tree) return;
  const paths = attachments
    .filter((a) => a.exportable && attachChecked.has(a.path))
    .map((a) => a.path);
  if (!paths.length) {
    alert("请先勾选至少一个可导出的附件");
    return;
  }
  reindexPaths(tree, "root");
  $("#btnDownloadZip").disabled = true;
  try {
    const res = await fetch("/api/attachments/zip", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ tree, paths }),
    });
    if (!res.ok) {
      let detail = `HTTP ${res.status}`;
      try {
        const j = await res.json();
        detail = j.error || detail;
      } catch (_) { /* ignore */ }
      await loadAttachments();   // 状态可能已变化（如编辑后变为可导出）
      alert("打包失败：" + detail);
      return;
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "attachments.zip";
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  } catch (e) {
    alert("打包失败：" + e.message);
  } finally {
    $("#btnDownloadZip").disabled = false;
  }
}

// --------------------------------------------------------- CID 引用检查 --

const CID_STATUS_LABEL = {
  ok: "唯一匹配",
  missing: "缺失",
  ambiguous: "重复歧义",
  "no-scope": "无作用域",
};

function scheduleCidRefresh(delay = 500) {
  // 正文/头部编辑、增删部分、CTE 切换等树变化后防抖重查（面板激活后才自动运行）
  if (!tree || !cidActivated) return;
  clearTimeout(cidDebounce);
  cidDebounce = setTimeout(runCidCheck, delay);
}

async function runCidCheck() {
  if (!tree) return;
  clearTimeout(cidDebounce);
  cidActivated = true;
  reindexPaths(tree, "root");
  const btn = $("#btnCidCheck");
  btn.disabled = true;
  try {
    const data = await postJSON("/api/cid-check", { tree });
    renderCidResults(data);
  } catch (e) {
    $("#cidMeta").textContent = "CID 检查失败：" + e.message;
  } finally {
    btn.disabled = false;
  }
}

function renderCidResults(data) {
  const s = data.summary || {};
  const bits = [`text/html 叶子 <b>${data.htmlCount}</b>`,
                `cid: 引用 <b>${data.refCount}</b>`];
  if (s.ok) bits.push(`唯一匹配 <b>${s.ok}</b>`);
  if (s.missing) bits.push(`缺失 <b>${s.missing}</b>`);
  if (s.ambiguous) bits.push(`歧义 <b>${s.ambiguous}</b>`);
  if (s.noScope) bits.push(`无作用域 <b>${s.noScope}</b>`);
  if (data.uncheckableCount) bits.push(`无法检查 <b>${data.uncheckableCount}</b>`);
  $("#cidMeta").innerHTML = bits.join(" · ");

  const list = $("#cidList");
  list.innerHTML = "";
  for (const h of (data.htmlParts || []).filter((p) => !p.checkable)) {
    const li = document.createElement("li");
    li.className = "cid-entry st-uncheckable";
    li.innerHTML =
      `<div class="cid-line1">` +
      `<span class="cid-status uncheckable">无法检查</span>` +
      `<code class="cid-ref">${escapeHtml(h.path)}</code>` +
      `</div>` +
      `<div class="cid-line2">${escapeHtml(h.reason || "")}</div>`;
    list.appendChild(li);
  }
  for (const e of data.entries || []) list.appendChild(renderCidEntry(e));
  if (!data.htmlCount) {
    const li = document.createElement("li");
    li.className = "cid-empty";
    li.textContent = "当前树中没有 text/html 叶子。";
    list.appendChild(li);
  } else if (!list.children.length) {
    const li = document.createElement("li");
    li.className = "cid-empty";
    li.textContent = "HTML 中未发现 cid: 引用。";
    list.appendChild(li);
  }

  // 各作用域内可用的 Content-ID 清单（折叠展示，便于排查缺失/歧义）
  const sc = $("#cidScopes");
  sc.innerHTML = "";
  for (const scope of data.scopes || []) {
    const cids = scope.contentIds || [];
    const det = document.createElement("details");
    det.className = "cid-scope";
    const summary = document.createElement("summary");
    summary.textContent = `作用域 ${scope.path} · ${cids.length} 个 Content-ID`;
    det.appendChild(summary);
    const ul = document.createElement("ul");
    ul.className = "cid-scope-list";
    if (!cids.length) {
      const li = document.createElement("li");
      li.className = "muted";
      li.textContent = "（作用域内没有带 Content-ID 的叶子）";
      ul.appendChild(li);
    }
    for (const c of cids) {
      const li = document.createElement("li");
      li.innerHTML = `${escapeHtml(c.path)} → <b>${escapeHtml(c.contentId)}</b>` +
        (c.duplicate ? ' <span class="cid-dup">（重复）</span>' : "");
      ul.appendChild(li);
    }
    det.appendChild(ul);
    sc.appendChild(det);
  }
}

function renderCidEntry(e) {
  const li = document.createElement("li");
  li.className = "cid-entry st-" + e.status;
  const status = CID_STATUS_LABEL[e.status] || e.status;
  let cands = "";
  if (e.status === "missing") {
    cands = '<div class="cid-cands cid-muted">作用域内没有匹配的 Content-ID</div>';
  } else if ((e.candidates || []).length) {
    cands = '<div class="cid-cands">' +
      e.candidates.map((c) =>
        `<span class="cid-cand">→ ${escapeHtml(c.path)}` +
        `（Content-ID: ${escapeHtml(c.contentId)}）</span>`).join("") +
      (e.status === "ambiguous"
        ? '<div class="cid-ambig-note">存在重复 Content-ID，未默认选取任何候选</div>'
        : "") +
      "</div>";
  }
  li.innerHTML =
    `<div class="cid-line1">` +
    `<span class="cid-status ${e.status}">${status}</span>` +
    `<code class="cid-ref">${escapeHtml(e.reference)}</code>` +
    `<span class="cid-attr">${escapeHtml(e.tag)} · ${escapeHtml(e.attribute)}</span>` +
    `</div>` +
    `<div class="cid-line2">来源 ${escapeHtml(e.sourcePath)}` +
    ` · 标识符 ${escapeHtml(e.identifier)}` +
    ` · 作用域 ${e.scopePath ? escapeHtml(e.scopePath) : "（无 multipart/related 祖先）"}</div>` +
    cands;
  return li;
}

// --------------------------------------------------------- sample compare --

function countParts(node) {
  let n = 0;
  (function walk(nd) {
    (nd.parts || []).forEach((c) => { n += 1; walk(c); });
  })(node || {});
  return n;
}

function loadSamples() {
  for (const slot of ["a", "b"]) {
    try {
      const raw = localStorage.getItem("mime-sample-" + slot);
      if (raw) SAMPLES[slot] = JSON.parse(raw);
    } catch (_) { /* 损坏的数据直接忽略 */ }
  }
}

function persistSample(slot) {
  try {
    if (SAMPLES[slot]) {
      localStorage.setItem("mime-sample-" + slot, JSON.stringify(SAMPLES[slot]));
    } else {
      localStorage.removeItem("mime-sample-" + slot);
    }
  } catch (_) {
    alert("样本过大，无法写入 localStorage；仅在当前页面有效。");
  }
}

function invalidateDiff() {
  diffResult = null;
  $("#diffResult").classList.add("hidden");
  $("#btnExportDiff").disabled = true;
  $("#diffMeta").textContent = "";
}

function saveSample(slot) {
  if (!tree) {
    alert("请先解析报文，再保存为样本");
    return;
  }
  reindexPaths(tree, "root");
  SAMPLES[slot] = {
    tree: JSON.parse(JSON.stringify(tree)),   // 深拷贝快照，后续编辑不影响样本
    issues: (parseResult && parseResult.issues) || [],
    counts: (parseResult && parseResult.counts) || null,
    mediaType: (parseResult && parseResult.mediaType) || tree.mediaType || null,
    savedAt: Date.now(),
  };
  persistSample(slot);
  invalidateDiff();
  renderSlots();
}

function clearSample(slot) {
  SAMPLES[slot] = null;
  persistSample(slot);
  invalidateDiff();
  renderSlots();
}

function renderSlots() {
  for (const slot of ["a", "b"]) {
    const el = $(slot === "a" ? "#slotA" : "#slotB");
    const meta = $(".slot-meta", el);
    const s = SAMPLES[slot];
    el.classList.toggle("filled", !!s);
    if (!s) {
      meta.textContent = `空 — 解析报文后点击「存为样本 ${slot.toUpperCase()}」`;
    } else {
      const c = s.counts || {};
      const time = new Date(s.savedAt).toLocaleString();
      meta.textContent =
        `${s.mediaType || "未知类型"} · ${countParts(s.tree)} 个部分 · ` +
        `诊断 ${c.error || 0}错/${c.warning || 0}警/${c.info || 0}提示 · 保存于 ${time}`;
    }
  }
  $("#btnCompare").disabled = !(SAMPLES.a && SAMPLES.b);
}

async function doCompare() {
  if (!SAMPLES.a || !SAMPLES.b) return;
  $("#btnCompare").disabled = true;
  try {
    const data = await postJSON("/api/diff", {
      a: { tree: SAMPLES.a.tree, issues: SAMPLES.a.issues || [] },
      b: { tree: SAMPLES.b.tree, issues: SAMPLES.b.issues || [] },
    });
    diffResult = data;
    renderDiff(data);
    $("#btnExportDiff").disabled = false;
  } catch (e) {
    alert("对比失败：" + e.message);
  } finally {
    $("#btnCompare").disabled = false;
  }
}

function renderDiff(data) {
  const s = data.summary || {};
  $("#cntAdded").textContent = s.added || 0;
  $("#cntRemoved").textContent = s.removed || 0;
  $("#cntModified").textContent = s.modified || 0;
  $("#cntUnchanged").textContent = s.unchanged || 0;
  $("#diffMeta").innerHTML =
    `新增 <b>${s.added || 0}</b> · 删除 <b>${s.removed || 0}</b> · ` +
    `修改 <b>${s.modified || 0}</b> · 未变化 <b>${s.unchanged || 0}</b>`;
  $("#diffResult").classList.remove("hidden");

  const changeMap = {};
  for (const e of data.entries || []) if (e.path) changeMap[e.path] = e.change;

  renderDiffList(data.entries || []);
  renderDiffTree("#diffTreeA", SAMPLES.a.tree, changeMap);
  renderDiffTree("#diffTreeB", SAMPLES.b.tree, changeMap);
  $("#diffResult").scrollIntoView({ behavior: "smooth", block: "start" });
}

function renderDiffList(entries) {
  const list = $("#diffList");
  list.innerHTML = "";
  const shown = entries.filter((e) => diffFilter[e.change] !== false);
  if (!shown.length) {
    const li = document.createElement("li");
    li.className = "diff-empty";
    li.textContent = "当前筛选下没有差异条目";
    list.appendChild(li);
    return;
  }
  for (const e of shown) list.appendChild(renderDiffEntry(e));
}

function briefText(s, prefix) {
  if (!s) return prefix;
  const bits = [s.mediaType || "?", `${s.descendantParts || 0} 个后代部分`,
                `${s.headers || 0} 条头部`, humanSize(s.rawSize || 0)];
  return `${prefix} · ${bits.join(" · ")}`;
}

function summarizeFields(e) {
  if (e.change === "added") return briefText(e.summary, "仅存在于样本 B");
  if (e.change === "removed") return briefText(e.summary, "仅存在于样本 A");
  if (e.change === "unchanged") return "一致";
  return (e.fields || []).map((f) => {
    if (f.field === "headers") {
      return `头部 +${f.added.length}/-${f.removed.length}${f.reordered ? "·仅顺序" : ""}`;
    }
    if (f.field === "issues") return `诊断 +${f.added.length}/-${f.removed.length}`;
    return f.label;
  }).join(" · ");
}

function renderDiffEntry(e) {
  const li = document.createElement("li");
  li.className = `diff-entry chg-${e.change} collapsed`;
  const isGlobal = !e.path;

  const bar = document.createElement("div");
  bar.className = "diff-entry-bar";
  bar.innerHTML =
    `<span class="caret">▼</span>` +
    `<span class="chg-badge ${e.change}">${CHANGE_LABEL[e.change] || e.change}</span>` +
    `<span class="diff-path">${isGlobal ? "（全局诊断）" : escapeHtml(e.path)}</span>` +
    `<span class="diff-fields-summary">${escapeHtml(summarizeFields(e))}</span>` +
    `<span class="node-spacer"></span>`;
  if (!isGlobal) {
    if (e.change !== "added") bar.appendChild(locateBtn("a", e.path));
    if (e.change !== "removed") bar.appendChild(locateBtn("b", e.path));
  }
  bar.addEventListener("click", (ev) => {
    if (ev.target.closest("button")) return;
    li.classList.toggle("collapsed");
  });
  li.appendChild(bar);

  const body = document.createElement("div");
  body.className = "diff-entry-body";
  if (e.change === "added" || e.change === "removed") {
    const brief = document.createElement("div");
    brief.className = "hint diff-brief";
    brief.textContent = summarizeFields(e) + "（整棵子树已折叠为一条）";
    body.appendChild(brief);
  }
  for (const f of e.fields || []) body.appendChild(renderField(f));
  if (!(e.fields || []).length && e.change !== "added" && e.change !== "removed") {
    body.innerHTML = '<div class="hint">无字段级差异</div>';
  }
  li.appendChild(body);
  return li;
}

function valHtml(v) {
  if (!v || v.text == null) return '<span class="trunc-note">（无）</span>';
  const t = escapeHtml(v.text);
  return v.truncated
    ? `${t}<span class="trunc-note"> …（已截断，共 ${v.length} 字符）</span>` : t;
}

function renderField(f) {
  const div = document.createElement("div");
  div.className = "diff-field";
  if (f.field === "headers") {
    let html = `<div class="diff-field-label">头部（A ${f.aCount} 条 / B ${f.bCount} 条）</div>`;
    for (const line of f.removed) html += `<div class="hdr-line del">− ${escapeHtml(line)}</div>`;
    for (const line of f.added) html += `<div class="hdr-line add">+ ${escapeHtml(line)}</div>`;
    if (f.reordered) html += `<div class="hint">头部内容一致，仅顺序不同</div>`;
    div.innerHTML = html;
    return div;
  }
  if (f.field === "issues") {
    let html = `<div class="diff-field-label">${escapeHtml(f.label)}</div><div class="issue-diff">`;
    for (const c of f.removed) html += `<span class="issue-pill del">− ${escapeHtml(c)}</span>`;
    for (const c of f.added) html += `<span class="issue-pill add">+ ${escapeHtml(c)}</span>`;
    html += "</div>";
    div.innerHTML = html;
    return div;
  }
  const table = document.createElement("table");
  table.className = "diff-field-table";
  table.innerHTML =
    `<tr><th rowspan="2">${escapeHtml(f.label)}</th>` +
    `<td><span class="side-tag">A</span> <span class="diff-val old">${valHtml(f.a)}</span></td></tr>` +
    `<tr><td><span class="side-tag">B</span> <span class="diff-val new">${valHtml(f.b)}</span></td></tr>`;
  div.appendChild(table);
  return div;
}

function renderDiffTree(sel, sampleTree, changeMap) {
  const el = $(sel);
  el.innerHTML = "";
  if (!sampleTree) {
    el.textContent = "（空）";
    return;
  }
  el.appendChild(renderRoNode(sampleTree, changeMap));
}

function renderRoNode(node, changeMap) {
  const wrap = document.createElement("div");
  wrap.className = "ro-node";
  wrap.dataset.diffPath = node.path || "";
  const change = changeMap[node.path];
  if (change && change !== "unchanged") wrap.classList.add("chg-" + change);
  const hasKids = node.kind === "multipart" && (node.parts || []).length > 0;
  const cteBadge = node.kind === "multipart" ? "" :
    `<span class="cte-badge ${cteStateClass(node)}" title="${escapeHtml(DECODE_STATE_LABEL[node.decodeState] || "")}">` +
    `${escapeHtml(node.transferEncoding || "7bit")}</span>`;
  const bar = document.createElement("div");
  bar.className = "ro-bar";
  bar.innerHTML =
    `<span class="caret">${hasKids ? "▼" : "·"}</span>` +
    `<span class="badge ${node.kind === "multipart" ? "multipart" : ""}">` +
    `${escapeHtml(node.mediaType || "?")}</span>` +
    cteBadge +
    `<span class="node-title">${escapeHtml(node.path || "")}</span>` +
    (change && change !== "unchanged"
      ? `<span class="chg-tag tag-${change}">${CHANGE_LABEL[change]}</span>` : "");
  if (hasKids) {
    bar.addEventListener("click", () => wrap.classList.toggle("collapsed"));
    const kids = document.createElement("div");
    kids.className = "ro-kids";
    node.parts.forEach((c) => kids.appendChild(renderRoNode(c, changeMap)));
    wrap.appendChild(bar);
    wrap.appendChild(kids);
  } else {
    wrap.appendChild(bar);
  }
  return wrap;
}

function locateBtn(slot, path) {
  const b = document.createElement("button");
  b.className = "btn btn-sm locate-btn";
  b.type = "button";
  b.textContent = slot.toUpperCase();
  b.title = `在样本 ${slot.toUpperCase()} 结构树中定位该节点`;
  b.addEventListener("click", (ev) => {
    ev.stopPropagation();
    locateNode(slot, path);
  });
  return b;
}

function locateNode(slot, path) {
  const panel = $(slot === "a" ? "#diffTreeA" : "#diffTreeB");
  const el = panel.querySelector(`[data-diff-path="${path}"]`);
  if (!el) return;
  // 展开所有祖先节点
  let p = el.parentElement;
  while (p && p !== panel) {
    if (p.classList && p.classList.contains("ro-node")) p.classList.remove("collapsed");
    p = p.parentElement;
  }
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  el.classList.remove("flash");
  void el.offsetWidth;   // 重新触发高亮动画
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1600);
}

function exportDiff() {
  if (!diffResult) return;
  const metaOf = (s) => s ? {
    mediaType: s.mediaType,
    savedAt: new Date(s.savedAt).toISOString(),
    parts: countParts(s.tree),
    counts: s.counts || null,
  } : null;
  const report = {
    tool: "mime-multipart-inspector",
    report: "sample-diff",
    version: 1,
    generatedAt: new Date().toISOString(),
    sampleA: metaOf(SAMPLES.a),
    sampleB: metaOf(SAMPLES.b),
    summary: diffResult.summary,
    entries: diffResult.entries,
  };
  const blob = new Blob([JSON.stringify(report, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = "mime-diff-report.json";
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ----------------------------------------------------------------- init --
function init() {
  $("#btnParse").addEventListener("click", doParse);
  $("#btnEncode").addEventListener("click", () => doEncode(true));
  $("#btnDownload").addEventListener("click", downloadBlob);
  $("#btnCopy").addEventListener("click", copyOutput);
  $("#showOutput").addEventListener("change", (e) => {
    $("#outputPreview").classList.toggle("hidden", !e.target.checked);
  });
  $("#btnSampleBasic").addEventListener("click", () => {
    fillSample($("#rawInput"), SAMPLE_BASIC);
  });
  $("#btnSampleNested").addEventListener("click", () => {
    fillSample($("#rawInput"), SAMPLE_NESTED);
  });
  $("#btnSampleBad").addEventListener("click", () => {
    fillSample($("#rawInput"), SAMPLE_BAD);
  });
  $$(".issue-filters .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const lvl = chip.dataset.level;
      issueFilter[lvl] = !issueFilter[lvl];
      chip.classList.toggle("active", issueFilter[lvl]);
      if (parseResult) renderIssues($("#issuesList"), parseResult.issues);
    });
  });
  // 样本对比
  $("#btnSaveA").addEventListener("click", () => saveSample("a"));
  $("#btnSaveB").addEventListener("click", () => saveSample("b"));
  $$(".slot-clear").forEach((b) => {
    b.addEventListener("click", () => clearSample(b.dataset.slot));
  });
  $("#btnCompare").addEventListener("click", doCompare);
  $("#btnExportDiff").addEventListener("click", exportDiff);
  // 附件提取
  $("#btnRefreshAttach").addEventListener("click", () => loadAttachments());
  $("#btnCidCheck").addEventListener("click", runCidCheck);
  $("#btnDownloadZip").addEventListener("click", downloadAttachmentsZip);
  $("#attachSelectAll").addEventListener("change", (e) => {
    if (e.target.checked) {
      for (const a of attachments) {
        if (a.exportable) { attachChecked.add(a.path); attachUnchecked.delete(a.path); }
      }
    } else {
      for (const a of attachments) {
        if (a.exportable) { attachChecked.delete(a.path); attachUnchecked.add(a.path); }
      }
    }
    renderAttachments({ count: attachments.length,
      exportableCount: attachments.filter((x) => x.exportable).length });
  });
  $$(".diff-filters .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const c = chip.dataset.change;
      diffFilter[c] = !diffFilter[c];
      chip.classList.toggle("active", diffFilter[c]);
      if (diffResult) renderDiffList(diffResult.entries || []);
    });
  });
  loadSamples();
  renderSlots();
  // 默认载入表单示例，打开即可体验
  fillSample($("#rawInput"), SAMPLE_BASIC);
}

document.addEventListener("DOMContentLoaded", init);
