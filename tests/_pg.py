"""A throwaway PostgreSQL server for testing SQL meant for Supabase.

Starts a private cluster in a temp folder (unix socket only, no TCP), with the
three roles Supabase's API uses (``anon``, ``authenticated``, ``service_role``),
and runs statements through ``psql`` as one of them. Skips when the server
binaries are not installed.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
from pathlib import Path


def _bindir() -> str | None:
    for d in sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True):
        if os.path.exists(os.path.join(d, "initdb")):
            return d
    found = shutil.which("initdb")
    return os.path.dirname(found) if found else None


class SqlError(Exception):
    pass


class Pg:
    def __init__(self, root: Path):
        self.bindir = _bindir()
        self.root = Path(root)
        self.data = self.root / "data"
        self.sock = self.root / "s"
        self.sock.mkdir(parents=True, exist_ok=True)

    @classmethod
    def available(cls) -> bool:
        return bool(_bindir()) and bool(shutil.which("psql"))

    def start(self) -> "Pg":
        run = lambda *a: subprocess.run(a, check=True, capture_output=True, text=True)
        run(os.path.join(self.bindir, "initdb"), "-D", str(self.data), "-U", "postgres",
            "--auth=trust", "-E", "UTF8")
        run(os.path.join(self.bindir, "pg_ctl"), "-D", str(self.data), "-w", "-l",
            str(self.root / "log"), "-o", f"-k {self.sock} -c listen_addresses='' -p 5999", "start")
        self.sql("CREATE ROLE anon NOLOGIN; CREATE ROLE authenticated NOLOGIN; "
                 "CREATE ROLE service_role NOLOGIN BYPASSRLS;"
                 "GRANT USAGE ON SCHEMA public TO anon, authenticated, service_role;")
        return self

    def stop(self) -> None:
        subprocess.run([os.path.join(self.bindir, "pg_ctl"), "-D", str(self.data), "-m",
                        "immediate", "stop"], capture_output=True)

    def sql(self, text: str, role: str | None = None) -> str:
        if role:
            text = f"SET ROLE {role};\n{text}"
        r = subprocess.run(["psql", "-h", str(self.sock), "-p", "5999", "-U", "postgres",
                            "-d", "postgres", "-v", "ON_ERROR_STOP=1", "-X", "-q", "-A", "-t"],
                           input=text, capture_output=True, text=True)
        if r.returncode != 0:
            raise SqlError(r.stderr.strip())
        return r.stdout.strip()

    def call(self, fn: str, *args, role: str = "anon"):
        """Call a function with literal args (None → NULL) and parse JSON results."""
        def lit(v):
            if v is None:
                return "NULL"
            if isinstance(v, bool):
                return "TRUE" if v else "FALSE"
            if isinstance(v, int):
                return str(v)
            if isinstance(v, (dict, list)):
                v = json.dumps(v)
                return "'" + v.replace("'", "''") + "'::jsonb"
            return "'" + str(v).replace("'", "''") + "'"
        out = self.sql(f"SELECT {fn}({', '.join(lit(a) for a in args)});", role=role)
        try:
            return json.loads(out)
        except ValueError:
            return {"t": True, "f": False}.get(out, out)
