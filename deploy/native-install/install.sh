#!/bin/sh
# Bootstrap the private Python runtime; all later work runs inside that prefix.
set -eu
umask 077
here=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd -P)
usage() {
    echo 'Usage: install.sh [--prefix DIRECTORY] [--bin-dir DIRECTORY] [--web-port N] [--api-port N] [--restore-from DIRECTORY] [--no-start] [--no-path-update]'
    echo '       HTTPS options: [--web-origin URL --api-origin URL] [--cert PEM --key PEM | --acme]'
    echo '       Separate Web/API HTTPS ports accept all hosts on all interfaces by default. Use --no-any-host for named hosts only.'
    echo '       Additional HTTPS origins: [--web-aliases URL,...] [--api-aliases URL,...]'
    echo '       Local HTTP: --http-loopback (fresh installations use HTTPS by default)'
    echo '       install.sh --repair-path [--prefix DIRECTORY] [--bin-dir DIRECTORY]'
    echo '       install.sh --uninstall [--prefix DIRECTORY] [--bin-dir DIRECTORY]'
}
prefix=
bin_dir=
web_port=
api_port=
web_origin=
api_origin=
web_aliases=
api_aliases=
any_host=
cert=
key=
acme=no
http=no
start=yes
restore=
uninstall=no
repair_path=no
update_path=yes
interactive=no
while [ "$#" -gt 0 ]; do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --repair-path) repair_path=yes; shift; continue ;;
        --no-path-update) update_path=no; shift; continue ;;
        --uninstall) uninstall=yes; shift; continue ;;
        --no-start) start=no; shift; continue ;;
        --any-host|--no-any-host)
            [ -z "$any_host" ] || { usage >&2; exit 2; }
            any_host=$1; shift; continue ;;
        --acme) acme=yes; shift; continue ;;
        --http-loopback) http=yes; shift; continue ;;
        --prefix|--bin-dir|--web-port|--api-port|--restore-from|--web-origin|--api-origin|--web-aliases|--api-aliases|--cert|--key)
            [ "$#" -ge 2 ] && [ -n "$2" ] || { usage >&2; exit 2; }
            case "$1" in
                --prefix) prefix=$2 ;;
                --bin-dir) bin_dir=$2 ;;
                --web-port) web_port=$2 ;;
                --api-port) api_port=$2 ;;
                --restore-from) restore=$2 ;;
                --web-origin) web_origin=$2 ;;
                --api-origin) api_origin=$2 ;;
                --web-aliases) web_aliases=$2 ;;
                --api-aliases) api_aliases=$2 ;;
                --cert) cert=$2 ;;
                --key) key=$2 ;;
            esac
            shift 2 ;;
        *) usage >&2; exit 2 ;;
    esac
done
default_root="$HOME/AcadeMatePaper"
if [ "$repair_path" = yes ]; then
    [ "$uninstall" = no ] && [ "$update_path" = yes ] || { usage >&2; exit 2; }
    prefix=${prefix:-"$default_root"}
fi
if [ -z "$prefix" ]; then
    interactive=yes
    printf 'AcadeMate Paper 安装目录 [%s]: ' "$default_root"
    IFS= read -r prefix || :
    prefix=${prefix:-$default_root}
