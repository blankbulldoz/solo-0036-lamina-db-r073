# LaminaDB 查询语义教学对照（核心篇）

本资料帮助使用者在不查源码、不猜实现的情况下，**根据事务历史自行算出** null 匹配、同值排序和 count/sum 的答案，并用真实运行结果核对。

每个场景给出四样东西：

1. **参与者请求**：按时间顺序排列的 HTTP 请求，写者、读者、维护者分别标记。
2. **独立推算**：只从输入历史（建表、提交、回收记录）和规则速查表推出的答案，不依赖任何一次实际查询结果。
3. **真实运行事实**：同一请求在真实实例上的观测输出（由 `examples/semantics_probe.py` 产生，可复现）。
4. **适用界限**：这条结论在哪些前提之外不再成立。

**独立性原则**：SQL 入口（`POST /sql`）和结构化入口（`POST /query`）在本资料中都是*被观测对象*，互不充当对方的依据。答案先从历史推出，再分别与两个入口的观测对照。两个入口给出相同结果时，那是因为 SQL 被解析成同一个结构化请求、走同一条执行路径（`internal/transport/query.go`、`internal/syntax/parser.go`），而不是"互相验证"。

**可算性前提**：一个快照序号 `at` 只有在保留区间 `[horizon, sequence]` 内、且关系在该序号已存在时，查询才是"可算"的。事务编辑维护的私有状态决定*事务自己*看到什么；版本回收维护的水位决定*哪些历史序号*还可算。两者共同划定本资料所有答案的有效范围。

## 推算规则速查

| 主题 | 规则 | 源码锚点 |
| --- | --- | --- |
| 快照选择 | 普通读用当前序号；`transaction` 固定事务开始时的序号；`at` 指定序号。`at` 与 `transaction` 互斥（400） | `internal/engine/read.go:18-31` |
| 可算区间 | `horizon ≤ at ≤ sequence`，否则 410；`at` 早于关系创建序号也是 410 | `internal/engine/read.go:32-37` |
| 暂存 | `put` 是**整行替换**：缺失可空列补 null，缺失非空列拒绝。`delete` 形成墓碑。事务自己的查询 = 开始快照 + 私有写集覆盖 | `internal/catalog/schema.go:62-86`、`internal/engine/replay.go:69-77` |
| 可见性 | 暂存写只对本事务可见；已开始的读者事务不受后续提交影响 | `internal/engine/transaction.go:42`、`internal/engine/read.go:55-57` |
| null 匹配 | 普通比较（`= != < <= > >=`）遇 null 不匹配——包括 `!=`；谓词值为 null 也不匹配。null 检查只用 `is_null` / `not_null` | `internal/query/bind.go:63-105` |
| 排序 | 无 `order` 时按类型化主键升序；排序键比较时 null 恒为"最小"（升序在前、降序在后）；同值并列时**恒按主键升序**收尾，与排序方向无关 | `internal/datum/row.go:25-37`、`internal/query/execute.go:36-47` |
| 聚合 | `count(*)` 计全部匹配行；`count(列)`、`sum` 跳过 null；`sum` 空输入返回 null，溢出报错（400）；聚合在 LIMIT 之前归约，LIMIT 不影响聚合结果 | `internal/query/execute.go:28-35,69-92` |
| 提交 | 同键 first-committer-wins（409 且事务关闭、不发布任何写入）；唯一约束按提交后的最终状态检查 | `internal/engine/transaction.go:155-194`、`internal/engine/constraints.go` |
| 回收 | 实际水位 = min(请求水位, 所有活动事务快照)；每链保留水位时最后版本及更新版本；水位前最终墓碑整链移除；水位不回退、随日志持久 | `internal/engine/maintenance.go:73-96`、`internal/versions/vacuum.go:10-30` |

## 共享输入历史

所有核心场景共用一条历史（一个临时实例）。`†` 表示墓碑。

| 序号 | 记录 | 内容 |
| --- | --- | --- |
| 1 | relation | `spectra(sample int 主键, band text, amplitude int 可空, valid bool)` |
| 2 | commit S | 7 行：`(1,b1,40,t) (2,b2,20,t) (3,b3,40,t) (4,b4,null,t) (5,b5,60,t) (6,b6,40,t) (7,b7,null,f)` |
| 3 | commit W | 整行替换 1→`(1,b1x,null,t)`；删除 2 `†`；新增 8→`(8,b8,80,t)` |
| 4 | commit X | 新增 9→`(9,b9,9223372036854775807,t)`、10→`(10,b10,1,t)` |
| 5 | commit Y | 删除 9、10 `†` |
| 6 | commit Z | 整行替换 3→`(3,b3,44,t)` |
| 7 | vacuum | 请求水位 6；活动读者 P 快照为 5 → 实际水位 5 |
| 8 | vacuum | 请求水位 6 → 实际水位 6 |

