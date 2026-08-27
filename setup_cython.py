import os
import shutil
import glob
from setuptools import setup, Extension

try:
    from Cython.Build import cythonize
except ImportError:
    print("? Cython not found. Please install it using: pip install cython")
    exit(1)

# List of sensitive source files to compile into binary (.so / .pyd)
SOURCE_FILES = [
    "config.py",
    "database.py",
    "client_manager.py",
    "bot.py"
]

extensions = [
    Extension(
        name=os.path.splitext(f)[0],
        sources=[f],
        extra_compile_args=["-O3"] if os.name != "nt" else []
    )
    for f in SOURCE_FILES if os.path.exists(f)
]

setup(
    name="TelegramJoinDMBotSecure",
    ext_modules=cythonize(
        extensions,
        compiler_directives={
            "language_level": "3",
            "always_allow_keywords": True,
            "embedsignature": False,
            "emit_code_comments": False
        },
        quiet=False
    )
)

print("\n?? Cleaning up build artifacts and renaming binaries...")
# Rename platform-tagged binaries (e.g. bot.cpython-311-x86_64-linux-gnu.so -> bot.so)
for src in SOURCE_FILES:
    base = os.path.splitext(src)[0]
    # Look for compiled binary
    matches = glob.glob(f"{base}.*.so") + glob.glob(f"{base}.*.pyd")
    if matches:
        compiled_file = matches[0]
        ext = os.path.splitext(compiled_file)[1]
        target_name = f"{base}{ext}"
        if compiled_file != target_name:
            if os.path.exists(target_name):
                os.remove(target_name)
            os.rename(compiled_file, target_name)
            print(f"? Generated Secure Binary: {target_name}")
        else:
            print(f"? Generated Secure Binary: {target_name}")

# Clean temporary .c files and build directories
for c_file in glob.glob("*.c"):
    try:
        os.remove(c_file)
    except Exception:
        pass

if os.path.exists("build"):
    shutil.rmtree("build", ignore_errors=True)

print("\n?? All Core Modules Successfully Secured with Binary Compilation (.so / .pyd)!")
