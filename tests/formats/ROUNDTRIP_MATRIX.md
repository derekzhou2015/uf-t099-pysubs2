# 往返语义矩阵：口径与白名单依据

本文档随 `tests/formats/test_roundtrip_matrix.py` 交付，说明矩阵的判定口径、白名单条目的依据，
以及本次由矩阵甄别并修复的读写不对称缺陷。矩阵覆盖 **ASS / SRT / WebVTT** 三种格式的读写往返；
MicroDVD 不在本次验收范围内。

---

## 1. 矩阵结构

一次"往返"定义为 `源格式 -> 中间格式 -> 源格式`。矩阵对以下组合全量展开：

- 源格式 `src` ∈ {ass, srt, vtt}
- 中间格式 `mid` ∈ {ass, srt, vtt}
- 用例 `case` ∈ `CASES`（当前 18 个语义场景）

即每个用例产生 9 个往返组合，当前共 162 个语义比对用例，外加同格式稳定性与元用例。

### 五个语义维度

| 维度 | 比对内容 |
| --- | --- |
| `count` | 事件数量 |
| `time` | 每条事件的 `start` / `end`（毫秒） |
| `style` | 事件样式名、事件元数据（`type`、`layer`、`name`、`marginl/r/v`、`effect`）；中间格式为 ASS 时还包括整张样式表 |
| `text` | 文本内容。文本格式下按"逐字符 + 该字符的粗体/斜体/下划线/删除线"比对；ASS→ASS 时比对含覆盖标签的原始文本 |
| `order` | 事件顺序 |

### 扩展方式

- 新增场景：在 `CASES` 中追加一个 `Case(name, build, allowed_losses, rationale)`。
  `build()` 返回全新的 `SSAFile`，参数化自动展开到全部 `src × mid` 组合，无需改动测试函数。
- 新增能力丢失：在 `CAPABILITIES` 中登记 `(id, formats, summary, basis)`。**未登记的 id 无法被记录**
  （`LossLog.add()` 会直接报错），从而保证每条白名单都有书面依据。
- 元测试 `test_capability_ids_are_declared_and_used` 同时校验两个方向：用例引用的 id 必须已登记；
  已登记的 id 必须真的能被矩阵触发（否则视为死条目）。因此白名单不会悄悄膨胀，也不会悄悄失效。

---

## 2. 判定口径

判定不是"两个文件必须逐字节相同"，而是：

> **在扣除中间格式确无表达能力的内容之后，两端语义必须一致。**

- **能力丢失（loss）**：中间格式在语法上不存在对应表达的内容。记入白名单，逐项带依据（见第 3 节）。
  每个用例显式声明它*预期*会触发哪些能力丢失（`allowed_losses`）。
  若某次往返触发了用例未声明的能力丢失，测试失败——白名单是按用例授权的，不是全局豁免。
- **缺陷（mismatch）**：格式本可承载、却在往返中**变形、丢失或错位**的语义。测试失败。

### 不属于缺陷的既有约定（明令不动）

这些行为已被仓库既有测试或提交锁定，矩阵将其作为投影的一部分，而非缺陷：

| 约定 | 出处 |
| --- | --- |
| SubStation 厘秒四舍五入（半入） | `tests/formats/test_substation.py::test_centisecond_rounding`、`::test_ass_ms_to_timestamp`；release notes 1.6.0 |
| 时间戳越界钳制（负数→0，超上限→格式最大值） | `test_substation.py::test_overflow_timestamp_write` / `::test_underflow_timestamp_write`；`test_subrip.py` 同名用例；release notes 1.6.0 |
| WebVTT 写入按开始时间排序 | `tests/formats/test_vtt.py::test_writes_in_time_order`；release notes 1.6.1 |
| SRT/WebVTT 丢弃注释与绘图事件 | `SSAFile.get_text_events()`；`test_subrip.py::test_write_drawing` |

---

## 3. 白名单（能力丢失）与依据

