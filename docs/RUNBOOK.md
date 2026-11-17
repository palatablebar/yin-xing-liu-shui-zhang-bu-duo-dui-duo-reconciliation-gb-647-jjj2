# 运行手册 ledgerbank

## 启动与停止

```bash
docker compose up -d --build
docker compose ps
curl -s http://127.0.0.1:18206/api/health
docker compose down -v --remove-orphans
```

## 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `ledgerbank-data` 保存批次、流水行、候选与匹配。重建后从「载入样例」重新开始。

## 主流程

1. `POST /api/demo/run`：读取两份脏样例，采信、质量、按账户求解、关账发布一次跑完。
2. `GET /api/quarantine`：查看隔离行与原因分布（隔离行与关账状态无关，始终可见）。
3. `GET /api/candidates`：查看已确认账户的候选。
4. `GET /api/matches`：查看已发布关账版本的匹配组与手续费分摊。
5. `GET /api/differences`：查看两侧守恒与差异归因。
6. `GET /api/export/report.csv` 与 `GET /api/export/manifest.json`：导出。

页面、接口与导出都只读**当前已发布关账版本**（`run_publications` 指针），
所以三者看到同一份结果，且结果带同一个完成标记与 `revision_id`。

## 分批关账（大账户场景）

单账户流水上千笔时，整批一次求解会很慢。把采信与求解拆开：

```bash
# 1. 采信：立即返回，不求解
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F solve=false -F file=@bank.csv
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=ledger -F solve=false -F file=@ledger.csv

# 2. 分批推进：每批最多算完 2 个账户，每批完成写一版并切换发布指针
curl -s -X POST http://127.0.0.1:18206/api/close/advance \
  -H 'content-type: application/json' -d '{"max_accounts":2}'

# 3. 看账户状态与完成比例
curl -s http://127.0.0.1:18206/api/close/status

# 4. 只推进指定账户（例如财务先要某几个账户的结果）
curl -s -X POST http://127.0.0.1:18206/api/close/advance \
  -H 'content-type: application/json' -d '{"accounts":["6222-0012"]}'

# 5. 查看关账版本链；可按任意历史版本导出
curl -s http://127.0.0.1:18206/api/close/revisions
curl -s http://127.0.0.1:18206/api/close/revisions/<revision_id>/report.csv
```

规则：

- 已确认（`done`）的账户不会被重算，也不会被不同结果覆盖；显式点名已确认账户只做一致性核对，
  结果不同返回 `conflicts`，库里的记录原样保留。
- 中断或超时最多丢掉「正在算的那一个账户」，已发布版本与已确认账户完好；重跑只重算非 `done` 账户。
- `timeout` 账户不进已发布账区；调高 `LEDGERBANK_SOLVE_BUDGET` 后重跑即可补上。

## 单步重跑

```bash
curl -s -X POST http://127.0.0.1:18206/api/candidates
curl -s -X POST http://127.0.0.1:18206/api/matches   # 等价于 /api/close/advance
```

同一输入重跑命中同一 `run_id`；已确认账户与已发布版本逐字不变，未确认账户继续推进。

## 导入自有文件

```bash
curl -s -X POST http://127.0.0.1:18206/api/statements/import \
  -F side=bank -F file=@sample_data/bank_statement_dirty.csv
```

支持 UTF-8（含 BOM）、UTF-8 无 BOM、GB18030；列名按 `GET /api/contract` 的别名表映射。
导入默认 `solve=true`（立即求解）；大文件用 `-F solve=false` 先采信，再分批推进。

## 失败处理

- 400：参数或文件不可用，页面顶部提示原因，保留已填内容。
- 404：批次或样例不存在，或者还没有已发布的关账结果（`E_NOT_PUBLISHED`，先推进账户）。
- 409：还没有采信任何账户就求解匹配，先执行 `POST /api/statements/import`。
- 账户 `timeout`：不是失败，是确定性预算用完；调高 `LEDGERBANK_SOLVE_BUDGET` 后重跑该账户。
- 隔离行不是失败：按原因码修正源文件后重新导入。

## 日志

```bash
docker compose logs -f app
```