场景 A、B 在序号 2 的当前快照上观察；场景 C 跨越序号 2→3；场景 D 跨越 3→5；场景 E 跨越 5→8。

## 场景 A：null 的匹配（观察序号 2）

### 参与者请求

```
POST /relations                          { spectra 模式 }                → 序号 1
POST /transactions/<S>/put × 7           7 行种子                        → （暂存）
POST /transactions/<S>/commit                                            → 序号 2
POST /query  {"relation":"spectra","where":[{"column":"amplitude","op":"!=","value":20}],"columns":["sample","amplitude"]}
POST /query  {"relation":"spectra","where":[{"column":"amplitude","op":"=","value":null}]}
POST /sql    {"sql":"SELECT * FROM spectra WHERE amplitude = null;"}
POST /query  {"relation":"spectra","where":[{"column":"amplitude","op":"is_null"}],"columns":["sample","amplitude"]}
POST /query  {"relation":"spectra","aggregate":{"function":"count","column":"*"}}
POST /query  {"relation":"spectra","aggregate":{"function":"count","column":"amplitude"}}
POST /query  {"relation":"spectra","aggregate":{"function":"sum","column":"amplitude"}}
POST /sql    {"sql":"SELECT sum(amplitude) FROM spectra;"}
POST /query  {"relation":"spectra","where":[{"column":"amplitude","op":"is_null"}],"aggregate":...}   （三个聚合各一次）
```

### 独立推算

- **A1 `!= 20`**：普通比较遇 null 不匹配——4、7 两行 amplitude 为 null，连 `!=` 也不满足。非 null 行里只有 2 等于 20。→ 命中 `{1,3,5,6}`，matched=4。
- **A2 `= null`**：谓词值为 null 的普通比较永不匹配 → matched=0。SQL 的 `= null` 解析成同一个谓词形式，同样为 0。查 null 行只能用 `is_null`。
- **A3 `is_null`** → `{4,7}`，matched=2。
- **A4**：`count(*)` 计全部 7 行；`count(amplitude)` 跳过 4、7 → 5；`sum` 跳过 null：40+20+40+60+40=200。
- **A5**（`is_null` 上的聚合）：匹配行 `{4,7}`。`count(*)`=2；`count(amplitude)`=0（两行皆 null）；`sum` 的非 null 输入为空 → **null，不是 0**。

### 真实运行事实（节选）

```json
A1   expect {"matched":4}            observed {"rows":[{"sample":1,"amplitude":40},{"sample":3,"amplitude":40},{"sample":5,"amplitude":60},{"sample":6,"amplitude":40}],"matched":4,"scanned":7}
A2   expect {"rows":[],"matched":0}  observed {"rows":[],"matched":0,"scanned":7}
A2-sql 同上                           observed {"rows":[],"matched":0,"scanned":7}
A3   expect {"matched":2}            observed {"rows":[{"sample":4,"amplitude":null},{"sample":7,"amplitude":null}],"matched":2}
A4   count→7  count(amplitude)→5  sum→200   （三个聚合观测与推算一致；SQL 的 sum 同为 200）
A5   count(*)→2  count(amplitude)→0  sum→null
```

### 适用界限

- "null 不满足普通比较"对两个入口、任意快照都成立，但**只对普通比较**成立；`is_null`/`not_null` 是另一组算子。
- A4 的三个数（7/5/200）只在序号 2..2 可算；序号 3 之后行集已变（见场景 C）。
- 若种子没有 null 行，A1/A4/A5 都失去判别力——`count(列)` 与 `count(*)` 会碰巧相等（见 `docs/coverage-comparison.md`）。

## 场景 B：同值与 null 的排序（观察序号 2）

### 参与者请求

```
POST /query {"relation":"spectra","order":[{"column":"amplitude"}],"columns":["sample","amplitude"]}
POST /query {"relation":"spectra","order":[{"column":"amplitude","desc":true}],"columns":["sample","amplitude"]}
POST /query {"relation":"spectra","columns":["sample"]}
POST /query {"relation":"spectra","order":[{"column":"amplitude","desc":true}],"limit":2,"columns":["sample","amplitude"]}
```

### 独立推算

