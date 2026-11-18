# LaminaDB 查询语义教学对照（扩展篇）

扩展篇在核心篇（`docs/query-semantics-guide.md`）之外补充较完整的交错与边界：访问路径事实、提交边界、唯一约束时机、SQL 词法层差异、可算范围随图像与重启的行为。四要素（参与者请求 / 独立推算 / 真实运行事实 / 适用界限）与核心篇相同，推算规则也相同——扩展篇不引入新规则，只把规则放到更密集的历史里。

**为什么需要它**：核心篇的每个错误假设都有至少一个案例能排除，但仍有一批假设在核心篇里"碰巧正确"（例如"有索引必走索引"——核心篇没有索引）。扩展篇用更多交错补上这些判别。两档材料的排除清单与代价对照见 `docs/coverage-comparison.md`。

## 扩展输入历史（独立实例）

| 序号 | 记录 | 内容 |
| --- | --- | --- |
| 1 | relation | `spectra`（模式同核心篇） |
| 2 | commit S | 5 行：`(n,bn,n*10,t)`，n=1..5 |
| 3 | index | `by_amplitude`（非唯一，列 amplitude） |
| 4 | commit U | 整行替换 2→`(2,b2,25,t)` |
| 5 | commit A | 整行替换 1→`(1,b1,11,t)`（同快照的 B 提交失败，见 X3） |
| 6 | commit D | 删除不存在的键 99（墓碑链） |
| 7 | index | `uq_band`（唯一，列 band） |
| 8 | commit U1 | 新增 6→`(6,shared,60,t)`（U2 唯一冲突失败，见 X6） |
| 9 | index | `uq_amp`（唯一，列 amplitude） |
| 10 | commit N | 新增 8→`(8,b8,null,t)`、9→`(9,b9,null,t)` |
| 11 | commit W2 | 新增 7→`(7,b7,70,t)`（故意乱序插入，见 X7b） |
| 12 | vacuum | 请求水位 8 → 实际水位 8 |

之后执行检查点与重启（不改变序号与水位，见 X17）。

## 主题一：访问路径与计划事实（观察序号 4）

### 参与者请求与独立推算

- **X1 历史候选**：`by_amplitude` 在序号 3 建成，序号 4 把行 2 从 20 改为 25。索引保留**历史非空值到主键的关联**（`internal/index/registry.go:25-35`），所以树里同时有 `20→k2`（历史）和 `25→k2`。查询 `amplitude = 20`：规划器选索引，候选键 `{k2}`（candidates=1）；但行永远取自指定快照的真实版本——k2 在序号 4 的值是 25，残余判断拒绝 → scanned=1、matched=0、rows=[]。`force_scan:true` 对照：candidates=5（全部链）、scanned=5、matched=0。
- **X2 私有写强制扫描**：事务 T 暂存 `3→35` 后带 `transaction` 查询 `amplitude >= 30`：即使索引可用也强制 scan（`internal/engine/read.go:41-43`），看到自己的 35；同一谓词不带事务：走索引（candidates=3），看到已提交的 30。**注意两者 matched 都是 3**——只看计数会误以为两条路径等价，行内容与 `plan.access` 才把它们分开。

### 真实运行事实

```json
X1       {"rows":[],"matched":0,"scanned":1,"plan":{"access":"index","candidates":1}}
X1-scan  {"rows":[],"matched":0,"scanned":5,"plan":{"access":"scan","candidates":5}}
X2-tx    {"rows":[{"sample":3,"amplitude":35},{"sample":4,"amplitude":40},{"sample":5,"amplitude":50}],"matched":3,"plan":{"access":"scan"}}
X2-no-tx {"rows":[{"sample":3,"amplitude":30},{"sample":4,"amplitude":40},{"sample":5,"amplitude":50}],"matched":3,"plan":{"access":"index","candidates":3}}
```

### 适用界限

- `plan.candidates/scanned/matched` 是**执行事实**，不是结果语义；用它们回答"查询返回什么"是范畴错误。它们回答的是"引擎怎么到达结果"。
- 索引只含非空值关联；null 行从不出现在候选里，但 `is_null` 查询照样能命中它们（走扫描）。

## 主题二：提交边界（序号 4→6）

### 参与者请求与独立推算

