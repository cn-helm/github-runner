# GitHub persistent runner

在 Kubernetes 上部署一个长期运行、仓库级别的 GitHub Actions runner。
直接使用公共镜像 `public.ecr.aws/ubuntu/ubuntu:noble`，无需构建或推送自定义镜像。
Ubuntu 24.04 / Linux amd64，依赖安装完成后以 UID/GID 1001 运行 runner。整个 runner 安装目录、
注册凭据和工作区都存放在 PVC 中，支持 runner 自身自动更新以及 Pod 重建后恢复。

Chart 名称为 `github-runner`。这是单 runner Chart，不包含自动扩容、GitHub App 自动签发注册 token
或构建语言 SDK。工作流需要的额外工具可通过 workflow 安装到用户可写目录。

## 文件

- `values.yaml`：Helm 默认配置。旧的 `value.yaml` 保留为空覆盖文件，原有 `-f value.yaml` 用法仍可用。
- `scripts/download-dependencies.sh`：initContainer 下载依赖包。
- `scripts/start-container.sh`：主容器离线安装依赖并切换用户。
- `scripts/runner.sh`：保留原持久化初始化、注册及运行逻辑。
- `templates/`：单副本 Deployment、PVC、脚本 ConfigMap 和安装提示，无 Service / Ingress。
- `tests/test_runner.py`：不访问 GitHub、不使用真实 token 的本地验证。

## 公共镜像与初始化

两个容器使用同一个 Ubuntu Noble 镜像。initContainer 的根文件系统不会传递给主容器，
所以采用共享软件包的方式，而不是只在 initContainer 中执行 `apt install`：

1. `dependencies` initContainer 更新 Ubuntu 官方软件源索引，将 runner 系统依赖及工具的
   `.deb` 包下载到 `/packages`（`emptyDir`）。记录基础镜像 dpkg 状态和软件包 SHA256，完成后写入标记。
2. 主容器确认标记、基础镜像状态和软件包校验值，将包复制到容器自身的 APT 缓存目录，
   再使用 `apt-get --no-download` 从本地包安装。
   此阶段不下载依赖；APT 按依赖关系安排安装顺序。
3. 创建 runner 用户，以 `setpriv` 切换到 UID/GID 1001，再通过 tini 启动 runner。

依赖准备及安装需要 root，dependencies 和 runner 容器不是 privileged；runner 和 workflow 进程使用非 root 用户。
默认启用的 Docker sidecar 使用 privileged，集群准入策略必须允许；拥有 Docker socket 访问权的工作流也能控制该特权 daemon，仅运行可信工作流。
集群若强制所有容器 `runAsNonRoot`，该方案无法启动。
通过 `kubectl exec` 新建的进程仍按容器配置使用 root；维护命令应显式切换到 runner 用户，见后文。

`/packages` 只在当前 Pod 内保留，主容器只读挂载。主容器重启会重新离线安装；Pod 重建会重新下载依赖。
这会增加启动耗时，并要求每次创建 Pod 时能访问 Ubuntu 软件源。PVC 内的 runner 程序和注册状态不受影响。
默认 `IfNotPresent`，可通过 `image.digest` 固定 Noble 镜像以获得一致的基础环境。
如果可变标签在同一 Pod 的两个容器中解析为不同的软件包状态，启动会明确失败，需固定 digest 后重建 Pod。

默认安装 runner 系统依赖以及 Bash、Git、curl、jq、SSH 客户端等工具。
同时从 Ubuntu Noble 软件源安装 `docker.io`（提供 Docker CLI，也包含 daemon 文件）、
`docker-buildx`。从 0.4.1 起，AWS CLI v2 使用 [AWS 官方 ZIP 安装器](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html)，不依赖 APT 的 `awscli` 包。
initContainer 先缓存全部 Debian 依赖，再离线安装 curl、CA 证书和 unzip，随后从
`https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip` 下载 Linux x86_64 安装包。
主容器校验共享缓存后，离线解压并运行 `aws/install`，安装到 `/usr/local/aws-cli`，
命令链接放在 `/usr/local/bin`，执行 `aws --version` 后清理临时解压目录。
ZIP 保留在当前 Pod 的共享临时卷中，供主容器重启使用，Pod 删除时一起回收。
初始化阶段还需能通过 HTTPS 访问 `awscli.amazonaws.com`；该 URL 跟随最新 v2，未固定 AWS CLI 版本。
缓存 SHA256 用于检测下载完成后的文件损坏，不替代 AWS 发布签名验证。
Docker CLI 和 buildx 仍与系统依赖一起从 Ubuntu 软件源下载、校验并离线安装；继续使用 Ubuntu Noble 镜像。
Chart 默认启动 `public.ecr.aws/docker/library/docker:dind` sidecar，供 Docker CLI / buildx 构建镜像。
推送 ECR 仍需在 workflow 中获取 AWS 权限并登录目标仓库。
`ubuntu24` 只是自定义匹配标签，不代表具有 GitHub 托管 runner 的完整工具集。

