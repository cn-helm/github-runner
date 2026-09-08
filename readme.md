# GitHub persistent runner

在 Kubernetes 上部署一个长期运行、仓库级别的 GitHub Actions runner。
Ubuntu 24.04 / Linux amd64，使用非 root 用户 UID/GID 1001。整个 runner 安装目录、
注册凭据和工作区都存放在 PVC 中，支持 runner 自身自动更新以及 Pod 重建后恢复。

Chart 名称为 `github-runner`。这是单 runner Chart，不包含自动扩容、GitHub App 自动签发注册 token、Docker daemon
或构建语言 SDK。工作流需要的工具应加入自定义镜像或通过 workflow 安装。

## 文件

- `values.yaml`：Helm 默认配置。旧的 `value.yaml` 保留为空覆盖文件，原有 `-f value.yaml` 用法仍可用。
- `image/Dockerfile`、`image/entrypoint.sh`：需要自行构建并推送的基础运行镜像及启动脚本。
- `templates/`：单副本 Deployment、PVC 和安装提示，无 Service / Ingress。
- `tests/test_runner.py`：不访问 GitHub、不使用真实 token 的本地验证。

## 准备镜像

在本目录运行，替换镜像仓库地址。默认镜像名称只是占位配置，并非已经发布的镜像。

```bash
docker build --platform linux/amd64 -t registry.example.com/ci/github-runner:0.1.0 ./image
docker push registry.example.com/ci/github-runner:0.1.0
```

受限网络下，先为当前命令或会话设置代理 `http://165.225.112.16:10015`。
例如为构建中的 apt 下载传递代理：

```bash
HTTP_PROXY=http://165.225.112.16:10015 HTTPS_PROXY=http://165.225.112.16:10015 \
docker build --platform linux/amd64 \
  --build-arg HTTP_PROXY=http://165.225.112.16:10015 \
  --build-arg HTTPS_PROXY=http://165.225.112.16:10015 \
  -t registry.example.com/ci/github-runner:0.1.0 ./image
```

基础镜像拉取由 Docker daemon / BuildKit 执行，也需要其网络可达；构建参数不配置 daemon 代理。
代理不可用或遇到登录、认证、证书问题时停止，准备可用网络、镜像仓库权限或组织 CA，
切换网络并确认后继续，不自动换源或更换依赖版本。

镜像只内置 runner 系统依赖以及 Bash、Git、curl、jq、SSH 客户端等工具。
不支持开箱即用的 `docker build`、容器 action、`jobs.<job>.container` 和 service containers；
这些场景需要另行设计容器执行方案。`ubuntu24` 只是自定义匹配标签，不代表具有 GitHub 托管 runner 的完整工具集。

## 首次安装

需要 Kubernetes、Helm、可写的 PVC，以及集群节点到镜像仓库、runner 到 GitHub 下载和 Actions 服务的网络访问。
StorageClass 应支持 UID/GID 1001 写入、目录原子重命名及跨 Pod 有效的 `flock` 文件锁。
若存储驱动不应用 `fsGroup`，应由存储管理员预置目录权限。

在 GitHub 仓库 Settings → Actions → Runners → New self-hosted runner 获取**注册 token**，
不是 PAT。注册 token 通常一小时过期，应临近首次启动时创建 Secret。
使用交互读取避免将 token 字面值写入 shell 历史：

```bash
kubectl create namespace github-runner
read -rsp 'Runner registration token: ' RUNNER_TOKEN
printf '\n'
printf '%s' "$RUNNER_TOKEN" | kubectl create secret generic github-runner-registration \
  -n github-runner --from-file=token=/dev/stdin
unset RUNNER_TOKEN
```

创建 `runner.local.yaml`（已由 `.gitignore` 忽略）：

```yaml
image:
  repository: registry.example.com/ci/github-runner
  tag: "0.1.0"
github:
  repo: cn-ph-spm/spm
  existingSecret: github-runner-registration
runner:
  name: spm-k8s-runner
  labels: k8s,ubuntu24
persistence:
  size: 20Gi
  # storageClass: your-storage-class
```

```bash
helm lint . --strict -f runner.local.yaml
helm upgrade --install spm-runner . -n github-runner -f runner.local.yaml
kubectl logs -n github-runner deployment/spm-runner-runner -f
```

在 GitHub 界面确认 runner Online，再运行 workflow：

```yaml
name: Runner smoke test
on: workflow_dispatch
jobs:
  smoke:
    runs-on: [self-hosted, Linux, X64, k8s, ubuntu24]
    steps:
      - uses: actions/checkout@v4
      - run: |
          id
          uname -m
          git --version
          echo "Persistent runner is working"
```

如需运行时代理，在覆盖文件中设置（集群内必须能访问该代理）：

