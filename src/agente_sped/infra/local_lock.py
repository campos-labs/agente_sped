"""Exclusividade da raiz de sessões entre processos locais."""

import os
from pathlib import Path


class SessionRootLock:
    def __init__(self, root: Path) -> None:
        root = root.expanduser().absolute()
        if any(
            p.is_symlink() or p.is_junction() for p in (root, *root.parents)
        ):
            raise ValueError('Raiz de sessões insegura.')
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = root / '.instance.lock'
        if target.is_symlink() or target.is_junction():
            raise ValueError('Raiz de sessões insegura.')
        self._handle = target.open('a+b')
        self._handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt  # noqa: PLC0415 -- disponível só no Windows

                if target.stat().st_size == 0:
                    self._handle.write(b'0')
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl  # noqa: PLC0415 -- disponível só no Unix

                fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self._handle.close()
            raise ValueError(
                'A raiz de sessões já está em uso por outra instância.'
            ) from error

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()