| id | 触发格式 | 内容 | 依据 |
| --- | --- | --- | --- |
| `non-text-events` | SRT, VTT | Comment 事件与 SSA 绘图无对应表达，写入时被省略，事件数减少 | `SSAFile.get_text_events()`；`test_subrip.py::test_write_drawing`（绘图被丢弃）；注释走同一条路径 |
| `style-library` | SRT, VTT | 文本格式无样式表。样式名回退到 `Default`，样式表整体不保留。样式携带的粗/斜/下划线/删除线由写入器内联为标记，仍可存活 | `docs/supported-formats.rst`："SRT 使用 HTML 标签做格式"；VTT "no extra support for ... styles" |
| `event-metadata` | SRT, VTT | `layer` / `name` / 边距 / `effect` 是 SubStation 专有事件字段 | SRT/WebVTT cue 语法本身（仅时间行 + 载荷）；写入器按构造不输出这些字段 |
| `hard-space` | SRT, VTT | 不换行空格 `\h` 写成普通空格 | `SubripFormat.to_file()` 将 `\h` 替换为空格；共享写入器未实现 WebVTT 实体转义 |
| `blank-lines` | SRT, VTT | 载荷内的空行（`\N\N`）被折叠，首尾换行被裁剪 | 两种格式均以空行结束 cue；写入器 `re.sub('\n+', '\n', ...)` + `strip()`；读取器再次 `strip()` |
| `edge-whitespace` | SRT, VTT | 载荷首尾空白在格式中不具语义，写入与读取都会裁剪（被强调标记包住的空白不受影响） | `SubripFormat.to_file()` 与 `from_file()` 各裁剪一次 |
| `payload-markup` | SRT, VTT | 文本中的字面尖括号（如 `<i>...`）无法与内联标记区分，回读时被当作样式或被剥离 | `SubripFormat.from_file()` 改写 `<i>/<b>/<u>/<s>` 并剥离其余标签；未实现 WebVTT 实体转义 |
| `payload-timing-line` | SRT, VTT | 载荷中自带一对时间戳时会被读成 cue 分隔行，把一条字幕拆成两条 | WebVTT 规范禁止 cue 载荷含 `-->`；SRT 亦无转义；读取器将"含两个时间戳的行"一律视为时间行，`test_warning_about_extra_timestamp` 锁定该行为 |
| `time-quantization` | ASS | 仅厘秒精度，毫秒按半入取整到 10ms | `test_centisecond_rounding`、`test_ass_ms_to_timestamp`；release notes 1.6.0 |
| `time-range` | ASS, SRT, VTT | 越界时间戳钳制 | 见第 2 节"既有约定" |
| `event-order` | VTT | WebVTT 写入按开始时间排序，输入顺序不保留 | `test_vtt.py::test_writes_in_time_order`；release notes 1.6.1 |
| `field-commas` | ASS | Style/Name/Effect 以逗号分隔字段，值内含逗号会让该行后续字段整体错位 | `is_valid_field_content()` 明确"逗号不可用于 SubStation 字段"；读取器按逗号切分字段 |

---

## 4. 矩阵甄别并修复的缺陷

在建立矩阵的过程中，矩阵暴露了三处真实的读写不对称，均已修复：

1. **SRT/WebVTT 写入器从不写 `<b>`**
   `SubripFormat.from_file()` 早已解析 `<b>`，但 `to_file()` 只输出 `<i>/<u>/<s>`，
   导致粗体经 SRT/WebVTT 往返后静默丢失。
   *修复：* `pysubs2/formats/subrip.py` 输出 `<b>...</b>`。
   *回归测试：* `inline_emphasis`、`style_emphasis`、`style_library` 用例（ASS→SRT→ASS 等）。

2. **SRT/WebVTT 写入器为每个空片段输出 `<i></i>`**
   覆盖序列（如 `{\i1}{\b1}`）会切出空片段，写入器逐片段包一层空标签，
   于是每次保存文件都会变长，回读还会凭空生成样式标签。
   *修复：* `parse_tags(..., skip_empty_fragments=True)`，空片段不再输出。
   *回归测试：* `test_single_format_roundtrip_is_stable`（读→写→读→写 必须收敛）。
   *缺陷复现（修复前）：* `{\i1}i{\b1}b{\b0}{\i0}` 第二次保存变为
   `{\i1}i{\i0}{\i1}{\b1}b{\b0}{\i0}{\i1}{\i0}`，并继续膨胀。

3. **ASS 写入器把 `text` 中的裸换行原样写出，导致字幕被截断**
   SubStation 每条事件占一行，裸换行会让读取器丢弃其后全部内容，字幕静默丢失半截文本。
   *修复：* `SubstationFormat._field_to_string()` 将 `\r\n`/`\r`/`\n` 统一转义为 `\N`。
   *回归测试：* `newlines_in_text` 用例 + `test_ass_writer_does_not_truncate_raw_newlines`。

---

## 5. 矩阵未能建模、以专门用例固定的限制

以下限制不值得在五维投影里建模（语义映射本身不成立），但行为已被专门用例锁定，
并计入"能力必须被触发"的元测试：

- `test_ass_field_commas_are_not_representable`：字段内逗号会让该行后续字段整体错位。
- `test_timing_line_in_payload_is_not_roundtrip_safe`：载荷冒充时间行会拆 cue
  （SRT 用逗号小数、WebVTT 用点小数，两者分别验证）。
- `test_markup_in_payload_is_reinterpreted_as_styling`：字面 `<i>x</i>` 回读为斜体。
- `test_positional_and_karaoke_tags_are_dropped_by_text_formats`：`\an`/`\k` 在文本格式中丢失，词句保留。

---

## 6. 矩阵自身的可信度

- **正向验证**：矩阵对 162 个语义组合、同格式收敛与元用例全部通过，无告警泄漏。
- **反向验证**：逐一回退上述三处修复，对应用例立即转红
  （回退修复 1 时 10 条失败，回退修复 3 时 `newlines_in_text` 失败）。
- **误报压力测试**：用随机生成的用例（含样式表、注释、绘图、各类覆盖标签、Unicode、越界时间、
  空文本、多行文本、乱序事件）在矩阵比对器上跑了 10800 次 `src→mid→src` 组合，
  在修复口径中的两处比较精度问题（边缘空白的标记包裹、标签顺序规范化）后，**误报为 0**。
  这说明矩阵判定的是语义，而不是"两端字符串相同"。

---

## 7. 运行方式

Windows 原生 Python 3.12，仓库以可编辑方式源码安装，以仓库根为工作目录：

```powershell
python -m pytest
```

矩阵测试集中在 `tests/formats/test_roundtrip_matrix.py`，可与既有测试一并运行：

```powershell
python -m pytest tests/formats/test_roundtrip_matrix.py
```
