"""Register the direct installation's command directory with the host user's shell."""
import argparse
import fcntl
import hashlib
import json
import os
import pwd
import shlex
import stat
import sys
import tempfile
from pathlib import Path

RECEIPT = ".amp-shell-path.json"


def profiles(shell):
    home = Path.home()
    if shell == "zsh":
        base = Path(os.environ.get("ZDOTDIR") or home)
        return [base / ".zshrc", base / ".zprofile"]
    if shell == "bash":
        login = next((home / f for f in (".bash_profile", ".bash_login", ".profile")
                      if (home / f).is_file() and os.access(home / f, os.R_OK)), home / ".profile")
        return [home / ".bashrc", login]
    if shell == "fish":
        base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
        return [base / "fish/config.fish"]
    if shell in {"sh", "dash", "ksh"}:
        paths = [home / ".profile"]
        env = os.path.expanduser(os.path.expandvars(os.environ.get("ENV", "")))
        if os.path.isabs(env) and Path(env) not in paths:
            paths.append(Path(env))
        return paths
    return []


def path_command(bin_dir, fish):
    directory = str(bin_dir)
    if fish:
        quoted = "'" + directory.replace("\\", "\\\\").replace("'", "\\'") + "'"
        return f"contains -- {quoted} $PATH; or set -gx PATH {quoted} $PATH"
    quoted = shlex.quote(directory)
    return f'case ":${{PATH-}}:" in *:{quoted}:*) ;; *) export PATH={quoted}${{PATH:+":$PATH"}} ;; esac'


def profile_marker(root):
    owner = hashlib.sha256(os.fsencode(root)).hexdigest()[:20]
    return f"# AcadeMate Paper amp {owner}"


def block(root, bin_dir, fish):
    return os.fsencode(f"\n{path_command(bin_dir, fish)} {profile_marker(root)}\n")


def load_records(root):
    path = root / RECEIPT
    if path.is_symlink():
        raise ValueError("无效的 PATH 配置记录")
    return json.loads(path.read_bytes()) if path.is_file() else []


def save_records(root, records):
    with tempfile.NamedTemporaryFile(mode="w", dir=root, delete=False) as out:
        json.dump(records, out)
    os.replace(out.name, root / RECEIPT)


def open_profile(path, create):
    # Resolve dotfile symlinks through normal file opening, preserving their original link and mode.
    flags = os.O_RDWR | os.O_NONBLOCK
    if create:
        flags |= os.O_CREAT | os.O_EXCL
    fd = os.open(path, flags, 0o600)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"shell 配置不是普通文件：{path}")
    stream = os.fdopen(fd, "r+b")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        stream.close()
        raise
    return stream


def register(root, profile, bin_dir, fish):
    if not profile.is_absolute():
        raise ValueError(f"shell 配置目录需要绝对路径：{profile}")
    profile.parent.mkdir(parents=True, exist_ok=True)
    records = load_records(root)
    owned = block(root, bin_dir, fish)
    try:
        stream = open_profile(profile, create=True)
        created = True
    except FileExistsError:
        stream = open_profile(profile, create=False)
        created = False
    with stream:
        content = stream.read()
        record = next((r for r in records if r["profile"] == str(profile)
                       and r["bin"] == str(bin_dir) and r["fish"] == fish), None)
        if owned in content:
            return
        if record is None:
            records.append(dict(profile=str(profile), bin=str(bin_dir), fish=fish, created=created))
            save_records(root, records)
        stream.write(owned)


def remove(root):
    marker = profile_marker(root)
    try:
        records = load_records(root)
    except (ValueError, OSError) as error:
        print(f"无法读取 PATH 配置记录：{error}。请从 shell 配置手动移除以 {marker} 结尾的行。", file=sys.stderr)
        return
    for record in records:
        profile = Path(record["profile"])
        owned = block(root, record["bin"], record["fish"])
        try:
            with open_profile(profile, create=False) as stream:
                data = stream.read()
                if owned not in data:
                    continue
                if data == owned and record["created"] and not profile.is_symlink():
                    profile.unlink()
                else:
                    stream.seek(0)
                    stream.write(data.replace(owned, b"", 1))
                    stream.truncate()
        except FileNotFoundError:
            continue
        except (OSError, ValueError) as error:
            print(f"无法更新 {profile}：{error}。请手动移除以 {marker} 结尾的行。", file=sys.stderr)


def command_directory(root, explicit):
    amp = root / "bin/amp"
    if (root / ".academate-personal-server").read_bytes() != b"format=1\n" or not (
        amp.is_file() and not amp.is_symlink() and os.access(amp, os.X_OK)
    ):
        raise ValueError("请选择已安装且含 bin/amp 的直装实例")
    receipt = root / ".amp-link"
    if receipt.is_symlink():
        raise ValueError("无效的命令入口记录")
    recorded = Path(receipt.read_text().rstrip("\n")) if receipt.is_file() else None
    if explicit:
        link = Path(explicit) / "amp"
    elif recorded is not None:
        link = recorded
    else:
        default = Path.home() / ".local/bin/amp"
        link = default if default.resolve() == amp else amp
    if recorded == link and link.name == "amp" and link.is_absolute() and not os.path.lexists(link):
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(amp)
    if not link.is_absolute() or link.name != "amp" or link.resolve() != amp:
        raise ValueError(f"此命令入口未指向所选实例：{link}。可显式选择 --bin-dir {shlex.quote(str(amp.parent))}")
    return link.parent.resolve()


def configure(root, explicit, skip):
    bin_dir = command_directory(root, explicit)
    shell = Path(os.environ.get("SHELL") or pwd.getpwuid(os.getuid()).pw_shell).name
    command = path_command(bin_dir, shell == "fish")
    if any(c in str(bin_dir) for c in ":\r\n"):
        print(f"此目录不能自动加入 PATH，请使用 {shlex.quote(str(bin_dir / 'amp'))} status")
        return
    settings = profiles(shell)
    if not skip and settings:
        failed = False
        for profile in settings:
            try:
                register(root, profile, bin_dir, shell == "fish")
                print(f"amp 命令目录已配置：{profile}")
            except (OSError, ValueError) as error:
                failed = True
                print(f"无法更新 {profile}：{error}。可手动把下方命令加入此文件。", file=sys.stderr)
        if not failed:
            print("打开新终端后可运行 amp status。")
    elif not skip:
        print(f"未识别登录 shell {shell!r}，可直接运行 {shlex.quote(str(bin_dir / 'amp'))} status。")
    if shell == "fish" or settings:
        print(f"在当前终端立即使用 amp，可执行：\n{command}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("configure", "remove"))
    parser.add_argument("root", type=Path)
    parser.add_argument("bin_dir", nargs="?")
    parser.add_argument("--no-path-update", action="store_true")
    args = parser.parse_args()
    try:
        root = args.root.resolve(strict=True)
        if args.operation == "remove":
            remove(root)
        else:
            configure(root, args.bin_dir, args.no_path_update)
    except (OSError, ValueError) as error:
        sys.exit(f"amp PATH: {error}")
