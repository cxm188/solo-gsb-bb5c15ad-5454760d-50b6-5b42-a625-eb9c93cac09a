# MIME Multipart 报文检查器

基于 **FastAPI + 原生 HTML/CSS/JavaScript** 的 MIME `multipart/*` 报文解析、
查看、编辑与重新编码工具。严格按 **RFC 2045 / 2046** 处理：

- 🔍 **解析**：粘贴 `Content-Type`、`boundary` 和原始报文，自动识别
  *完整 MIME 报文*（含顶层头部）或 *仅 multipart body*；
- 🪆 **嵌套 multipart**：递归解析 `multipart/mixed`、`multipart/alternative`、
  `multipart/form-data` 等任意 `multipart/*`；
- 📐 **CRLF 边界**：识别 `CRLF "--boundary"` 起始定界符与
  `"--boundary--"` 关闭定界符，校验 transport padding、preamble/epilogue、
  缺关闭定界符、报文截断等；
- 📝 **引号参数**：正确解析 Content-Type 中带引号的 `boundary` 参数、
  分号与反斜杠转义（如 `boundary="a;b"`、`charset="utf\"-8"`）；
- 🔡 **Content-Transfer-Encoding 语义**：按 RFC 2045 识别叶子部分的
  `base64`、`quoted-printable`、`7bit`、`8bit` 与 `binary`，把传输正文
  **解码为可编辑内容**并保留原始编码信息；可在前端切换
  base64 / quoted-printable 后重新编码下载（遵守 76 字符 MIME 折行）。
  畸形编码产生**带节点路径**的诊断且不丢失原始载荷，未编辑节点逐字节稳定；
- 🩺 **诊断面板**：按 *错误 / 警告 / 提示* 三级列出每个解析问题及所属部分路径；
- ✏️ **可视化编辑**：逐层展开/折叠，编辑任意部分的头部、正文、boundary、
  preamble / epilogue，可新增或删除部分；
- 🔁 **重新编码并下载**：按（可能已修改的）boundary 规则以 CRLF 重新生成报文，
  按头部声明回写 Content-Transfer-Encoding（base64/QP 自动折行），
  可选择是否包含顶层头部；未修改的部分逐字节还原（含二进制）。
- 🆚 **样本对比**：把两份解析结果存为样本 A / B（保存在浏览器 localStorage），
  按树路径对齐比较媒体类型、头部、正文编码、boundary、序言/尾注与诊断代码；
  差异以可展开的新增/删除/修改清单展示，点击条目即可在两侧结构树中定位节点，
  并可导出 JSON 差异报告。
- 📎 **附件提取（ZIP）**：从当前解析或编辑后的树递归列出带
  `Content-Disposition: attachment` 或 `filename` 参数的叶子，展示
  节点路径、文件名、字节数与不可导出原因，勾选后打包为 ZIP 下载。
  文件名遵循 **RFC 2231**：支持 `filename*` 与 `filename*0*`/`filename*1`
  分段拼接，扩展值优先于普通 `filename`，非法扩展值回退普通值，无文件名时
  按节点路径命名；文件名中的路径分隔符与控制字符被清除，重名按遍历顺序加序号。
  ZIP 使用 **CTE 解码后的字节**（已编辑叶子取编辑后内容）；畸形或未知
  Content-Transfer-Encoding 的**未编辑**叶子禁止导出并说明原因。
- 🔗 **CID 引用检查面板**：在编辑树旁的侧栏中静态检查
  `multipart/related` 内 HTML 的 `cid:` 引用。仅扫描**合法 UTF-8 的
  `text/html`** 叶子（其他含 html 的类型 / 非 UTF-8 正文报“无法检查”），
  用 HTML 分词器提取各标签 `src` / `href` 中的 `cid:` 引用，**不执行、
  不加载任何资源**。每个引用只在**最近的 `multipart/related` 祖先**作用域内
  匹配叶子（嵌套 related 子树归内层作用域，不作为外层候选）；HTML 没有
  related 祖先时报“无作用域”。`Content-ID` 去首尾空白与外层尖括号
  （`<id>` → `id`），引用去 `cid:` 后**百分号解码一次**，标识符区分大小写。
  逐条展示来源路径、原始引用、作用域、候选路径以及**唯一匹配 / 缺失 / 重复歧义**
  （重复时列出全部候选但不默认选取）。点击「检查」使用当前树；正文、头部、
  CTE、增删部分后自动重新检查并更新。

