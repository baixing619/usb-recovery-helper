"""Collect runtime notices from the selected build interpreter, not global projects."""
import importlib.metadata
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
TARGET=ROOT/'resources'/'licenses'


def main():
    TARGET.mkdir(parents=True,exist_ok=True)
    selected=[]
    for name in ('openpyxl','et-xmlfile','olefile','defusedxml','pyinstaller'):
        dist=importlib.metadata.distribution(name)
        for item in dist.files or ():
            if '.dist-info' not in str(item):continue
            if not any(word in item.name.upper() for word in ('LICENSE','LICENCE','COPYING','NOTICE')):continue
            source=Path(dist.locate_file(item))
            if not source.is_file():continue
            output=TARGET/(name+'-'+dist.version+'-'+source.name)
            data=source.read_bytes()
            if output.exists():
                if output.read_bytes()!=data:raise ValueError('License collision: '+output.name)
            else:
                with output.open('xb') as handle:handle.write(data)
            selected.append(output.name)
    base=Path(sys.base_prefix)
    for relative,label in [('LICENSE.txt','Python-LICENSE.txt'),('LICENSE','Python-LICENSE'),
                           ('tcl/tcl8.6/license.terms','Tcl-license.terms'),
                           ('tcl/tk8.6/license.terms','Tk-license.terms')]:
        source=base/relative
        if source.is_file():
            output=TARGET/label
            if output.exists():
                if output.read_bytes()!=source.read_bytes():raise ValueError('License collision')
            else:
                with output.open('xb') as handle:handle.write(source.read_bytes())
            selected.append(label)
    if not any(name.startswith('Python-LICENSE') for name in selected):
        raise RuntimeError('Python runtime license was not found')
    print('Collected notices: '+', '.join(selected))


if __name__=='__main__':main()
