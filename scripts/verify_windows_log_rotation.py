"""Exercise actual Windows file sharing with two concurrent logging processes."""
import os
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4


def worker(directory):
    from utils.logger import setup_module_logger
    target = str(Path(directory).resolve() / "shared.log")
    first = setup_module_logger("rotation.first", target)
    second = setup_module_logger("rotation.second", target)
    handler = first.handlers[0]
    assert handler is second.handlers[0], "same process opened duplicate handles"
    first.info("before-first")
    second.info("before-second")
    print("READY", flush=True)
    assert sys.stdin.readline().strip() == "rotate"
    handler.rolloverAt = int(time.time()) - 1
    first.info("after-rotation")
    handler.flush()
    archives = list(Path(directory).glob(f"shared.{os.getpid()}.log.*"))
    assert len(archives) == 1, f"rollover failed: {archives}"
    archived = archives[0].read_text(encoding="utf-8")
    assert "before-first" in archived and "before-second" in archived
    assert "after-rotation" in Path(handler.baseFilename).read_text(encoding="utf-8")
    print("PASS: shared module handle and real Windows rollover", flush=True)


def main():
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        return worker(sys.argv[2])
    if os.name != "nt":
        raise RuntimeError("This verification must run on Windows")
    directory = Path("tmp") / ("log-rotation-" + uuid4().hex[:10])
    directory.mkdir(parents=True)
    children = [subprocess.Popen([sys.executable, "-m", "scripts.verify_windows_log_rotation", "--worker", str(directory)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8") for _ in range(2)]
    try:
        for child in children:
            assert child.stdout.readline().strip() == "READY"
        for child in children:
            child.stdin.write("rotate\n")
            child.stdin.flush()
        for child in children:
            output, errors = child.communicate(timeout=15)
            assert child.returncode == 0, errors
            print(output.strip())
        print(f"PASS: two concurrent Windows processes; evidence in {directory}")
    finally:
        for child in children:
            if child.poll() is None:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