---

## 快速开始（Docker）

```bash
docker compose up --build
```

启动后用浏览器访问：

> **http://localhost:8000**

停止：

```bash
docker compose down
```

## 不使用 Compose（docker）

```bash
docker build -t mime-inspector .
docker run --rm -p 8000:8000 mime-inspector
# 浏览器打开 http://localhost:8000
```

## 本地开发（Python 3.11+）

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

访问 http://localhost:8000 ；交互式 API 文档在
http://localhost:8000/docs 。

---

## 使用说明

1. 在顶部选择一个内置示例（**表单 / 嵌套 / 错误报文**），或直接粘贴：
   - **Content-Type**（可选）：例如
     `multipart/form-data; boundary="----xyz"`；留空则从报文头部读取；
   - **boundary**（可选）：单独填写，会覆盖 Content-Type 中的参数（可带引号）；
   - **原始报文**：完整 MIME 报文或 multipart body 均可，支持 CRLF / LF。
2. 点击 **解析报文**，查看诊断信息与树形结构。
3. 展开任意 multipart 节点，修改 boundary、头部、正文、preamble/epilogue，
   或增删子部分。
4. 在叶子节点上通过 **Content-Transfer-Encoding** 下拉框切换
   `7bit / 8bit / binary / base64 / quoted-printable`：
   - 解析时若头部声明为 `base64` 或 `quoted-printable`，正文框展示的是
     **解码后内容**，可直接编辑；节点标题旁的 `CTE:` 徽章与下拉框右侧的
     状态胶囊显示编码方式与解码状态（原始 / 解码成功 / 畸形 / 未知透传）；
   - 切换编码或修改正文后，点击 **重新编码并下载**，后端按所选 CTE 回写
     并遵守 MIME 折行规则（base64 与 quoted-printable 每行不超过 76 字符，
     以 CRLF 折行），同时同步 Content-Transfer-Encoding 头部；
   - **未编辑**的节点（含畸形 base64/QP）始终回写解析时保存的原始传输字节，
     保证逐字节稳定；原始载荷保存在节点的 `rawBodyBase64` 字段中，不会丢失。
