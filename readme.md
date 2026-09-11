# GitHub persistent runner

English | [简体中文](readme.zh.md)

Deploy a long-running, repository-level GitHub Actions runner on Kubernetes. The chart uses the public `public.ecr.aws/ubuntu/ubuntu:noble` image directly, without building or pushing a custom image. It runs on Ubuntu 24.04 / Linux amd64. After dependency installation, the runner runs as UID/GID 1001. The entire runner installation, registration credentials, and workspace reside on a PVC, allowing automatic runner updates and recovery after Pod recreation.

The chart is named `github-runner`. It deploys one runner and does not include autoscaling, automatic registration token issuance through a GitHub App, or language SDKs. Workflows can install additional tools in user-writable directories.

## Files

- `values.yaml`: Helm defaults. The legacy `value.yaml` remains an empty override file, so existing `-f value.yaml` commands still work.
- `scripts/download-dependencies.sh`: downloads dependency packages in the initContainer.
- `scripts/start-container.sh`: installs dependencies offline in the main container and switches users.
- `scripts/runner.sh`: handles persistent initialization, registration, and execution.
- `templates/`: a single-replica Deployment, PVC, script ConfigMap, and installation notes. No Service or Ingress is required.
- `tests/test_runner.py`: local checks that do not contact GitHub or use real tokens.
- `readme.md` / `readme.zh.md`: English and Simplified Chinese documentation.
- `docs/render.py`: renders both READMEs as static GitHub Pages documentation.

## Public image and initialization

The dependency initContainer and runner container use the same Ubuntu Noble image. An initContainer's root filesystem is not shared with the main container, so the chart shares downloaded packages rather than merely running `apt install` in the initContainer:

1. The `dependencies` initContainer updates the official Ubuntu package indexes and downloads system dependencies and tools as `.deb` files into `/packages`, an `emptyDir` volume. It records the base image's dpkg state and package SHA256 checksums, then writes a completion marker.
2. The main container checks the marker, base image state, and checksums, copies packages into its own APT cache, and installs them with `apt-get --no-download`. No dependencies are downloaded during this stage; APT determines the installation order.
3. It creates the runner user, switches to UID/GID 1001 using `setpriv`, and starts the runner through tini.

Dependency preparation and installation require root. The dependencies and runner containers are not privileged; runner and workflow processes run as a non-root user. The Docker sidecar, enabled by default, is privileged and requires admission policies that permit it. Workflows with Docker socket access can control that privileged daemon, so run only trusted workflows. This setup cannot start in a cluster that enforces `runAsNonRoot` for every container. Processes started through `kubectl exec` still run as root according to the container configuration; explicitly switch users for maintenance commands, as shown below.

`/packages` lasts for the lifetime of the Pod and is mounted read-only in the main container. A main-container restart reinstalls dependencies offline; Pod recreation downloads them again. This increases startup time and requires access to Ubuntu repositories whenever a Pod is created. The runner program and registration state on the PVC remain intact. The default image pull policy is `IfNotPresent`; set `image.digest` to pin the Noble base image. If a mutable tag resolves to different package states for the two containers in a Pod, startup fails explicitly; pin the digest and recreate the Pod.

Default tools include the runner's system dependencies, Bash, Git, curl, jq, and an SSH client. The chart also installs `docker.io` (including the Docker CLI and daemon files) and `docker-buildx` from Ubuntu Noble repositories.

Starting with 0.4.1, AWS CLI v2 uses the [official AWS ZIP installer](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), without depending on the APT `awscli` package. The initContainer caches all Debian dependencies before installing curl, CA certificates, and unzip offline, then downloads the Linux x86_64 installer from `https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip`. After verifying the shared cache, the main container extracts the archive and runs `aws/install` offline, installing into `/usr/local/aws-cli` with command links in `/usr/local/bin`. It runs `aws --version` and removes the temporary extraction directory. The ZIP remains in the Pod's shared temporary volume for container restarts and is removed with the Pod.

Initialization also requires HTTPS access to `awscli.amazonaws.com`. This URL tracks the latest v2 release; the AWS CLI version is not pinned. Cache SHA256 checks detect corruption after download and do not replace AWS publisher signature verification. Docker CLI and buildx continue to be downloaded, checked, and installed offline with Ubuntu system dependencies; the base image remains Ubuntu Noble.

By default, the chart starts a `public.ecr.aws/docker/library/docker:dind` sidecar for Docker CLI / buildx image builds. Pushing to ECR still requires the workflow to obtain AWS permissions and log in to the target registry. `ubuntu24` is only a custom matching label; it does not imply the full GitHub-hosted runner toolset.

