# 多格式往返回归防线：判定口径（Rubric）

本文件是 `tests/test_roundtrip.py` 的配套口径文档，说明防线形态、覆盖范围、
比对轴、场景集、判定规则与白名单依据。测试模块中的常量名（R1–R6、W1）与本文
条目一一对应。

## 防线形态

- **自往返（A → A）**：每种可写格式写出再读回，验证单一格式读写器互逆。
  不对称的读写器（能读不能写、或写出后读不回）在此暴露。
- **成对往返（A → B → A）**：先经 A 自往返得到 stage1，再把 stage1 经 B
  转出转回得到 stage2，比较 stage1 与 stage2。这样 A 自身的不对称（已由自
  往返覆盖）不会污染对 B 的判定，每个失败都能归因于中转格式 B。
- 全部用例在内存中经 `SSAFile.to_string()` / `SSAFile.from_string()` 完成，
  不触碰磁盘。

## 覆盖的格式对

矩阵 = 8 种可写格式 {srt, ass, ssa, vtt, ttml, mpl2, tmp, json} 的
8 个自往返 + 8×7=56 个有序对，每个（格式对 × 场景）一个用例。

- **MicroDVD**：按任务要求排除。
- **sami、whisper_jax**：只有读器没有写器（`SAMIFormat`/`WhisperJAXFormat`
  未实现 `to_file`），无法构成"转出再转回"，按构造排除。

## 比对轴

1. **可见事件数**（`SSAFile.get_text_events()`，即非注释、非绘图的 Dialogue）。
2. **start**：绝对误差 ≤ 中转格式的时间量子（R1）。
3. **end**：同上；但 TMP 文件不携带结束时间（R2），经 TMP 中转时跳过该轴。
4. **文本**：`SSAEvent.plaintext` 严格相等（库自身的语义文本模型，已把
   `\N`/`\n`/原始换行/`\h` 归一到换行与空格）。
5. **行内样式**：以 `parse_tags()` 计算的有效样式把事件切成
   (文本, bold, italic, underline, strikeout) 游程序列，相邻同样式游程合并
   后比较；仅当中转格式属于样式保持集合（R4）时启用。

 cue 顺序不是比对轴（R5）。样式表中字体、颜色、边距等其余属性、以及
 `[Script Info]` 元数据不在矩阵比对范围内（ass/ssa/json 的保真由既有测试
 与 `test_ssa_family_preserves_non_dialogue_events` 覆盖）。

## 场景集

| 场景 | 覆盖点 |
|---|---|
| `basic` | 普通文本、毫秒级时间、跨小时时刻 |
| `inline_styles` | i/b/u/s 单标签、嵌套与交叠标签 |
| `multiline` | `\N` 换行、`\h` 硬空格 |
| `special_chars` | Unicode（CJK/重音/emoji）、`<` `>` `&`、逗号 |
| `raw_newline` | 事件文本含原始 `\n`（非 `\N` 标签） |
| `timing_edge` | 非整十/整百/整千毫秒时刻，触发各格式取整路径 |
| `styled_lines` | 非 Default 行样式（bold+italic）作用于整行 |

## 判定规则

- **R1（时间取整既有约定不动）**：ass/ssa 厘秒取整（Aegisub 约定，既有测试
  `tests/formats/test_substation.py::test_centisecond_rounding` 与
  `test_ass_ms_to_timestamp` 钉死）、mpl2 十分之秒、tmp 整秒，均为格式既有
  约定，以时间量子容差吸收，不视为缺陷，也不改动相关代码。
- **R2（无结束时间的格式）**：TMP 写出端不含结束时间，读入端按
  500ms + 67ms/字符 估算（`tests/formats/test_tmp.py::test_simple_read`、
  `test_overlapping_read` 钉死）。经 TMP 中转跳过 end 轴。
- **R3（文本规范化）**：比较前把 `\h`→空格、`\N`/`\n`/原始换行→`\n`，与
  `SSAEvent.plaintext` 的语义一致；标签写法差异（如未闭合的 `{\u1}`）在
  游程合并后不视为差异。
- **R4（样式能力差）**：ttml 读器文档声明不支持样式
  （`ttml.py::from_file` docstring "No formatting/styling apart from
  newlines is supported"）；mpl2 写器文档声明不支持样式
  （`mpl2.py::to_file` docstring "No styling is supported at the moment"，
  代码内 `# TODO handle italics`）；tmp 读器只保留 `<u>` 且从不生成闭合并
  会剥离 `<b>`（`tests/formats/test_tmp.py::test_styled_read` 钉死）。
  经这三者中转时样式轴不启用，但文本轴仍然生效——样式可丢，文字不可丢。
- **R5（cue 顺序）**：WebVTT 写器按开始时间排序
  （`tests/formats/test_vtt.py::test_writes_in_time_order` 钉死），SRT 写器
  按给定顺序（`test_subrip.py::test_writes_in_given_order` 钉死），二者均为
  既有约定，故顺序不作为比对轴。
- **R6（与既有测试/提交历史冲突归白名单）**：凡修复会与仓库既有测试钉死的
  行为冲突的差异，不改代码，进白名单并注明出处（见 W1）。

**真缺陷判定**：凡在源格式与中转格式都可表达的信息（文字、换行、双方读写器
都支持的样式）在往返中丢失或损坏，且修复不与 R1–R6 冲突者，判为真缺陷并修复。
本次甄别出并已修复 3 项：

1. **SRT/VTT 写器丢失粗体**：读器支持 `<b>`（`test_subrip.py::test_read_tags`
   钉死），写器却只输出 `<i>/<u>/<s>`。修复：`subrip.py` 写器补 `<b>` 输出。
2. **ASS/SSA 写器原样输出文本中的原始换行**：行式格式被拆行，读回时该条
   字幕被截断。修复：`substation.py` 写器把原始换行规范化为 `\N`。
3. **TTML 写器丢失原始换行**：原始 `\n` 被字面写入 XML，读回时坍缩成空格。
   修复：`ttml.py::_append_text` 把原始换行同样切分为 `<br/>`。

## 白名单（逐项依据）

- **W1 — (inline_styles, ttml, text, ev2)**：相邻两个带样式的 `<span>` 之间，
  `ET.indent()` 美化输出会插入仅含空白符的尾文本，读回时多出一个空格
  （`'both  only italic'`）。该缩进输出被既有测试
  `tests/formats/test_ttml.py::test_serialize` 逐字钉死（其中 `<p>` 内就包含
  缩进产生的换行空白），按 R6 归白名单，不改动。

白名单实行**双向校验**：出现未列入白名单的差异判失败；白名单条目在矩阵中
一次都未实际出现（即底层行为已改变、条目过期）同样判失败
（`test_whitelist_entries_are_observed`），防止白名单 silently 失效。