- **B1 升序**：null 恒"最小"→ 4、7 在前（二者同值，主键升序 4 后 7）；然后 20(2)；三个 40 按主键升序 1、3、6；最后 60(5)。
- **B2 降序**：方向只翻转排序键比较：60(5) 在前，三个 40 **仍按主键升序** 1、3、6（并列收尾规则不随方向翻转），然后 20(2)，null 的 4、7 落在最后。
- **B3 无 order**：按类型化主键升序 → 1..7。
- **B4 降序 LIMIT 2**：取 B2 前两行 → (5,60)、(1,40)；matched 仍是 7，`truncated=true`。

### 真实运行事实

```json
B1  {"rows":[{"sample":4,"amplitude":null},{"sample":7,"amplitude":null},{"sample":2,"amplitude":20},{"sample":1,"amplitude":40},{"sample":3,"amplitude":40},{"sample":6,"amplitude":40},{"sample":5,"amplitude":60}],"matched":7}
B2  {"rows":[{"sample":5,"amplitude":60},{"sample":1,"amplitude":40},{"sample":3,"amplitude":40},{"sample":6,"amplitude":40},{"sample":2,"amplitude":20},{"sample":4,"amplitude":null},{"sample":7,"amplitude":null}],"matched":7}
B3  {"rows":[{"sample":1},{"sample":2},{"sample":3},{"sample":4},{"sample":5},{"sample":6},{"sample":7}],"matched":7}
B4  {"rows":[{"sample":5,"amplitude":60},{"sample":1,"amplitude":40}],"matched":7,"truncated":true}
```

### 适用界限

- "并列按主键升序"需要**至少两个同值行**才能与"并列随方向翻转"区分开；本场景的三个 40 就是为此准备的。
- B3 中主键序与插入序相同，单靠它无法排除"按插入顺序输出"这一错误假设；扩展篇 X7b 用乱序插入补上这一判别（见 `docs/query-semantics-extended.md`）。
- 排序规则与快照无关，但**被排序的行集**由快照决定；快照失效后排序问题本身不可算。

## 场景 C：暂存替换与其他观察者（序号 2→3）

### 参与者请求（R=读者，W=写者）

```
POST /transactions {}                 → R（快照 2）
POST /transactions {}                 → W（快照 2）
POST /transactions/<W>/put     {"relation":"spectra","row":{"sample":1,"band":"b1x","valid":true}}   （未给 amplitude）
POST /transactions/<W>/delete  {"relation":"spectra","key":2}
POST /transactions/<W>/put     {"relation":"spectra","row":{"sample":8,"band":"b8","amplitude":80,"valid":true}}
POST /query {"relation":"spectra","transaction":"<W>","order":[{"column":"sample"}],"columns":["sample","amplitude"]}   （W 看自己）
POST /query {"relation":"spectra","transaction":"<R>","aggregate":{"function":"sum","column":"amplitude"}}            （R 看）
POST /query {"relation":"spectra","aggregate":{"function":"sum","column":"amplitude"}}                                 （当前读）
POST /transactions/<W>/commit                                                                            → 序号 3
POST /query {"relation":"spectra","aggregate":{"function":"sum","column":"amplitude"}}                                 （当前读）
POST /query {"relation":"spectra","transaction":"<R>","aggregate":{"function":"sum","column":"amplitude"}}            （R 再看）
POST /query {"relation":"spectra","at":2,"aggregate":{"function":"sum","column":"amplitude"}}
POST /sql    {"sql":"SELECT sum(amplitude) FROM spectra;","at":2}
POST /sql    {"sql":"SELECT count(*) FROM spectra;","transaction":"<R>"}
POST /transactions/<R>/rollback
```

### 独立推算

- **W 的暂存视图** = 快照 2 的行 + 私有覆盖：`put` 是整行替换，未提供的 amplitude 补 **null**（不是保留旧值 40）；2 被墓碑移除；8 新增。→ 行集 `{1(null),3(40),4(null),5(60),6(40),7(null),8(80)}`，sum=220。
- **提交前**：R 固定在快照 2；当前读序号也是 2（W 未提交）→ 两者都是 sum=200。暂存写对任何其他观察者不可见。
- **提交后（序号 3）**：当前读 → 220，`count(amplitude)`=4（null 为 1、4、7）。R 仍固定快照 2 → 200。提交不改变已开始事务的视图。
- **at=2**：此时 horizon=0 ≤ 2 ≤ 3，可算；历史 ≤2 只有种子 → 200。两个入口同一快照、同一语义 → 都是 200。SQL 入口同样接受 `transaction` 与 `at`。

