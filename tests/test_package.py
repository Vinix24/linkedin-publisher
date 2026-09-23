import pkgutil
import sys

import linkedin_publisher


def test_no_module_shadows_the_standard_library():
    # A module named like a stdlib module (it was once called queue.py) breaks
    # requests as soon as someone runs the CLI from inside the package folder.
    ours = {m.name for m in pkgutil.iter_modules(linkedin_publisher.__path__)}
    assert not ours & set(sys.stdlib_module_names), ours & set(sys.stdlib_module_names)