5. 勾选/取消“编码时包含顶层头部”，点击 **重新编码并下载**。
6. 在结果区可重新下载、复制为文本，或查看编码后的完整报文。
7. **附件提取**（「3. 附件提取」卡片，解析后自动出现）：
   - 递归列出所有带 `Content-Disposition: attachment` 或 `filename` /
     `filename*` 参数的叶子（包括嵌套 multipart 内部），每行显示
     **ZIP 内文件名、字节数、节点路径、disposition、媒体类型、文件名来源、
     CTE 与解码状态**；
   - 文件名按 **RFC 2231** 解析：`filename*=UTF-8''%E2%82%AC` 单值或
     `filename*0*`/`filename*1*` 分段（首段携带 `charset'language'`，
     后续段可以是扩展段或普通段）优先；扩展值非法（错误的百分号转义、
     未知字符集、分段不连续等）时回退普通 `filename`，并在行内注明原因；
     两者都没有时按节点路径命名（如 `parts[2].parts[0]`）；
   - 路径分隔符（`/`、`\`）与控制字符会从文件名中删除，重名按树的先序
     遍历顺序自动加序号（`report.csv`、`report (2).csv`）；
   - 勾选要导出的附件后点击 **打包所选并下载 ZIP**：ZIP 条目内容是
     **CTE 解码后的字节**，已编辑叶子（正文改动或 CTE 切换）取编辑后的
     内容；未编辑叶子若处于 `malformed`（畸形 base64/QP）或
     `passthrough`（未知 CTE）状态则**无法勾选**，并显示具体原因——
     在结构树中编辑该节点后即变为可导出。
8. **CID 引用检查**（结构树右侧侧栏，解析后自动出现）：
   - 只检查 `Content-Type: text/html` 且正文为**合法 UTF-8** 的叶子：
     `application/xhtml+xml` 等其他 html 类型、二进制（base64 编辑通道）或
     非法 UTF-8 的 text/html 一律显示「无法检查」与原因，不做猜测；
   - 逐条引用展示：**来源路径**、标签/属性/行号与原始属性值、
     **解码后的 CID**、**作用域**（最近 `multipart/related` 祖先的路径；
     无祖先显示「无作用域」）、该作用域内的**候选路径**，以及结论徽章
     **✓ 唯一匹配**（绿色）/ **缺失**（红色）/ **重复歧义**（黄色，
     列出全部候选，不默认选任何一个）/ **无作用域**（灰色）；
   - 点击侧栏右上角「检查」立即使用**当前树**重新检查；在结构树中编辑正文、
     头部（含 Content-ID / Content-Type）、切换 CTE、新增或删除部分后，
     停止输入片刻自动重查（与附件列表共用防抖）。
9. **样本对比**（页面底部「5. 样本对比」卡片）：
   - 解析一份报文后点击结构区右上角的 **存为样本 A**；再解析（或编辑后重新解析）
     另一份报文，点击 **存为样本 B**。样本保存在浏览器 localStorage，刷新不丢失；
   - 点击 **开始对比**，查看按树路径对齐的差异清单（新增 / 删除 / 修改 / 未变化，
     可点击徽章筛选），展开条目查看字段级 A/B 对照；
   - 点击条目右侧的 **A** / **B** 按钮，在下方对应的只读结构树中高亮定位该节点；
   - 点击 **导出 JSON 差异报告** 下载 `mime-diff-report.json`。

> 不是合法 UTF-8 的正文（如二进制附件）会以 **base64 编辑通道** 呈现；
> 只要不修改，重新编码时会逐字节还原。注意区分两个层面的编码：
> **Content-Transfer-Encoding**（MIME 传输编码，记录在
> `transferEncoding` 字段）与**编辑通道**（`bodyEncoding`，仅表示
> 前后端 JSON 中该正文以 utf-8 文本还是 base64 承载）。

---

## 解析规则与诊断代码

解析器采用“尽量恢复、绝不轻易失败”的策略：能继续解析的问题记录为 issue，
而不是中断。常见诊断：

| 代码 | 级别 | 含义 |
| --- | --- | --- |
| `NO_DELIMITER` | 错误 | 正文中找不到任何 `--boundary` |
| `MISSING_CLOSE_DELIMITER` | 错误 | 缺少 `--boundary--`，报文可能被截断 |
| `TRUNCATED_PART` | 错误 | 某部分之后缺少结束该部分的 CRLF |
| `BAD_DELIMITER` | 错误 | 定界符后存在非法字符，或出现碰撞文本 |
| `MISSING_BOUNDARY` | 错误 | `multipart/*` 的 Content-Type 无 boundary 参数 |
| `BOUNDARY_COLLISION` | 错误 | （编码时）正文行首出现本层定界符，会截断报文 |
| `BAD_HEADER_LINE` / `BAD_HEADER_NAME` | 错误 | 头部缺少冒号 / 字段名非法 |
| `BAD_QUOTED_STRING` / `BAD_PARAM_SYNTAX` | 错误 | 引号未闭合 / 参数语法错误 |
| `LF_NORMALIZED` | 警告 | 输入含孤立 LF/CR，已规范化为 CRLF |
| `BOUNDARY_NONCOMPLIANT` | 警告 | boundary 超过 70 字符或含非法字符 |
| `BOUNDARY_OVERRIDE` | 警告 | 输入框/Content-Type 中的 boundary 不一致 |
| `TRAILING_PADDING` | 警告 | 定界符后存在 transport padding 空白 |
| `NO_LEADING_CRLF` | 提示 | 首定界符前无 CRLF（HTTP multipart 常见） |
| `PREAMBLE` / `EPILOGUE` | 提示 | 存在序言 / 尾注，MIME 处理器应忽略 |
| `BINARY_BODY` | 提示 | 该部分为非文本，已按 base64 呈现 |
| `BAD_CTE_SYNTAX` | 警告 | Content-Transfer-Encoding 头不是单个合法 token（含多余参数），已原样透传 |
| `UNKNOWN_TRANSFER_ENCODING` | 警告 | 未知/非标准 CTE（如 `x-uuencode`），正文按原始字节透传 |
| `CTE_NOT_7BIT` / `CTE_BINARY_IN_8BIT` | 警告 | 声明 7bit 却含非 ASCII/控制字节，或 8bit 中混入 NUL 等 |
| `CTE_BARE_NEWLINE` | 警告 | 7bit/8bit/QP 正文含孤立 CR/LF（RFC 要求 CRLF） |
| `CTE_LONG_LINE` | 警告 | base64/QP 编码行超过 76 字符折行限制 |
| `CTE_TRAILING_WSP` | 警告 | QP 硬换行前的空格/制表符未编码为 `=20`/`=09` |
| `BAD_BASE64` | 警告 | base64 含非法字符/错误 padding，已剔除/补齐后尽力解码（状态 `malformed`） |
| `BAD_QUOTED_PRINTABLE` | 警告 | QP 含非法 `=XX` 转义，已按字面保留（状态 `malformed`，载荷不丢） |
| `BAD_CTE_ON_MULTIPART` | 警告 | multipart 实体只能使用 7bit/8bit/binary，已忽略该 CTE |
| `CTE_HEADER_ADDED` | 提示 | 切换到 base64/QP 等编码时自动添加 Content-Transfer-Encoding 头 |

### Content-Transfer-Encoding 语义（RFC 2045 §6）

- **识别**：叶子部分的 `Content-Transfer-Encoding` 头（不区分大小写）
  支持 `7bit`、`8bit`、`binary`、`base64`、`quoted-printable`；缺省视为 `7bit`。
  值非法或未知时记录诊断并**原样透传**，不做破坏性转换。
- **解码**：`base64` / `quoted-printable` 正文被解码为编辑内容
  （再按是否合法 UTF-8 决定编辑通道 `bodyEncoding`）。畸形数据尽力恢复，
  诊断消息带节点路径（如 `parts[1].parts[0]：base64 正文含非法字符…`），
  节点 `decodeState` 标为 `malformed`；原始传输体始终保留在 `rawBodyBase64`。
- **重新编码**：按节点的 `transferEncoding`（即头部声明）回写：
  base64 与 quoted-printable 自动按 **76 字符** 折行（CRLF），
  QP 中 `=`、非 ASCII、控制字节转义为 `=XX`，硬换行前的 WSP 编码为
  `=20`/`=09`，孤立 CR/LF 转义以避免裸换行；同时同步头部。
- **字节稳定**：未编辑（`bodyEdited` 非真、CTE 未切换）的节点直接回写
  `rawBodyBase64` 保存的原始传输体，即使原始编码畸形也逐字节还原。

---

## HTTP API

### `POST /api/parse`

请求：

```json
{
  "raw": "Content-Type: multipart/form-data; boundary=B\r\n\r\n--B\r\n\r\nv\r\n--B--",
  "rawBase64": "...(可选，二进制无损通道，优先于 raw)...",
  "contentType": "multipart/form-data; boundary=B",
  "boundary": "B"
}
```

响应（节选）：

```json
{
  "ok": true,
  "mediaType": "multipart/form-data",
  "effectiveBoundary": "B",
  "hasTopHeaders": true,
  "counts": { "error": 0, "warning": 0, "info": 0 },
  "issues": [ { "level": "warning", "code": "LF_NORMALIZED", "message": "..." } ],
  "tree": {
    "kind": "multipart",
    "mediaType": "multipart/form-data",
    "boundary": "B",
    "leadingCrlf": false,
    "headers": [ { "name": "Content-Type", "value": "..." } ],
    "preamble": "", "preambleEncoding": "utf-8",
    "epilogue": "", "epilogueEncoding": "utf-8",
    "parts": [ {
      "kind": "part",
      "mediaType": "text/plain",
      "headers": [],
      "hasHeaders": false,
      "body": "v",
      "bodyEncoding": "utf-8",
      "transferEncoding": "7bit",
      "transferEncodingDeclared": "7bit",
      "decodeState": "raw",
      "rawBodyBase64": "dg=="
    } ]
  }
}
```

### `POST /api/encode`

请求：

```json
{ "tree": { /* /api/parse 返回的 tree，可修改 */ }, "includeHeaders": true }
```

响应（叶子节点 CTE 字段说明）：

叶子节点与 Content-Transfer-Encoding 相关的字段（`/api/parse` 返回，
可在 tree 中修改后回传 `/api/encode`）：

| 字段 | 含义 |
| --- | --- |
| `transferEncoding` | 当前生效 CTE：`7bit`/`8bit`/`binary`/`base64`/`quoted-printable`，或非标准透传值 |
| `transferEncodingDeclared` | 解析时头部声明的 CTE；与 `transferEncoding` 相同且未编辑时走字节稳定快速路径 |
| `decodeState` | `raw`（7/8bit/binary 原样）/ `decoded`（base64、QP 解码成功）/ `malformed`（畸形已尽力恢复）/ `passthrough`（未知 CTE） |
| `rawBodyBase64` | **未做 CTE 解码的原始传输体**；未编辑节点重新编码时原样回写，保证载荷不丢 |
| `body` / `bodyEncoding` | CTE 解码后的可编辑内容，及其编辑通道（`utf-8` 文本或 `base64` 二进制通道） |
| `bodyEdited` | 前端在正文被修改或 CTE 切换时置 `true`；缺省/为假时节点视为未编辑 |

```json
{
  "ok": true,
  "payloadBase64": "Q29udGVudC1UeXBlOi...",
  "size": 123,
  "lineEnding": "CRLF",
  "counts": { "error": 0, "warning": 0, "info": 0 },
  "issues": []
}
```

### `POST /api/diff`

样本对比：按树路径对齐比较两份解析结果。请求中的 `tree` / `issues`
直接取自两次 `POST /api/parse` 的响应（`issues` 可省略，省略则不对比诊断代码）。

请求：

```json
{
  "a": { "tree": { /* 样本 A 的 tree */ }, "issues": [ /* 样本 A 的 issues */ ] },
  "b": { "tree": { /* 样本 B 的 tree */ }, "issues": [ /* 样本 B 的 issues */ ] }
}
```

响应（节选）：

```json
{
  "ok": true,
  "summary": { "added": 1, "removed": 0, "modified": 2, "unchanged": 3 },
  "previewLimit": 400,
  "entries": [
    {
      "path": "root",
      "kind": "multipart",
      "change": "modified",
      "fields": [
        { "field": "boundary", "label": "boundary",
          "a": { "text": "AA", "truncated": false, "length": 2 },
          "b": { "text": "BB", "truncated": false, "length": 2 } },
        { "field": "headers", "label": "头部",
          "added": ["X-New: 1"], "removed": [], "reordered": false,
          "aCount": 1, "bCount": 2 },
        { "field": "issues", "label": "诊断代码",
          "added": ["PREAMBLE"], "removed": [] }
      ]
    },
    {
      "path": "parts[2]",
      "kind": "part",
      "change": "added",
      "summary": { "mediaType": "application/json", "rawSize": 42,
                   "descendantParts": 0, "headers": 1 },
      "fields": []
    }
  ]
}
```

说明：

- `change` 取值：`added`（仅 B 有）/ `removed`（仅 A 有）/ `modified` / `unchanged`；
  增删的子树整体折叠为一条，`summary.descendantParts` 给出后代部分数量；
- `path` 为 `""` 的条目表示**全局诊断**变化（如 `LF_NORMALIZED` 这类不归属
  任何节点的 issue）；
- 长文本字段（正文、序言/尾注）以预览形式返回，`truncated: true` 时
  `length` 为完整长度（上限见 `previewLimit`）。

### `POST /api/attachments`

附件提取：从当前解析或编辑后的树递归列出附件叶子。请求体只有
`/api/parse` 返回（可能已被前端修改）的 `tree`：

```json
{ "tree": { /* /api/parse 返回的 tree，可修改 */ } }
```

响应（节选）：

```json
{
  "ok": true,
  "count": 3,
  "exportableCount": 2,
  "attachments": [
    {
      "path": "parts[1].parts[0]",
      "mediaType": "text/csv",
      "disposition": "attachment",
      "filename": "café.csv",
      "filenameSource": "extended",
      "filenameNote": null,
      "zipName": "café.csv",
      "transferEncoding": "quoted-printable",
      "decodeState": "decoded",
      "edited": false,
      "exportable": true,
      "reason": null,
      "size": 42
    },
    {
      "path": "parts[2]",
      "filename": "bad.b64",
      "filenameSource": "plain",
      "zipName": "bad.b64",
      "transferEncoding": "base64",
      "decodeState": "malformed",
      "edited": false,
      "exportable": false,
      "reason": "Content-Transfer-Encoding（base64）的传输正文畸形，未编辑时无法保证 CTE 解码字节正确；请在结构树中编辑该节点后再导出",
      "size": 0
    }
  ]
}
```

字段说明：

| 字段 | 含义 |
| --- | --- |
| `path` | 叶子在树中的节点路径（同时是下载请求的选择键） |
| `disposition` | Content-Disposition 类型（`attachment`/`form-data`/…），可能为 `null` |
| `filename` | 最终选用的文件名原始值；无文件名时为 `null`（此时按路径命名） |
| `filenameSource` | `extended`（RFC 2231 扩展值）/ `plain`（普通 filename）/ `path`（按路径命名） |
| `filenameNote` | 扩展值存在但非法、已回退普通值时的原因说明 |
| `zipName` | 净化路径分隔符/控制字符、重名加序号之后的 ZIP 内文件名 |
| `decodeState` | 与叶子节点一致：`raw`/`decoded` 可直接导出；`malformed`/`passthrough` 仅在编辑后可导出 |
| `edited` | 叶子是否已被编辑（`bodyEdited`） |
| `exportable` / `reason` | 当前是否可导出，以及不可导出的原因 |
| `size` | CTE 解码后（编辑后）的字节数；不可导出时为 `0` |

### `POST /api/attachments/zip`

把勾选的附件以 **CTE 解码后字节** 打包为 ZIP（`application/zip`，
DEFLATE 压缩；非 ASCII 文件名置 UTF-8 语言编码标志位）：

```json
{
  "tree": { /* 当前树，可修改 */ },
  "paths": ["parts[0]", "parts[1].parts[0]"]
}
```

服务端会按树的先序遍历顺序重新校验每个路径：未勾选任何附件
（`EMPTY_SELECTION`）、路径在当前树中不存在（`STALE_PATHS`）或所选节点
当前不可导出（`NOT_EXPORTABLE`）时返回 `400` 与 JSON 错误体。

### `POST /api/cid-check`

CID 引用静态检查：请求体只有 `/api/parse` 返回（可能已被前端修改）的
`tree`。按节点**当前头部**判断媒体类型与 `Content-ID`，正文从叶子的
**正文编辑通道**（`body` + `bodyEncoding`，即 CTE 解码后的可编辑内容）
还原字节；只检查**合法 UTF-8 的 `text/html`**，其他含 "html" 的类型
（如 `application/xhtml+xml`）与非合法 UTF-8 的 text/html 报“无法检查”。
使用标准库 `html.parser` 仅做分词，提取各标签 `src` / `href` 属性中
`cid:`（大小写不敏感）开头的引用，**不执行脚本、不加载任何资源**。

```json
{ "tree": { /* 当前树，可修改 */ } }
```

响应（节选）：

```json
{
  "ok": true,
  "summary": {
    "htmlDocuments": 3, "checked": 2, "uncheckable": 1,
    "references": 4, "unique": 1, "missing": 1,
    "duplicate": 1, "noScope": 1
  },
  "scopes": [
    { "path": "parts[1]", "mediaType": "multipart/related", "leafCount": 4 }
  ],
  "documents": [
    {
      "path": "parts[1].parts[0]",
      "mediaType": "text/html",
      "contentId": "html-root@x",
      "scope": { "path": "parts[1]", "mediaType": "multipart/related" },
      "status": "checked",
      "reason": null,
      "references": [
        {
          "tag": "img", "attr": "src", "line": 1,
          "raw": "CID:Logo%20PNG", "cid": "Logo PNG",
          "status": "unique",
          "candidates": ["parts[1].parts[1]"],
          "match": "parts[1].parts[1]"
        },
        { "tag": "img", "attr": "src", "line": 1,
          "raw": "cid:dup", "cid": "dup",
          "status": "duplicate", "match": null,
          "candidates": ["parts[1].parts[2]", "parts[1].parts[3]"] },
        { "tag": "a", "attr": "href", "line": 1,
          "raw": "cid:missing-one", "cid": "missing-one",
          "status": "missing", "candidates": [], "match": null }
      ]
    },
    { "path": "parts[2]", "mediaType": "text/html", "contentId": null,
      "scope": null, "status": "uncheckable",
      "reason": "正文不是合法 UTF-8（以base64 编辑通道承载…），无法按 text/html 检查 cid: 引用",
      "references": [] }
  ]
}
```

规则与字段：

| 项 | 说明 |
| --- | --- |
| 作用域 | 每个 HTML 只在**最近的 `multipart/related` 祖先**内匹配；嵌套 related 子树中的叶子归内层作用域，不作为外层候选；无祖先时引用 `status` 为 `no-scope`，文档 `scope` 为 `null` |
| 候选 | 作用域内 `Content-ID` 与引用相等（**区分大小写**）的叶子路径，按树先序排列；`status` 为 `unique`（恰好 1 个，`match` 给出路径）/ `missing`（0 个）/ `duplicate`（多于 1 个，`match` 为 `null`，不默认选取） |
| Content-ID | 去首尾空白并剥去**一对外层**尖括号（`<id>` → `id`，`<<a>>` → `<a>`）；取第一个 `Content-ID` 头 |
| 引用解码 | 属性值以 `cid:`（大小写不敏感）开头才提取；去前缀后用百分号解码**一次**（`%25` → `%`），不再按空白裁剪；`tag`/`attr` 已小写，`line` 为 HTML 内行号，`raw` 为原始属性值 |
| 非引用 | `http(s):` URL、CSS 中的 `url(cid:…)`、注释文本均不扫描（只识别标签属性）；`srcset` 等其他属性不提取 |
| 叶子 | 仅叶子（`kind: "part"`）参与 Content-ID 匹配；multipart 容器无 Content-ID |

### `GET /api/health`

返回 `{"ok": true}`，供容器健康检查使用。

---

## 项目结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI 应用：/api/parse、/api/encode、/api/diff、/api/attachments、/api/cid-check、静态文件
│   ├── mime.py              # 零第三方依赖的 MIME 解析 / 重新编码核心
│   ├── attachments.py       # RFC 2231 文件名解析、附件发现与 ZIP 打包（标准库 zipfile）
│   ├── cidcheck.py          # multipart/related 作用域内的 cid: 引用静态检查（标准库 html.parser）
│   ├── diff.py              # 样本对比：按树路径对齐的字段级差异计算
│   └── static/
│       ├── index.html       # 页面结构
│       ├── styles.css       # 深色主题样式
│       └── app.js           # 原生 JS：示例、树形编辑器、附件提取、CID 引用检查、样本对比、下载
├── tests/
│   └── smoke_test.py        # 解析/编码/对比端到端测试（仅需 Python 标准库）
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
└── README.md
```

