"""Remove a confirmed, locally installed AcadeMate Paper instance."""

import argparse
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack, contextmanager
from pathlib import Path

MARKER = ".academate-personal-server"


def validate_root(selected):
    if not selected.is_absolute() or ".." in selected.parts or selected.is_symlink():
        raise ValueError("请选择实例的实际绝对目录。")
    root = selected.resolve(strict=True)
    home = Path.home().resolve()
    script = Path(__file__).resolve()
    # Only the installed copy may remove the directory containing itself.
    if root == home or root in home.parents or (
        root in script.parents and script != root / "releases/personal/uninstall.py"
    ):
        raise ValueError("不能删除主目录、安装向导所在目录或其上级目录。")
    if root.stat().st_uid != os.getuid() or not root.is_dir():
        raise ValueError("请选择当前用户拥有的安装目录。")
    marker = root / MARKER
    if marker.is_symlink() or not marker.is_file() or marker.read_bytes() != b"format=1\n":
        raise ValueError("目录缺少有效的 AcadeMate Paper 直装标记。")
    return root


def check_mounts(root):
    # Linux bind mounts can share the parent filesystem's device number.
    if sys.platform.startswith("linux"):
        with open("/proc/self/mountinfo", "rb") as mounts:
            for line in mounts:
                raw = line.split(b" ")[4]
                point = re.sub(rb"\\([0-7]{3})", lambda m: bytes([int(m[1], 8)]), raw)
                path = Path(os.fsdecode(point))
                if path == root or root in path.parents:
                    raise ValueError(f"请先卸载挂载点：{path}")
    else:
        def on_error(error):
            raise error

        if root.is_mount():
            raise ValueError(f"请先卸载挂载点：{root}")
        for directory, children, _ in os.walk(root, onerror=on_error):
            for name in children:
                path = Path(directory, name)
                if not path.is_symlink() and path.is_mount():
                    raise ValueError(f"请先卸载挂载点：{path}")


def external_links(root, bin_dir):
    dirs = [bin_dir, Path.home() / ".local/bin"]
    dirs.extend(Path(d) for d in os.get_exec_path() if d and os.path.isabs(d))
    receipt = root / ".amp-link"
    if receipt.is_symlink():
        raise ValueError("命令入口记录无效。")
    if receipt.is_file():
        link = Path(receipt.read_text().rstrip("\n"))
        if not link.is_absolute() or link.name != "amp":
            raise ValueError("命令入口记录无效。")
        dirs.append(link.parent)
    target = root / "bin/amp"
    links = set()
    for d in dirs:
        link = d.resolve() / "amp"
        if root not in link.parents and points_to(link, target):
            links.add(link)
    return sorted(links)


def points_to(link, target):
    try:
        return Path(os.path.normpath(link.parent / os.readlink(link))) == target
    except OSError:
        if not link.is_symlink():
            return False
        raise


def lock_file(stack, path):
    fd = os.open(path, os.O_NOFOLLOW | os.O_RDWR | os.O_CREAT, 0o600)
    stack.callback(os.close, fd)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise ValueError(f"实例仍在使用中，请稍后重试：{path}") from None


def stop_and_lock(root, stack):
    data = root / "data"
    for path in (root / "bin", data, data / "run", data / "database", root / "instance.json"):
        if path.is_symlink():
            raise ValueError(f"实例路径指向目录外或是符号链接：{path}")
    current = root / "current"
    if current.exists() or current.is_symlink():
        release = root / "releases/personal"
        if current.resolve(strict=True) != release or not release.is_dir():
            raise ValueError("当前运行时不属于该实例。")
        config = json.loads((root / "instance.json").read_text())
        if (config.get("STORAGE_ROOT") != str(data)
                or config.get("DATABASE_URL") != f"sqlite:///{data}/database/workspace.sqlite3"):
            raise ValueError("实例配置指向其他数据目录，已取消删除。")
        amp = root / "bin/amp"
        if amp.is_symlink() or not amp.is_file():
            raise ValueError("没有找到该实例的 amp 启停命令。")
        stopped = subprocess.run([str(amp), "stop"], capture_output=True, text=True, check=False)
        if stopped.returncode or json.loads(stopped.stdout).get("server") != "stopped":
            raise ValueError("无法确认服务已停止，已取消删除。")
    # amp stop itself takes command.lock. Acquire it afterwards and verify the database
    # lock too, in case a concurrent start acquired the command lock first.
    for path in (data / "run/command.lock", data / "database/.academate-server.lock"):
        if path.parent.is_dir():
            lock_file(stack, path)


