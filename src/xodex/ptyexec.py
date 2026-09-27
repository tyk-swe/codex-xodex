"""Internal PTY child bootstrap; never a remote-callable tool."""
import fcntl
import os
import sys
import termios

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit(2)
    fcntl.ioctl(0, termios.TIOCSCTTY, 0)
    os.execvpe(sys.argv[1], sys.argv[1:], os.environ)