```yaml
extraEnv:
  - name: http_proxy
    value: http://165.225.112.16:10015
  - name: https_proxy
    value: http://165.225.112.16:10015
  - name: no_proxy
    value: localhost,127.0.0.1,.svc,.cluster.local
```

带凭据的代理使用 `extraEnv[].valueFrom.secretKeyRef`，不要在 values 中保存密码。
代理设置也会被 workflow 子进程继承。私有镜像凭据使用 `imagePullSecrets` 引用现有 Secret。

## 启动与更新

1. 获取 `/persistent/.runner.lock`，同一 PVC 的第二个 runner 立即报错退出。
2. 首次安装时下载到 `/persistent/.runner-install`，严格校验 SHA256，解压完成后原子移动到
   `/persistent/gh-runner`。中途下载或解压失败，下次启动在持锁状态清理暂存目录并重新安装。
3. 首次注册执行 `config.sh --unattended --work _work`，保存注册设置摘要及 GitHub 凭据。
4. 已注册时复用凭据，不需要有效的注册 token，不重复下载安装；启动前从环境中移除注册 token。
5. 运行官方 `run.sh`，开启其 TERM/INT 转发模式，由 tini 管理 PID 1 和孤儿进程。
   保留默认自动更新行为，不传 `--disableupdate`，不使用 `--ephemeral`。

