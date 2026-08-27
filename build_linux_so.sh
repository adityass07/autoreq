#!/bin/bash
set -e

echo "?? Starting Binary Obfuscation & .so Compilation..."

# Install dependencies and Cython
pip install --upgrade pip setuptools cython

# Compile core modules into .so shared binaries
python setup_cython.py build_ext --inplace

# Remove plain source code (keeping only run.py and compiled .so files)
echo "?? Removing original plain .py files to secure code..."
rm -f bot.py client_manager.py database.py config.py *.c setup_cython.py

echo "?? Done! Your project now runs 100% on compiled C-binary .so files!"
echo "?? Run with: python run.py"