fi
explicit_bin_dir=$bin_dir
bin_dir=${bin_dir:-"$HOME/.local/bin"}
case "$prefix" in /*) ;; *) echo "安装目录必须是绝对路径。" >&2; exit 2 ;; esac
case "$bin_dir" in /*) ;; *) echo "amp 入口目录必须是绝对路径。" >&2; exit 2 ;; esac
if [ "$interactive" = yes ] && [ "$uninstall" = no ] && [ -f "$prefix/.academate-personal-server" ]; then
    printf '\n已找到 AcadeMate Paper 实例：%s\n' "$prefix"
    printf '1) 保留并退出（默认）\n2) 永久删除此实例的全部代码和数据\n'
    if [ ! -L "$prefix/current" ]; then printf '3) 继续未完成的安装\n'; fi
    printf '请选择 [1]: '
    IFS= read -r choice || exit 0
    case "$choice" in
        ''|1) exit 0 ;;
        2) uninstall=yes ;;
        3) [ ! -L "$prefix/current" ] || exit 2 ;;
        *) echo '选项无效。' >&2; exit 2 ;;
    esac
fi
if [ "$uninstall" = yes ] || [ "$repair_path" = yes ]; then
    [ -z "$restore" ] || { echo '不能同时恢复和卸载。' >&2; exit 2; }
    marker="$prefix/.academate-personal-server"
    case "$prefix/" in */../*|*/./*) echo '请选择实例的实际绝对目录。' >&2; exit 2 ;; esac
    [ ! -L "${prefix%/}" ] && [ -d "$prefix" ] && [ -f "$marker" ] && [ ! -L "$marker" ] &&
        [ "$(cat "$marker")" = 'format=1' ] || { echo '请选择有效的 AcadeMate Paper 直装目录。' >&2; exit 1; }
    if ! python=$(command -v python3); then
        python="$prefix/releases/personal/runtime/bin/python"
        [ -O "$prefix" ] && [ -O "$marker" ] && [ -x "$python" ] ||
            { echo '未找到可用的实例 Python 或 python3。' >&2; exit 1; }
    fi
    if [ "$repair_path" = yes ]; then
        mkdir "$prefix/.install-lock" || { echo '安装或卸载正在进行，请稍后重试。' >&2; exit 1; }
        trap 'rmdir "$prefix/.install-lock"' EXIT
        "$python" -IB "$here/shell_path.py" configure "$prefix" "$explicit_bin_dir"
        exit
    fi
    exec "$python" -IB "$here/uninstall.py" "$prefix" "$bin_dir"
fi
if [ "$interactive" = yes ] && [ -z "$restore$web_origin$api_origin$web_port$api_port$web_aliases$api_aliases" ] &&
   [ "$http" = no ] && [ ! -f "$prefix/.academate-personal-server" ]; then
    printf 'Web HTTPS 主地址 [https://127.0.0.1:8443]: '
    IFS= read -r web_origin || :
    web_origin=${web_origin:-https://127.0.0.1:8443}
    printf 'App API HTTPS 地址 [https://127.0.0.1:9443]: '
    IFS= read -r api_origin || :
    api_origin=${api_origin:-https://127.0.0.1:9443}
    if [ "$any_host" = --no-any-host ]; then
        printf '其他 Web HTTPS 地址（可留空，多个用逗号分隔）: '
        IFS= read -r web_aliases || :
        printf '其他 API HTTPS 地址（可留空，多个用逗号分隔）: '
        IFS= read -r api_aliases || :
    fi
fi
if { [ -n "$cert" ] && [ -z "$key" ]; } || { [ -n "$key" ] && [ -z "$cert" ]; }; then
    echo '请同时指定 --cert 和 --key。' >&2; exit 2
fi
if { [ -n "$web_origin" ] && [ -z "$api_origin" ]; } || { [ -n "$api_origin" ] && [ -z "$web_origin" ]; }; then
    echo '请同时指定 --web-origin 和 --api-origin。' >&2; exit 2
fi
if [ -n "$web_origin" ] && [ -n "$web_port$api_port" ]; then
    echo '请在 URL 中填写端口，或单独使用端口参数。' >&2; exit 2
fi
if { [ "$http" = yes ] && { [ -n "$web_origin$cert$web_aliases$api_aliases$any_host" ] || [ "$acme" = yes ]; }; } ||
   { [ -n "$cert" ] && [ "$acme" = yes ]; }; then
    echo 'HTTP、自有证书和公网 ACME 是不同的部署选项。' >&2; exit 2
fi
if [ -n "$cert" ] && { [ ! -f "$cert" ] || [ ! -r "$cert" ] || [ ! -f "$key" ] || [ ! -r "$key" ]; }; then
    echo '证书和密钥须为当前用户可读取的文件。' >&2; exit 2
fi
if [ "$acme" = yes ] && [ -z "$web_origin" ]; then
    echo '公网 ACME 需要 --web-origin 和 --api-origin。' >&2; exit 2
fi
for port in "$web_port" "$api_port"; do
    [ -n "$port" ] || continue
    case "$port" in ''|*[!0-9]*) echo "端口必须是 1–65535。" >&2; exit 2 ;; esac
    [ "$port" -ge 1 ] && [ "$port" -le 65535 ] || { echo "端口必须是 1–65535。" >&2; exit 2; }
done
if [ "$http" = yes ]; then
    default_web=3000; default_api=8000
else
    default_web=8443; default_api=9443
fi
[ "${api_port:-$default_api}" -ne "${web_port:-$default_web}" ] ||
    { echo "API 和 Web 需要不同端口。" >&2; exit 2; }
case "$(uname -s)-$(uname -m)" in
    Darwin-arm64) platform=osx-arm64; hash=bd5b8d3f151c3ad2d92f0fb918806543d713c7f67895c020db4041097a7f004d ;;
    Darwin-x86_64) platform=osx-64; hash=49c2375bc7553287404f3e348494f3dc7f7987bcd99fe996d2099a654b384b95 ;;
    Linux-aarch64|Linux-arm64) platform=linux-aarch64; hash=02ed4fe982c8ecbf9c5b1759c07ee29063994842b65fdaab439db77b9b00510a ;;
    Linux-x86_64) platform=linux-64; hash=e7274528ceb9c20d048a428d6c22d7e02e268f8ffb762c4c365422347c8b8ba2 ;;
    *) echo "需要受支持的 macOS 或 glibc Linux 系统。" >&2; exit 1 ;;
esac
mkdir -p -- "$prefix"
prefix=$(CDPATH='' cd -- "$prefix" && pwd -P)
# Check ownership before writing inside a prefix chosen by an operator.
if [ ! -f "$prefix/.academate-personal-server" ]; then
    [ -z "$(ls -A "$prefix")" ] || { echo "请选择空目录或已有的 AcadeMate Paper 直装目录。" >&2; exit 1; }
    printf '%s\n' 'format=1' > "$prefix/.academate-personal-server"
fi
chmod 700 "$prefix"
if ! mkdir "$prefix/.install-lock"; then
    echo "另一个安装程序可能还在运行，详见安装说明。" >&2
    exit 1
fi
trap 'rmdir "$prefix/.install-lock"' EXIT
trap 'exit 130' HUP INT TERM
if [ -e "$prefix/current" ] || [ -L "$prefix/current" ]; then
    echo "这个实例已安装。升级请用新目录和 --restore-from。卸载可用 --uninstall --prefix 指定此目录。" >&2
    exit 1
fi
if [ -e "$bin_dir/amp" ] || [ -L "$bin_dir/amp" ]; then
    echo "目标目录已存在 amp，请选择空闲的 --bin-dir。" >&2
    exit 1
fi
if [ -n "$restore" ]; then
    restore=$(CDPATH='' cd -- "$restore" && pwd -P)
    [ -f "$restore/instance.json" ] && [ -d "$restore/data/database" ] || {
        echo "恢复源必须包含直装实例的 instance.json 和 data/database。" >&2; exit 1;
    }
fi
mkdir -p "$prefix/bootstrap/bin" "$prefix/cache" "$prefix/releases/personal" "$prefix/home"
bootstrap="$prefix/bootstrap"
archive="$bootstrap/micromamba.tar.bz2"
if [ ! -f "$archive" ]; then
    curl -fsSL --retry 4 --max-time 600 \
        "https://conda.anaconda.org/conda-forge/$platform/micromamba-2.3.3-0.tar.bz2" -o "$archive.part"
    mv "$archive.part" "$archive"
fi
if command -v sha256sum >/dev/null 2>&1; then
    actual=$(sha256sum < "$archive" | awk '{print $1}')
else
    actual=$(shasum -a 256 < "$archive" | awk '{print $1}')
fi
[ "$hash" = "$actual" ] || { echo "micromamba SHA-256 校验失败。" >&2; exit 1; }
if command -v bzip2 >/dev/null 2>&1 || tar --version 2>/dev/null | grep -qi bsdtar; then
    tar -xjf "$archive" -C "$bootstrap" bin/micromamba
elif command -v python3 >/dev/null 2>&1; then
    # Minimal cloud distributions may have GNU tar without its bzip2 helper.
    python3 -I -c 'import sys, tarfile, shutil
with tarfile.open(sys.argv[1], "r:bz2") as source, open(sys.argv[2], "wb") as target:
    shutil.copyfileobj(source.extractfile("bin/micromamba"), target)' "$archive" "$bootstrap/bin/micromamba"
    chmod 700 "$bootstrap/bin/micromamba"
else
    echo "解包引导程序需要支持 bzip2 的 tar 或系统自带的 python3。" >&2
    exit 1
fi
release="$prefix/releases/personal"
# Also contain the package manager's user metadata and caches.
if [ ! -x "$release/runtime/bin/python" ] || ! cmp -s "$here/locks/$platform.txt" "$release/runtime.lock"; then
    env -i "HOME=$prefix/home" "PATH=/usr/bin:/bin" \
        "$bootstrap/bin/micromamba" --no-rc create -y -r "$prefix/cache/mamba" \
        -p "$release/runtime" -f "$here/locks/$platform.txt"
    cp "$here/locks/$platform.txt" "$release/runtime.lock"
fi
set -- "$prefix" "$bin_dir" "$web_port" "$api_port" "$start" "$restore"
if [ -n "$web_origin" ]; then set -- "$@" --web-origin "$web_origin" --api-origin "$api_origin"; fi
if [ -n "$web_aliases" ]; then set -- "$@" --web-aliases "$web_aliases"; fi
if [ -n "$api_aliases" ]; then set -- "$@" --api-aliases "$api_aliases"; fi
if [ -n "$any_host" ]; then set -- "$@" "$any_host"; fi
if [ -n "$cert" ]; then set -- "$@" --cert "$cert" --key "$key"; fi
if [ "$acme" = yes ]; then set -- "$@" --acme; fi
if [ "$http" = yes ]; then set -- "$@" --http-loopback; fi
env -i "HOME=$prefix/home" "PATH=$release/runtime/bin:/usr/bin:/bin" \
    "PYTHONNOUSERSITE=1" "PYTHONDONTWRITEBYTECODE=1" \
    "$release/runtime/bin/python" "$here/install.py" "$@"
# Shell startup belongs to the invoking user, outside the private runtime's HOME.
set -- configure "$prefix" "$bin_dir"
if [ "$update_path" = no ]; then set -- "$@" --no-path-update; fi
"$release/runtime/bin/python" -IB "$here/shell_path.py" "$@" || {
    printf '服务已安装。请直接使用 %s，或用 --repair-path 重新配置。\n' "$bin_dir/amp" >&2
}