## Docker sidecar（0.4.0）

`dind.enabled` 默认 true。runner 继续使用 Ubuntu，daemon 单独使用 `dind.image`。
两个容器共享 `/var/run/docker/docker.sock`，runner 自动设置 `DOCKER_HOST`；daemon 使用 `--group=1001`
允许 runner 用户访问。显式传入 dockerd 命令，仅监听 Unix socket，不开放 2375/2376 TCP。
runner 注册前以 UID 1001 等待 `docker info` 成功，默认最多约 180 秒（可用 `dind.startupTimeoutSeconds` 调整），
失败后退出并提示检查 sidecar 日志；maintenance 模式不执行此等待。就绪探针持续检查 daemon。
daemon 后续故障会使 Pod NotReady，但不会自动暂停已运行的 GitHub runner；此时任务可能失败。

`/var/lib/docker` 使用 emptyDir：容器重启保留，Pod 重建后镜像和构建缓存清空，消耗节点临时磁盘。
两个容器也以相同路径挂载 `/persistent`，支持工作区 bind mount。其他路径不会自动共享；
容器 actions、job containers 和 service containers 的额外挂载及网络需求尚未进行端到端验证。
普通 sidecar 与 runner 在 Pod 终止时没有严格退出顺序，升级前需确认 runner 空闲。

daemon 拉取镜像如需代理，单独使用 `dind.extraEnv` 配置 `HTTP_PROXY`、`HTTPS_PROXY`、`NO_PROXY`，
格式与 `extraEnv` 相同，可使用 Secret 引用。runner 的 `extraEnv` 不传入 daemon；Dockerfile 内部下载
依赖所需的构建代理也应由 workflow 配置。`dind.resources` 单独设置 daemon 的 CPU/内存。
可设置 `dind.enabled: false` 禁用 sidecar；此时不会设置 Docker 地址或等待 daemon。

升级后检查（替换 namespace / Deployment 名称）：

```bash
kubectl logs -n github-runner deployment/spm-runner-runner -c docker
kubectl exec -n github-runner deployment/spm-runner-runner -c runner -- \
  setpriv --reuid=1001 --regid=1001 --init-groups docker info
```

随后在 workflow 中执行 `docker buildx version` 和实际构建，验证构建及 ECR 网络权限。
本地已有 DinD 镜像时可运行 `bash tests/dind-smoke.sh`：创建临时特权 daemon，
用 UID/GID 1001 的独立客户端通过共享 socket 构建 scratch 镜像，全程不访问镜像仓库，结束后清理临时容器及匿名卷。

## 首次安装

需要 Kubernetes、Helm、可写的 PVC、节点到公共镜像仓库的访问，以及 Pod 到 Ubuntu 官方软件源、
GitHub 下载和 Actions 服务的网络访问。无需本地 Docker。
StorageClass 应支持 UID/GID 1001 写入、目录原子重命名及跨 Pod 有效的 `flock` 文件锁。
若存储驱动不应用 `fsGroup`，应由存储管理员预置目录权限。

在 GitHub 仓库 Settings → Actions → Runners → New self-hosted runner 获取**注册 token**，
不是 PAT。注册 token 通常一小时过期，应临近首次启动时获取。

从 Chart 0.4.2 起，在 Rancher 安装/升级应用的 **Customize** 页面，展开 **GitHub Runner**，
填写仓库和密码输入框 **GITHUB_RUNNER_TOKEN** 即可，无需预先创建 Secret。
该字段对应 `github.token`；非空时优先使用它，留空时回退到 `github.existingSecret`。
两者都为空时不注入 token，适用于已有注册状态的 PVC。

命令行也可以直接传入同一字段（`xxx` 替换为有效注册 token）：

```bash
helm upgrade --install spm . -n github-runner --create-namespace \
  -f runner.local.yaml --set github.token=xxx
```

直接输入真实值会进入 shell 历史；需要避免历史记录时可用 `read -rsp 'Runner token: ' RUNNER_TOKEN`，
再传入 `--set-string github.token="$RUNNER_TOKEN"`，执行完 `unset RUNNER_TOKEN`。

