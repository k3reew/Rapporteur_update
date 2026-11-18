"""Storage preflight and explicit offline ownership repair; standard library only."""

import argparse
import errno
import os
import re
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

DIRECTORIES = {"blobs", "cache", "slides", "pages", "reports", "report-templates", "tmp"}
_DATABASE = re.compile(r"rapporteur\.db(?:-wal|-shm|-journal|\.before-v\d+\.bak)?")


class StoragePermissionError(PermissionError):
    pass


def access_error(path: Path, error: OSError) -> StoragePermissionError:
    try:
        info = path.stat()
        owner = f"владелец {info.st_uid}:{info.st_gid}, права {stat.S_IMODE(info.st_mode):04o}"
    except OSError:
        owner = "владелец недоступен"
    message = (
        f"Нет доступа к хранилищу: {path}; процесс UID:GID={os.geteuid()}:{os.getegid()}, "
        f"{owner}. Остановите обработку и выполните make storage-repair от пользователя "
        "запуска приложения. Проверьте также ACL и режим монтирования."
    )
    return StoragePermissionError(error.errno or errno.EACCES, message, str(path))


def probe_directory(path: Path) -> None:
    try:
        with tempfile.TemporaryFile(dir=path):
            pass
    except OSError as error:
        if error.errno in (errno.EACCES, errno.EPERM, errno.EROFS):
            raise access_error(path, error) from error
        raise


def managed_paths(root: Path, *, strict: bool = True) -> Iterator[Path]:
    yield root

    def descend(path: Path) -> Iterator[Path]:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise ValueError(f"Внутри хранилища обнаружена символическая ссылка: {path}")
            if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise ValueError(f"Неподдерживаемый объект хранилища: {path}")
            yield path
            if stat.S_ISDIR(info.st_mode):
                for child in sorted(path.iterdir()):
                    yield from descend(child)
        except (FileNotFoundError, NotADirectoryError):
            if strict:
                raise

    for path in sorted(root.iterdir()):
        if path.name in DIRECTORIES or _DATABASE.fullmatch(path.name):
            yield from descend(path)


def check_storage(root: Path) -> None:
    """Probe existing managed directories as the actual application user."""
    try:
        for path in managed_paths(root, strict=False):
            try:
                if path.is_dir():
                    probe_directory(path)
                else:
                    mode = (
                        "r+b"
                        if path.name
                        in {
                            "rapporteur.db",
                            "rapporteur.db-wal",
                            "rapporteur.db-shm",
                            "rapporteur.db-journal",
                        }
                        else "rb"
                    )
                    with path.open(mode):
                        pass
            except (FileNotFoundError, NotADirectoryError):
                # Atomic cache replacement and upload cleanup are normal during live doctor.
                if path == root:
                    raise
    except StoragePermissionError:
        raise
    except PermissionError as error:
        raise access_error(Path(error.filename or root), error) from error


def repair_storage(root: Path, uid: int, gid: int) -> int:
    """Only managed application data; validate everything before changing ownership."""
    root = root.resolve(strict=True)
    if root == Path("/") or uid < 0 or gid < 0:
        raise ValueError("Недопустимый каталог или UID:GID")
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        if len(fields) < 5:
            continue
        mount = Path(re.sub(r"\\([0-7]{3})", lambda m: chr(int(m[1], 8)), fields[4]))
        if mount != root and mount.is_relative_to(root):
            raise ValueError(f"Вложенное монтирование требует ручной проверки: {mount}")
    if not (root / "rapporteur.db").is_file() and any(
        path.name not in DIRECTORIES for path in root.iterdir()
    ):
        raise ValueError("Каталог не похож на отдельное хранилище Репортёра")
    paths = list(managed_paths(root))
    for path in paths:
        if path.is_file() and path.stat().st_nlink != 1:
            raise ValueError(f"Файл с жёсткими ссылками требует ручной проверки: {path}")
    root_descriptor = os.open(root, os.O_RDONLY | os.O_NOFOLLOW | os.O_DIRECTORY)
    try:
        for path in paths:
            descriptor = _open_relative(root_descriptor, path.relative_to(root))
            try:
                info = os.fstat(descriptor)
                directory = stat.S_ISDIR(info.st_mode)
                if not directory and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 1):
                    raise ValueError(f"Объект изменился во время исправления: {path}")
                os.fchown(descriptor, uid, gid)
                mode = stat.S_IMODE(os.fstat(descriptor).st_mode)
                os.fchmod(descriptor, mode | 0o600 | (0o100 if directory else 0))
            finally:
                os.close(descriptor)
    finally:
        os.close(root_descriptor)
    return len(paths)


def _open_relative(root_descriptor: int, relative: Path) -> int:
    descriptor = os.dup(root_descriptor)
    try:
        for index, part in enumerate(relative.parts):
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            if index < len(relative.parts) - 1:
                flags |= os.O_DIRECTORY
            child = os.open(part, flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "repair"])
    parser.add_argument("root", type=Path)
    parser.add_argument("--uid", type=int)
    parser.add_argument("--gid", type=int)
    args = parser.parse_args()
    try:
        if args.action == "repair":
            if args.uid is None or args.gid is None:
                parser.error("Для repair нужны --uid и --gid")
            count = repair_storage(args.root, args.uid, args.gid)
            print(f"Права хранилища исправлены: {count} объектов, UID:GID={args.uid}:{args.gid}")
        else:
            check_storage(args.root)
            print(f"Хранилище доступно: {args.root}, UID:GID={os.geteuid()}:{os.getegid()}")
    except (OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
