"""Compare each OPUB in sample_library/opub with its counterpart in sample_library/recreated_opub.

Files are matched by name and compared byte for byte.
"""

import filecmp
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REF_DIR = os.path.join(ROOT, "sample_library", "opub")
NEW_DIR = os.path.join(ROOT, "sample_library", "recreated_opub")


def main():
    files = sorted(f for f in os.listdir(REF_DIR) if f.lower().endswith(".opub"))
    if not files:
        print("no opub files in", REF_DIR)
        return 1
    identical = 0
    for f in files:
        ref = os.path.join(REF_DIR, f)
        new = os.path.join(NEW_DIR, f)
        if not os.path.exists(new):
            status = "MISSING"
        elif filecmp.cmp(ref, new, shallow=False):
            status = "IDENTICAL"
            identical += 1
        else:
            status = "DIFFERENT"
        print("%-10s %s" % (status, f))
    print("\n%d/%d files identical" % (identical, len(files)))
    return 0 if identical == len(files) else 1


if __name__ == "__main__":
    sys.exit(main())
