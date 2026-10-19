"""Directory mtimes change only when their contents change, not on every getattr."""

import pyfuse3
import trio

from program.services.filesystem.vfs.rivenvfs import RivenVFS
from program.services.filesystem.vfs.vfs_node import VFSDirectory, VFSRoot


def make_vfs():
    vfs = RivenVFS.__new__(RivenVFS)
    vfs._root = VFSRoot()
    shows = VFSDirectory(name="shows", inode=pyfuse3.InodeT(2), parent=None)
    vfs._root.add_child(shows)
    nodes = {pyfuse3.ROOT_INODE: vfs._root, 2: shows}
    vfs._get_path_from_inode = lambda inode: nodes[inode].path
    vfs._get_node_by_path = lambda path: next(
        node for node in nodes.values() if node.path == path
    )
    return vfs, shows


def test_getattr_returns_a_stable_directory_mtime():
    vfs, shows = make_vfs()

    async def mtimes():
        first = await vfs.getattr(pyfuse3.InodeT(2))
        await trio.sleep(0.01)
        second = await vfs.getattr(pyfuse3.InodeT(2))
        root = await vfs.getattr(pyfuse3.ROOT_INODE)
        return first.st_mtime_ns, second.st_mtime_ns, root.st_mtime_ns

    first, second, root = trio.run(mtimes)

    assert first == second == shows.modified_ns
    assert root == vfs._root.modified_ns


def test_adding_or_removing_a_child_updates_the_mtime():
    _, shows = make_vfs()
    child = VFSDirectory(name="Show (2020)", inode=pyfuse3.InodeT(3), parent=None)

    before = shows.modified_ns
    shows.add_child(child)
    added = shows.modified_ns
    shows.remove_child("Show (2020)")

    assert before < added < shows.modified_ns
    assert shows.remove_child("missing") is None
