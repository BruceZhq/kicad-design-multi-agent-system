# Governed Evolution 固定回归套件

候选 Harness 在隔离 worktree 中执行三类内容寻址套件：

- `optimization.v1.json`：公开给 Optimizer 的约束保持、断点恢复和发布真实性案例；
- `../sealed/regression/holdout.v1.json`：不得提供给 Optimizer 的泛化案例；
- `../sealed/regression/adversarial.v1.json`：伪造发布结论和产物路径逃逸案例。

manifest 同时绑定 suite、pytest node ID 和测试源文件 SHA-256。Runner 不接受 manifest
提供任意 shell 命令；源文件、manifest digest、sealed 分区或配置 digest 任一漂移都会在执行
前失败。运行全部套件：

```bash
python -m evolution.regression_runner --root .
```

Evolution sandbox 分别传入 `--suite` 与控制面冻结的
`--expected-suite-digest`，确保“配置的套件”和“实际执行的套件”是同一内容。可用
`--output candidate.json --baseline baseline.json` 得到同 case 集合上的 improved、regressed
和 unchanged 比较；存在任何回归或候选仍有失败时 `candidatePassed=false`。

这些套件是候选代码的快速治理门，不冒充 KiCad 端到端执行。真实 Release-ready 成功率、
ERC/DRC、未连接、Freerouting 和产物指标由固定的 10 对单/多 Agent 黄金计划
`frontend/public/evals/paired-kicad-golden.v1.json` 采集。
