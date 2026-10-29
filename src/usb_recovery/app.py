"""Graphical entry point and explicit, window-free verification commands."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import platform
import sys
import threading
import uuid

from . import __version__
from .acquisition import AcquisitionRunner
from .controller import Controller, run_recovery_sync
from .engine.demo import create_demo_image
from .engine.exfat import _output_tree
from .engine.image import no_reparse


def resource_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[2])) / 'resources'


def _new_report(path, value):
    path=Path(path).absolute()
    if not path.parent.is_dir():
        raise ValueError('报告的上级文件夹必须已经存在。')
    no_reparse(path)
    with _output_tree(path.parent,[]):
        with path.open('x',encoding='utf-8') as output:
            json.dump(value,output,ensure_ascii=False,indent=2,default=str)


def diagnostics():
    import tkinter
    import openpyxl
    import olefile
    import defusedxml
    runner=AcquisitionRunner(resource_root()/'cygwin'/'bin')
    engine=runner.verify_engine()
    version=runner._tool('ddrescue.exe',['--version']).splitlines()[0]
    # Tcl without Tk creates no visible window or input events.
    interpreter=tkinter.Tcl()
    return dict(version=__version__,platform=platform.platform(),python_version=platform.python_version(),
                frozen=bool(getattr(sys,'frozen',False)),executable=sys.executable,
                resource_root=str(resource_root()),tcl_patchlevel=interpreter.eval('info patchlevel'),
                openpyxl=openpyxl.__version__,olefile=olefile.__version__,defusedxml=defusedxml.__version__,
                engine_version=version,engine_files_verified=len(engine['files']),visible_window_created=False)


def self_test(output):
    output=Path(output).absolute()
    no_reparse(output)
    if not output.is_dir():
        raise ValueError('自检输出位置必须为已有普通文件夹。')
    with _output_tree(output,[]):
        work=output/('软件自检_'+datetime.now().strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:6])
        work.mkdir()
    diagnostic=diagnostics()
    demo=create_demo_image(work/'虚构演示U盘.img')
    source_hash=hashlib.sha256(demo.read_bytes()).hexdigest()
    acquisition=work/'镜像采集';acquisition.mkdir()
    cancel=threading.Event()
    polls=0
    def progress(row):
        nonlocal polls
        # The tiny fixture may finish before ddrescue publishes a positive map
        # update. Poll callbacks prove the child was started; stop independently
        # of that asynchronous map publication, then verify its closed snapshot.
        polls+=1
        if polls>=3:cancel.set()
    runner=AcquisitionRunner(resource_root()/'cygwin'/'bin',max_read_rate=65536)
    first=runner.run(demo,acquisition,cancel=cancel,progress=progress)
    if not first['cancelled'] or first['complete'] or not first['can_resume']:
        raise RuntimeError('合成镜像的安全停止/续接准备未通过。')
    second=runner.run(demo,acquisition,resume=True)
    if not second['complete'] or hashlib.sha256(Path(second['image_path']).read_bytes()).hexdigest()!=source_hash:
        raise RuntimeError('合成镜像的实际引擎续接及输出哈希未通过。')
    result=run_recovery_sync(second['image_path'],work,map_path=second['map_path'],deep=True,include_history=True)
    if hashlib.sha256(demo.read_bytes()).hexdigest()!=source_hash or result.get('cancelled') or not result.get('counts',{}).get('structure_checked'):
        raise RuntimeError('合成数据恢复或源文件不变校验未通过。')
    proof=dict(passed=True,diagnostics=diagnostic,work_dir=str(work),synthetic_source_sha256=source_hash,
               real_device_accessed=False,source_unchanged=True,acquisition_cancel_resume_verified=True,
               acquired_bytes=second['copied_bytes'],recovery_counts=result['counts'],
               recovery_complete=result['complete'],image_completeness=result.get('image_completeness'),
               report_path=result.get('report_path'),native_gui_exercised=False)
    _new_report(work/'自检记录.json',proof)
    return proof


def main(argv=None):
    parser=argparse.ArgumentParser(description='本地只读U盘镜像与文件恢复助手')
    parser.add_argument('--version',action='store_true')
    parser.add_argument('--diagnostics',action='store_true')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--output',type=Path)
    parser.add_argument('--report',type=Path)
    parser.add_argument('--recover-image',type=Path)
    parser.add_argument('--map',dest='map_path',type=Path)
    parser.add_argument('--offset',type=int)
    parser.add_argument('--deep',action='store_true')
    parser.add_argument('--history',action='store_true')
    args=parser.parse_args(argv)
    headless=args.version or args.diagnostics or args.self_test or args.recover_image is not None
    if args.version:
        if sys.stdout is not None:print(__version__)
        return 0
    try:
        if args.report is not None and args.report.exists():
            raise ValueError('指定报告已经存在，不能覆盖。')
        if args.diagnostics:
            result=diagnostics()
        elif args.self_test:
            if args.output is None:raise ValueError('自检需要指定已有输出文件夹。')
            result=self_test(args.output)
        elif args.recover_image is not None:
            if args.output is None:raise ValueError('镜像恢复需要指定已有输出文件夹。')
            result=run_recovery_sync(args.recover_image,args.output,map_path=args.map_path,
                deep=args.deep,include_history=args.history,offset=args.offset)
        else:
            from .ui import run
            run(Controller(engine_dir=resource_root()/'cygwin'/'bin'))
            return 0
        if args.report is not None:_new_report(args.report,result)
        if sys.stdout is not None:print(json.dumps(result,ensure_ascii=False,indent=2,default=str))
        return 0 if result.get('passed',True) and not result.get('error') else 1
    except Exception as error:
        result=dict(passed=False,error_type=type(error).__name__,error=str(error),visible_window_created=False)
        if args.report is not None and not args.report.exists():
            try:_new_report(args.report,result)
            except Exception:pass
        if headless:
            if sys.stderr is not None:print(json.dumps(result,ensure_ascii=False),file=sys.stderr)
        else:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,'软件未能启动：'+str(error)+'\n请完整解压软件包后再运行。','U盘恢复助手',0x10)
        return 1


if __name__=='__main__':
    raise SystemExit(main())
