# ledgerbank 银行流水与账簿多对多对账

连锁零售财务共享组每月核对 12 个银行账户与账簿凭证。一笔付款可能拆成多张凭证，
多笔流水也可能合并支付一张发票；会计原先逐条手工比对，手续费与尾差经常对不上。

## 核心规则

- 候选：方向相同、金额差 ≤ 0.02 元、本地日期差 ≤ 3 天，且**同一账户内**。
- 一对一、一对多、多对一都参与求解；多对一用受限子集和，最多 4 条，按金额降序枚举。
- 择一：层级优先（一对一 → 一对多 → 多对一），再按金额差、日期差、业务号升序，取不重叠的组合。
- 手续费只计银行侧，不进入匹配金额；匹配组按银行金额比例**在账户内**分摊手续费，尾差用最大余额法分到分。
- 银行合计 = 匹配金额 + 未匹配金额 − 手续费合计；账簿合计 = 匹配金额 + 未匹配金额。

## 分批关账（M8）

促销月结后单个账户的同向流水可能从几十笔涨到上千笔，整批一次求解会超时。本系统按
**账户**作为完成与发布的最小单位：

- **采信与求解分离**：`import` 支持 `solve=false` 快速采信；求解交给 `POST /api/close/advance`，
  可用 `max_accounts` 限定每次算完几个账户，财务当天就能分批拿到结果。
- **账户原子完成**：一个账户要么整体算完并标记 `done`，要么保持 `pending/timeout`；
  不存在半个账户的匹配组进入发布结果。
- **只发布已完成账户**：每轮推进写一份不可变的关账版本（`close_revisions`）并原子切换发布指针。
  页面、接口与导出只读当前版本，看到同一份带完成标记的结果。
- **旧版完整保留**：中断或超时不会破坏已有版本；重跑只重算非 `done` 账户，
  已确认记录按结果指纹比对，结果不同则拒绝覆盖。
- **确定性预算**：子集搜索按节点数计量（`LEDGERBANK_SOLVE_BUDGET`，默认 1 亿 ≈ 单账户 15 秒），
  同一输入同一预算得到同一结果；超预算只把该账户延后，不影响其他账户。

## 功能模块

| 模块 | 使用者动作 | 入口 |
|---|---|---|
| M1 流水采信 | 导入银行流水或账簿凭证，看到编码、分段与逐行诊断 | `POST /api/statements/import`、页面 `/` |
| M2 金额日期规则 | 查看列定义、借贷符号、容差与日期窗口 | `GET /api/contract` |
| M3 质量隔离 | 按原因码看隔离行并导出 CSV | `GET /api/quarantine`、`/api/quarantine.csv`、页面 `/quarantine` |
| M4 候选匹配 | 按账户生成一对一、一对多、多对一候选 | `POST /api/candidates`、页面 `/candidates` |
| M5 多对多求解 | 按账户求解不重叠匹配组，看手续费分摊 | `POST /api/matches`、页面 `/matches` |
| M6 差异与批次 | 看两侧守恒、差异归因、账户状态与关账版本 | `GET /api/differences`、`GET /api/batches`、页面 `/batches` |
| M7 报告导出 | 导出报告 CSV 与运行清单 JSON | `GET /api/export/report.csv`、`/api/export/manifest.json`、页面 `/export` |
| M8 分批关账 | 查看账户完成状态、推进未完成账户、按版本导出 | `GET /api/close/status`、`POST /api/close/advance`、`GET /api/close/revisions` |

## 启动

```bash
docker compose up -d --build
```

打开 <http://127.0.0.1:18206/>，点「载入样例」即可跑完采信 → 质量 → 候选 → 匹配 → 差异 → 导出。

## 演示步骤

1. 首页点「载入样例」，页面顶部出现银行 22 笔、账簿 19 笔、匹配 11 组，右上角显示
   「已关账 3/3 账户 · rev-…」。
2. 打开「隔离区」，看 8 条隔离行的原因码，下载隔离 CSV。
3. 打开「候选」，核对一对一 7 条、一对多 2 条、多对一 2 条。
4. 打开「匹配」，看 11 组匹配与手续费分摊合计 22.31 元。
5. 打开「批次」，核对银行笔数、账簿笔数、手续费分摊、匹配差额四项平衡，并看账户完成状态与关账版本链。
6. 打开「导出」，下载报告 CSV 与运行清单 JSON，可见同一 `revision_id` 与完成标记。

## 分批关账演示

```bash
# 快速采信，不立即求解
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F solve=false -F file=@sample_data/bank_statement_dirty.csv
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=ledger -F solve=false -F file=@sample_data/ledger_book_dirty.csv

# 每批只算 1 个账户，每批完成即发布一版；未完成账户不进已发布账区
curl -s -X POST http://127.0.0.1:18206/api/close/advance -H 'content-type: application/json' -d '{"max_accounts":1}'
curl -s http://127.0.0.1:18206/api/close/status
curl -s http://127.0.0.1:18206/api/close/revisions
```

## 测试

```bash
docker compose exec -T app python -m pytest -q
```

## 页面

- `/` 工作台：左右对照、固定合计条、差异行左边框。
- `/quarantine` 隔离区。
- `/candidates` 候选清单。
- `/matches` 匹配组。
- `/batches` 对账与批次历史。
- `/export` 导出。

## 目录

```text
app/            平台层与八个功能模块
app/templates/  base + 六个页面
app/static/     theme / layout / components + 三个脚本
docs/           DEPTH、DATA-DICTIONARY、RUNBOOK
sample_data/    六份脏样例与一份空白样例
tests/          模块测试、端到端、不变量、分批关账
```

## 已知不覆盖

- 凭证拆分到科目层的匹配不做，只按银行账户与金额对账。
- 多币种不折算，样例只有人民币；外币流水会因金额列带币种符号之外的字符进隔离区。
- 跨年账单不分摊手续费，一次运行只对应一份月度流水。
- 手续费按账户内比例分摊；不做跨账户调剂，也不区分银行收费与第三方收费。