## Docker sidecar (0.4.0)

`dind.enabled` defaults to true. The runner continues to use Ubuntu, while the daemon uses `dind.image`. Both containers share `/var/run/docker/docker.sock`. The chart sets `DOCKER_HOST` for the runner, and the daemon uses `--group=1001` to grant the runner user access. An explicit dockerd command makes it listen only on the Unix socket, without TCP listeners on ports 2375/2376.

Before registration, the runner waits as UID 1001 for `docker info` to succeed, for approximately 180 seconds by default; configure this with `dind.startupTimeoutSeconds`. On failure, it exits and directs you to the sidecar logs. Maintenance mode skips this wait. A readiness probe continuously checks the daemon. A later daemon failure makes the Pod NotReady but does not automatically pause an already-running GitHub runner; jobs may fail.

`/var/lib/docker` uses `emptyDir`: it survives container restarts, but images and build caches are lost on Pod recreation and consume node ephemeral storage. Both containers also mount `/persistent` at the same path to support workspace bind mounts. Other paths are not shared automatically. Additional mount and network requirements for container actions, job containers, and service containers have not been validated end to end. The regular sidecar and runner have no strict shutdown ordering; confirm the runner is idle before upgrades.

If the daemon needs a proxy to pull images, configure `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` separately through `dind.extraEnv`. Its format matches `extraEnv` and supports Secret references. The runner's `extraEnv` is not passed to the daemon. Workflows must also configure build proxies for dependencies downloaded inside Dockerfiles. Use `dind.resources` to set daemon CPU and memory independently. Set `dind.enabled: false` to disable the sidecar, Docker address, and daemon startup wait.

After upgrading, check the following, substituting your namespace and Deployment name:

```bash
kubectl logs -n github-runner deployment/${repo}-runner-runner -c docker
kubectl exec -n github-runner deployment/${repo}-runner-runner -c runner -- \
  setpriv --reuid=1001 --regid=1001 --init-groups docker info
```

Then run `docker buildx version` and an actual build in a workflow to verify building and ECR network permissions. If the DinD image is already available locally, run `bash tests/dind-smoke.sh`. It creates a temporary privileged daemon and uses a separate UID/GID 1001 client to build a scratch image through the shared socket without registry access. Temporary containers and anonymous volumes are cleaned up afterward.

## First installation

You need Kubernetes, Helm, a writable PVC, node access to the public image registry, and Pod access to official Ubuntu repositories, GitHub downloads, and Actions services. Local Docker is not required. The StorageClass should support UID/GID 1001 writes, atomic directory renames, and effective cross-Pod `flock` locks. If the driver does not apply `fsGroup`, a storage administrator must prepare directory permissions.

Obtain a **registration token**, not a PAT, from the repository's Settings → Actions → Runners → New self-hosted runner page. Registration tokens usually expire after one hour, so obtain one shortly before startup.

Starting with Chart 0.4.2, open **GitHub Runner** on Rancher's **Customize** page when installing or upgrading an application. Fill in the repository and **GITHUB_RUNNER_TOKEN** password field; no pre-created Secret is required. The field maps to `github.token`: a non-empty value takes precedence, while an empty value falls back to `github.existingSecret`. If both are empty, no token is injected, which is suitable for an already-registered PVC.

You can also supply the same field on the command line; replace `xxx` with a valid registration token:

```bash
helm upgrade --install ${repo} . -n github-runner --create-namespace \
  -f runner.local.yaml --set github.token=xxx
```

Typing the actual value directly saves it in shell history. To avoid this, use `read -rsp 'Runner token: ' RUNNER_TOKEN`, pass `--set-string github.token="$RUNNER_TOKEN"`, and run `unset RUNNER_TOKEN` afterward.

After registration succeeds, you can clear the token when upgrading the application in Rancher. The runner continues to reuse credentials on the PVC. The password field only masks the UI: the token is still saved in Helm values/release history and the Deployment environment. Clearing it changes the current configuration without removing older release or ReplicaSet records. Do not commit real tokens to Git.

To continue using Secret injection, use this compatible setup. Skip it when entering the token directly in the UI:

```bash
kubectl create namespace github-runner
read -rsp 'Runner registration token: ' RUNNER_TOKEN
printf '\n'
printf '%s' "$RUNNER_TOKEN" | kubectl create secret generic github-runner-registration \
  -n github-runner --from-file=token=/dev/stdin
unset RUNNER_TOKEN
```

