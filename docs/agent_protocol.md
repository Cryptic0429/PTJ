# Agent 进程协议（v0.1）

每个角色可以对应不同模型或程序。`CommandAgent` 以 `shell=False` 启动配置中的命令数组，给 stdin 传入单个 UTF-8 JSON 对象；进程应在 stdout 只输出单个 JSON 对象，诊断信息写 stderr。路径是绝对路径，图像文件是未修改的全帧。VLM 接入程序负责读取图像并发给模型；PTJ 不假设任何服务商的 HTTP API。

## 控制器 `role=controller`

输入：`objective`、`phase`、`phase_order`、`remaining_decisions`、`latest_frame`、`actual_action_history`、`allowed_actions`。`latest_frame` 含原图路径、帧 ID、视频时间。输出：

```json
{"phase":"attempt","command":"HOLD","actions":["forward"],"control_ticks":1,
 "phase_complete":false,
 "event_hints":[{"event_type":"possible_contact","status":"expected","note":"向前动作后待核对"}]}
```

`command` 只能是 `HOLD`、`WAIT`、`FINISH`。`HOLD` 的动作必须在适配器白名单；`WAIT` 和 `FINISH` 的动作列表为空。提示状态只能是 `observed`、`expected`、`unknown`，由程序绑定实际动作与当前原始帧 ID。即使写了 `observed`，仍只是候选提示。不要输出自猜的帧号。

## 证据检查 `role=evidence_checker`

输入：一个 `question`、当前 `frames`（绝对原图路径、ID、时间戳）、`actual_actions`。输出：

```json
{"sufficient":false,"missing_evidence":["接触后画面被遮挡"],
 "supplement_frame_ids":["f000023"],"notes":"建议查看接触后连续帧"}
```

`sufficient` 必填。补取 ID 必须已经存在于完整帧日志；Agent 不能生成新帧。达到预算且仍不足，Judge 不会收到该题，程序直接记 `undetermined`。

## 事件定位 `role=event_locator`（可选）

输入：一个 `question`、完整 `frames`、`actual_actions`。输出 `{"frame_ids":["f000023", "f000024"]}`。定位只帮助补取，不代表事件成立。

## 规则裁判 `role=rule_judge`

输入：一个 `question` 和对应 `evidence`，内有完整原图路径、帧 ID/时间戳、证据检查结果、是否覆盖全时段。输出：

```json
{"verdict":"violation","opportunity":"present",
 "evidence_frame_ids":["f000020","f000023"],
 "observations":["主体在连续帧中进入可见实体区域"],
 "reason":"符合该题冻结的违反判据"}
```

`verdict` 为 `pass`、`violation`、`undetermined`、`untriggered`；`opportunity` 为 `present`、`absent`、`unknown`。决定性结论需要触发条件、充分证据和所选原始帧引用；未触发还需要全时段覆盖。`undetermined` 必须说明原因。裁判不改任务判据、不自行给总分。

## 数据与失败

`config.json` 保存冻结任务和 SHA-256；`actions.jsonl` 区分请求/实际动作；`frames.jsonl` 的帧 ID 和时间戳由程序写入；`event_hints.jsonl` 只记候选；`evidence.json` 保存逐题原帧索引与检查意见；`scores.json` 保存逐题四态与汇总。进程超时、非 JSON、非法帧引用、修改任务、无原图或非法动作会显式报错。生产接入还应在外部封装进程中保存模型原始响应和版本信息。
