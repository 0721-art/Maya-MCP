"""Run the bridge inside Maya standalone, with all Maya calls on the owning thread."""

from queue import Empty, Queue
from threading import Thread

from .bridge import Bridge
from .capabilities import ensure_plugin
from .config import Settings, _is_ascii, staging_temp_root


def main():
    import maya.standalone

    settings = Settings.from_env()
    if not settings.token:
        raise ValueError("Standalone bridge requires a nonempty MAYA_MCP_TOKEN")
    maya.standalone.initialize(name="python")
    try:
        from maya import cmds
        from .handlers import MayaHandlers

        workspace_dir = settings.root if _is_ascii(str(settings.root)) else staging_temp_root(settings.root)
        try:
            cmds.workspace(workspace_dir.as_posix(), openWorkspace=True)
        except RuntimeError:
            pass
        try:
            cmds.undoInfo(state=True, infinity=False, length=100)
        except RuntimeError:
            pass
        # Preload FBX and OBJ translators when available so export/import and scripts work immediately.
        ensure_plugin(cmds, "fbxmaya")
        ensure_plugin(cmds, "objExport")

        pending = Queue()
        with Bridge(settings, MayaHandlers(cmds, settings).commands(), "headless",
                    cmds.about(version=True), pending.put_nowait) as bridge:
            thread = Thread(target=bridge.serve_forever, daemon=True)
            thread.start()
            try:
                while thread.is_alive():
                    try:
                        job = pending.get(timeout=0.2)
                    except Empty:
                        continue
                    job()
            finally:
                bridge.shutdown()
                thread.join()
    finally:
        maya.standalone.uninitialize()


if __name__ == "__main__":
    main()
