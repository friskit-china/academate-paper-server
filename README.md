# AcadeMate Paper Server

AcadeMate Paper Server 是可自行部署的个人论文服务器。你可以从 arXiv 导入论文，在内置 Web 中阅读正文、查看译文、做批注，并在连接同一服务器的客户端间同步阅读位置。论文、批注和后台任务保存在自己的实例中。翻译等模型功能可在设置中配置服务商和 API key 后使用。

## 部署

克隆仓库并进入源码目录，或下载源码压缩包后解压。以下命令均从仓库根目录执行。

```sh
git clone https://github.com/friskit-china/academate-paper-server.git
cd academate-paper-server
```

### macOS / Linux 直装

以日常用户运行安装向导，选择一个由自己拥有的空目录。默认位置为 `~/AcadeMatePaper`。首次安装需联网下载 Python、Node、完整 TeX Live、字体等依赖并构建 Web，请预留下载时间和磁盘空间。运行时、数据及安装缓存都会放在所选目录内。

```sh
./deploy/native-install/install.sh
```

安装完成后，按下文「信任实例根证书」将根证书加入本机信任库，再打开终端打印的一次性 HTTPS 设置链接，设置至少 12 个字符的密码。个人账号固定为 `admin`，之后登录只需密码。使用默认设置时，Web 地址为 `https://127.0.0.1:8443`，原生客户端连接 `https://127.0.0.1:9443`。如需新的设置链接，请运行 `"$HOME/AcadeMatePaper/bin/setup-url"`。

向导默认将 `amp` 链接放在 `~/.local/bin`。若终端找不到该命令，可将这个目录加入当前 shell 的 PATH。

```sh
export PATH="$HOME/.local/bin:$PATH"
amp status
```

以下示例显式选择安装目录、命令目录和端口。

```sh
./deploy/native-install/install.sh \
  --prefix "$HOME/AcadeMatePaper" --bin-dir "$HOME/.local/bin" \
  --web-port 8443 --api-port 9443
```

直装提供 macOS 和 Linux 的 arm64 与 x86_64 依赖锁。Linux 需要 glibc 2.28 或更高版本。引导阶段使用 POSIX shell、curl、SHA-256 校验工具及支持 bzip2 的 tar，或系统已有的 Python 3。

### Docker 单容器

安装 Docker 并启动引擎后，从源码构建镜像，再用具名卷 `acade-paper-data` 保存数据库、论文和实例密钥。以下命令把 Web 和 API 端口绑定到宿主机回环地址。

```sh
docker build -f Dockerfile.server -t academatepaper/server:local . && \
docker run -d --name acade-paper --restart unless-stopped --stop-timeout 45 \
  -p 127.0.0.1:8443:8443 -p 127.0.0.1:9443:9443 \
  --mount type=volume,source=acade-paper-data,target=/data \
  academatepaper/server:local
```

用 `docker inspect -f '{{.State.Health.Status}}' acade-paper` 查看启动状态。出现 `healthy` 后，先用下面的命令导出根证书，按下一节说明信任它，再打开终端输出的一次性 HTTPS 链接设置密码。

```sh
docker cp acade-paper:/data/https/caddy/pki/authorities/local/root.crt ./academate-paper-ca.crt
docker exec -it acade-paper python -m app.container_entry setup prepare-container-setup
```

后续从浏览器打开 `https://127.0.0.1:8443`。运行 `docker exec -it acade-paper sh` 可进入容器使用 `amp`，也可以直接运行 `docker exec acade-paper amp status`。若容器本身已停止，在宿主机运行 `docker start acade-paper`。

### 信任实例根证书

新安装默认只在本机提供 HTTPS。不指定 `--cert` 和 `--key` 时，Server 首次启动会创建这个实例专用的自签根证书（CA），并自动为访问地址签发和续期服务证书。使用浏览器时，在当前设备的信任库中导入一次实例根证书。沿用原安装数据即可保留这份信任。

在新版 **AcadeMate Paper App** 中，可直接输入原生 API 地址（本机默认 `https://127.0.0.1:9443`）。首次遇到未受信的证书时，核对 App 显示的地址和 SHA-256 指纹后即可在 App 内信任，无需修改系统信任库。此选择只适用于当前服务器地址和端口。若证书与已记住的身份不再匹配，App 会要求重新确认。App 按输入的协议连接，显式使用 `http://` 时密码和会话会以明文传输。

直装用户可以在宿主机执行以下命令导出根证书。Docker 用户使用上节的 `docker cp` 命令。请从自己管理的实例取得并分发这个公开的 `.crt` 文件，私钥留在服务端。

```sh
cp "$HOME/AcadeMatePaper/data/https/caddy/pki/authorities/local/root.crt" ./academate-paper-ca.crt
```

- macOS：在「钥匙串访问」将证书导入「登录」钥匙串，打开证书，在「信任」里将 SSL 设为「始终信任」。
- Ubuntu / Debian：运行 `sudo install -m 0644 academate-paper-ca.crt /usr/local/share/ca-certificates/academate-paper-ca.crt`，再运行 `sudo update-ca-certificates`。
- iPhone / iPad：通过可信渠道将证书传到设备，安装描述文件后，到「设置 → 通用 → 关于本机 → 证书信任设置」对该根证书启用完全信任。其他系统或使用独立信任库的客户端，请将同一根证书导入其受信任的根证书库。

也可只为单次命令指定根证书，验证原生 API 的 HTTPS 连接：

```sh
curl --cacert ./academate-paper-ca.crt https://127.0.0.1:9443/api/v2/server/info
```

## 服务管理

