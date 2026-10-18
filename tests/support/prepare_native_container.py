"""Export code/history only for the native-PM container; never copy a profile."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tarfile


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(root), *args])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge", type=Path, required=True)
    parser.add_argument("--core", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bridge-tree", default="HEAD")
    args = parser.parse_args()
    bridge, core = args.bridge.resolve(strict=True), args.core.resolve(strict=True)
    output = args.output.absolute()
    output.mkdir(parents=True, exist_ok=False)
    core_ref = git(core, "rev-parse", "HEAD").decode().strip()
    tree = git(bridge, "rev-parse", args.bridge_tree+"^{tree}").decode().strip()
    for name, source, revision in (("bridge.tar",bridge,args.bridge_tree),("core.tar",core,"HEAD")):
        with (output/name).open("xb") as stream:
            subprocess.run(["git","-C",str(source),"archive",revision],stdout=stream,check=True)
    # Core needs only object identity for source qualification. Exclude config,
    # credential helpers, hooks, logs, remotes and any object-store alternates.
    gitdir=Path(git(core,"rev-parse","--absolute-git-dir").decode().strip())
    if (gitdir/"objects/info/alternates").exists():
        raise RuntimeError("provide a self-contained shallow core checkout, not a borrowed object store")
    with tarfile.open(output/"core.tar","a") as archive:
        for name in ("HEAD","objects","refs","packed-refs","shallow"):
            item=gitdir/name
            if item.exists():
                archive.add(item,arcname=".git/"+name)
    subprocess.run(["git","-C",str(bridge),"bundle","create",str(output/"bridge.bundle"),"HEAD"],check=True)
    dockerfile=Path(__file__).with_name("native-install.Dockerfile")
    (output/"Dockerfile").write_bytes(dockerfile.read_bytes())
    record={"candidate_tree":tree,"core_ref":core_ref,"files":{}}
    for item in output.iterdir():
        with item.open("rb") as stream:
            record["files"][item.name]={"bytes":item.stat().st_size,"sha256":hashlib.file_digest(stream,"sha256").hexdigest()}
    (output/"inputs.json").write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    print(json.dumps({"candidate_tree":tree,"core_ref":core_ref,"output":str(output)}))


if __name__=="__main__":
    main()