Create `runner.local.yaml`, which is excluded by `.gitignore`:

```yaml
image:
  repository: public.ecr.aws/ubuntu/ubuntu
  tag: noble
github:
  repo: org/repo
  existingSecret: github-runner-registration
runner:
  name: ${repo}-k8s-runner
  labels: k8s,ubuntu24
persistence:
  size: 20Gi
  # storageClass: your-storage-class
```

```bash
helm lint . --strict -f runner.local.yaml
helm upgrade --install ${repo}-runner . -n github-runner -f runner.local.yaml
kubectl logs -n github-runner deployment/${repo}-runner-runner -f
```

If the Pod remains in the Init phase, inspect the dependency logs:

```bash
kubectl logs -n github-runner deployment/${repo}-runner-runner -c dependencies -f
```

Confirm the runner is Online in GitHub, then run a workflow:

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

In a restricted network, configure the initContainer and main-container proxy before installation. The proxy must be reachable from the cluster:

```yaml
extraEnv:
  - name: http_proxy
    value: http://111.222.333.444:10015
  - name: https_proxy
    value: http://111.222.333.444:10015
  - name: no_proxy
    value: localhost,127.0.0.1,.svc,.cluster.local
```

For proxies with credentials, use `extraEnv[].valueFrom.secretKeyRef` instead of passwords in values. Both containers receive the same `extraEnv`; the initContainer maps uppercase proxy variables to APT's lowercase variables. Workflow subprocesses inherit these settings. Image pulls are handled by the node's container runtime; Pod environment variables do not configure node proxies.

If the proxy is unavailable or login, authentication, or certificate issues arise, stop affected operations and prepare the necessary network access, permissions, or organizational CA. Switch networks and confirm before continuing; do not automatically change package sources or dependency versions. APT downloads have no script-level retries or source fallback. Kubernetes restarts failed initContainers according to its restart policy. Scale the Deployment to zero during network diagnosis to avoid continued attempts.

## Startup and updates

Each new Pod completes dependency preparation first. These steps then run as the runner user:

1. Acquire `/persistent/.runner.lock`. A second runner using the same PVC fails immediately.
2. On first installation, download into `/persistent/.runner-install`, strictly verify SHA256, then atomically move the completed extraction to `/persistent/gh-runner`. If downloading or extraction fails, the next startup cleans the staging directory while holding the lock and installs again.
3. On first registration, run `config.sh --unattended --work _work`, saving a record of registration settings and the GitHub credentials.
4. Once registered, reuse credentials without a valid registration token or another download/install. Remove the registration token from the environment before execution.
5. Run the official `run.sh` with TERM/INT forwarding. Tini manages PID 1 and orphan processes. Keep default automatic updates enabled; neither `--disableupdate` nor `--ephemeral` is used.

