"""Run the bridge inside Maya standalone, with all Maya calls on the owning thread."""

from queue import Empty, Queue
from threading import Thread

from .bridge import Bridge
from .config import Settings


def main():
    import maya.standalone

    settings = Settings.from_env()
    if not settings.token:
        raise ValueError("Standalone bridge requires a nonempty MAYA_MCP_TOKEN")
    maya.standalone.initialize(name="python")
    try:
        from maya import cmds
        from .handlers import MayaHandlers

        cmds.workspace(settings.root.as_posix(), openWorkspace=True)
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
