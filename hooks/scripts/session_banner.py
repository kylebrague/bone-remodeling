#!/usr/bin/env python3

from __future__ import annotations

import os


def main() -> int:
    if os.environ.get("OSTEOBLAST_SHOW_BANNER", "1") == "0":
        return 0

    print(
        "OSTEOBLAST POLICY ACTIVE\n"
        "  discovery mode must stay read-only\n"
        "  serious findings must not be remediated locally\n"
        "  routine work stays within the configured diff budget"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
