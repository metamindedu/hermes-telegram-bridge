"""Launch the native-PM-only container after verifying its exact OS boundary."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import subprocess
import uuid


def docker(*args: str) -> str:
    return subprocess.check_output(["docker",*args],text=True).strip()


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image",required=True)
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--test",default="test_native_install")
    args=parser.parse_args()
    if args.test!="test_native_install" and not args.test.startswith("test_native_install.NativeInstallTests.test_"):
        raise RuntimeError("only the native-install test slice is permitted")
    output=args.output.absolute()
    output.mkdir(parents=True,exist_ok=False)
    image=docker("image","inspect",args.image,"--format","{{.Id}}")
    name="bridge-nativepm-"+uuid.uuid4().hex[:12]
    volume=name+"-scratch"
    docker("volume","create","--label","bridge.test=native-pm",volume)
    cid=docker("create","--name",name,"--label","bridge.test=native-pm",
        "--read-only","--cap-drop=ALL","--security-opt","no-new-privileges:true",
        "--cpus=2","--memory=3g","--pids-limit=256",
        "--mount",f"type=volume,source={volume},target=/opt/bridge/.ci-scratch",image,
        "/opt/core/.venv/bin/python","-I","-S","/opt/bridge/tests/support/container_native_install.py",
        "--test",args.test)
    inspected=json.loads(docker("inspect",cid))[0]
    host=inspected["HostConfig"]
    mounts=inspected["Mounts"]
    if not host["ReadonlyRootfs"] or host["Privileged"] or host.get("Binds") or host.get("Devices"):
        raise RuntimeError("container root/mount privilege boundary rejected before start")
    if "ALL" not in host["CapDrop"] or "no-new-privileges:true" not in host["SecurityOpt"]:
        raise RuntimeError("container capability boundary rejected before start")
    if host.get("PidMode") or host["NetworkMode"]=="host":
        raise RuntimeError("host process/network namespaces are forbidden")
    if len(mounts)!=1 or any(m.get("Type")!="volume" or m.get("Name")!=volume or m.get("Destination")!="/opt/bridge/.ci-scratch" for m in mounts):
        raise RuntimeError("unexpected mount rejected before start")
    keys={value.split("=",1)[0] for value in inspected["Config"]["Env"]}
    forbidden={"TELEGRAM_BOT_TOKEN","GITHUB_TOKEN","GH_TOKEN","OPENAI_API_KEY","ANTHROPIC_API_KEY","XAI_API_KEY"}
    if keys & forbidden or any(key.endswith(("_API_KEY","_ACCESS_TOKEN")) for key in keys):
        raise RuntimeError("credential environment rejected before start")
    record={"container_id":cid,"name":name,"volume":volume,"image_id":image,
        "test_selection":args.test,"read_only_root":True,"host_bind_mounts":0,
        "cap_drop":host["CapDrop"],"security_opt":host["SecurityOpt"],
        "provider_credential_keys":False,"verified_before_start":True}
    receipt=output/"boundary.json"
    receipt.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    with (output/"stdout.log").open("x",encoding="utf-8") as stream:
        result=subprocess.run(["docker","start","--attach",cid],stdout=stream,stderr=subprocess.STDOUT,timeout=1800)
    state=json.loads(docker("inspect",cid))[0]["State"]
    record.update(container_exit=state["ExitCode"],docker_client_exit=result.returncode,
        oom_killed=state["OOMKilled"],finished_at=state["FinishedAt"])
    receipt.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    subprocess.run(["docker","cp",cid+":/opt/bridge/.ci-scratch",str(output/"scratch-evidence")],check=True)
    # Exact IDs created above only; no global prune or existing-container cleanup.
    docker("rm",cid)
    docker("volume","rm",volume)
    record["owned_container_and_volume_removed"]=True
    receipt.write_text(json.dumps(record,indent=2)+"\n",encoding="utf-8")
    print(json.dumps(record,sort_keys=True))
    return state["ExitCode"] or result.returncode


if __name__=="__main__":
    raise SystemExit(main())
