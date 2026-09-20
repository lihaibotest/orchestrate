# Artifact / Checkpoint / Incremental Recovery

## Artifact reusable 判定
同时满足：对象存在；content hash 一致；schema 与消费者兼容；input_fingerprint 适用；quality 满足完成标准；跨边界权限通过。任一失败 → reusable=false，并触发失效传播。

## Resume Root
1. seed = FAILED / low-quality / invalid-artifact nodes。
2. `invalidated = downstream_closure(seed)`。
3. 若前序 Artifact hash 变化，把最早受影响 producer 加入 seed 后重算。
4. 向上寻找能够提供全部可信输入的最近 Checkpoint。
5. Resume Root = 必须重新执行的最早节点。
6. Resume Root 之前只有通过 reusable 校验的 Artifact 才能复用。

伪代码：
```text
affected = seed_failed_or_low_quality_nodes()
affected += nodes_with_invalid_artifacts()
invalidated = downstream_closure(affected)
resume_root = earliest_node_required_to_rebuild(invalidated, trusted_checkpoints)
reused_nodes = predecessors(resume_root) with reusable artifacts and valid fingerprints
```

必须单测：链式 DAG、分叉/汇合 DAG、多个失败 seed、hash 篡改、checkpoint 缺失、schema 不兼容、权限失败。