`runner.initVersion` 和 `runner.sha256` **仅决定空 PVC 的首次安装**。
修改它们不会覆盖已有程序，也不能用于降级。初始值来自
[官方 v2.337.0 发布](https://github.com/actions/runner/releases/tag/v2.337.0)，对应 Linux x64。
整个安装目录必须可写，包含隐藏凭据、更新生成的版本目录、`_diag` 和 `_work`。
Pod 重启及 Helm 升级镜像时均保留这些文件。

Deployment 固定一个副本，升级策略为 `Recreate`，升级期间有短暂停机。
RWO 并非“仅一个 Pod”，所以还使用文件锁。不要手动扩容，也不要强制删除仍在运行的旧 Pod；
同一 PVC 不应给其他任务使用。默认不设 liveness/readiness 探针：避免下载和自动更新期间误杀；
Kubernetes 的 Ready 或 Helm `--wait` 不代表 GitHub Online，需要结合 GitHub 状态和实际 workflow 验证。

默认终止宽限时间 120 秒只保证一定的退出时间，不保证正在执行的 job 完成。
升级、重启和维护前，在 GitHub 确认 runner 空闲，并暂停可能投递新 job 的工作流。
操作系统及构建工具不会随 runner 自动更新，仍需定期重建镜像并升级 image tag/digest。

## 主要配置

| 配置                                            | 默认值 / 说明                                                     |
| ----------------------------------------------- | ----------------------------------------------------------------- |
| `image.repository`, `image.tag`             | 自行构建并推送的镜像，默认 `github-persistent-runner:0.1.0`     |
| `image.digest`                                | 可选 SHA256 digest，设置后优先于 tag                              |
| `github.repo`                                 | `cn-ph-spm/spm`，仅支持 github.com 仓库级 runner                |
| `github.existingSecret`, `github.tokenKey`  | `github-runner-registration` / `token`                        |
| `runner.name`                                 | 留空使用 Helm release 名称；同一仓库内须唯一                      |
| `runner.labels`                               | `k8s,ubuntu24`，保留默认 self-hosted / Linux / X64 标签         |
| `runner.workDir`                              | `_work`，必须是安装目录下的单层相对目录，不能是 bin / externals |
| `persistence.existingClaim`                   | 非空时复用同 namespace 的 PVC，不创建或管理该 PVC                 |
| `persistence.storageClass`                    | null 使用默认；空字符串显式禁用 StorageClass；也可填具体名称      |
| `persistence.size`                            | `20Gi`；已有 PVC 扩容取决于 StorageClass，不支持缩容            |
| `persistence.retain`                          | true，卸载时保留 Chart 创建的 PVC；不改变底层 PV reclaim policy   |
| `resources`                                   | requests 250m / 512Mi，limits 2 CPU / 4Gi，按构建负载调整         |
| `terminationGracePeriodSeconds`               | 120                                                               |
| `nodeSelector`, `tolerations`, `affinity` | 节点调度；当前实现只支持 Linux amd64                              |

## 恢复、配置变更与卸载

- **token 过期 / 缺失**：空 PVC 注册前需要更新 Secret。通过相同的交互读取流程获取新值，
  将 Secret 创建命令加 `--dry-run=client -o yaml | kubectl apply -f -`，然后重启 Deployment。
  已注册 PVC 重启不依赖此 Secret，可以删除过期的注册 Secret。
- **注册失败或凭据不完整**：启动会保留现场并报错，不自动删除凭据或抢占同名 runner。
  先检查 GitHub 端是否留下注册记录，再按下面维护流程解除注册并重试。
- **修改仓库、名称、标签、工作目录**：这些配置注册时写入；直接修改 values 会明确报错。
  恢复原值即可继续使用；确需更改则先维护并重新注册。在线手动修改 GitHub 标签后，本地摘要不感知该修改。
- **runner 在 GitHub 被删除**：本地凭据不会自动失效清理，需要重新注册。
- **不完整安装 / 原先手工安装的目录**：不会直接覆盖；先备份和检查，再在停机期间将原目录
  移到备份位置或更换 PVC。旧草案安装目录没有本实现的完成标记，不能直接接管。

维护步骤：

1. 确认无正在执行的 job，暂停新任务，执行
   `kubectl scale deployment/spm-runner-runner -n github-runner --replicas=0`，等待原 Pod 完全退出。
2. 备份 PVC。用临时维护 Pod 挂载同一个 PVC 到 `/persistent`，使用相同 runner 镜像、
   UID/GID 1001 和 `fsGroup: 1001`，将 command 覆盖为 `[/bin/bash, -c, "sleep infinity"]`。
   此时不要同时运行 runner Pod。
3. 在维护 Pod 的 `/persistent/gh-runner` 执行 `./config.sh remove --token <REMOVE_TOKEN>`；
   从 GitHub 的 Remove runner 流程取得移除 token，使用 shell 交互变量传入，避免写入命令历史。
   成功后删除 `.chart-registration` 和可能残留的 `.chart-registration.tmp`。
   若无法正常移除，先在 GitHub 清理旧记录，再备份并重命名整个 `gh-runner` 目录，使用全新目录初始化。
4. 删除维护 Pod，准备有效的**注册 token**，更新配置，通过上述 `helm upgrade --install` 恢复一个副本。

不要每次 Pod 退出都注销 runner。永久下线时，按维护步骤移除 GitHub 注册后卸载：

```bash
helm uninstall spm-runner -n github-runner
```

默认 PVC `spm-runner-runner` 保留，其中含注册凭据、源码及缓存，应按凭据数据管理访问和备份。
重新安装时显式指定 `--set persistence.existingClaim=spm-runner-runner`，并使用原 runner 名称及设置。
永久删除存储前确认备份及 PV 回收策略，再单独删除 PVC。

长期运行的 `_work`、`_diag`、工具缓存和旧更新目录会占用磁盘；需要监控 PVC 容量。
当前没有自动清理器，维护期间按业务需要清理确认不再使用的数据，不能删除活动版本或凭据。
运行任意 workflow 的进程可访问 runner 凭据及共享工作区，此部署仅用于可信仓库和可信任务。

## 验证与发布 Chart

在 WSL Bash 中执行；仓库没有 Python 虚拟环境时，测试只使用系统 Python 标准库：

```bash
bash -n image/entrypoint.sh
helm lint . --strict
python3 -m unittest discover -s tests -v
helm template smoke . -f value.yaml > /tmp/github-runner.yaml
helm package . --destination dist
```

本地测试模拟下载和注册，检查持久化恢复、校验失败、文件锁、配置变化及 Helm 渲染；
不能替代真实容器、存储驱动和 GitHub 端到端验证。部署验收包括：

1. 构建镜像，验证非 root 启动及运行库依赖，确认 runner Online 并完成示例 workflow。
2. 空闲时删除 Pod，确认新 Pod 使用同一个 runner ID，且没有再次下载初始安装包。
3. runner 实际自动更新后记录 `bin/Runner.Listener --version`，重建 Pod，确认版本保持，重新完成 workflow。
4. 在空闲时执行 Helm 镜像升级，确认无两个 runner 同时运行；测试 PVC 保留及复用恢复。

HTTP Chart 仓库：将 `dist/` 中生成的包和索引发布到自己的静态站点。

```bash
helm repo index dist --url https://charts.example.com
```

发布已有仓库的新版本时应合并原索引，避免丢失历史版本。也可使用 OCI：

```bash
helm push dist/github-runner-0.1.0.tgz oci://registry.example.com/charts
```

上述地址都是占位地址，不会自动发布。镜像和 Chart 分别发布，修改入口脚本后必须重新构建镜像。
每次正式发布增加 Chart version；修改镜像时也增加镜像 tag，并同步 `values.yaml`。

参考：[注册 runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners)、
[runner 参数](https://github.com/actions/runner/blob/v2.337.0/src/Runner.Listener/Runner.cs)、
[官方运行脚本](https://github.com/actions/runner/blob/v2.337.0/src/Misc/layoutroot/run.sh)。