注册成功后，可以在 Rancher 升级应用时清空 token；runner 会继续复用 PVC 中的凭据。
密码输入框只遮盖界面显示：token 仍保存在 Helm values/release 历史和 Deployment 环境变量中。
清空仅更新当前配置，不会清除旧 release 或旧 ReplicaSet 中的记录。不要将真实 token 提交到 Git。

若仍使用 Secret 注入，可按以下兼容方式安装；在 UI 直接填写 token 时跳过这一步：

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
  repository: public.ecr.aws/ubuntu/ubuntu
  tag: noble
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

如果 Pod 停留在 Init 阶段，查看依赖下载日志：

```bash
kubectl logs -n github-runner deployment/spm-runner-runner -c dependencies -f
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
          docker --version
          docker buildx version
          aws --version
          echo "Persistent runner is working"
```

受限网络下，在安装前为 initContainer 和主容器设置代理（集群内必须能访问该代理）：

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
同一份 `extraEnv` 传给两个容器，initContainer 会将大写代理变量映射到 APT 使用的小写变量；
代理设置也会被 workflow 子进程继承。镜像拉取由节点的容器运行时负责，Pod 环境变量不配置节点代理。
代理不可用或遇到登录、认证、证书问题时停止相关操作，准备可用网络、权限或组织 CA，
切换网络并确认后继续，不自动换源或更换依赖版本。
APT 下载没有脚本内重试或源回退；Kubernetes 会按重启策略重启失败的 initContainer。
排查网络期间可将 Deployment 缩容为 0，避免持续尝试。

## 启动与更新

每次 Pod 创建先完成上述依赖阶段，随后以下步骤以 runner 用户执行：

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
操作系统及构建工具不会随 runner 自动更新；新 Pod 会从配置的软件源重新解析依赖版本，
基础镜像则按 `image.pullPolicy` 和 tag/digest 获取。当前不承诺 APT 包版本完全可重复。
更新基础镜像应更新 Noble digest 或明确拉取策略，不需要构建自定义镜像。

## 主要配置

| 配置                                            | 默认值 / 说明                                                     |
| ----------------------------------------------- | ----------------------------------------------------------------- |
| `image.repository`, `image.tag`             | 公共镜像 `public.ecr.aws/ubuntu/ubuntu:noble`，两个容器共用 |
| `image.digest`                                | 可选 SHA256 digest，设置后优先于 tag                              |
| `github.repo`                                 | `cn-ph-spm/spm`，仅支持 github.com 仓库级 runner                |
| `github.token` | Rancher Customize 的 GITHUB_RUNNER_TOKEN，默认空；填写后优先于 Secret |
| `github.existingSecret`, `github.tokenKey`  | `github-runner-registration` / `token`                        |
| `runner.name`                                 | 留空使用 Helm release 名称；同一仓库内须唯一                      |
| `runner.labels`                               | `k8s,ubuntu24`，保留默认 self-hosted / Linux / X64 标签         |
| `runner.workDir`                              | `_work`，必须是安装目录下的单层相对目录，不能是 bin / externals |
| `persistence.existingClaim`                   | 非空时复用同 namespace 的 PVC，不创建或管理该 PVC                 |
| `persistence.storageClass`                    | null 使用默认；空字符串显式禁用 StorageClass；也可填具体名称      |
| `persistence.size`                            | `20Gi`；已有 PVC 扩容取决于 StorageClass，不支持缩容            |
| `persistence.retain`                          | true，卸载时保留 Chart 创建的 PVC；不改变底层 PV reclaim policy   |
| `resources`                                   | requests 250m / 512Mi，limits 2 CPU / 4Gi，按构建负载调整         |
| `initResources` | initContainer 默认 requests 250m / 256Mi，limits 1 CPU / 1Gi |
| `runner.maintenance` | false；true 时准备依赖后以 UID 1001 等待，不注册、不接收任务 |
| `terminationGracePeriodSeconds`               | 120                                                               |
| `nodeSelector`, `tolerations`, `affinity` | 节点调度；当前实现只支持 Linux amd64                              |

## 恢复、配置变更与卸载

- **token 过期 / 缺失**：在 Rancher Customize 填写新的注册 token 并升级应用。使用 Secret 时，空 PVC 注册前需要更新 Secret。通过相同的交互读取流程获取新值，
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
2. 备份 PVC。使用同一个 release 和原覆盖文件启用维护模式：
   `helm upgrade spm-runner . -n github-runner -f runner.local.yaml --set runner.maintenance=true`。
   Chart 恢复一个 Pod，准备依赖后只执行 sleep，不连接 GitHub。
   使用 `kubectl exec -it -n github-runner deployment/spm-runner-runner -c runner -- setpriv --reuid=1001 --regid=1001 --init-groups --no-new-privs /bin/bash`
   进入非 root 维护终端。