### 真实运行事实

```json
C1  W 看自己   {"rows":[{"sample":1,"amplitude":null},{"sample":3,"amplitude":40},{"sample":4,"amplitude":null},{"sample":5,"amplitude":60},{"sample":6,"amplitude":40},{"sample":7,"amplitude":null},{"sample":8,"amplitude":80}],"matched":7,"plan":{"access":"scan"}}
C1-sum        {"rows":[{"sum":220}],"matched":7}
C2  R 看       {"rows":[{"sum":200}]}        C3 提交前当前读 {"rows":[{"sum":200}]}
C4-commit     {"sequence":3,"writes":3}
C4  提交后当前 {"rows":[{"sum":220}]}  count(amplitude)→4
C5  R 再看     {"rows":[{"sum":200}]}        （ repeatable read ）
C6  at=2 结构化 {"rows":[{"sum":200}]}       C6-sql at=2 {"rows":[{"sum":200}]}
C7-sql 事务内 count(*) → 7
```

### 适用界限

- C1 的 `plan.access=scan` 是因为"事务在该关系上有私有写时强制扫描"（`internal/engine/read.go:41-43`）；但本场景没有索引，扫描是必然结果——"有索引也强制扫描"这一判别留给了扩展篇 X2。
- "暂存写他人不可见"与"读者视图不变"都以**事务仍活跃**为前提；R 一旦关闭，快照 2 的读入口只剩 `at=2`，而它的可算性由水位决定（场景 E）。
- 本场景未涉及提交冲突；first-committer-wins 的失败语义见扩展篇 X3。

## 场景 D：count/sum 边界（序号 3→5）

### 参与者请求

```
POST /transactions/<X>/put {"relation":"spectra","row":{"sample":9,"band":"b9","amplitude":9223372036854775807,"valid":true}}
POST /transactions/<X>/put {"relation":"spectra","row":{"sample":10,"band":"b10","amplitude":1,"valid":true}}
POST /transactions/<X>/commit                                                        → 序号 4
POST /query {"relation":"spectra","aggregate":{"function":"sum","column":"amplitude"}}
POST /query {"relation":"spectra","aggregate":{"function":"count","column":"*"},"limit":1}
POST /sql   {"sql":"SELECT count(*) FROM spectra LIMIT 1;"}
POST /transactions/<Y>/delete {"relation":"spectra","key":9}
POST /transactions/<Y>/delete {"relation":"spectra","key":10}
POST /transactions/<Y>/commit                                                        → 序号 5
POST /query {"relation":"spectra","aggregate":{"function":"count","column":"*"}}
```

### 独立推算

- **D1**：序号 4 的非 null amplitude 为 40+60+40+80+1+int64 最大值。累加在 64 位有符号范围内上溢 → 不是回绕成负数，而是查询报错（400 `invalid`，"sum overflow"）。
- **D2**：聚合在 LIMIT 之前归约（`execute.go` 先 `reduce` 后截断）→ `count(*)` 得 9，`limit:1` 不起作用；SQL 的 `LIMIT 1` 同样不影响。
- **D3**：删除 9、10 后回到 7 行。

### 真实运行事实

```json
D1   {"status":400,"code":"invalid","message":"sum overflow"}
D2   {"rows":[{"count":9}],"matched":9,"truncated":false}     （SQL 入口同为 9）
D3   {"rows":[{"count":7}],"matched":7}
```

### 适用界限

- 溢出检查覆盖正负两个方向（`execute.go:79-81`），本场景只演示了正方向；负方向（向 `sum` 注入 int64 最小值）留作读者练习。
- "LIMIT 不影响聚合"与"LIMIT 截断行输出"（B4）是同一次执行里的两条独立规则：`matched` 永远统计限制前的匹配数。

## 场景 E：回收水位与可算范围（序号 5→8）

### 参与者请求（P=读者，Z=写者，M=维护者）

```
POST /transactions {}                       → P（快照 5）
POST /transactions/<Z>/put {"relation":"spectra","row":{"sample":3,"band":"b3","amplitude":44,"valid":true}}
POST /transactions/<Z>/commit               → 序号 6
POST /query {"relation":"spectra","transaction":"<P>",...sum...}     （P 的视图）
POST /query {"relation":"spectra",...sum...}                         （当前）
POST /vacuum {"horizon":6}                  → M 请求回收
POST /query {"relation":"spectra","at":2,...sum...}                  （场景 C 用过的同一请求）
POST /sql    {"sql":"SELECT sum(amplitude) FROM spectra;","at":2}
POST /query {"relation":"spectra","at":5,...sum...}                  （水位边界）
POST /transactions/<P>/rollback
POST /vacuum {"horizon":6}                  → 再次回收
POST /query {"relation":"spectra","at":5,...sum...}
POST /query {"relation":"spectra","at":6,...sum...}
POST /vacuum {"horizon":9}                  （超出头部）
POST /vacuum {"horizon":5}                  （低于当前水位）
GET  /stats
```

