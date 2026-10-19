"""Terminate only one verified owned process group after its parent disappears."""
import os
import signal
import sys
import time
import psutil


def main():
    parent, parent_start, child, child_start = int(sys.argv[1]), float(sys.argv[2]), int(sys.argv[3]), float(sys.argv[4])
    def same(pid, started):
        try:
            process=psutil.Process(pid)
            return process.create_time()==started and process.status()!=psutil.STATUS_ZOMBIE
        except psutil.NoSuchProcess:
            return False
    while same(parent,parent_start) and same(child,child_start):
        time.sleep(0.25)
    if same(child,child_start):
        try:
            if os.getpgid(child)==child:
                os.killpg(child,signal.SIGKILL)
        except ProcessLookupError:
            pass


if __name__=='__main__':
    main()
