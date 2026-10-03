# 红白喜事服务运营平台

这是一个面向婚庆公司、殡葬服务机构和现场调度人员的 Python 后端服务，用于管理服务套餐、家庭订单、现场执行队列、服务人员、结果版本和运营干预。服务保留登录、角色权限、会话、审计和配额等基础能力，所有业务状态与审计事件写入本地 SQLite 数据库，适合在单个应用容器中离线运行。

## 运行环境

- Python 3.11
- SQLite 3（由 Python 标准库提供）
- FastAPI 与 Uvicorn

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

默认数据库位于 `./data/ceremony-operations.db`，可复制 `.env.example` 并设置 `TOWNSHIP_DATABASE_PATH` 指向其他本地路径。

## 初始化与启动

```bash
python -m app.cli init-db
python -m app.cli check-db
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

服务订单运营接口使用 `/api/compute` 前缀，身份、角色、审计和系统接口分别位于 `/api/auth`、`/api/roles`、`/api/audit` 与 `/api/system`；节前档案维护窗口接口位于 `/api/maintenance/windows`。

## 维护窗口流程

礼仪人员与车辆档案维护前，运营主管按带版本的窗口流程收敛在途服务单，所有接口位于 `/api/maintenance/windows`（需要 `maintenance.operate` 权限）：

1. `POST /plan`：计划窗口（原因、可选排空截止时间），返回窗口与版本号。
2. `POST /{id}/drain`：进入排空，普通新服务单开始被 409 拒绝，在途租约允许安全结束。
3. `POST /{id}/pause-claims`：暂停领取，调度员只能领取紧急豁免单。
4. `GET /{id}/leases`：检查剩余租约与未收敛服务单；`POST /{id}/reconcile-leases` 可回收过期租约。
5. `POST /{id}/switch`：全部收敛后完成切换，仍有剩余租约时 409 拒绝。
6. `POST /{id}/complete`：维护结束，窗口关闭，接单与领取恢复。

异常恢复：任意进行中阶段可 `POST /{id}/abort` 立即恢复服务；服务重启时以数据库中的阶段为准自动恢复门禁（启动时执行过期清扫），不会把未完成服务重新暴露给调度员。

- 每次阶段变化都记录操作者、原因与版本，写入窗口事件流和审计表；推进接口支持 `expected_version` 乐观并发校验。
- 同一阶段重复推进幂等（返回 `changed:false`，版本与事件不重复）；超过排空截止时间仍未进入排空的计划自动过期，不影响下一次计划。
- 窗口期间的紧急白事走 `POST /emergency-exemption`，规则 R1–R5 全部命中（白事 + immediate + 家属联系信息 + 值班主管授权码 + 有效窗口）才放行，受理与拒绝均落审计；授权码在同一窗口不可复用。
- 固定时钟演练：设置 `TOWNSHIP_FIXED_NOW` 为 ISO8601 时间，或在测试中使用 `clock_registry.override(FrozenClock(...))`。


## 测试与编译检查

```bash
python -m pytest
python -m compileall -q app tests
```

本地冒烟命令：

```bash
python -m app.cli smoke
python -m app.cli compute-demo
```

## 目录结构

```text
app/compute/       任务模板、配额、提交、领取、回执和人工干预
app/maintenance/    带版本的维护窗口、排空门禁与紧急白事豁免
app/api/            登录、角色、审计和系统管理接口
app/core/           时钟、安全、异常和分页能力
app/repositories/   SQLite 查询与事务封装
app/services/       身份、审计和后台任务服务
app/database.py     SQLite 连接、事务、表结构和权限初始化
tests/              领域、接口、调度和身份回归测试
tools/              本地维护脚本
```

## 数据一致性

SQLite 连接启用外键、WAL 和忙等待策略。提交、领取、回执和人工干预在即时事务中完成；租约、配额与结果版本使用可注入时钟，便于复现跨日和恢复边界。会话令牌只保存摘要，审计与人工干预记录不会写入明文密码或令牌。