## 运行测试

测试不依赖 FastAPI，只用 Python 标准库：

```bash
python3 tests/smoke_test.py
```

覆盖：基本 form-data、CJK、嵌套 multipart、preamble/epilogue、
引号与转义参数、boundary 覆盖、定界符碰撞、缺关闭定界符、LF 规范化、
二进制逐字节往返、编辑后重新编码、样本对比（节点增删折叠、
字段级差异、头部逐行对比、诊断代码归属与全局诊断），以及
Content-Transfer-Encoding（base64 / quoted-printable / 7bit / 8bit / binary
的识别、解码、畸形诊断与载荷保留、切换后按 76 字符折行重新编码、
未编辑节点字节稳定、超长行/裸换行/7bit 约束诊断、diff 检出 CTE 变化），以及
附件提取（递归发现、form-data 带 filename、普通 form-data 字段排除、
RFC 2231 `filename*` 单值与 `filename*0*` 分段拼接/非 UTF-8 字符集/
非法扩展值回退、路径分隔符与控制字符净化、重名序号、无文件名按路径命名、
CTE 解码字节计数、畸形/未知 CTE 未编辑禁导、编辑后可导且取编辑后字节、
ZIP 条目内容与错误码、嵌套 multipart 附件），以及
CID 引用检查（最近 related 祖先作用域、嵌套 related 子树排除、
无祖先无作用域、Content-ID 去尖括号与空白、cid 前缀大小写不敏感、
百分号解码一次、标识符区分大小写、唯一/缺失/重复歧义、非 UTF-8 与
其他 html 类型无法检查、标签/属性/行号记录）。

---

## 安全说明

- 该工具用于**本地检查报文**，默认只监听容器的 `8000` 端口；请勿直接暴露到公网。
- 所有解析都在内存中完成，请求不会转发到任何第三方。
- 二进制正文以 base64 在前后端之间传输，避免编码损坏。