| 命令 | 用途 |
| --- | --- |
| `amp start` | 启动服务 |
| `amp stop` | 停止服务，包括正在运行的 worker |
| `amp restart` | 重启服务 |
| `amp status` | 查看服务和子进程状态、worker 最近心跳及处理容量 |
| `amp resume` | 将执行中断且已失去有效 worker 的任务重新加入队列，可重复执行 |
| `amp passwd` | 在终端修改个人账号密码 |

## HTTPS 与其他设备访问

要从其他设备访问，可为 Web 和 API 选同一个局域网名称的两个端口。以下示例假定 `paper.home.arpa` 在客户端能解析为服务器的局域网 IP。也可以换成服务器的固定局域网 IP；连接时使用与配置完全一致的地址。

直装用户从实例目录运行以下命令。重新启动后，浏览器使用 `https://paper.home.arpa:8443`，原生客户端使用 `https://paper.home.arpa:9443`。已有 HTTP 直装实例也可以用此流程切换；只在本机使用时，直接运行 `./bin/configure-https` 即可配置默认 HTTPS。

```sh
cd "$HOME/AcadeMatePaper"
./bin/amp stop
./bin/configure-https --web-origin https://paper.home.arpa:8443 --api-origin https://paper.home.arpa:9443
./bin/amp start
```

Docker 用户使用同一镜像。以下命令将两个端口发布到宿主机网络接口。更改已有容器前，先按下一节备份，再停止并移除旧容器，重新挂载原来的卷。设置两个 HTTPS 地址也可将原先的 HTTP 卷迁移到 HTTPS。

```sh
docker run -d --name acade-paper --restart unless-stopped --stop-timeout 45 \
  -p 8443:8443 -p 9443:9443 \
  -e WEB_PUBLIC_ORIGIN=https://paper.home.arpa:8443 \
  -e SERVER_HTTPS_API_ORIGIN=https://paper.home.arpa:9443 \
  --mount type=volume,source=acade-paper-data,target=/data \
  academatepaper/server:local
```

**使用已有证书**时，在安装向导或 `configure-https` 命令后同时加上 `--cert /absolute/path/fullchain.pem --key /absolute/path/privkey.pem`。Docker 则在 `docker run` 增加 `--mount type=bind,source=/absolute/path/tls,target=/tls,readonly`、`-e SERVER_HTTPS_CERT_FILE=/tls/fullchain.pem` 和 `-e SERVER_HTTPS_KEY_FILE=/tls/privkey.pem`。证书应覆盖两个入口的主机名或 IP，容器服务用户（UID 10001）需要读取私钥的权限。替换证书后请运行 `amp restart`。

**使用公网 CA** 时，给 Web、API 设置不同的可解析公网域名，两者都用 HTTPS 默认端口。直装增加 `--acme`，Docker 增加 `-e SERVER_HTTPS_MODE=acme` 并发布 `-p 443:443`。此模式需要公网能访问 443，直装服务账号也需要监听该端口的权限。

升级已有实例时，直装恢复原配置，Docker 使用原数据卷和原启动参数会保留其协议。仍需本机 HTTP 的新直装实例可显式选择 `--http-loopback`。Docker 可显式设置 `-e WEB_PUBLIC_ORIGIN=http://127.0.0.1:3000` 并只发布 `-p 127.0.0.1:3000:3000 -p 127.0.0.1:8000:8000`。

## 备份、升级和卸载

直装备份应包含原安装的整个 `data/` 目录和 `instance.json`，后者保存解密模型凭据所需的实例密钥。先停止服务，再将两者复制到安装目录之外的私有目录。以下示例同时演示从备份升级；请在拿到新版源码后，从新版仓库根目录执行安装命令。

```sh
"$HOME/AcadeMatePaper/bin/amp" stop
backup_dir=$(mktemp -d "$HOME/AcadeMatePaper-backup.XXXXXXXX")
cp -Rp "$HOME/AcadeMatePaper/data" "$HOME/AcadeMatePaper/instance.json" "$backup_dir/"
./deploy/native-install/install.sh \
  --prefix "$HOME/AcadeMatePaper-next" --bin-dir "$HOME/AcadeMatePaper-next/bin" \
  --restore-from "$backup_dir"
```

确认新实例的文库、批注和阅读进度后，用新目录下的 `bin/amp` 管理服务。只做备份时，复制完成后启动原实例即可。要永久卸载某一实例，运行下面的命令，核对范围后输入向导显示的完整目录。确认后，该目录内的代码、数据和密钥将被永久删除。

```sh
./deploy/native-install/install.sh --uninstall --prefix "$HOME/AcadeMatePaper"
```

使用 Docker 时，先停止容器，再备份完整数据卷，包括隐藏文件 `.amp-instance.json`。以下示例将备份保存到当前用户的私有目录。

```sh
docker stop acade-paper
backup_dir=$(mktemp -d "$HOME/AcadeMatePaper-backup.XXXXXXXX")
docker run --rm --network none --entrypoint tar \
  --mount type=volume,source=acade-paper-data,target=/data,readonly \
  academatepaper/server:local -C /data -czf - . > "$backup_dir/data.tar.gz"
```

只做备份时执行 `docker start acade-paper`。升级时，从新版源码重新构建镜像，用 `docker rm acade-paper` 移除已停止的容器，然后按原有端口、证书和数据卷参数重新创建。同一份数据在任一时刻只应由一个 Server 写入。要永久卸载，请在确认备份可用后停止并移除容器，再执行 `docker volume rm acade-paper-data` 删除全部实例数据；本机镜像可用 `docker image rm academatepaper/server:local` 移除。

## API

登录后可在 Web 设置中打开接口查看器，按路径或 HTTP 方法查看当前实例的接口和数据结构。程序可以使用自己的设备会话从 `/api/v2/openapi.json` 读取同一份 OpenAPI 定义。

## 许可证

MIT License

Copyright (c) 2026 AcadeMate Paper Server contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