- **X3 first-committer-wins**：A、B 都在快照 4 开始。A 暂存 `1→11`，B 暂存 `1→12` 和 `6→60`。A 先提交（序号 5）。B 提交时 k1 的最新版本序号 5 > B 的快照 4 → 409 `conflict`，B 关闭，**B 的全部写入（包括不冲突的 6）都不发布**；对已关闭事务再提交 → 404 `not_found`。当前行集仍是 5 行，1 号为 11。
- **X4 空提交**：事务 E 没有任何暂存写就提交 → 返回自己的快照序号（5）、`writes:0`；`stats` 的 sequence 与 journal_records 都停在 5。**提交不必然推进序号**（`internal/engine/transaction.go:180-182`）。
- **X5 墓碑链**：删除不存在的键 99 照样暂存墓碑并提交成功（序号 6）。墓碑也是版本链：`stats` 得 keys=6、versions=8；`get` 99 → 404；`count(*)` 仍是 5。这条链要等水位越过才整链移除（X15 的 removed 包含它）。

### 真实运行事实

```json
X3-conflict {"status":409,"code":"conflict","message":"write conflict on spectra"}
X3-closed   {"status":404,"code":"not_found","message":"transaction not active"}
X3          {"rows":[{"sample":1,"amplitude":11},{"sample":2,"amplitude":25},{"sample":3,"amplitude":30},{"sample":4,"amplitude":40},{"sample":5,"amplitude":50}],"matched":5}
X4   {"commit":{"sequence":5,"writes":0},"sequence_before":5,"sequence_after":5,"journal_records_after":5}
X5-stats {"keys":6,"versions":8}
X5-get   {"status":404,"code":"not_found","message":"tuple not found"}
X5       {"rows":[{"count":5}],"matched":5}   （plan.candidates=6：墓碑链也计入扫描候选）
```

### 适用界限

- X3 的"只有一个成功"不承诺**哪一个**成功——本资料按请求时序推算，并发到达顺序由调度决定；可复现的是"恰一个 409、输家零发布"。
- 空提交不推进序号，但**非空提交、DDL、vacuum 都各消耗一个序号**；用 `stats.sequence` 推算历史时要计入所有记录类型。

## 主题三：唯一约束与 null（序号 7→10）

### 参与者请求与独立推算

- **X6 检查时机**：U1、U2 同快照，分别暂存 `6→band "shared"`、`7→band "shared"`。两个 `put` 都成功——唯一约束不在暂存时检查。U1 先提交（序号 8）；U2 提交时按**提交后的最终可见状态**检查 → 409 "unique constraint uq_band violated"。
- **X7 唯一索引与 null**：`uq_amp`（唯一，amplitude）建成后，事务 N 在同一事务里写入 8、9 两行 amplitude 均为 null 的行 → 提交成功。唯一检查跳过 null（`internal/engine/constraints.go:26-27`），任意多个 null 共存。`is_null` 计数 → 2。

### 真实运行事实

```json
X6  {"status":409,"code":"conflict","message":"unique constraint uq_band violated"}
X7  {"rows":[{"count":2}],"matched":2}
```

### 适用界限

- "唯一约束在提交时检查"意味着同一事务内可以**置换**唯一值（先删后建），但不能推出"put 时就会报错"。
- 多 null 共存只对**可空列**成立；主键列不可空，不受此规则保护。

## 主题四：默认顺序的判别补充（序号 11）

核心篇 B3 的种子按主键序插入，无法区分"按类型化主键排序"与"按插入顺序输出"。

- **X7b**：序号 10 时行集主键为 `{1,2,3,4,5,6,8,9}`；commit W2 在序号 11 **最后**插入主键 7。无 `order` 查询若按插入/提交顺序，7 应在末尾；按类型化主键，7 在 6 与 8 之间。

### 真实运行事实

```json
X7b  {"rows":[{"sample":1},{"sample":2},{"sample":3},{"sample":4},{"sample":5},{"sample":6},{"sample":7},{"sample":8},{"sample":9}],"matched":9}
```

7 落在主键序位置 → 排除"按插入顺序输出"。

## 主题五：SQL 入口的词法与校验层（观察序号 10）

SQL 入口把文本解析成结构化请求（`internal/syntax/parser.go`），之后共用绑定与执行。两入口的差异全部发生在**解析与绑定**阶段：

- **X8 大小写**：关键字与未引号标识符统一转小写（`internal/syntax/lexer.go:84`）→ `SELECT SAMPLE, AMPLITUDE ... WHERE VALID = true ORDER BY AMPLITUDE DESC LIMIT 2` 正常执行，得 `[(6,60),(5,50)]`，matched=8、truncated=true。
- **X9 多语句**：`SELECT * FROM spectra; SELECT * FROM spectra;` → 400 "SQL: expected end of statement"。
- **X10 类型**：`WHERE amplitude = '20'`（文本字面量对 int 列）→ 400 "predicate type mismatch for amplitude"；结构化入口传 `"value":"20"` 得到同一错误——类型检查在共用的绑定层（`internal/query/bind.go:39-41`）。
- **X11 聚合类型**：`sum(band)` → 400 "sum requires integer column"。
- **X12 LIMIT 0**：SQL 要求正整数 → 400；而结构化请求的 `limit:0` 表示"未设置"（默认 1000）。**这是两个入口一处真实的输入层差异**：SQL 无法表达"未设置"，结构化 JSON 可以。绑定之后两者语义相同。

