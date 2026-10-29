# 逐规则验收

`parity.json` 是清单唯一事实源，HTML 只用于展示。Windows 基线符号覆盖、规则映射已审查、当前离线测试通过、当前实机场景通过分别计数。清单中 `mapped` 表示已读过对应断言并建立映射，本身不表示测试或实机已通过。

每条规则使用稳定 ID，例如 `A35.R04`。已有 ID 不因排序或改名重新编号。规则通过 `scenarios` 关联场景；场景反向列出精确的 `rule_ids`，同时保存断言说明、审核者和具体 unittest 选择器。每个选择器保存测试方法与所在模块辅助代码的 AST 指纹。改了断言或 fixture 后，必须重新审读其含义再更新映射，不能批量把所有测试哈希刷新后宣布覆盖。

同一场景可以证明几条规则，但必须逐条说明实际断言。模块名、测试数量、测试名称和旧能力级测试数组都不证明规则。部分覆盖保留 `pending`，在 `review_note` 写清已证明部分和缺口。只有具体插件扩展可以使用已批准例外；通用插件宿主仍须核对。Windows 实机是用户明确延期的范围，不计为本轮实测通过。

## 更新顺序

1. 修改实现和测试后，实际审读每条受影响规则的测试断言、平台差异与公共层归属。补足缺失场景，更新经过审读的选择器指纹；确实没有证明的规则保留待验。
2. 完成所有代码、测试、工具及文档修改。运行结构检查 `python tools/audit_parity.py`。它不把旧测试报告算作当前通过，也不会因仍有待验规则自动伪造证据。
3. 运行 `python tools/run_tests.py --suite Full --report docs/parity-tests.json`。回归前后源集合必须一致。失败、跳过、预期失败、子测试失败和结果未知都不满足对应规则的通过条件。
4. 实际完成最后一轮公共层与规则映射复检后，运行 `python tools/audit_parity.py --record-final-review REVIEW_ID --reviewer REVIEWER`，填入真实复检编号与审核者。此操作只记录当前指纹，不修改规则状态。第 3、4 步可交换，但其间不得修改源输入。
5. 运行 `python tools/audit_parity.py --require-reviewed`。它要求全部非例外规则已审读并建立有效场景映射、所有引用的回归确实通过、最终复检指纹一致。缺证据时保持失败，不把“脚本能运行”写成验收完成。
6. 按清单的手工场景执行真实操作并保存下述证据；再运行 `--require-complete`。它代表全部规则完成，没有实际操作就保留该门禁失败。Windows 实机按用户要求延期，本轮可以报告已完成代码复检及离线回归，但不能因此通过 `--require-complete`。延期仍是待验，不是通过。

最终测试报告之后任何输入文件新增、删除或修改，都必须重新审查受影响规则、重新生成完整测试报告并记录新的最终复检。输出 HTML 建议放在仓库外，例如 `/tmp/codex-parity.html`。

## 指纹范围

指纹包含 `src`、`tests`、`tools`、`docs`、`.github`，以及根目录的安装脚本、版本、构建元数据和操作说明。它比较完整路径集合，因此新增文件也会使旧报告失效。`__pycache__` 等缓存和 `docs/parity-tests.json` 输出不参与输入指纹。

`parity.json` 的规则、断言映射、边界和公共层决定都参与指纹；仅输出性质的 `final_review`、`native_evidence`、`historical_evidence` 三个封套不参与，避免“报告包含自己的哈希”。`docs/parity-native-*.json` 也是输出，但必须由 `native_evidence.current` 单独绑定文件字节 SHA-256 并通过内容校验。其余文件不因其处于 docs/tools 而豁免。

## 当前实机证据

场景在清单中定义 `kind: manual`、平台、验证方法、预期观察和精确规则 ID。证据引用形式：

```json
{"current":[{"path":"docs/parity-native-current.json","sha256":"文件实际字节的 SHA-256"}]}
```

文件内容示例（场景 ID、规则 ID 和观察必须来自实际执行，示例不能作为验收结果）：

```json
{
  "schema_version": 2,
  "source_digest": "当前审计输出的 source_digest",
  "mapping_sha256": "当前审计输出的 mapping_sha256",
  "platform": "darwin",
  "records": [{
    "scenario_id": "MAC-TELEGRAM-REPLY",
    "rule_ids": ["A34.R01", "A35.R04"],
    "outcome": "passed",
    "method": "telegram-ui",
    "observed_at": "2026-10-28T23:00:00Z",
    "observations": ["实际引用指定消息，核对原任务 ID 与实际回复；记录本次可追溯结果。"]
  }]
}
```

验收器读取文件并检查字节指纹、schema、当前源/规则版本、平台、方法、精确场景和规则集合、时间格式及非空观察记录。重复场景或缺失内容被拒绝。工具负责验证证据结构与版本，审核者仍负责确认观察确实来自实际操作；不能把模拟结果写成实机记录。历史结果留在 `historical_evidence`，不会自动升级成本轮通过。

## 本轮已批准的范围澄清

- 额度预约继续只支持 6、12、24 小时。R16 A 只纠正清单，不新增自定义时长入口。
- 整个宿主操作系统最多一个主服务，跨用户、profile、data_dir 与 bot 身份互斥。短生命周期 Hook 和诊断命令不占主服务锁。独立容器/网络命名空间作为独立系统，不属于普通桌面部署范围。
- 通用行为与测试共用；平台层只保留各自原生访问、进程生命周期和服务安装差异。离线 Windows 分支测试不证明 Windows 实机安装与 IPC 行为。