3. 在 `/persistent/gh-runner` 执行 `./config.sh remove --token <REMOVE_TOKEN>`；
   从 GitHub 的 Remove runner 流程取得移除 token，使用 shell 交互变量传入，避免写入命令历史。
   成功后删除 `.chart-registration` 和可能残留的 `.chart-registration.tmp`。
   若无法正常移除，先在 GitHub 清理旧记录，再备份并重命名整个 `gh-runner` 目录，使用全新目录初始化。
4. 准备有效的**注册 token**，更新配置，执行
   `helm upgrade spm-runner . -n github-runner -f runner.local.yaml --set runner.maintenance=false`
   退出维护模式并恢复 runner。

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
for script in scripts/*.sh; do bash -n "$script"; done
helm lint . --strict
python3 -m unittest discover -s tests -v
helm template smoke . -f value.yaml > /tmp/github-runner.yaml
helm package . --destination dist
```

本地测试模拟下载和注册，检查持久化恢复、校验失败、文件锁、配置变化及 Helm 渲染；
不能替代真实容器、存储驱动和 GitHub 端到端验证。部署验收包括：

1. 确认 initContainer 完成下载、主容器完成离线安装，runner Online，示例 workflow 中 `id` 输出 UID 1001。
2. 空闲时删除 Pod，确认新 Pod 使用同一个 runner ID，且没有再次下载初始安装包。
3. runner 实际自动更新后记录 `bin/Runner.Listener --version`，重建 Pod，确认版本保持，重新完成 workflow。
4. 在空闲时执行 Helm 镜像升级，确认无两个 runner 同时运行；测试 PVC 保留及复用恢复。

可选的真实容器冒烟测试（需要 Docker，无需 GitHub token）：

```bash
export HTTP_PROXY=http://165.225.112.16:10015 HTTPS_PROXY=http://165.225.112.16:10015
docker pull public.ecr.aws/ubuntu/ubuntu:noble
bash tests/container-smoke.sh
```

该测试用两个独立容器共享临时卷，主容器禁用网络以验证离线安装，检查系统依赖、Docker CLI、buildx、AWS CLI v2 和 UID/GID 1001，
结束后清理测试容器和临时卷。它不注册 GitHub runner，也不访问现有 PVC。

### GitHub Pages 自动发布

`.github/workflows/publish-helm-repository.yaml` 沿用 dbgate 的发布流程。
推送到 `main` 且修改 Chart、values、schema、Customize 表单、脚本、模板或文档时自动触发，
也可在 GitHub Actions 页面手动运行 **Publish Helm repository**。
流程先执行 `helm lint . --strict`，再打包 Chart，合并现有索引，并发布到 `gh-pages` 分支。
首次发布时允许 `gh-pages` 不存在；后续发布保留旧版本安装包。

仓库需允许 Actions 写入内容，并在 **Settings → Pages** 设置
**Deploy from a branch → gh-pages → / (root)**（首次运行创建分支后设置）。
仓库地址自动生成为 `https://<GitHub owner>.github.io/<repository name>`，可用于 Rancher Chart 仓库。
发布新内容前递增 `Chart.yaml` 的 `version`，避免覆盖同版本安装包。
`.helm-repository/` 是本地发布暂存目录，已从 Git 和 Chart 打包中排除。

### 手动发布

HTTP Chart 仓库：将 `dist/` 中生成的包和索引发布到自己的静态站点。

```bash
helm repo index dist --url https://charts.example.com
```

发布已有仓库的新版本时应合并原索引，避免丢失历史版本。也可使用 OCI：

```bash
helm push dist/github-runner-0.4.2.tgz oci://registry.example.com/charts
```

上述发布地址都是占位地址，不会自动发布。脚本随 Chart 打包，通过 ConfigMap 挂载；
修改脚本后执行 Helm 升级即可，Pod 模板中的脚本校验值变化会触发重建，无需构建镜像。
每次正式发布增加 Chart version。升级到 0.2.0 时移除旧覆盖文件中的自定义镜像地址，
确保最终使用 Noble 公共镜像；保持原 runner 名称、注册设置及 PVC，即可复用已有状态。

参考：[注册 runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners)、
[runner 参数](https://github.com/actions/runner/blob/v2.337.0/src/Runner.Listener/Runner.cs)、
[官方运行脚本](https://github.com/actions/runner/blob/v2.337.0/src/Misc/layoutroot/run.sh)、
[Kubernetes initContainers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/)。
