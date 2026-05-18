"""Post-install: create utxorpc namespace package symlinks for spec v0.19.0+.

The utxorpc-spec 0.19.0 uses `utxorpc.v1alpha.*` imports internally,
but the package files are under `utxorpc_spec/utxorpc/v1alpha/`.
This script creates redirected namespace packages.
"""
import os
import sys
import shutil


def setup_utxorpc_namespace(site_packages_dir: str | None = None) -> bool:
    """Create utxorpc namespace packages that redirect to utxorpc_spec.

    Returns True if successful, False if utxorpc-spec not found.
    """
    if site_packages_dir is None:
        site_packages_dir = next(
            (p for p in sys.path if "site-packages" in p),
            None,
        )
        if not site_packages_dir:
            print("ERROR: Could not find site-packages directory")
            return False

    spec_root = os.path.join(site_packages_dir, "utxorpc_spec", "utxorpc")
    if not os.path.isdir(spec_root):
        print("utxorpc-spec not found — skipping namespace setup")
        return False

    target_root = os.path.join(site_packages_dir, "utxorpc")

    # Create utxorpc __init__.py
    os.makedirs(target_root, exist_ok=True)
    init_path = os.path.join(target_root, "__init__.py")
    if not os.path.exists(init_path):
        with open(init_path, "w") as f:
            f.write("")

    # Walk utxorpc_spec/utxorpc and mirror structure with symlinks
    for dirpath, _dirnames, filenames in os.walk(spec_root):
        rel_dir = os.path.relpath(dirpath, spec_root)
        target_dir = os.path.join(target_root, rel_dir)

        # Create target dir and __init__.py
        os.makedirs(target_dir, exist_ok=True)
        init_file = os.path.join(target_dir, "__init__.py")
        if not os.path.exists(init_file):
            with open(init_file, "w") as f:
                f.write("")

        # Symlink .py files
        for fn in filenames:
            if fn.endswith(".py"):
                src = os.path.join(dirpath, fn)
                tgt = os.path.join(target_dir, fn)
                if not os.path.exists(tgt):
                    os.symlink(src, tgt)

    print(f"utxorpc namespace → {target_root}")
    return True


if __name__ == "__main__":
    setup_utxorpc_namespace()