@contextmanager
def guard_installation(root, identity):
    # Refer to our directory even when another operator renames the selected path.
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        if not os.path.samestat(identity, os.fstat(fd)):
            raise ValueError("所选目录已改变，请重试。")
        try:
            os.mkdir(".install-lock", mode=0o700, dir_fd=fd)
        except FileExistsError:
            raise ValueError("安装或卸载正在进行，请稍后重试。") from None
        try:
            yield
        finally:
            os.rmdir(".install-lock", dir_fd=fd)
    finally:
        os.close(fd)


def main(selected, bin_dir, *, confirm_with_yes=False):
    root = validate_root(selected)
    identity = root.stat()
    links = external_links(root, bin_dir)
    print(f"将永久删除整个目录及其中的所有文件：{root}")
    print("包括程序、私有依赖、数据库、论文、批注、设置、密钥和缓存。")
    for link in links:
        print(f"同时移除此命令链接：{link}")
    if confirm_with_yes:
        prompt, expected = "确认卸载请输入 yes，其他输入取消：", "yes"
    else:
        prompt, expected = "确认删除请输入上面的完整安装目录，直接回车取消：", str(root)
    try:
        answer = input(prompt)
    except EOFError:
        answer = ""
    if answer != expected:
        print("已取消。")
        return
    if validate_root(selected) != root or not os.path.samestat(identity, root.stat()):
        raise ValueError("所选目录已改变，请重试。")

    with guard_installation(root, identity), ExitStack() as stack:
        check_mounts(root)
        stop_and_lock(root, stack)
        if validate_root(selected) != root or not os.path.samestat(identity, root.lstat()):
            raise ValueError("服务停止期间，所选目录发生变化，已取消删除。")
        # Detach before deleting. The validated directory must still be the one that moved.
        staging = Path(tempfile.mkdtemp(prefix=".amp-remove-", dir=root.parent))
        tree = staging / "instance"
        try:
            root.rename(tree)
        except OSError:
            staging.rmdir()
            raise
        if not os.path.samestat(identity, tree.lstat()):
            if not os.path.lexists(root):
                tree.rename(root)
                staging.rmdir()
            raise ValueError(f"所选目录已被替换，已中止删除。请核对：{root.parent}")
        try:
            for link in links:
                if not os.path.lexists(root) and points_to(link, root / "bin/amp"):
                    link.unlink()
        except OSError as error:
            raise ValueError(f"无法移除命令链接。已停止实例的文件仍在：{tree}") from error
    try:
        shutil.rmtree(tree)
        staging.rmdir()
    except OSError as error:
        raise ValueError(f"服务已停止，但删除未完成。残留目录：{staging}（{error}）") from error
    print(f"已删除 AcadeMate Paper 实例：{root}")


if __name__ == "__main__":
    try:
        if sys.argv[1:2] == ["--amp"]:
            parser = argparse.ArgumentParser(prog="amp uninstall", description="确认后永久删除本机直装实例。")
            parser.parse_args(sys.argv[2:])
            root = Path(__file__).parent.parent
            if Path(__file__).resolve() != root / "releases/personal/uninstall.py":
                raise ValueError("当前卸载程序不属于所选的实例。")
            main(root, root / "bin", confirm_with_yes=True)
        else:
            main(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, RuntimeError) as error:
        sys.exit(f"卸载失败：{error}")
