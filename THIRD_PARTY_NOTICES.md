# Third-party components

The MIT license in the repository root applies to this project's original code. It does not replace the licenses of the following bundled or build-time components.

- GNU ddrescue and the Cygwin runtime/DLLs retain their original licensing. Original notices are in `resources/licenses/`; `resources/engine-manifest.json` records exact versions, hashes and public package/source URLs.
- Seven corresponding original source archives are provided in the source ZIP attached to the same GitHub release, under `third_party/sources/`. They are omitted from Git history to keep the repository small. The source ZIP also contains this project's source, build scripts, resource binaries and notices.
- Python, Tcl/Tk, openpyxl, et_xmlfile, olefile, defusedxml and PyInstaller retain their own notices. Copies included with the packaged program are in `第三方许可证/` and `_internal/resources/licenses/`.

The program launches the included ddrescue executable as a separate process. No Cygwin installation, registry change or system PATH modification is performed by the application.

See `docs/third_party.md` for resource preparation and `resources/licenses/` for the full texts.