### 真实运行事实

```json
X8   {"rows":[{"sample":6,"amplitude":60},{"sample":5,"amplitude":50}],"matched":8,"truncated":true}
X9   {"status":400,"code":"invalid","message":"SQL: expected end of statement at byte 23"}
X10-sql        {"status":400,"code":"invalid","message":"predicate type mismatch for amplitude"}
X10-structured {"status":400,"code":"invalid","message":"predicate type mismatch for amplitude"}
X11  {"status":400,"code":"invalid","message":"sum requires integer column"}
X12  {"status":400,"code":"invalid","message":"SQL: expected positive integer limit at byte 28"}
```

### 适用界限

- "两入口一致"的合法表述是：**解析成功后归约到同一 `query.Request`，经同一绑定与执行**。凡是解析/绑定阶段的差异（X12），或快照前提的失效（核心篇 E2），都不能用"两入口应一致"来外推。

## 主题六：快照选择边界（观察序号 10）

- **X13 互斥**：同一请求同时给 `at` 与 `transaction` → 400 "at and transaction are mutually exclusive"。
- **X14 关系尚未存在**：`at:0` 在保留区间 `[0,11]` 内，但关系序号 1 才创建 → 410 "relation did not exist at snapshot"。与核心篇 E2 的 410 "snapshot unavailable" 同为 410、原因不同：一个超出保留区间，一个早于目录。

```json
X13  {"status":400,"code":"invalid","message":"at and transaction are mutually exclusive"}
X14  {"status":410,"code":"snapshot","message":"relation did not exist at snapshot"}
```

## 主题七：可算范围、备份图像与重启（序号 11→12）

### 参与者请求与独立推算

- **X15 可算范围随保留历史走**：先在序号 11 取 `GET /backup`（图像含 horizon=0 与全部版本链）。原实例 `vacuum {"horizon":8}` → 实际水位 8（无活动事务），removed=3（k1 的 v2、k2 的 v2、k99 墓碑链）、retained=9，序号 12。原实例 `at:2` → 410 "retained interval 8..12"。用备份**还原**出独立实例：图像里的水位是 0、版本完整 → 同一 `at:2` 可算，得种子状态 sum=150（含行 2 的旧值 20）。同一个"序号 2"在两个实例的可算性不同——可算范围由**各自保留的历史与水位**决定，不由序号本身决定。
- **X16 水位在还原实例独立前进**：还原实例自己 `vacuum {"horizon":8}`（其序号为 11 → 12）→ 同样的 removed=3、retained=9；此后它的 `at:2` 也 → 410。
- **X17 水位重启不回退**：原实例 `POST /checkpoint` 后停止并重启 → `stats` 仍 `{"sequence":12,"horizon":8}`；`at:2` 仍 410；`at:8`（恰为水位）可算 → sum=216（行 1..6 的 11+25+30+40+50+60；行 7、8、9 的提交序号 11、10、10 都晚于 8，不可见）。

### 真实运行事实

```json
X15-vacuum    {"horizon":8,"removed":3,"retained":9,"pinned":false}
X15-original  {"status":410,"code":"snapshot","message":"snapshot unavailable; retained interval 8..12"}
X15-restored  {"rows":[{"sum":150}],"matched":5}
X16-vacuum    {"horizon":8,"removed":3,"retained":9,"pinned":false}
X16-restored  {"status":410,"code":"snapshot","message":"snapshot unavailable; retained interval 8..12"}
X17-stats     {"sequence":12,"horizon":8}
X17-old       {"status":410,"code":"snapshot","message":"snapshot unavailable; retained interval 8..12"}
X17-boundary  at=8 → {"rows":[{"sum":216}],"matched":6}
```

### 适用界限

- 备份/还原**不扩大**可算范围：图像只含当时保留的版本与水位；若源实例已回收到水位 8，还原实例同样从水位 8 开始。
- 还原实例的变更历史从恢复基点重新对齐（`history_base`），旧游标 410——这与"快照可算性"是两条独立的保留边界，本资料不展开（见 README 变更与回收一节）。

## 复现方式

```sh
python3 examples/semantics_probe.py --group extended
```

扩展组 30 条检查全部 `ok=true`（本次运行约 0.4 秒，含备份、还原与重启）。