### 独立推算

- **E0**：P 固定快照 5（sample 3 还是 40）→ sum=220；Z 提交后当前读 → 220−40+44=224。
- **E1**：实际水位 = min(请求 6, 活动快照 {P:5}) = 5，`pinned=true`。逐链应用保留规则（保留水位时最后版本及更新版本；水位前最终墓碑整链移除）：

  | 链 | 版本（序） | 水位 5 的结果 |
  | --- | --- | --- |
  | 1 | 2, 3 | 留 [3]，removed 1 |
  | 2 | 2, 3† | 最终墓碑 ≤5 → 整链移除，removed 2 |
  | 3 | 2, 6 | 留 [2,6]，removed 0 |
  | 4,5,6,7 | 2 | 各留 [2] |
  | 8 | 3 | 留 [3] |
  | 9, 10 | 4, 5† | 各整链移除，removed 2+2 |

  合计 removed=7、retained=8。vacuum 本身是日志记录 → 序号 7。
- **E2**：at=2 < 水位 5 → **410 `snapshot`**，报文给出保留区间 `5..7`。SQL 入口同一请求同样 410。
- **E2 边界**：at=5 == 水位 → 仍可算，得 P 的视图 sum=220。
- **E3**：P 关闭后再请求水位 6 → 无阻挡，水位 6，`pinned=false`；链 3 的 v2 被淘汰 → removed=1、retained=7。序号 8。
- **E4**：at=5 现在 < 6 → 410；at=6 可算 → 224。
- **E5**：请求水位 9 > 序号 8 → 400；请求水位 5 < 当前水位 6 → 400（水位不回退）。
- **E6**：`stats` 应为 `{"sequence":8,"horizon":6}`。

### 真实运行事实

```json
E0-reader {"rows":[{"sum":220}]}   E0-current {"rows":[{"sum":224}]}
E1   {"horizon":5,"removed":7,"retained":8,"pinned":true}
E2   {"status":410,"code":"snapshot","message":"snapshot unavailable; retained interval 5..7"}
E2-sql   同上（同一报文）
E2-boundary  at=5 → {"rows":[{"sum":220}]}
E3   {"horizon":6,"removed":1,"retained":7,"pinned":false}
E4-old-boundary  at=5 → {"status":410,"code":"snapshot","message":"snapshot unavailable; retained interval 6..8"}
E4-new-boundary  at=6 → {"rows":[{"sum":224}]}
E5-ahead   {"status":400,"code":"invalid","message":"vacuum horizon outside retained interval"}
E5-retreat {"status":400,"code":"invalid","message":"vacuum horizon outside retained interval"}
E6-stats   {"sequence":8,"horizon":6}
```

### 适用界限（本资料最重要的一条）

- 场景 C6 中，两个入口在快照 2 上都返回 200；场景 E2 中，**同一对请求**都返回 410。共同快照失效后，不应再宣称"两种入口应得到同一个答案（行集）"——那个答案所需的历史已被回收，推算本身失去输入。此时两个入口唯一可宣称的一致是：都在快照选择阶段以同一规则拒绝（同为 410、同一报文），因为拒绝发生在 SQL/结构化语义分叉之前。
- 反过来，只要 `horizon ≤ at ≤ sequence` 且关系已存在，两个入口的答案都从历史可算，本资料的推算方法继续有效。
- 水位随日志持久（重启不回退）和"可算范围随备份图像走"两点超出核心篇范围，见扩展篇 X15–X17。

## 复现方式

```sh
go build -o bin/laminad ./cmd/laminad
python3 examples/semantics_probe.py --group core
```

探针对每条检查输出 `{"id","request","expect","observed","ok"}` 一行 JSON：`expect` 由探针内置的独立模型只从请求历史算出，`observed` 是真实实例的应答。核心组 42 条检查全部 `ok=true`（本次运行约 0.2 秒）。探针是教学演示，不是基线测试套件。

更全的交错与边界见 `docs/query-semantics-extended.md`；两档材料各自能排除哪些错误假设、代价如何，见 `docs/coverage-comparison.md`。