`runner.initVersion` and `runner.sha256` **only control the first installation on an empty PVC**. Changing them does not overwrite or downgrade an existing installation. Initial values come from the [official v2.337.0 release](https://github.com/actions/runner/releases/tag/v2.337.0) for Linux x64. The entire installation directory must be writable, including hidden credentials, update-created version directories, `_diag`, and `_work`. These files survive Pod restarts and Helm image upgrades.

The Deployment has one replica and uses `Recreate`, causing brief upgrade downtime. RWO does not mean only one Pod, hence the additional file lock. Do not scale it manually or forcibly delete an old Pod that is still running. Other workloads should not use the same PVC. The runner has no liveness/readiness probes by default to avoid interruption during downloads and updates. Kubernetes Ready status or Helm `--wait` does not mean GitHub Online; check GitHub status and an actual workflow.

The default 120-second termination grace period provides time to exit but does not guarantee an active job finishes. Before upgrades, restarts, or maintenance, confirm the runner is idle in GitHub and pause workflows that may dispatch new jobs. The operating system and build tools do not update with the runner. New Pods resolve dependencies again from configured repositories; the base image follows `image.pullPolicy` and its tag/digest. APT package versions are not guaranteed to be fully reproducible. Update the Noble digest or explicitly configure the pull policy to update the base image; no custom build is required.

## Main configuration

| Setting | Default / description |
| --- | --- |
| `image.repository`, `image.tag` | Public `public.ecr.aws/ubuntu/ubuntu:noble`, shared by the dependency and runner containers |
| `image.digest` | Optional SHA256 digest, taking precedence over tag |
| `github.repo` | `org/repo`; only repository-level runners on github.com are supported |
| `github.token` | GITHUB_RUNNER_TOKEN in Rancher Customize; empty by default; overrides the Secret when filled |
| `github.existingSecret`, `github.tokenKey` | `github-runner-registration` / `token` |
| `runner.name` | Uses the Helm release name when empty; must be unique within the repository |
| `runner.labels` | `k8s,ubuntu24`; retains default self-hosted / Linux / X64 labels |
| `runner.workDir` | `_work`; one relative directory under the installation, excluding bin / externals |
| `persistence.existingClaim` | Reuse a PVC in the same namespace when non-empty, without creating or managing it |
| `persistence.storageClass` | null uses the cluster default; an empty string explicitly disables StorageClass selection; a class name may also be specified |
| `persistence.size` | `20Gi`; expanding existing PVCs depends on the StorageClass; shrinking is unsupported |
| `persistence.retain` | true; retain chart-created PVCs on uninstall without changing the underlying PV reclaim policy |
| `resources` | Requests: 250m / 512Mi; limits: 2 CPU / 4Gi. Adjust for your builds |
| `initResources` | InitContainer requests: 250m / 256Mi; limits: 1 CPU / 1Gi |
| `runner.maintenance` | false; when true, prepare dependencies and wait as UID 1001 without registering or accepting jobs |
| `terminationGracePeriodSeconds` | 120 |
| `nodeSelector`, `tolerations`, `affinity` | Node scheduling; only Linux amd64 is supported |

## Recovery, configuration changes, and uninstall

- **Expired or missing token:** enter a new registration token in Rancher Customize and upgrade. For Secret-based installation, update the Secret before registering an empty PVC. Use the same interactive input procedure, add `--dry-run=client -o yaml | kubectl apply -f -` to the Secret creation command, and restart the Deployment. Registered PVCs do not need this Secret on restart, so expired registration Secrets can be deleted.
- **Failed registration or incomplete credentials:** startup preserves the files and reports an error, without deleting credentials or taking over a same-name runner. Check GitHub for leftover registrations, then unregister and retry using the maintenance procedure.
- **Changed repository, name, labels, or work directory:** these are written during registration. Direct changes to values produce an explicit error. Restore the original values to resume, or perform maintenance and re-register if the change is intentional. The local record does not detect labels changed manually in GitHub.
- **Runner deleted in GitHub:** local credentials are not automatically cleaned up; re-registration is required.
- **Incomplete or previously manual installation:** the chart does not overwrite it. Back up and inspect it, then move the old directory aside during downtime or use another PVC. Original-draft installations lack a completion marker and cannot be adopted directly.

Maintenance procedure:

1. Confirm no job is running, pause new jobs, run `kubectl scale deployment/${repo}-runner-runner -n github-runner --replicas=0`, and wait for the original Pod to exit completely.
2. Back up the PVC. Enable maintenance mode with the same release and original values file: `helm upgrade ${repo}-runner . -n github-runner -f runner.local.yaml --set runner.maintenance=true`. The chart restores one Pod, prepares dependencies, and runs only sleep without connecting to GitHub. Open a non-root maintenance shell using `kubectl exec -it -n github-runner deployment/${repo}-runner-runner -c runner -- setpriv --reuid=1001 --regid=1001 --init-groups --no-new-privs /bin/bash`.
3. In `/persistent/gh-runner`, run `./config.sh remove --token <REMOVE_TOKEN>`. Obtain a removal token from GitHub's Remove runner flow and pass it through an interactively read shell variable to avoid shell history. On success, remove `.chart-registration` and any remaining `.chart-registration.tmp`. If normal removal is impossible, first delete the old record in GitHub, then back up and rename the entire `gh-runner` directory to initialize a fresh one.
4. Prepare a valid **registration token**, update configuration, and run `helm upgrade ${repo}-runner . -n github-runner -f runner.local.yaml --set runner.maintenance=false` to leave maintenance mode and resume the runner.

Do not unregister on every Pod exit. For permanent removal, unregister in GitHub using the maintenance procedure, then uninstall:

```bash
helm uninstall ${repo}-runner -n github-runner
```

The default PVC `${repo}-runner-runner` is retained. It contains credentials, source code, and caches; manage access and backups accordingly. Reinstall with `--set persistence.existingClaim=${repo}-runner-runner` and the original runner name and settings. Before permanently deleting storage, check your backup and PV reclaim policy, then delete the PVC separately.

Long-running `_work`, `_diag`, tool caches, and old update directories consume disk space; monitor PVC capacity. There is no automatic cleaner. During maintenance, remove only data confirmed to be unused, never active versions or credentials. Workflow processes can access runner credentials and the shared workspace. Use this deployment only for trusted repositories and jobs.

## Validation and chart publishing

Run in WSL Bash. If no repository Python virtual environment exists, these tests use only the system Python standard library:

```bash
for script in scripts/*.sh; do bash -n "$script"; done
helm lint . --strict
python3 -m unittest discover -s tests -v
helm template smoke . -f value.yaml > /tmp/github-runner.yaml
helm package . --destination dist
```

Local tests mock downloads and registration and check persistent recovery, checksum failures, locks, configuration changes, and Helm rendering. They do not replace real-container, storage-driver, and GitHub end-to-end validation. Deployment acceptance checks include:

1. Confirm dependency download and offline installation complete, the runner is Online, and the sample workflow's `id` reports UID 1001.
2. Delete an idle Pod and confirm its replacement uses the same runner ID without redownloading the initial runner package.
3. After an actual automatic update, record `bin/Runner.Listener --version`, recreate the Pod, confirm the version persists, and run the workflow again.
4. Perform a Helm image upgrade while idle. Confirm two runners never run simultaneously, and test PVC retention and recovery through reuse.

Optional real-container smoke test, requiring Docker but no GitHub token:

```bash
export HTTP_PROXY=http://165.225.112.16:10015 HTTPS_PROXY=http://165.225.112.16:10015
docker pull public.ecr.aws/ubuntu/ubuntu:noble
bash tests/container-smoke.sh
```

This test shares a temporary volume between two independent containers and disables networking in the main container to verify offline installation. It checks system dependencies, Docker CLI, buildx, AWS CLI v2, and UID/GID 1001, then cleans up containers and the temporary volume. It does not register a GitHub runner or access existing PVCs.

### Automatic publishing to GitHub Pages

`.github/workflows/publish-helm-repository.yaml` follows the dbgate publishing workflow. It runs when a push to `main` changes the chart, values, schema, Customize form, scripts, templates, either README, or documentation tooling. You can also run **Publish Helm repository** manually in GitHub Actions. It runs `helm lint . --strict`, packages the chart, merges the existing index, renders documentation, and publishes to `gh-pages`. The branch may be absent on the first run; subsequent runs retain previous chart archives.

Allow Actions to write repository contents. In **Settings → Pages**, select **Deploy from a branch → gh-pages → / (root)** after the first run creates the branch. The generated URL is `https://<GitHub owner>.github.io/<repository name>` and can be used as a Rancher chart repository.

The site root displays the English README as `index.html`; `readme.zh.html` displays Chinese. Both pages have language-switch links. Original `readme.md` and `readme.zh.md` files are also published for download. Helm clients continue to use `index.yaml` and chart archives at the same URL. The workflow uses a pinned Python Markdown dependency to generate static HTML, without a Jekyll build. For a local preview, install `docs/requirements.txt` in a disposable Python virtual environment and run `python docs/render.py --output .helm-repository`.

Increment `Chart.yaml`'s `version` before publishing changed chart content to avoid overwriting the same package version. `.helm-repository/` is the local publishing staging directory and is excluded from Git and chart packages.

### Manual publishing

For an HTTP chart repository, publish the packages and index generated in `dist/` to your static website:

```bash
helm repo index dist --url https://charts.example.com
```

Merge the previous index when publishing a new version to preserve older releases. Alternatively, use OCI:

```bash
helm push dist/github-runner-0.4.2.tgz oci://registry.example.com/charts
```

These publishing addresses are placeholders; commands do not run automatically. Scripts are packaged with the chart and mounted through a ConfigMap. After editing scripts, a Helm upgrade is sufficient: the changed script checksum in the Pod template triggers recreation without an image build. Increment the chart version for each release. When upgrading to 0.2.0, remove custom image addresses from older override files so the final configuration uses public Noble. Keep the original runner name, registration settings, and PVC to reuse existing state.

References: [Registering a runner](https://docs.github.com/en/actions/hosting-your-own-runners/managing-self-hosted-runners/adding-self-hosted-runners), [runner arguments](https://github.com/actions/runner/blob/v2.337.0/src/Runner.Listener/Runner.cs), [official run script](https://github.com/actions/runner/blob/v2.337.0/src/Misc/layoutroot/run.sh), and [Kubernetes initContainers](https://kubernetes.io/docs/concepts/workloads/pods/init-containers/).
