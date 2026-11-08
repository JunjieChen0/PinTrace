# PinTrace 练习题讲解

版本 v1.0（2026-11-08）· 对应练习包 `PinTrace_practice.html` · 配合《编码手册》v1.1 阅读

感谢完成练习！下面是 20 道练习题的参考答案和判定依据。题号是练习工具里显示的"第 n 条"，编号可以在每题顶部核对。

请先看第 1 节的五个要点，再对照第 2 节的逐题说明。看完后就可以开始正式标注了。

---

## 1 本轮要点

### 要点 1：只判断卡片里列出的引用

"变更前"和"变更后"两栏卡片里列出的，才是这道题**关注的引用**。下方"文件改动"里可能还有别的行在变，那些**不影响**本题答案。

> 例：第 6 题的改动里，`mikefarah/yq@v4.44.6` 也改成了 SHA，但卡片里只列了 `actions/checkout`，所以本题只看 `actions/checkout`：`v4` → 完整 SHA，选 `pin`。第 8、16 题也一样，改动里同时有多个 Action 在变，只看卡片里那一条。

### 要点 2：子路径不同就是另一个 Action

Action 身份包括 `owner/repo` 和后面的子路径。`actions/cache` 和 `actions/cache/restore` 是**两个不同的 Action**。

> 例：第 9 题 `actions/cache@v5` → `actions/cache/restore@v5`，引用值没变，但 Action 变了，选 `replace`。

### 要点 3：job 改名、行号移动，引用值没变，就是 `no_change`

判断"是不是同一条引用"，看的是**是不是同一个步骤**，不看行号和 job 名。

> 例：第 10 题，job 从 `get-available-disks` 改名为 `get-cached-disks`，文件上方插入了很多行，所以引用从第 54 行移到了第 75 行。但它仍是同一个名为 `Set up Cloud SDK` 的步骤，值始终是 `google-github-actions/setup-gcloud@v2.1.1`，选 `no_change`。

这是本轮最需要仔细看的一题，请对照工具里的文件改动再看一遍。

### 要点 4：`pin` / `unpin` 看形式变化，不看版本号是否同时改了

可变值变成完整 SHA 就是 `pin`，完整 SHA 变成可变值就是 `unpin`，即使版本号同时也变了。

> 例：第 8 题 `google-github-actions/auth@v2` → 完整 SHA（注释 `v2.1.8`），选 `pin`。第 4 题完整 SHA（注释 `v4.2.1`）→ `@v6`，选 `unpin`。

### 要点 5：整个文件新增或删除

- 文件整个被删除，卡片里的引用随之消失，选 `remove`（第 1、13 题）。
- 新建一个文件，卡片里的引用随之出现，选 `entry`（第 11、20 题）。

这类题的文件改动默认折叠，不需要逐行看完。

### 另：作答方式

请在网页工具里**逐题手动点选**作答。工具记录的作答时间，是向审稿人证明"由人逐题完成"的依据。详见《编码手册》v1.1 第 2 节第 5 条。

---

## 2 逐题参考答案

### 变化判断题

| 题号 | 编号 | 参考答案 | 依据 | 手册 |
|---|---|---|---|---|
| 1 | `i934deb491` | `remove` | `actions/cache@v4` 所在的工作流文件被整个删除 | 5.1、5.4 |
| 2 | `i58ed40618` | `sha_change` | 同一步骤 `Renovate`，`renovatebot/github-action` 从一个完整 SHA 换成另一个（注释 v43.0.5 → v43.0.6） | 5.1 |
| 4 | `i1194ad5ab` | `unpin` | 同一步骤 `Push on Docker Registry`（因上方插入步骤，从第 120 行移到第 126 行），`docker/build-push-action` 从完整 SHA 变为 `@v6` | 5.1，要点 4 |
| 6 | `i4b69cb0cb` | `pin` | 同一步骤 `checkout`，`actions/checkout@v4` → 完整 SHA；改动里 `yq` 的变化不属于本题 | 5.1，要点 1 |
| 7 | `ic68b4ed24` | `replace` | 同一步骤由 `octo-sts/action` 换成 `shopware/octo-sts-action`，`owner/repo` 不同 | 5.1 |
| 8 | `i190fb499c` | `pin` | 同一步骤，`google-github-actions/auth@v2` → 完整 SHA（注释 v2.1.8） | 5.1，要点 1、4 |
| 9 | `i6ddd200f2` | `replace` | 同一步骤 `Cache`，`actions/cache` → `actions/cache/restore`，子路径不同即为另一个 Action | 3、5.4，要点 2 |
| 10 | `ie45177f1c` | `no_change` | job 改名、行号移动，但仍是步骤 `Set up Cloud SDK`，值 `setup-gcloud@v2.1.1` 没变 | 3、5.4，要点 3 |
| 11 | `ic23889edf` | `entry` | 新建文件 `automerge-gate.yaml`，其中的 `pkgdeps/automerge-gate` 是新出现的引用 | 5.1，要点 5 |
| 13 | `i598579e0d` | `remove` | `readmeio/rdme@7.0.1` 所在的工作流文件被整个删除 | 5.1，要点 5 |
| 16 | `ib2dc83d73` | `tag_change` | 同一步骤 `Set up Cloud SDK`，`setup-gcloud@v2.1.1` → `@v2.1.2`，都是可变值 | 5.1，要点 1 |
| 17 | `i1e4ed0678` | `sha_change` | 同一步骤 `Stop instance`，`zama-ai/slab-github-runner` 从一个完整 SHA 换成另一个 | 5.1 |
| 19 | `i98322b1a0` | `tag_change` | 同一步骤 `Upload SBOM`，`softprops/action-gh-release@v2` → `@v3` | 5.1 |
| 20 | `i09f0efba5` | `entry` | 新建文件 `deploy-docker-dockerhub.yml`，其中的 `docker/setup-qemu-action@v3` 是新出现的引用 | 5.1，要点 5 |

### 引用分类题

| 题号 | 编号 | 参考答案 | 依据 |
|---|---|---|---|
| 3 | `i7ca6ffaf3` | `mutable` | `actions/download-artifact@v4`，`v4` 是标签 |
| 5 | `i45240bf54` | `pinned` | `step-security/harden-runner@` 后是 40 位十六进制；注释 `v2.16.1` 不影响分类 |
| 12 | `i69d7808fb` | `pinned` | `actions/checkout@` 后是 40 位十六进制 |
| 14 | `ic4cf9ca8a` | `mutable` | `rtCamp/action-slack-notify@v2`，`v2` 是标签 |
| 15 | `i1f3882a39` | `mutable` | `actions/setup-python@v6`，`v6` 是标签 |
| 18 | `i8ddd69efd` | `pinned` | `actions/checkout@` 后是 40 位十六进制；注释 `v6.0.3` 不影响分类 |

引用分类题的方法：数 `@` 后面的字符，恰好 40 位十六进制就是 `pinned`，否则是 `mutable`；注释一律不看（手册第 4 节）。

---

## 3 正式标注前

1. 请阅读更新后的《编码手册》v1.1。修订内容见手册第 9 节，主要是把上面的要点 1、2、4 和手动作答的要求写进了规则。
2. 打开 `PinTrace_annotation_A1.html` 或 `…_A2.html`（只打开发给你的那份）开始正式标注。
3. 正式标注期间如有规则疑问，请发给组织者，答复会以 FAQ 形式同时发给两位。
4. 完成后导出答案，原样发回，并附一句确认："本人确认所有题目均由本人阅读材料后判断，并在工具中手动作答，未使用 AI 助手或自动化工具。"
