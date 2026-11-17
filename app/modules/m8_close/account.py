"""账户状态机：账户是对账完成、确认与发布的最小单位。

状态流转：

    pending ──算完──> done        （已确认，不可覆盖）
       │
       └──预算耗尽/中断──> timeout 或 partial
                              │
                              └──重跑──> pending ──> done

只有 `done` 能进入已发布账区。`timeout`、`partial`、`pending` 一律不进，账户内部
整体算完才落库：不存在「半个账户」的匹配组混进发布结果。
"""
from __future__ import annotations

PENDING = "pending"
DONE = "done"
TIMEOUT = "timeout"
FAILED = "failed"

# 可以被重跑重算的状态；done 不在其中。
RETRYABLE = (PENDING, TIMEOUT, FAILED)

LABELS = {
    PENDING: "待求解",
    DONE: "已确认",
    TIMEOUT: "已延后",
    FAILED: "求解失败",
}

# 可进入已发布账区的状态。
RELEASABLE = (DONE,)


def label(status: str) -> str:
    return LABELS.get(status, status)


def is_releasable(status: str) -> bool:
    return status in RELEASABLE